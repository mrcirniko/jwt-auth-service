import asyncio
import aio_pika
import os
from aiogram import Bot, exceptions
from aiogram.types import Update

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/")

bot = Bot(token=TELEGRAM_BOT_TOKEN)

async def get_chat_id(username: str):
    """Преобразует username в chat_id, если не получается — ищет через getUpdates()."""
    try:
        user = await bot.get_chat(username)
        print(f"✅ Найден chat_id {user.id} для {username}")
        return user.id
    except exceptions.TelegramBadRequest:
        print(f"❌ {username} не найден через get_chat(). Проверяю getUpdates()...")

    # Пробуем получить chat_id через getUpdates
    try:
        updates = await bot.get_updates()
        for update in updates:
            if update.message and update.message.from_user.username == username.lstrip("@"):
                print(f"✅ Найден chat_id {update.message.chat.id} через getUpdates()")
                return update.message.chat.id
    except Exception as e:
        print(f"🔥 Ошибка при получении getUpdates(): {e}")

    print(f"⚠️ Не удалось найти chat_id для {username}")
    return None


async def process_message(message: aio_pika.IncomingMessage):
    """Обработка входящего сообщения из RabbitMQ."""
    async with message.process():
        body = message.body.decode()
        print(f"📩 Получено сообщение: {body}")

        try:
            user_id, telegram_username = body.split(",")

            text = f"Привет, {telegram_username}! Добро пожаловать в наш сервис! 🎉"
            chat_id = await get_chat_id(telegram_username)

            if chat_id:
                await bot.send_message(chat_id, text)
                print(f"✅ Сообщение отправлено {telegram_username} (chat_id: {chat_id})")
            else:
                print(f"⚠️ Не удалось отправить сообщение {telegram_username}")

        except Exception as e:
            print(f"🔥 Ошибка обработки сообщения: {e}")


async def main():
    """Основной цикл воркера."""
    print("📡 Worker started. Connecting to RabbitMQ...")
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    channel = await connection.channel()

    queue = await channel.declare_queue("telegram_queue")
    await queue.consume(process_message)

    print("📡 Worker listening for messages...")

    while True:
        await asyncio.sleep(1)  # Бесконечный цикл для стабильности


if __name__ == "__main__":
    asyncio.run(main())
