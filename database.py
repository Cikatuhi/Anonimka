import asyncpg
import os
import secrets
from dotenv import load_dotenv
import asyncio

load_dotenv()

DB_HOST = os.getenv("DB_HOST", "45.10.43.48")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "default_db")
DB_USER = os.getenv("DB_USER", "gen_user")
DB_PASSWORD = os.getenv("DB_PASSWORD")

DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# ============================================
# ПРОСТЫЕ ФУНКЦИИ ДЛЯ РАБОТЫ С БД (без пула)
# ============================================

async def get_conn():
    """Создаёт НОВОЕ соединение для каждого запроса"""
    return await asyncpg.connect(DATABASE_URL)

async def release_conn(conn):
    """Закрывает соединение"""
    if conn:
        await conn.close()

# ============================================
# ФУНКЦИИ ДЛЯ АДМИНКИ (отдельные соединения)
# ============================================

async def get_users_count():
    """Получить количество пользователей"""
    conn = await get_conn()
    try:
        return await conn.fetchval("SELECT COUNT(*) FROM users")
    finally:
        await release_conn(conn)

async def get_messages_count():
    """Получить количество сообщений"""
    conn = await get_conn()
    try:
        return await conn.fetchval("SELECT COUNT(*) FROM messages")
    finally:
        await release_conn(conn)

async def get_active_subscriptions_count():
    """Получить количество активных подписок"""
    conn = await get_conn()
    try:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM subscriptions WHERE status = 'active' AND (end_date IS NULL OR end_date > NOW())"
        )
    finally:
        await release_conn(conn)

# ============================================
# ОСТАЛЬНЫЕ ФУНКЦИИ (ТЕ ЖЕ)
# ============================================

async def init_db():
    conn = await get_conn()
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                has_seen_premium_offer BOOLEAN DEFAULT FALSE
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
                subscription_id TEXT,
                card_token TEXT,
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
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS unsubscribed_users (
                telegram_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                unsubscribed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                reason TEXT DEFAULT 'manual'
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS payment_tokens (
                id SERIAL PRIMARY KEY,
                token TEXT UNIQUE NOT NULL,
                user_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                used BOOLEAN DEFAULT FALSE,
                expires_at TIMESTAMP DEFAULT (NOW() + INTERVAL '1 hour')
            )
        """)
        
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at DESC)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_broadcast_jobs_status ON broadcast_jobs(status)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_user_id ON subscriptions(user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_status ON subscriptions(status)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_unsubscribed_users_id ON unsubscribed_users(telegram_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_payment_tokens_token ON payment_tokens(token)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_payment_tokens_user_id ON payment_tokens(user_id)")
        
        print("✅ База данных инициализирована")
    finally:
        await release_conn(conn)

# ============================================
# ОСТАЛЬНЫЕ ФУНКЦИИ (ПОЛНЫЙ СПИСОК)
# ============================================

async def register_user(user_id: int, username: str, first_name: str):
    conn = await get_conn()
    try:
        result = await conn.fetchrow("SELECT telegram_id FROM users WHERE telegram_id = $1", user_id)
        if not result:
            await conn.execute(
                "INSERT INTO users (telegram_id, username, first_name, registered_at) VALUES ($1, $2, $3, NOW())",
                user_id, username, first_name
            )
            return True
        return False
    finally:
        await release_conn(conn)

async def get_user(user_id: int):
    conn = await get_conn()
    try:
        row = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", user_id)
        return dict(row) if row else None
    finally:
        await release_conn(conn)

async def get_all_users(limit: int = 1000):
    conn = await get_conn()
    try:
        rows = await conn.fetch("SELECT telegram_id FROM users ORDER BY registered_at LIMIT $1", limit)
        return [row['telegram_id'] for row in rows]
    finally:
        await release_conn(conn)

async def count_users():
    conn = await get_conn()
    try:
        return await conn.fetchval("SELECT COUNT(*) FROM users")
    finally:
        await release_conn(conn)

async def has_user_seen_premium_offer(user_id: int) -> bool:
    conn = await get_conn()
    try:
        row = await conn.fetchrow("SELECT has_seen_premium_offer FROM users WHERE telegram_id = $1", user_id)
        return row['has_seen_premium_offer'] if row else False
    finally:
        await release_conn(conn)

async def mark_premium_offer_as_seen(user_id: int):
    conn = await get_conn()
    try:
        await conn.execute("UPDATE users SET has_seen_premium_offer = TRUE WHERE telegram_id = $1", user_id)
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
        await conn.execute("DELETE FROM users WHERE telegram_id = $1", user_id)
    finally:
        await release_conn(conn)

async def remove_blocked_user(user_id: int):
    conn = await get_conn()
    try:
        row = await conn.fetchrow("SELECT username, first_name FROM blocked_users WHERE telegram_id = $1", user_id)
        if row:
            await conn.execute(
                "INSERT INTO users (telegram_id, username, first_name) VALUES ($1, $2, $3) ON CONFLICT (telegram_id) DO NOTHING",
                user_id, row['username'], row['first_name']
            )
            await conn.execute("DELETE FROM blocked_users WHERE telegram_id = $1", user_id)
            return True
        return False
    finally:
        await release_conn(conn)

async def is_user_blocked(user_id: int):
    conn = await get_conn()
    try:
        row = await conn.fetchrow("SELECT telegram_id FROM blocked_users WHERE telegram_id = $1", user_id)
        return row is not None
    finally:
        await release_conn(conn)

async def get_subscription_by_user_id(user_id: int):
    conn = await get_conn()
    try:
        row = await conn.fetchrow(
            "SELECT * FROM subscriptions WHERE user_id = $1 ORDER BY created_at DESC LIMIT 1",
            user_id
        )
        return dict(row) if row else None
    finally:
        await release_conn(conn)

async def create_subscription(
    user_id: int, 
    plan: str, 
    status: str = 'active', 
    end_date=None,
    card_token: str = None,
    subscription_id: str = None
):
    conn = await get_conn()
    try:
        await conn.execute("""
            INSERT INTO subscriptions 
            (user_id, plan, status, end_date, card_token, subscription_id, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, NOW())
        """, user_id, plan, status, end_date, card_token, subscription_id)
    finally:
        await release_conn(conn)

async def update_subscription_status(user_id: int, status: str, end_date=None):
    conn = await get_conn()
    try:
        if end_date:
            await conn.execute("UPDATE subscriptions SET status = $1, end_date = $2 WHERE user_id = $3", status, end_date, user_id)
        else:
            await conn.execute("UPDATE subscriptions SET status = $1 WHERE user_id = $2", status, user_id)
    finally:
        await release_conn(conn)

async def update_subscription_plan(user_id: int, plan: str, end_date=None):
    conn = await get_conn()
    try:
        if end_date:
            await conn.execute("UPDATE subscriptions SET plan = $1, end_date = $2 WHERE user_id = $3", plan, end_date, user_id)
        else:
            await conn.execute("UPDATE subscriptions SET plan = $1 WHERE user_id = $2", plan, user_id)
    finally:
        await release_conn(conn)

async def save_cloudpayments_subscription_id(user_id: int, subscription_id: str):
    conn = await get_conn()
    try:
        await conn.execute("UPDATE subscriptions SET subscription_id = $1 WHERE user_id = $2", subscription_id, user_id)
    finally:
        await release_conn(conn)

async def save_card_token(user_id: int, card_token: str):
    conn = await get_conn()
    try:
        await conn.execute("UPDATE subscriptions SET card_token = $1 WHERE user_id = $2", card_token, user_id)
    finally:
        await release_conn(conn)

async def get_card_token(user_id: int) -> str:
    conn = await get_conn()
    try:
        row = await conn.fetchrow("SELECT card_token FROM subscriptions WHERE user_id = $1 ORDER BY created_at DESC LIMIT 1", user_id)
        return row['card_token'] if row else None
    finally:
        await release_conn(conn)

async def get_all_users_in_retry_low():
    conn = await get_conn()
    try:
        rows = await conn.fetch("SELECT user_id FROM subscriptions WHERE status = 'retry_low'")
        return [row['user_id'] for row in rows]
    finally:
        await release_conn(conn)

async def count_active_subscriptions():
    conn = await get_conn()
    try:
        return await conn.fetchval("SELECT COUNT(*) FROM subscriptions WHERE status = 'active' AND (end_date IS NULL OR end_date > NOW())")
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

async def get_users_batch(limit: int = 1000, offset: int = 0):
    conn = await get_conn()
    try:
        rows = await conn.fetch("SELECT telegram_id FROM users ORDER BY telegram_id LIMIT $1 OFFSET $2", limit, offset)
        return [row['telegram_id'] for row in rows]
    finally:
        await release_conn(conn)

async def get_setting(key: str, default: str = None) -> str:
    conn = await get_conn()
    try:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key = $1", key)
        return row['value'] if row else default
    finally:
        await release_conn(conn)

async def set_setting(key: str, value: str):
    conn = await get_conn()
    try:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()",
            key, value
        )
    finally:
        await release_conn(conn)

async def unsubscribe_user(user_id: int, username: str = None, first_name: str = None, reason: str = 'manual'):
    conn = await get_conn()
    try:
        if not username or not first_name:
            user_data = await get_user(user_id)
            if user_data:
                username = user_data.get('username', '')
                first_name = user_data.get('first_name', '')

        await conn.execute(
            "UPDATE subscriptions SET status = 'cancelled' WHERE user_id = $1",
            user_id
        )
        
        await conn.execute("""
            INSERT INTO unsubscribed_users (telegram_id, username, first_name, reason, unsubscribed_at)
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (telegram_id) DO UPDATE SET 
                username = $2, 
                first_name = $3, 
                reason = $4, 
                unsubscribed_at = NOW()
        """, user_id, username or '', first_name or '', reason)
        
        return True
    except Exception as e:
        print(f"❌ Ошибка отписки: {e}")
        return False
    finally:
        await release_conn(conn)

async def is_user_unsubscribed(user_id: int) -> bool:
    conn = await get_conn()
    try:
        row = await conn.fetchrow("SELECT telegram_id FROM unsubscribed_users WHERE telegram_id = $1", user_id)
        return row is not None
    finally:
        await release_conn(conn)

async def get_unsubscribed_users(limit: int = 100, offset: int = 0):
    conn = await get_conn()
    try:
        rows = await conn.fetch("""
            SELECT * FROM unsubscribed_users 
            ORDER BY unsubscribed_at DESC 
            LIMIT $1 OFFSET $2
        """, limit, offset)
        return [dict(row) for row in rows]
    finally:
        await release_conn(conn)

async def count_unsubscribed():
    conn = await get_conn()
    try:
        return await conn.fetchval("SELECT COUNT(*) FROM unsubscribed_users")
    finally:
        await release_conn(conn)

async def remove_from_unsubscribed(user_id: int):
    conn = await get_conn()
    try:
        await conn.execute("DELETE FROM unsubscribed_users WHERE telegram_id = $1", user_id)
        return True
    except Exception as e:
        print(f"❌ Ошибка удаления из отписавшихся: {e}")
        return False
    finally:
        await release_conn(conn)

async def generate_payment_token(user_id: int) -> str:
    conn = await get_conn()
    try:
        token = secrets.token_urlsafe(32)
        await conn.execute("""
            INSERT INTO payment_tokens (token, user_id, expires_at)
            VALUES ($1, $2, NOW() + INTERVAL '1 hour')
        """, token, user_id)
        return token
    finally:
        await release_conn(conn)

async def get_user_by_payment_token(token: str) -> int:
    conn = await get_conn()
    try:
        row = await conn.fetchrow("""
            SELECT user_id FROM payment_tokens 
            WHERE token = $1 AND used = FALSE AND expires_at > NOW()
        """, token)
        return row['user_id'] if row else None
    finally:
        await release_conn(conn)

async def mark_payment_token_as_used(token: str) -> bool:
    conn = await get_conn()
    try:
        await conn.execute("UPDATE payment_tokens SET used = TRUE WHERE token = $1", token)
        return True
    finally:
        await release_conn(conn)

async def get_expired_subscriptions():
    conn = await get_conn()
    try:
        rows = await conn.fetch("""
            SELECT user_id, plan, end_date, status, subscription_id 
            FROM subscriptions 
            WHERE status = 'active' 
            AND end_date IS NOT NULL 
            AND end_date < NOW()
            ORDER BY end_date DESC
        """)
        return [dict(row) for row in rows]
    finally:
        await release_conn(conn)

async def init_pool():
    """Пустая функция для совместимости"""
    pass
