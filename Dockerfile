# Используем официальный образ Python
FROM python:3.11

# Устанавливаем рабочую директорию
WORKDIR /app

# Устанавливаем необходимые системные зависимости
RUN apt-get update && apt-get install -y \
    gcc \
    python3-dev \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Копируем зависимости
COPY requirements.txt .

# Устанавливаем Python-зависимости
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir --upgrade bcrypt passlib && \
    pip install --no-cache-dir --upgrade aio_pika && \
    pip install --no-cache-dir --upgrade aiogram && \
    pip install -r requirements.txt

# Копируем исходный код приложения
COPY . .

# Указываем порт, который будет использовать приложение
EXPOSE 8000

# Команда для запуска приложения с автоматической перезагрузкой при изменениях
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
