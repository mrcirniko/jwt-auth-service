from fastapi import FastAPI, Depends, HTTPException, Request, Form
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from datetime import datetime, timedelta
import asyncpg
import os
import requests
from dotenv import load_dotenv
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from passlib.context import CryptContext
import aio_pika



load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
SECRET_KEY = os.getenv("SECRET_KEY")
YANDEX_CLIENT_ID = os.getenv("YANDEX_CLIENT_ID")
YANDEX_CLIENT_SECRET = os.getenv("YANDEX_CLIENT_SECRET")

RABBITMQ_URL = "amqp://guest:guest@rabbitmq/"

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/login")

async def connect_db():
    return await asyncpg.connect(DATABASE_URL)

async def init_db():
    conn = await connect_db()
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password TEXT,
            telegram_username TEXT UNIQUE
        );
        CREATE TABLE IF NOT EXISTS login_history (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ip_address TEXT
        );
    """)
    await conn.close()


@app.on_event("startup")
async def startup():
    await init_db()

def create_access_token(data: dict, expires_delta: timedelta = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def verify_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


async def send_to_rabbitmq(user_id: int, telegram_username: str):
    """Отправка данных в очередь RabbitMQ."""
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    channel = await connection.channel()

    queue_name = "telegram_queue"
    message_body = f"{user_id},{telegram_username}"

    await channel.default_exchange.publish(
        aio_pika.Message(body=message_body.encode()),
        routing_key=queue_name,
    )
    print(f"📤 Отправлено в очередь {queue_name}: {message_body}")

    await connection.close()

@app.get("/register")
async def register_form(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})

@app.post("/register")
async def register(email: str = Form(...), password: str = Form(...)):
    if not password:
        raise HTTPException(status_code=400, detail="Пароль не может быть пустым")

    hashed_password = pwd_context.hash(password)
    conn = await connect_db()
    
    try:
        await conn.execute("INSERT INTO users (email, password) VALUES ($1, $2)", email, hashed_password)
        user = await conn.fetchrow("SELECT * FROM users WHERE email=$1", email)  # Переместил внутрь try
    except asyncpg.UniqueViolationError:
        await conn.close()
        raise HTTPException(status_code=400, detail="Email уже зарегистрирован")

    await conn.execute("INSERT INTO login_history (user_id, ip_address) VALUES ($1, $2)", user["id"], "127.0.0.1")
    await conn.close()

    token = create_access_token({"sub": email})
    return RedirectResponse(url=f"/login-history?token={token}", status_code=303)



@app.get("/login")
async def login_form(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "yandex_client_id": YANDEX_CLIENT_ID})

@app.post("/login")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    conn = await connect_db()
    user = await conn.fetchrow("SELECT * FROM users WHERE email=$1", form_data.username)
    await conn.close()
    if not user or not pwd_context.verify(form_data.password, user["password"]):
        raise HTTPException(status_code=400, detail="Неверные учетные данные")

    token = create_access_token({"sub": user["email"]})

    conn = await connect_db()
    await conn.execute("INSERT INTO login_history (user_id, ip_address) VALUES ($1, $2)", user["id"], "127.0.0.1")
    await conn.close()

    return RedirectResponse(url=f"/login-history?token={token}")

@app.get("/users/me")
async def read_users_me(token: str = Depends(oauth2_scheme)):
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    email = payload.get("sub")
    conn = await connect_db()
    user = await conn.fetchrow("SELECT * FROM users WHERE email=$1", email)
    await conn.close()

    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    
    return {"email": user["email"]}

@app.get("/login-history")
async def login_history(request: Request):
    token = request.query_params.get("token")
    if not token:
        raise HTTPException(status_code=401, detail="Token is missing")

    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")

    email = payload.get("sub")
    conn = await connect_db()
    user = await conn.fetchrow("SELECT * FROM users WHERE email=$1", email)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    history = await conn.fetch("SELECT * FROM login_history WHERE user_id=$1", user["id"])
    await conn.close()

    return templates.TemplateResponse("history.html", {"request": request, "history": history})

@app.get("/auth/yandex")
async def auth_callback(code: str):
    response = requests.post(
        "https://oauth.yandex.ru/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": YANDEX_CLIENT_ID,
            "client_secret": YANDEX_CLIENT_SECRET,
        },
    )

    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Ошибка авторизации Яндекса")

    token_info = response.json()
    access_token = token_info.get("access_token")

    user_info = requests.get(
        "https://login.yandex.ru/info",
        headers={"Authorization": f"OAuth {access_token}"}
    ).json()

    user_email = user_info.get("default_email")

    conn = await connect_db()
    user = await conn.fetchrow("SELECT * FROM users WHERE email=$1", user_email)

    if user:
        await conn.execute(
            "INSERT INTO login_history (user_id, ip_address) VALUES ($1, $2)", 
            user["id"], "127.0.0.1"
        )
        await conn.close()

        token = create_access_token({"sub": user_email})

        return RedirectResponse(url=f"/login-history?token={token}")

    await conn.close()

    return RedirectResponse(url=f"/set-password?email={user_email}")



@app.get("/set-password")
async def set_password_form(request: Request, email: str):
    return templates.TemplateResponse("set_password.html", {"request": request, "email": email})

@app.post("/set-password")
async def set_password(email: str = Form(...), password: str = Form(...), confirm_password: str = Form(...)):
    if password != confirm_password:
        raise HTTPException(status_code=400, detail="Пароли не совпадают")

    hashed_password = pwd_context.hash(password)

    conn = await connect_db()

    existing_user = await conn.fetchrow("SELECT id FROM users WHERE email = $1", email)
    
    if existing_user:
        raise HTTPException(status_code=400, detail="Пользователь с таким email уже существует")
    
    user_id = await conn.fetchval("INSERT INTO users (email, password) VALUES ($1, $2) RETURNING id", email, hashed_password)

    await conn.execute("INSERT INTO login_history (user_id, ip_address) VALUES ($1, $2)", user_id, "127.0.0.1")
    
    await conn.close()

    token = create_access_token({"sub": email})

    return RedirectResponse(url=f"/login-history?token={token}", status_code=303)


@app.get("/telegram")
async def telegram_form(request: Request):
    return templates.TemplateResponse("telegram.html", {"request": request})

@app.post("/telegram")
async def save_telegram_username(
    request: Request,
    telegram_username: str = Form(...),
    token: str = Depends(oauth2_scheme)
):
    """Сохранение Telegram-юзернейма и отправка данных в RabbitMQ."""
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")

    email = payload.get("sub")
    conn = await connect_db()
    
    try:
        user = await conn.fetchrow("SELECT id FROM users WHERE email=$1", email)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        await conn.execute(
            "UPDATE users SET telegram_username=$1 WHERE email=$2",
            telegram_username, email
        )
        await send_to_rabbitmq(user["id"], telegram_username)

    finally:
        await conn.close()

    return {"message": "Telegram username сохранен!"}
