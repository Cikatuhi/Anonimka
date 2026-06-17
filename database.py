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
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    finally:
        await conn.close()

async def register_user(user_id: int, username: str, first_name: str):
    """Регистрирует пользователя, если его нет в базе"""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        result = await conn.fetchrow(
            "SELECT user_id FROM users WHERE user_id = $1",
            user_id
        )
        
        if not result:
            # Регистрируем нового пользователя
            await conn.execute(
                "INSERT INTO users (user_id, username, first_name) VALUES ($1, $2, $3)",
                user_id, username, first_name
            )
            return True
        return False
    finally:
        await conn.close()

async def get_user(user_id: int):
    """Получить данные пользователя по ID"""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        return await conn.fetchrow(
            "SELECT * FROM users WHERE user_id = $1",
            user_id
        )
    finally:
        await conn.close()