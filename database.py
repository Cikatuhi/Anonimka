import asyncpg
import os
import json
import redis
from dotenv import load_dotenv
from functools import lru_cache
import time

load_dotenv()

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "anonim_bot")
DB_USER = os.getenv("DB_USER", "bot_user")
DB_PASSWORD = os.getenv("DB_PASSWORD")

DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_DB = int(os.getenv("REDIS_DB", 0))

redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)

pool = None
CACHE_TTL = 300  # 5 минут

async def init_pool():
    global pool
    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=10,
        max_size=50,
        max_inactive_connection_lifetime=300,
        command_timeout=10
    )
    print("✅ Пул соединений с БД создан")

async def get_conn():
    return await pool.acquire()

async def release_conn(conn):
    await pool.release(conn)


def get_user_cached(user_id: int):
    """Получает пользователя из Redis кэша"""
    key = f"user:{user_id}"
    cached = redis_client.get(key)
    if cached:
        return json.loads(cached)
    return None

def set_user_cache(user_id: int, data: dict):
    """Сохраняет пользователя в Redis кэш"""
    key = f"user:{user_id}"
    redis_client.setex(key, CACHE_TTL, json.dumps(data, default=str))

def invalidate_user_cache(user_id: int):
    """Очищает кэш пользователя"""
    redis_client.delete(f"user:{user_id}")

def is_user_blocked_cached(user_id: int):
    """Проверяет, заблокирован ли пользователь, через Redis"""
    key = f"blocked:{user_id}"
    return redis_client.exists(key) > 0

def set_user_blocked_cache(user_id: int):
    """Сохраняет в Redis, что пользователь заблокирован"""
    key = f"blocked:{user_id}"
    redis_client.setex(key, CACHE_TTL, "1")

def remove_user_blocked_cache(user_id: int):
    """Удаляет из Redis информацию о блокировке"""
    redis_client.delete(f"blocked:{user_id}")


async def init_db():
    conn = await get_conn()
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
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                plan TEXT NOT NULL,
                start_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                end_date TIMESTAMP,
                status TEXT DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS broadcast_jobs (
                id SERIAL PRIMARY KEY,
                message TEXT NOT NULL,
                total_users INT DEFAULT 0,
                sent_users INT DEFAULT 0,
                failed_users INT DEFAULT 0,
                blocked_users INT DEFAULT 0,
                status TEXT DEFAULT 'pending',
                progress FLOAT DEFAULT 0,
                user_ids_json JSONB,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                paused_at TIMESTAMP,
                finished_at TIMESTAMP,
                cpu_usage FLOAT DEFAULT 0,
                memory_usage FLOAT DEFAULT 0,
                task_id TEXT
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at DESC)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_broadcast_jobs_status ON broadcast_jobs(status)")
    finally:
        await release_conn(conn)

async def register_user(user_id: int, username: str, first_name: str):
    conn = await get_conn()
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
            invalidate_user_cache(user_id)
            remove_user_blocked_cache(user_id)
            return True
        return False
    finally:
        await release_conn(conn)

async def get_user(user_id: int):
    cached = get_user_cached(user_id)
    if cached:
        return cached
    
    conn = await get_conn()
    try:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE telegram_id = $1",
            user_id
        )
        data = dict(row) if row else None
        if data:
            set_user_cache(user_id, data)
        return data
    finally:
        await release_conn(conn)

async def save_message(sender_id: int, recipient_id: int, content: str, content_type: str = 'text'):
    conn = await get_conn()
    try:
        await conn.execute(
            "INSERT INTO messages (sender_id, recipient_id, content, content_type) VALUES ($1, $2, $3, $4)",
            sender_id, recipient_id, content, content_type
        )
    finally:
        await release_conn(conn)

async def add_blocked_user(user_id: int, username: str, first_name: str):
    conn = await get_conn()
    try:
        await conn.execute(
            "INSERT INTO blocked_users (telegram_id, username, first_name) VALUES ($1, $2, $3) ON CONFLICT (telegram_id) DO NOTHING",
            user_id, username, first_name
        )
        await conn.execute(
            "DELETE FROM users WHERE telegram_id = $1",
            user_id
        )
        invalidate_user_cache(user_id)
        set_user_blocked_cache(user_id)
    finally:
        await release_conn(conn)

async def remove_blocked_user(user_id: int):
    conn = await get_conn()
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
            invalidate_user_cache(user_id)
            remove_user_blocked_cache(user_id)
            return True
        return False
    finally:
        await release_conn(conn)

async def is_user_blocked(user_id: int):
    if is_user_blocked_cached(user_id):
        return True
    conn = await get_conn()
    try:
        row = await conn.fetchrow(
            "SELECT telegram_id FROM blocked_users WHERE telegram_id = $1",
            user_id
        )
        if row:
            set_user_blocked_cache(user_id)
            return True
        return False
    finally:
        await release_conn(conn)

async def get_all_users(limit: int = 100, offset: int = 0):
    conn = await get_conn()
    try:
        rows = await conn.fetch(
            "SELECT telegram_id FROM users ORDER BY registered_at DESC LIMIT $1 OFFSET $2",
            limit, offset
        )
        return [row['telegram_id'] for row in rows]
    finally:
        await release_conn(conn)

async def count_users():
    conn = await get_conn()
    try:
        count = await conn.fetchval("SELECT COUNT(*) FROM users")
        return count
    finally:
        await release_conn(conn)

async def get_users_batch(limit: int = 1000, offset: int = 0):
    """Получает пользователей пачками для рассылки"""
    conn = await get_conn()
    try:
        rows = await conn.fetch(
            "SELECT telegram_id FROM users ORDER BY telegram_id LIMIT $1 OFFSET $2",
            limit, offset
        )
        return [row['telegram_id'] for row in rows]
    finally:
        await release_conn(conn)

async def add_subscription(user_id: int, plan: str, end_date):
    conn = await get_conn()
    try:
        await conn.execute(
            "INSERT INTO subscriptions (user_id, plan, end_date, status) VALUES ($1, $2, $3, 'active')",
            user_id, plan, end_date
        )
    finally:
        await release_conn(conn)

async def count_active_subscriptions():
    conn = await get_conn()
    try:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM subscriptions WHERE status = 'active' AND (end_date IS NULL OR end_date > NOW())"
        )
        return count
    finally:
        await release_conn(conn)

async def get_all_subscriptions(limit: int = 100, offset: int = 0):
    conn = await get_conn()
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
        await release_conn(conn)