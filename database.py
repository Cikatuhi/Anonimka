import asyncpg
import os
from dotenv import load_dotenv

load_dotenv()

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "anonim_bot")
DB_USER = os.getenv("DB_USER", "bot_user")
DB_PASSWORD = os.getenv("DB_PASSWORD")

# Строка подключения
DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

async def init_db():
    """Создаёт таблицу users, если она не существует"""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    finally:
        await conn.close()

async def register_user(user: int, username: str, first_name: str):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # Проверяем, есть ли пользователь
        result = await conn.fetchrow(
            "SELECT user FROM users WHERE user = $1",
            user
        )
        
        if not result:
            # Вставляем нового пользователя
            await conn.execute(
                "INSERT INTO users (user, username, first_name) VALUES ($1, $2, $3)",
                user, username, first_name
            )
            return True
        return False
    finally:
        await conn.close()

async def get_user(user: int):
    """Получить данные пользователя по ID"""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        return await conn.fetchrow(
            "SELECT * FROM users WHERE user = $1",
            user
        )
    finally:
        await conn.close()