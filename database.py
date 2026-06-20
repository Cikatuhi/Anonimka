import asyncpg
import os
from dotenv import load_dotenv

load_dotenv()

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "anonim_bot")
DB_USER = os.getenv("DB_USER", "bot_user")
DB_PASSWORD = os.getenv("DB_PASSWORD")

DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

async def init_db():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS blocked_users (
                telegram_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                blocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                sender_id BIGINT NOT NULL,
                recipient_id BIGINT NOT NULL,
                content TEXT,
                content_type TEXT DEFAULT 'text',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    finally:
        await conn.close()

async def register_user(user_id: int, username: str, first_name: str):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        result = await conn.fetchrow(
            "SELECT telegram_id FROM users WHERE telegram_id = $1",
            user_id
        )
        if not result:
            await conn.execute(
                "INSERT INTO users (telegram_id, username, first_name) VALUES ($1, $2, $3)",
                user_id, username, first_name
            )
            return True
        return False
    finally:
        await conn.close()

async def get_user(user_id: int):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE telegram_id = $1",
            user_id
        )
        return dict(row) if row else None
    finally:
        await conn.close()

async def save_message(sender_id: int, recipient_id: int, content: str, content_type: str = 'text'):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute(
            "INSERT INTO messages (sender_id, recipient_id, content, content_type) VALUES ($1, $2, $3, $4)",
            sender_id, recipient_id, content, content_type
        )
    finally:
        await conn.close()

async def add_blocked_user(user_id: int, username: str, first_name: str):
    """Добавляет пользователя в список ЧС и удаляет из основной таблицы"""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute(
            "INSERT INTO blocked_users (telegram_id, username, first_name) VALUES ($1, $2, $3) ON CONFLICT (telegram_id) DO NOTHING",
            user_id, username, first_name
        )
        await conn.execute(
            "DELETE FROM users WHERE telegram_id = $1",
            user_id
        )
    finally:
        await conn.close()

async def remove_blocked_user(user_id: int):
    """Удаляет пользователя из списка ЧС и возвращает в основную таблицу"""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        row = await conn.fetchrow(
            "SELECT username, first_name FROM blocked_users WHERE telegram_id = $1",
            user_id
        )
        if row:
            await conn.execute(
                "INSERT INTO users (telegram_id, username, first_name) VALUES ($1, $2, $3) ON CONFLICT (telegram_id) DO NOTHING",
                user_id, row['username'], row['first_name']
            )
            await conn.execute(
                "DELETE FROM blocked_users WHERE telegram_id = $1",
                user_id
            )
            return True
        return False
    finally:
        await conn.close()

async def is_user_blocked(user_id: int):
    """Проверяет, находится ли пользователь в ЧС"""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        row = await conn.fetchrow(
            "SELECT telegram_id FROM blocked_users WHERE telegram_id = $1",
            user_id
        )
        return row is not None
    finally:
        await conn.close()

async def get_blocked_users():
    """Получает список всех заблокировавших пользователей"""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch("SELECT * FROM blocked_users ORDER BY blocked_at DESC")
        return [dict(row) for row in rows]
    finally:
        await conn.close()

async def get_all_users():
    """Получает список всех активных пользователей"""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch("SELECT telegram_id FROM users")
        return [row['telegram_id'] for row in rows]
    finally:
        await conn.close()

async def add_subscription(user_id: int, plan: str, end_date):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute(
            "INSERT INTO subscriptions (user_id, plan, end_date, status) VALUES ($1, $2, $3, 'active')",
            user_id, plan, end_date
        )
    finally:
        await conn.close()

async def get_user_active_subscription(user_id: int):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        row = await conn.fetchrow(
            "SELECT * FROM subscriptions WHERE user_id = $1 AND status = 'active' AND (end_date IS NULL OR end_date > NOW()) ORDER BY end_date DESC LIMIT 1",
            user_id
        )
        return dict(row) if row else None
    finally:
        await conn.close()

async def get_all_subscriptions(limit: int = 100, offset: int = 0):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch("""
            SELECT s.*, u.username, u.first_name 
            FROM subscriptions s
            LEFT JOIN users u ON s.user_id = u.telegram_id
            ORDER BY s.created_at DESC
            LIMIT $1 OFFSET $2
        """, limit, offset)
        return [dict(row) for row in rows]
    finally:
        await conn.close()

async def count_active_subscriptions():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM subscriptions WHERE status = 'active' AND (end_date IS NULL OR end_date > NOW())"
        )
        return count
    finally:
        await conn.close()
