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
            telegram_username TEXT UNIQUE,
            role TEXT NOT NULL DEFAULT 'user'
        );

        CREATE TABLE IF NOT EXISTS login_history (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ip_address TEXT
        );

        DO $$ 
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM pg_trigger
                WHERE tgname = 'trigger_delete_user_history'
            ) THEN
                DROP TRIGGER trigger_delete_user_history ON users;
            END IF;
        END $$;

        CREATE OR REPLACE FUNCTION delete_user_history() RETURNS TRIGGER AS $$
        BEGIN
            DELETE FROM login_history WHERE user_id = OLD.id;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trigger_delete_user_history
        BEFORE DELETE ON users
        FOR EACH ROW
        EXECUTE FUNCTION delete_user_history();
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
    except JWTError as e:
        print(f"JWT Error: {e}")
        return None

async def get_current_user(token: str = Depends(oauth2_scheme)):
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    
    email = payload.get("sub")
    if not email:
        raise HTTPException(status_code=401, detail="Invalid token payload")
    
    return email

async def send_to_rabbitmq(user_id: int, telegram_username: str):
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    channel = await connection.channel()

    queue_name = "telegram_queue"
    message_body = f"{user_id},{telegram_username}"

    await channel.default_exchange.publish(
        aio_pika.Message(body=message_body.encode()),
        routing_key=queue_name,
    )
    print(f"Отправлено в очередь {queue_name}: {message_body}")

    await connection.close()

@app.get("/register")
async def register_form(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})

@app.post("/register")
async def register(
    email: str = Form(...),
    password: str = Form(...),
    telegram_username: str = Form(...)
):
    if not password:
        raise HTTPException(status_code=400, detail="Пароль не может быть пустым")

    hashed_password = pwd_context.hash(password)

    conn = await connect_db()
    
    try:
        await conn.execute(
            "INSERT INTO users (email, password, telegram_username, role) VALUES ($1, $2, $3, $4)",
            email, hashed_password, telegram_username, "user"
        )
        
        user = await conn.fetchrow("SELECT id FROM users WHERE email=$1", email)
        if not user:
            raise HTTPException(status_code=500, detail="Ошибка при создании пользователя")

        await conn.execute(
            "INSERT INTO login_history (user_id, ip_address) VALUES ($1, $2)",
            user["id"], "127.0.0.1"
        )

        await send_to_rabbitmq(user["id"], telegram_username)

    except asyncpg.UniqueViolationError:
        await conn.close()
        raise HTTPException(status_code=400, detail="Email уже зарегистрирован")

    finally:
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

    if user["role"] == "admin":
        return RedirectResponse(url=f"/admin?token={token}")
    else:
        return RedirectResponse(url=f"/login-history?token={token}")

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

        if user["role"] == "admin":
            return RedirectResponse(url=f"/admin?token={token}")
        else:
            return RedirectResponse(url=f"/login-history?token={token}")

    await conn.close()

    temp_token = create_access_token({"sub": user_email}, timedelta(minutes=15))

    return RedirectResponse(url=f"/set-password?token={temp_token}")




@app.get("/set-password")
async def set_password_form(request: Request, token: str):
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    return templates.TemplateResponse("set_password.html", {"request": request, "token": token})


@app.post("/set-password")
async def set_password(
    token: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    telegram_username: str = Form(...)
):
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    email = payload.get("sub")

    if password != confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match")

    hashed_password = pwd_context.hash(password)

    conn = await connect_db()
    user = await conn.fetchrow("SELECT * FROM users WHERE email=$1", email)

    if user:
        raise HTTPException(status_code=400, detail="User already exists")

    await conn.execute(
        "INSERT INTO users (email, password, telegram_username, role) VALUES ($1, $2, $3, 'user')",
        email, hashed_password, telegram_username
    )

    user = await conn.fetchrow("SELECT id FROM users WHERE email=$1", email)
    user_id = user["id"]

    await send_to_rabbitmq(user_id, telegram_username)

    await conn.close()

    new_token = create_access_token({"sub": email})
    return RedirectResponse(url=f"/login-history?token={new_token}", status_code=303)


@app.get("/protected")
async def protected_route(current_user: str = Depends(get_current_user)):
    return {"message": "Access granted", "user": current_user}

@app.get("/admin")
async def admin_panel(request: Request, token: str = None):
    if not token:
        token = request.headers.get("Authorization")
        if token:
            token = token.split("Bearer ")[-1]

    if not token:
        raise HTTPException(status_code=401, detail="Token is missing")

    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")

    email = payload.get("sub")
    conn = await connect_db()
    user = await conn.fetchrow("SELECT * FROM users WHERE email=$1", email)
    if not user or user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    users = await conn.fetch("SELECT * FROM users")
    await conn.close()

    return templates.TemplateResponse("admin.html", {"request": request, "users": users})

@app.post("/admin/delete-user")
async def delete_user(
    user_id: int = Form(...),
    token: str = Form(...)
):
    if not token:
        raise HTTPException(status_code=401, detail="Token is missing")

    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")

    email = payload.get("sub")
    conn = await connect_db()
    user = await conn.fetchrow("SELECT * FROM users WHERE email=$1", email)
    if not user or user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    await conn.execute("DELETE FROM users WHERE id=$1", user_id)
    await conn.close()

    return RedirectResponse(url=f"/admin?token={token}", status_code=303)

@app.post("/admin/add-user")
async def add_user(
    email: str = Form(...),
    password: str = Form(...),
    telegram_username: str = Form(...),
    role: str = Form(...),
    token: str = Form(...)
):
    if not token:
        raise HTTPException(status_code=401, detail="Token is missing")

    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")

    admin_email = payload.get("sub")
    conn = await connect_db()
    admin = await conn.fetchrow("SELECT * FROM users WHERE email=$1", admin_email)
    if not admin or admin["role"] != "admin":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    if not password:
        raise HTTPException(status_code=400, detail="Пароль не может быть пустым")

    hashed_password = pwd_context.hash(password)

    try:
        await conn.execute(
            "INSERT INTO users (email, password, telegram_username, role) VALUES ($1, $2, $3, $4)",
            email, hashed_password, telegram_username, role
        )
        
        user = await conn.fetchrow("SELECT id FROM users WHERE email=$1", email)
        if not user:
            raise HTTPException(status_code=500, detail="Ошибка при создании пользователя")

        await conn.execute(
            "INSERT INTO login_history (user_id, ip_address) VALUES ($1, $2)",
            user["id"], "127.0.0.1"
        )

        await send_to_rabbitmq(user["id"], telegram_username)

    except asyncpg.UniqueViolationError:
        await conn.close()
        raise HTTPException(status_code=400, detail="Email уже зарегистрирован")

    finally:
        await conn.close()

    return RedirectResponse(url=f"/admin?token={token}", status_code=303)
