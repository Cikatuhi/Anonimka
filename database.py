import asyncpg
import json
import logging
import os
import secrets
from datetime import datetime, timezone
from dotenv import load_dotenv
import asyncio
import ssl
from time import monotonic
from contextlib import asynccontextmanager
import aiohttp
from time_utils import to_db_timestamp

load_dotenv()

DB_HOST = os.getenv("DB_HOST", "45.10.43.48")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "default_db")
DB_USER = os.getenv("DB_USER", "gen_user")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_SSL_CA = os.getenv("DB_SSL_CA")
DB_SSL = ssl.create_default_context(cafile=DB_SSL_CA) if DB_SSL_CA else None

DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

SUBGRAM_API_TOKEN = os.getenv("SUBGRAM_API_TOKEN", "")
SUBGRAM_API_KEY = os.getenv("SUBGRAM_API_KEY", "")
# ============================================
# ПРОСТЫЕ ФУНКЦИИ ДЛЯ РАБОТЫ С БД (без пула)
# ============================================

pool = None
_pool_lock = asyncio.Lock()
_subgram_session = None
_subgram_session_lock = asyncio.Lock()
_subgram_check_cache: dict[tuple[int, int], tuple[float, dict]] = {}
_subgram_check_inflight: dict[tuple[int, int], asyncio.Task] = {}
_subgram_check_lock = asyncio.Lock()
SCHEMA_MIGRATION_LOCK = "anonimno.schema-migrations"

# Keep the existing 10-second upper bound while failing stalled connections
# earlier; a shared session handles the common case without a new TCP setup.
SUBGRAM_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=2, sock_read=8)
SUBGRAM_CHECK_CACHE_TTL = 5.0
SUBGRAM_CHECK_ERROR_CACHE_TTL = 2.0


@asynccontextmanager
async def schema_migration_lock(conn):
    """Serialize startup DDL across all application processes."""
    async with conn.transaction():
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1))",
            SCHEMA_MIGRATION_LOCK,
        )
        yield

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
    """Получить количество подписок с действующим оплаченным доступом."""
    conn = await get_conn()
    try:
        return await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM subscriptions
            WHERE (
                status = 'active' AND (end_date IS NULL OR end_date > NOW())
            ) OR (
                status = 'cancelled' AND end_date IS NOT NULL AND end_date > NOW()
                AND EXISTS (
                    SELECT 1
                    FROM unsubscribed_users AS archived
                    WHERE archived.telegram_id = subscriptions.user_id
                      AND archived.reason = 'manual'
                )
            )
            """
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
    # Remove the legacy message-retention trigger on installations where the
    # messages table already exists.  The helper checks for the table first,
    # keeping startup safe on a fresh database.
    await ensure_message_retention_schema(conn)
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
        async with schema_migration_lock(conn):
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
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_payment_link_visits_first_visited_at
        ON payment_link_visits (first_visited_at DESC)
    """)


async def ensure_admin_dashboard_notifications(conn):
    """Install PostgreSQL events used for live admin dashboard updates.

    PostgreSQL delivers ``NOTIFY`` only after the transaction that changed a
    row commits.  The event is therefore a lightweight invalidation signal:
    the admin panel can fetch one consistent snapshot instead of polling every
    few seconds.  The migration is idempotent and skips optional tables that
    have not been created on an older installation yet.
    """
    await conn.execute("""
        CREATE OR REPLACE FUNCTION notify_admin_dashboard_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            PERFORM pg_notify(
                'admin_dashboard_changed',
                json_build_object('table', TG_TABLE_NAME, 'operation', TG_OP)::text
            );
            -- The trigger is statement-level, so there is no row to return.
            RETURN NULL;
        END;
        $$
    """)

    # These are the only tables that affect the dashboard cards.  Do not
    # include service_heartbeats: the worker updates it every few seconds and
    # it would create an unnecessary event stream.
    trigger_operations = {
        # These counters depend on row membership, not profile-field edits.
        "users": "INSERT OR DELETE",
        "blocked_users": "INSERT OR DELETE",
        "unsubscribed_users": "INSERT OR DELETE",
        "messages": "INSERT OR DELETE",
        "payment_link_visits": "INSERT OR DELETE",
        # Successful payment totals are built from both webhook journal rows
        # and the recurrent-payment ledger.  Both tables are append-only.
        "webhook_events": "INSERT",
        "payment_events": "INSERT",
        # The active-subscription count changes when status or end_date does.
        "subscriptions": "INSERT OR UPDATE OF status, end_date OR DELETE",
    }
    for table_name, operations in trigger_operations.items():
        exists = await conn.fetchval(
            "SELECT to_regclass($1) IS NOT NULL",
            f"public.{table_name}",
        )
        if not exists:
            continue
        trigger_name = f"notify_admin_dashboard_{table_name}"
        await conn.execute(
            f'DROP TRIGGER IF EXISTS "{trigger_name}" ON public."{table_name}"'
        )
        await conn.execute(
            f'''CREATE TRIGGER "{trigger_name}"
                AFTER {operations} ON public."{table_name}"
                FOR EACH STATEMENT EXECUTE FUNCTION notify_admin_dashboard_change()'''
        )


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
        async with schema_migration_lock(conn):
            await _ensure_unsubscribed_schema(conn)
    finally:
        await release_conn(conn)


async def ensure_payment_link_visits_schema():
    """Run the payment-link visits migration from web processes."""
    conn = await get_conn()
    try:
        async with schema_migration_lock(conn):
            await _ensure_payment_link_visits_schema(conn)
    finally:
        await release_conn(conn)


async def ensure_message_retention_schema(conn):
    """Keep the full message history in ``messages``.

    Some older installations created a database-level trigger that called
    ``clean_old_messages()`` after every insert, silently deleting older
    messages once the table reached a fixed size.  The current application
    does not use that retention policy, so remove only that legacy trigger.
    Dropping a trigger does not modify any existing rows and is safe to run
    repeatedly during startup migrations.
    """
    messages_table_exists = await conn.fetchval(
        "SELECT to_regclass('public.messages') IS NOT NULL"
    )
    if messages_table_exists:
        await conn.execute(
            "DROP TRIGGER IF EXISTS trigger_clean_messages ON public.messages"
        )


async def ensure_webhook_events_schema(conn):
    """Create the durable CloudPayments webhook journal.

    The payment service logs requests to journald, which is not available to
    the admin-panel process as a database-backed list.  Keep one immutable
    record per received payload so the panel can show the same events that the
    webhook endpoint acknowledged.
    """
    await conn.execute("""
        CREATE OR REPLACE FUNCTION anonimno_try_parse_jsonb(value TEXT)
        RETURNS JSONB
        LANGUAGE plpgsql
        IMMUTABLE
        AS $$
        BEGIN
            IF value IS NULL OR btrim(value) = '' THEN
                RETURN '{}'::jsonb;
            END IF;
            RETURN value::jsonb;
        EXCEPTION WHEN OTHERS THEN
            RETURN '{}'::jsonb;
        END;
        $$
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS webhook_events (
            id BIGSERIAL PRIMARY KEY,
            received_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            event_type TEXT,
            transaction_id TEXT,
            account_id TEXT,
            status TEXT,
            is_recurrent BOOLEAN,
            payload JSONB NOT NULL
        )
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_webhook_events_received_at
        ON webhook_events (received_at DESC)
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_webhook_events_transaction_id
        ON webhook_events (transaction_id)
    """)


async def ensure_payment_events_schema(conn):
    """Create the recurrent-payment ledger used by subscriptions and reports.

    The payment service and the admin panel can start independently. Keep this
    migration in one shared helper so the dashboard query never runs before
    its ledger table exists.
    """
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS payment_events (
            id SERIAL PRIMARY KEY,
            transaction_id TEXT UNIQUE NOT NULL,
            user_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_payment_events_user_id
        ON payment_events(user_id)
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_payment_events_created_at
        ON payment_events(created_at DESC)
    """)


async def ensure_payment_events_user_fk_protection(conn):
    """Keep recurrent payment history when a user record is removed.

    Older installations may have created ``payment_events.user_id`` with
    ``ON DELETE CASCADE``. Payment events are accounting history, so deleting
    a user must not silently erase them or change the dashboard total.
    """
    async with conn.transaction():
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext('anonimno.payment-events-user-fk'))"
        )
        constraints = await conn.fetch(
            """
            SELECT conname, confdeltype
            FROM pg_constraint
            WHERE contype = 'f'
              AND conrelid = to_regclass('payment_events')
              AND confrelid = to_regclass('users')
            """
        )

        # ``r`` is RESTRICT and ``a`` is NO ACTION; both prevent a parent
        # delete from removing the payment history.
        if constraints and all(row["confdeltype"] in ("r", "a") for row in constraints):
            return

        for row in constraints:
            await conn.execute(
                f"ALTER TABLE payment_events DROP CONSTRAINT IF EXISTS {_quote_identifier(row['conname'])}"
            )

        await conn.execute(
            """
            ALTER TABLE payment_events
            ADD CONSTRAINT payment_events_user_id_users_fkey_protected
            FOREIGN KEY (user_id) REFERENCES users(telegram_id) ON DELETE RESTRICT
            NOT VALID
            """
        )


async def record_webhook_event(
    conn,
    *,
    payload: dict,
    event_type: str | None,
    transaction_id,
    account_id,
    status,
    is_recurrent: bool,
):
    """Append one CloudPayments payload to the durable webhook journal."""
    await conn.execute(
        """
        INSERT INTO webhook_events
            (event_type, transaction_id, account_id, status, is_recurrent, payload)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb)
        """,
        event_type,
        str(transaction_id) if transaction_id is not None else None,
        str(account_id) if account_id is not None else None,
        str(status) if status is not None else None,
        bool(is_recurrent),
        json.dumps(payload, ensure_ascii=False, default=str),
    )


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


def _quote_identifier(identifier: str) -> str:
    """Quote a PostgreSQL identifier returned from the system catalog."""
    return '"' + identifier.replace('"', '""') + '"'


async def ensure_subscription_user_fk_protection(conn):
    """Prevent deleting a user from cascading into subscription records.

    Older installations created ``subscriptions.user_id`` with
    ``ON DELETE CASCADE``.  Replace that relationship with ``RESTRICT`` once,
    while leaving every existing row untouched.  The migration is idempotent
    and also handles installations whose foreign-key constraint has a custom
    name.
    """
    # Both the bot and payment service run startup migrations.  Serialize this
    # one so two simultaneous starts cannot both try to recreate the constraint.
    async with conn.transaction():
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext('anonimno.subscription-user-fk'))"
        )
        constraints = await conn.fetch(
            """
            SELECT conname, confdeltype
            FROM pg_constraint
            WHERE contype = 'f'
              AND conrelid = to_regclass('subscriptions')
              AND confrelid = to_regclass('users')
            """
        )

        # ``r`` is RESTRICT and ``a`` is NO ACTION; both prevent a parent delete.
        if constraints and all(row["confdeltype"] in ("r", "a") for row in constraints):
            return

        for row in constraints:
            await conn.execute(
                f"ALTER TABLE subscriptions DROP CONSTRAINT IF EXISTS {_quote_identifier(row['conname'])}"
            )

        await conn.execute(
            """
            ALTER TABLE subscriptions
            ADD CONSTRAINT subscriptions_user_id_users_fkey_protected
            FOREIGN KEY (user_id) REFERENCES users(telegram_id) ON DELETE RESTRICT
            """
        )


async def _ensure_core_schema(conn):
    """Apply additive migrations required by the current application.

    Existing installations can have the original tables but lack columns that
    were introduced later.  These statements never delete rows or alter the
    values already stored in the database, so it is safe to run at startup.
    """
    await conn.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS has_seen_premium_offer BOOLEAN DEFAULT FALSE"
    )
    await conn.execute(
        "ALTER TABLE blocked_users ADD COLUMN IF NOT EXISTS blocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
    )
    await conn.execute(
        "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS card_token TEXT"
    )
    await conn.execute(
        "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMP WITH TIME ZONE"
    )
    await ensure_subscription_user_fk_protection(conn)
    await ensure_webhook_events_schema(conn)
    await ensure_payment_events_schema(conn)
    await ensure_payment_events_user_fk_protection(conn)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_subscriptions_next_retry_at
        ON subscriptions(next_retry_at)
    """)


async def init_db():
    conn = await get_conn()
    try:
        async with schema_migration_lock(conn):
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
            await ensure_message_retention_schema(conn)
            await ensure_webhook_events_schema(conn)

            await conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE RESTRICT,
                subscription_id TEXT,
                card_token TEXT,
                plan TEXT NOT NULL,
                start_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                end_date TIMESTAMP,
                status TEXT DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Keep an existing database from an older version compatible with the
        # current bot before any service starts reading subscription data.
            await _ensure_core_schema(conn)

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
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_registered_at ON users(registered_at DESC)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at DESC)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_sender_created_at ON messages(sender_id, created_at DESC)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_recipient_created_at ON messages(recipient_id, created_at DESC)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_broadcast_jobs_status ON broadcast_jobs(status)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_user_id ON subscriptions(user_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_user_created_at ON subscriptions(user_id, created_at DESC)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_status ON subscriptions(status)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_unsubscribed_users_id ON unsubscribed_users(telegram_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_payment_tokens_token ON payment_tokens(token)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_payment_tokens_user_id ON payment_tokens(user_id)")
            await ensure_admin_dashboard_notifications(conn)

            print("✅ База данных инициализирована")
    finally:
        await release_conn(conn)

# ============================================
# ОСТАЛЬНЫЕ ФУНКЦИИ (ПОЛНЫЙ СПИСОК)
# ============================================

async def register_user(
    user_id: int,
    username: str,
    first_name: str,
    *,
    clear_blocked: bool = False,
):
    conn = await get_conn()
    try:
        # Restore a user and optionally clear a previous Telegram block marker
        # atomically.  This replaces the old SELECT -> DELETE -> INSERT
        # sequence with one round trip while preserving other callers that
        # only register a user.
        row = await conn.fetchrow(
            """
            WITH unblocked AS (
                DELETE FROM blocked_users
                WHERE telegram_id = $1 AND $4::boolean
                RETURNING telegram_id
            )
            INSERT INTO users (telegram_id, username, first_name, registered_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (telegram_id) DO NOTHING
            RETURNING telegram_id
            """,
            user_id,
            username,
            first_name,
            clear_blocked,
        )
        return row is not None
    finally:
        await release_conn(conn)


async def get_random_user(exclude_user_id: int):
    """Return one random user directly from PostgreSQL.

    The previous implementation transferred up to 1,000 IDs to Python and
    then issued another query for the selected profile.  Keep the same
    candidate set, but choose the user and return the profile in one query.
    """
    conn = await get_conn()
    try:
        row = await conn.fetchrow(
            """
            SELECT telegram_id, username, first_name
            FROM (
                SELECT telegram_id, username, first_name
                FROM users
                ORDER BY registered_at
                LIMIT 1000
            ) AS candidates
            WHERE telegram_id <> $1
            ORDER BY random()
            LIMIT 1
            """,
            exclude_user_id,
        )
        return dict(row) if row else None
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
        # Keep the users row intact.  blocked_users is only an audience-control
        # marker; it must never remove billing data.
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
        """, user_id, plan, status, to_db_timestamp(end_date), card_token, subscription_id)
    finally:
        await release_conn(conn)

async def update_subscription_status(user_id: int, status: str, end_date=None):
    conn = await get_conn()
    try:
        if end_date:
            await conn.execute(
                "UPDATE subscriptions SET status = $1, end_date = $2 WHERE user_id = $3",
                status,
                to_db_timestamp(end_date),
                user_id,
            )
        else:
            await conn.execute("UPDATE subscriptions SET status = $1 WHERE user_id = $2", status, user_id)
    finally:
        await release_conn(conn)

async def update_subscription_plan(user_id: int, plan: str, end_date=None):
    conn = await get_conn()
    try:
        if end_date:
            await conn.execute(
                "UPDATE subscriptions SET plan = $1, end_date = $2 WHERE user_id = $3",
                plan,
                to_db_timestamp(end_date),
                user_id,
            )
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

async def count_users_in_retry_low():
    conn = await get_conn()
    try:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM subscriptions WHERE status = 'retry_low'"
        )
    finally:
        await release_conn(conn)

async def count_active_subscriptions():
    conn = await get_conn()
    try:
        return await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM subscriptions
            WHERE (
                status = 'active' AND (end_date IS NULL OR end_date > NOW())
            ) OR (
                status = 'cancelled' AND end_date IS NOT NULL AND end_date > NOW()
                AND EXISTS (
                    SELECT 1
                    FROM unsubscribed_users AS archived
                    WHERE archived.telegram_id = subscriptions.user_id
                      AND archived.reason = 'manual'
                )
            )
            """
        )
    finally:
        await release_conn(conn)

async def get_all_subscriptions(limit: int = 100, offset: int = 0):
    conn = await get_conn()
    try:
        rows = await conn.fetch("""
            WITH webhook_candidates AS (
                SELECT
                    wp.id,
                    wp.transaction_id,
                    COALESCE(
                        NULLIF(wp.account_id, ''),
                        wp.payload->>'AccountId',
                        wp.model_payload->>'AccountId'
                    ) AS raw_user_id,
                    COALESCE(
                        wp.payload->>'OperationType',
                        wp.model_payload->>'OperationType',
                        wp.payload->>'EventType',
                        wp.payload->>'Type',
                        NULLIF(wp.event_type, '')
                    ) AS raw_operation_type,
                    COALESCE(
                        NULLIF(wp.payload->>'Amount', ''),
                        wp.model_payload->>'Amount'
                    ) AS raw_amount
                FROM (
                    SELECT
                        we.*,
                        CASE
                            WHEN jsonb_typeof(we.payload->'Model') = 'object'
                                THEN we.payload->'Model'
                            WHEN jsonb_typeof(we.payload->'Model') = 'string'
                                THEN anonimno_try_parse_jsonb(we.payload->>'Model')
                            ELSE '{}'::JSONB
                        END AS model_payload
                    FROM webhook_events AS we
                ) AS wp
                WHERE LOWER(COALESCE(wp.status, '')) = 'completed'
                  AND NULLIF(wp.transaction_id, '') IS NOT NULL
            ), webhook_completed AS (
                SELECT DISTINCT ON (transaction_id)
                    transaction_id,
                    CASE
                        WHEN raw_user_id ~ '^[0-9]+$' THEN raw_user_id::BIGINT
                        ELSE NULL
                    END AS user_id,
                    CASE
                        WHEN raw_amount ~ '^[0-9]+([.,][0-9]+)?$'
                            THEN REPLACE(raw_amount, ',', '.')::NUMERIC
                        ELSE 0::NUMERIC
                    END AS amount_rub
                FROM webhook_candidates
                WHERE LOWER(COALESCE(raw_operation_type, '')) NOT LIKE '%refund%'
                  AND LOWER(COALESCE(raw_operation_type, '')) NOT LIKE '%chargeback%'
                  AND LOWER(COALESCE(raw_operation_type, '')) NOT LIKE '%reversal%'
                ORDER BY transaction_id, id
            ), successful_payments AS (
                SELECT user_id, amount_rub
                FROM webhook_completed
                WHERE user_id IS NOT NULL
                UNION ALL
                SELECT pe.user_id, pe.amount / 100.0
                FROM payment_events AS pe
                WHERE LOWER(COALESCE(pe.status, '')) = 'completed'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM webhook_completed AS wc
                      WHERE wc.transaction_id = pe.transaction_id
                  )
            ), payment_totals AS (
                SELECT user_id, SUM(amount_rub) AS successful_amount
                FROM successful_payments
                GROUP BY user_id
            )
            SELECT s.*, u.username, u.first_name,
                   COALESCE(pt.successful_amount, 0)::NUMERIC AS successful_amount
            FROM subscriptions AS s
            LEFT JOIN users AS u ON s.user_id = u.telegram_id
            LEFT JOIN payment_totals AS pt ON pt.user_id = s.user_id
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

async def unsubscribe_user(
    user_id: int,
    username: str = None,
    first_name: str = None,
    reason: str = 'manual',
    conn=None,
):
    owns_conn = conn is None
    if owns_conn:
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

            existing_archive_reason = await conn.fetchval(
                "SELECT reason FROM unsubscribed_users WHERE telegram_id = $1 FOR UPDATE",
                user_id,
            )

            # A technical archive (administrator block or an unavailable
            # card) must not be accidentally converted into a user-initiated
            # cancellation.  Keeping the original reason is important because
            # it controls whether paid access is still allowed until end_date.
            if (
                latest_subscription
                and latest_subscription["status"] == "cancelled"
                and existing_archive_reason
                and (
                    (reason == "manual" and existing_archive_reason != "manual")
                    or (reason != "manual" and existing_archive_reason == "manual")
                )
            ):
                return True

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
                """
                UPDATE subscriptions
                SET status = 'cancelled', next_retry_at = NULL
                WHERE user_id = $1
                """,
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
        if owns_conn:
            await release_conn(conn)


def _restore_snapshot_datetime(value, *, with_timezone: bool = False):
    """Convert a JSON timestamp back to the type expected by asyncpg."""
    if value is None or isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in {"none", "null"}:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            # Keep the transaction safe: an invalid optional timestamp should
            # not prevent restoring the rest of the saved subscription data.
            return None
    else:
        return None

    if parsed is None:
        return None
    if with_timezone:
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


async def restore_unsubscribed_user(user_id: int):
    """Restore the subscription records saved in the unsubscribe archive.

    Unsubscribing keeps a JSON snapshot because a user can have more than one
    historical subscription row.  Restore that snapshot atomically instead of
    inventing a new plan or term.  The archive is removed only after every row
    has been restored successfully.
    """
    conn = await get_conn()
    try:
        async with conn.transaction():
            archive = await conn.fetchrow(
                "SELECT * FROM unsubscribed_users WHERE telegram_id = $1 FOR UPDATE",
                user_id,
            )
            if not archive:
                return None

            raw_snapshot = archive.get("subscription_snapshot")
            if isinstance(raw_snapshot, str):
                try:
                    raw_snapshot = json.loads(raw_snapshot)
                except json.JSONDecodeError:
                    raw_snapshot = []
            if isinstance(raw_snapshot, dict):
                raw_snapshot = [raw_snapshot]
            if not isinstance(raw_snapshot, list):
                raw_snapshot = []

            # Only restore rows belonging to this user.  Older archive rows
            # may not have a snapshot, so fall back to the copied scalar fields.
            snapshots = []
            for item in raw_snapshot:
                if not isinstance(item, dict):
                    continue
                snapshot_user_id = item.get("user_id")
                if snapshot_user_id is not None:
                    try:
                        if int(snapshot_user_id) != user_id:
                            continue
                    except (TypeError, ValueError):
                        continue
                snapshots.append(item)
            if not snapshots:
                snapshots = [{
                    "id": archive.get("subscription_record_id"),
                    "user_id": user_id,
                    "subscription_id": archive.get("subscription_id"),
                    "card_token": archive.get("card_token"),
                    "plan": archive.get("subscription_plan") or "HIGH",
                    "start_date": archive.get("subscription_start_date"),
                    "end_date": archive.get("subscription_end_date"),
                    "status": archive.get("subscription_status") or "active",
                    "created_at": archive.get("subscription_created_at"),
                    "next_retry_at": archive.get("subscription_next_retry_at"),
                }]

            restored_ids = []
            for item in snapshots:
                record_id = item.get("id")
                try:
                    record_id = int(record_id) if record_id is not None else None
                except (TypeError, ValueError):
                    record_id = None

                subscription_id = item.get("subscription_id")
                card_token = item.get("card_token")
                plan = item.get("plan") or archive.get("subscription_plan") or "HIGH"
                start_date = _restore_snapshot_datetime(item.get("start_date"))
                end_date = _restore_snapshot_datetime(item.get("end_date"))
                status = item.get("status") or archive.get("subscription_status") or "active"
                created_at = _restore_snapshot_datetime(
                    item.get("created_at") or archive.get("subscription_created_at")
                )
                next_retry_at = _restore_snapshot_datetime(
                    item.get("next_retry_at"), with_timezone=True
                )

                if record_id is not None:
                    command = await conn.execute(
                        """
                        UPDATE subscriptions
                        SET subscription_id = $1,
                            card_token = $2,
                            plan = $3,
                            start_date = $4,
                            end_date = $5,
                            status = $6,
                            created_at = COALESCE($7, created_at),
                            next_retry_at = $8
                        WHERE id = $9 AND user_id = $10
                        """,
                        subscription_id,
                        card_token,
                        plan,
                        start_date,
                        end_date,
                        status,
                        created_at,
                        next_retry_at,
                        record_id,
                        user_id,
                    )
                    if command == "UPDATE 1":
                        restored_ids.append(record_id)
                        continue

                inserted = await conn.fetchrow(
                    """
                    INSERT INTO subscriptions (
                        user_id, subscription_id, card_token, plan, start_date,
                        end_date, status, created_at, next_retry_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, COALESCE($8, NOW()), $9)
                    RETURNING id
                    """,
                    user_id,
                    subscription_id,
                    card_token,
                    plan,
                    start_date,
                    end_date,
                    status,
                    created_at,
                    next_retry_at,
                )
                restored_ids.append(inserted["id"])

            if not restored_ids:
                raise RuntimeError("В архиве нет данных подписки для восстановления")

            await conn.execute(
                "DELETE FROM unsubscribed_users WHERE telegram_id = $1",
                user_id,
            )
            return {"restored_count": len(restored_ids), "subscription_ids": restored_ids}
    except Exception as e:
        print(f"❌ Ошибка восстановления подписки в БД: {e}")
        raise
    finally:
        await release_conn(conn)


async def is_user_unsubscribed(user_id: int) -> bool:
    conn = await get_conn()
    try:
        row = await conn.fetchrow("SELECT telegram_id FROM unsubscribed_users WHERE telegram_id = $1", user_id)
        return row is not None
    finally:
        await release_conn(conn)

async def get_unsubscribe_reason(user_id: int):
    """Return the archive reason for a cancelled subscription, if present."""
    conn = await get_conn()
    try:
        return await conn.fetchval(
            "SELECT reason FROM unsubscribed_users WHERE telegram_id = $1",
            user_id,
        )
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
                    ssl=DB_SSL,
                )
    return pool

async def close_pool():
    """Closes the process-local connection pool."""
    global pool
    if pool is not None:
        await pool.close()
        pool = None
    await close_http_session()


async def _get_subgram_session():
    """Return a process-local HTTP session with connection reuse."""
    global _subgram_session
    if _subgram_session is None or _subgram_session.closed:
        async with _subgram_session_lock:
            if _subgram_session is None or _subgram_session.closed:
                connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
                _subgram_session = aiohttp.ClientSession(connector=connector)
    return _subgram_session


async def close_http_session():
    """Close the shared external HTTP session during service shutdown."""
    global _subgram_session
    if _subgram_session is not None and not _subgram_session.closed:
        await _subgram_session.close()
    _subgram_session = None
    _subgram_check_cache.clear()
    _subgram_check_inflight.clear()


async def _fetch_subgram_check(user_id: int, chat_id: int) -> dict:
    """Perform one SubGram request without logging the provider payload."""
    if not SUBGRAM_API_KEY:
        logging.warning("SUBGRAM_API_KEY не настроен. Проверка пропущена.")
        return {"status": "ok", "message": "SubGram не настроен"}

    url = "https://api.subgram.org/get-sponsors"
    headers = {
        "Auth": SUBGRAM_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {"user_id": user_id, "chat_id": chat_id}

    try:
        session = await _get_subgram_session()
        async with session.post(
            url,
            headers=headers,
            json=payload,
            timeout=SUBGRAM_TIMEOUT,
        ) as response:
            response_text = await response.text()
            if response.status == 200:
                try:
                    return json.loads(response_text)
                except json.JSONDecodeError:
                    logging.error("SubGram вернул некорректный JSON: HTTP %s", response.status)
                    return {"status": "error", "message": "Некорректный ответ SubGram"}

            logging.warning("SubGram вернул HTTP %s для проверки пользователя %s", response.status, user_id)
            return {"status": "error", "message": f"HTTP {response.status}: {response_text[:300]}"}
    except Exception as exc:
        logging.warning("SubGram недоступен для проверки пользователя %s: %s", user_id, exc)
        return {"status": "error", "message": str(exc)}


async def _fetch_and_cache_subgram_check(key: tuple[int, int]) -> dict:
    """Share one in-flight request and cache its short-lived result."""
    result = await _fetch_subgram_check(*key)
    status = result.get("status")
    # A user may complete a sponsor task immediately after a warning.  Do not
    # cache that negative answer; concurrent checks are already coalesced by
    # the in-flight task above.  A successful result and a short-lived error
    # are safe to reuse for their respective small windows.
    if status == "ok":
        ttl = SUBGRAM_CHECK_CACHE_TTL
    elif status == "warning":
        ttl = 0
    else:
        ttl = SUBGRAM_CHECK_ERROR_CACHE_TTL
    async with _subgram_check_lock:
        now = monotonic()
        if ttl:
            _subgram_check_cache[key] = (now + ttl, dict(result))
        # A bot with many unique users must not turn this optimization into
        # another unbounded in-memory dictionary.
        if len(_subgram_check_cache) > 2048:
            for cached_key, (expires_at, _value) in list(_subgram_check_cache.items()):
                if expires_at <= now:
                    _subgram_check_cache.pop(cached_key, None)
    return result


async def check_subgram(user_id: int, chat_id: int) -> dict:
    """Check a user's SubGram status with a short per-user cache."""
    key = (int(user_id), int(chat_id))
    now = monotonic()
    async with _subgram_check_lock:
        cached = _subgram_check_cache.get(key)
        if cached and cached[0] > now:
            return dict(cached[1])

        task = _subgram_check_inflight.get(key)
        if task is None:
            task = asyncio.create_task(
                _fetch_and_cache_subgram_check(key),
                name=f"subgram-check-{key[0]}",
            )
            _subgram_check_inflight[key] = task

    try:
        # A cancelled Telegram update should not cancel the shared request for
        # another concurrent update from the same user.
        result = await asyncio.shield(task)
        return dict(result)
    finally:
        if task.done():
            async with _subgram_check_lock:
                if _subgram_check_inflight.get(key) is task:
                    _subgram_check_inflight.pop(key, None)



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
        session = await _get_subgram_session()
        async with session.post(url, headers=headers, json={}, timeout=SUBGRAM_TIMEOUT) as response:
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
