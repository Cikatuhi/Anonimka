import asyncpg
import json
import os
import secrets
from dotenv import load_dotenv
import asyncio
from contextlib import asynccontextmanager
import aiohttp

load_dotenv()

DB_HOST = os.getenv("DB_HOST", "45.10.43.48")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "default_db")
DB_USER = os.getenv("DB_USER", "gen_user")
DB_PASSWORD = os.getenv("DB_PASSWORD")

DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

SUBGRAM_API_TOKEN = os.getenv("SUBGRAM_API_TOKEN", "")
SUBGRAM_API_KEY = os.getenv("SUBGRAM_API_KEY", "")
# ============================================
# ПРОСТЫЕ ФУНКЦИИ ДЛЯ РАБОТЫ С БД (без пула)
# ============================================

pool = None
_pool_lock = asyncio.Lock()

async def get_conn():
    """Gets a connection from the process-local pool."""
    await init_pool()
    return await pool.acquire()

async def release_conn(conn):
    """Returns a connection to the pool."""
    if conn is not None and pool is not None:
        await pool.release(conn)
@asynccontextmanager
async def connection():
    """Provides one pooled connection and always returns it."""
    conn = await get_conn()
    try:
        yield conn
    finally:
        await release_conn(conn)
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


async def _ensure_broadcast_schema(conn):
    """Create/migrate the broadcast tables used by the worker.

    The migration is idempotent and also backfills recipients for jobs created
    by older versions that stored the audience in ``user_ids_json``.
    """
    # The admin panel and worker can start before the bot process on a fresh
    # installation.  Ensure the legacy parent table exists before applying
    # additive migrations or creating the recipient foreign key.
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
    await conn.execute("ALTER TABLE broadcast_jobs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMP")
    await conn.execute("ALTER TABLE broadcast_jobs ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP")
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_broadcast_jobs_heartbeat
        ON broadcast_jobs (status, heartbeat_at)
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS broadcast_job_recipients (
            job_id INTEGER NOT NULL REFERENCES broadcast_jobs(id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            error_code TEXT,
            error_message TEXT,
            sent_at TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (job_id, user_id)
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS service_heartbeats (
            service TEXT PRIMARY KEY,
            heartbeat_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_service_heartbeats_at
        ON service_heartbeats (heartbeat_at)
    """)
    # Recipient history must survive moving a blocked user out of ``users``.
    # Older installations may have created this foreign key before the audit
    # table was introduced, so remove it idempotently during migration.
    await conn.execute("""
        ALTER TABLE broadcast_job_recipients
        DROP CONSTRAINT IF EXISTS broadcast_job_recipients_user_id_fkey
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_broadcast_job_recipients_pending
        ON broadcast_job_recipients (job_id, status, user_id)
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_broadcast_job_recipients_lease
        ON broadcast_job_recipients (status, updated_at)
    """)
    await conn.execute("""
        INSERT INTO broadcast_job_recipients (job_id, user_id)
        SELECT jobs.id, recipient.user_id::BIGINT
        FROM broadcast_jobs AS jobs
        CROSS JOIN LATERAL jsonb_array_elements_text(
            COALESCE(jobs.user_ids_json, '[]'::jsonb)
        ) AS recipient(user_id)
        ON CONFLICT (job_id, user_id) DO NOTHING
    """)


async def ensure_broadcast_schema():
    """Run the broadcast migration from processes that do not call init_db."""
    conn = await get_conn()
    try:
        await _ensure_broadcast_schema(conn)
        await _ensure_payment_link_visits_schema(conn)
        await _ensure_unsubscribed_schema(conn)
    finally:
        await release_conn(conn)

# ============================================
# ОСТАЛЬНЫЕ ФУНКЦИИ (ТЕ ЖЕ)
# ============================================

async def _ensure_payment_link_visits_schema(conn):
    """Create the aggregate audit table for visits to payment links."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS payment_link_visits (
            user_id BIGINT PRIMARY KEY,
            visit_count INTEGER NOT NULL DEFAULT 1,
            first_visited_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_visited_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_payment_link_visits_last_visited_at
        ON payment_link_visits (last_visited_at DESC)
    """)


async def _ensure_unsubscribed_schema(conn):
    """Create and migrate the unsubscribe archive schema."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS unsubscribed_users (
            telegram_id BIGINT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            unsubscribed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reason TEXT DEFAULT 'manual',
            card_token TEXT,
            subscription_id TEXT,
            subscription_record_id INTEGER,
            subscription_plan TEXT,
            subscription_start_date TIMESTAMP,
            subscription_end_date TIMESTAMP,
            subscription_status TEXT,
            subscription_created_at TIMESTAMP,
            subscription_next_retry_at TIMESTAMP WITH TIME ZONE,
            subscription_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb
        )
    """)

    # Add columns to installations created before the archive stored a
    # complete subscription snapshot.  Every statement is idempotent.
    await conn.execute("ALTER TABLE unsubscribed_users ADD COLUMN IF NOT EXISTS card_token TEXT")
    await conn.execute("ALTER TABLE unsubscribed_users ADD COLUMN IF NOT EXISTS subscription_id TEXT")
    await conn.execute("ALTER TABLE unsubscribed_users ADD COLUMN IF NOT EXISTS subscription_record_id INTEGER")
    await conn.execute("ALTER TABLE unsubscribed_users ADD COLUMN IF NOT EXISTS subscription_plan TEXT")
    await conn.execute("ALTER TABLE unsubscribed_users ADD COLUMN IF NOT EXISTS subscription_start_date TIMESTAMP")
    await conn.execute("ALTER TABLE unsubscribed_users ADD COLUMN IF NOT EXISTS subscription_end_date TIMESTAMP")
    await conn.execute("ALTER TABLE unsubscribed_users ADD COLUMN IF NOT EXISTS subscription_status TEXT")
    await conn.execute("ALTER TABLE unsubscribed_users ADD COLUMN IF NOT EXISTS subscription_created_at TIMESTAMP")
    await conn.execute("ALTER TABLE unsubscribed_users ADD COLUMN IF NOT EXISTS subscription_next_retry_at TIMESTAMP WITH TIME ZONE")
    await conn.execute("ALTER TABLE unsubscribed_users ADD COLUMN IF NOT EXISTS subscription_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_unsubscribed_users_unsubscribed_at ON unsubscribed_users (unsubscribed_at DESC)")


async def ensure_unsubscribed_schema():
    """Run the unsubscribe archive migration from web processes."""
    conn = await get_conn()
    try:
        await _ensure_unsubscribed_schema(conn)
    finally:
        await release_conn(conn)


async def ensure_payment_link_visits_schema():
    """Run the payment-link visits migration from web processes."""
    conn = await get_conn()
    try:
        await _ensure_payment_link_visits_schema(conn)
    finally:
        await release_conn(conn)


async def record_payment_link_visit(user_id: int):
    """Record one visit without treating it as a payment."""
    conn = await get_conn()
    try:
        await conn.execute(
            """
            INSERT INTO payment_link_visits (user_id)
            VALUES ($1)
            ON CONFLICT (user_id) DO UPDATE SET
                visit_count = payment_link_visits.visit_count + 1,
                last_visited_at = CURRENT_TIMESTAMP
            """,
            user_id,
        )
    finally:
        await release_conn(conn)


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

        await _ensure_broadcast_schema(conn)
        
        await _ensure_unsubscribed_schema(conn)
        
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
        async with conn.transaction():
            if not username or not first_name:
                user_data = await conn.fetchrow(
                    "SELECT username, first_name FROM users WHERE telegram_id = $1",
                    user_id,
                )
                if user_data:
                    username = user_data['username'] or ''
                    first_name = user_data['first_name'] or ''

            subscription_rows = await conn.fetch(
                """
                SELECT to_jsonb(s) AS snapshot
                FROM subscriptions AS s
                WHERE s.user_id = $1
                ORDER BY s.created_at DESC, s.id DESC
                """,
                user_id,
            )
            latest_subscription = await conn.fetchrow(
                """
                SELECT *
                FROM subscriptions
                WHERE user_id = $1
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                user_id,
            )

            # Keep the complete history as JSON and also copy the current
            # subscription's key fields for filtering and restoration.
            snapshot_rows = []
            for row in subscription_rows:
                value = row['snapshot']
                if isinstance(value, str):
                    value = json.loads(value)
                snapshot_rows.append(value)
            subscription_snapshot = json.dumps(snapshot_rows, ensure_ascii=False, default=str)

            card_token = latest_subscription['card_token'] if latest_subscription else None
            subscription_id = latest_subscription['subscription_id'] if latest_subscription else None

            await conn.execute(
                "UPDATE subscriptions SET status = 'cancelled' WHERE user_id = $1",
                user_id,
            )

            await conn.execute(
                """
                INSERT INTO unsubscribed_users (
                    telegram_id, username, first_name, reason, unsubscribed_at,
                    card_token, subscription_id, subscription_record_id,
                    subscription_plan, subscription_start_date, subscription_end_date,
                    subscription_status, subscription_created_at,
                    subscription_next_retry_at, subscription_snapshot
                )
                VALUES (
                    $1, $2, $3, $4, NOW(), $5, $6, $7, $8, $9, $10,
                    $11, $12, $13, $14::jsonb
                )
                ON CONFLICT (telegram_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    reason = EXCLUDED.reason,
                    unsubscribed_at = EXCLUDED.unsubscribed_at,
                    card_token = EXCLUDED.card_token,
                    subscription_id = EXCLUDED.subscription_id,
                    subscription_record_id = EXCLUDED.subscription_record_id,
                    subscription_plan = EXCLUDED.subscription_plan,
                    subscription_start_date = EXCLUDED.subscription_start_date,
                    subscription_end_date = EXCLUDED.subscription_end_date,
                    subscription_status = EXCLUDED.subscription_status,
                    subscription_created_at = EXCLUDED.subscription_created_at,
                    subscription_next_retry_at = EXCLUDED.subscription_next_retry_at,
                    subscription_snapshot = EXCLUDED.subscription_snapshot
                """,
                user_id,
                username or '',
                first_name or '',
                reason,
                card_token,
                subscription_id,
                latest_subscription['id'] if latest_subscription else None,
                latest_subscription['plan'] if latest_subscription else None,
                latest_subscription['start_date'] if latest_subscription else None,
                latest_subscription['end_date'] if latest_subscription else None,
                latest_subscription['status'] if latest_subscription else None,
                latest_subscription['created_at'] if latest_subscription else None,
                latest_subscription.get('next_retry_at') if latest_subscription else None,
                subscription_snapshot,
            )

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
    """Creates one connection pool for the current process."""
    global pool
    if pool is None:
        async with _pool_lock:
            if pool is None:
                pool = await asyncpg.create_pool(
                    DATABASE_URL,
                    min_size=1,
                    max_size=10,
                    command_timeout=30,
                )
    return pool

async def close_pool():
    """Closes the process-local connection pool."""
    global pool
    if pool is not None:
        await pool.close()
        pool = None
async def check_subgram(user_id: int, chat_id: int) -> dict:
    """
    Проверяет подписку пользователя через SubGram.
    Возвращает:
        - status: 'ok' — можно пропустить
        - status: 'warning' — нужна подписка
        - status: 'error' — ошибка, пропустить
    """
    if not SUBGRAM_API_KEY:
        print("⚠️ SUBGRAM_API_KEY не настроен. Пропускаем проверку.")
        return {"status": "ok", "message": "SubGram не настроен"}
    
    url = "https://api.subgram.org/get-sponsors"
    headers = {
        "Auth": SUBGRAM_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "user_id": user_id,
        "chat_id": chat_id
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    print(f"🔍 SubGram ответ для {user_id}: {data}")
                    return data
                else:
                    print(f"❌ SubGram ошибка: {response.status}")
                    return {"status": "error", "message": f"HTTP {response.status}"}
    except Exception as e:
        print(f"❌ SubGram исключение: {e}")
        return {"status": "error", "message": str(e)}



async def get_subgram_balance() -> dict:
    """
    Получает баланс и статистику по ботам из SubGram.
    Документация: https://api.subgram.org/api-docs#get-balance
    """
    if not SUBGRAM_API_TOKEN:
        return {"status": "error", "message": "SUBGRAM_API_TOKEN не настроен"}

    url = "https://api.subgram.org/get-balance"
    headers = {
        "Auth": SUBGRAM_API_TOKEN,
        "Content-Type": "application/json"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json={}, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    print(f"🔍 SubGram Balance ответ: {data}")
                    return data
                else:
                    print(f"❌ SubGram ошибка: {response.status}")
                    return {"status": "error", "message": f"HTTP {response.status}"}
    except Exception as e:
        print(f"❌ SubGram исключение: {e}")
        return {"status": "error", "message": str(e)}
