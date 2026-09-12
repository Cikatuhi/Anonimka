import asyncio
import asyncpg
import json
import os
import time as monotonic_clock
import uuid
import psutil
import re
import aiohttp
from decimal import Decimal, InvalidOperation
from html import escape
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager, suppress
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response, StreamingResponse
from dotenv import load_dotenv
from cloudpayments import CloudPaymentsClient
from time_utils import now_msk
import database

load_dotenv()

# The admin panel is private infrastructure. Bind it to loopback by default;
# expose it publicly only through an explicitly configured, authenticated
# reverse proxy or an SSH tunnel.
ADMIN_BIND_HOST = os.getenv("ADMIN_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"
ADMIN_PORT = int(os.getenv("ADMIN_PORT", "8000"))

ADMINS = {
    "cikatuh": "2509",
}


ADMIN_TIMEZONE = ZoneInfo("Europe/Samara")

ADMIN_DASHBOARD_CHANNEL = "admin_dashboard_changed"
_dashboard_subscribers: set[asyncio.Queue] = set()
_dashboard_listener_task: asyncio.Task | None = None
_dashboard_listener_stop: asyncio.Event | None = None

# SubGram is an external service, so PostgreSQL cannot notify us when its
# balance changes.  Keep a short process-local cache to avoid calling the
# provider for every dashboard invalidation (a busy bot can generate many
# database notifications in a short period).
SUBGRAM_DASHBOARD_CACHE_TTL = 60.0
_subgram_balance_cache = {
    "expires_at": 0.0,
    "value": None,
    "ok": False,
    "error": None,
}
_subgram_balance_lock = asyncio.Lock()


def _publish_dashboard_event(payload: str = "changed") -> None:
    """Wake every connected dashboard without doing database work here."""
    for queue in tuple(_dashboard_subscribers):
        # One pending notification is enough: the browser always fetches a
        # complete fresh snapshot, so a slow client never builds a backlog.
        if not queue.full():
            queue.put_nowait(payload)


async def _dashboard_listener() -> None:
    """Keep one dedicated PostgreSQL LISTEN connection for the admin panel."""
    assert _dashboard_listener_stop is not None
    retry_delay = 1.0

    while not _dashboard_listener_stop.is_set():
        conn = None
        callback = None
        termination_listener = None
        stop_wait = None
        connection_lost_wait = None
        try:
            conn = await asyncio.wait_for(
                asyncpg.connect(
                    database.DATABASE_URL,
                    ssl=database.DB_SSL,
                    command_timeout=30,
                ),
                timeout=10,
            )

            def on_notification(_connection, _pid, _channel, payload):
                _publish_dashboard_event(payload or "changed")

            callback = on_notification
            await conn.add_listener(ADMIN_DASHBOARD_CHANNEL, callback)
            connection_lost = asyncio.Event()

            def on_termination(_connection):
                connection_lost.set()

            termination_listener = on_termination
            conn.add_termination_listener(termination_listener)
            retry_delay = 1.0
            print("✅ LISTEN admin_dashboard_changed подключён", flush=True)
            # A reconnect may have missed a notification.  Invalidate all
            # connected browsers once so they recover from the latest snapshot.
            _publish_dashboard_event("listener_reconnected")
            stop_wait = asyncio.create_task(_dashboard_listener_stop.wait())
            connection_lost_wait = asyncio.create_task(connection_lost.wait())
            done, _ = await asyncio.wait(
                (stop_wait, connection_lost_wait),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if connection_lost_wait in done and not _dashboard_listener_stop.is_set():
                print("⚠️ LISTEN-соединение PostgreSQL закрыто, переподключение", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"⚠️ LISTEN admin_dashboard_changed: {exc}", flush=True)
        finally:
            for pending_task in (stop_wait, connection_lost_wait):
                if pending_task is not None and not pending_task.done():
                    pending_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await pending_task
            if conn is not None:
                with suppress(Exception):
                    if callback is not None:
                        await conn.remove_listener(ADMIN_DASHBOARD_CHANNEL, callback)
                    if termination_listener is not None:
                        conn.remove_termination_listener(termination_listener)
                with suppress(Exception):
                    await conn.close()

        if not _dashboard_listener_stop.is_set():
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(_dashboard_listener_stop.wait(), timeout=retry_delay)
            retry_delay = min(retry_delay * 2, 30.0)


async def _stop_dashboard_listener() -> None:
    global _dashboard_listener_task, _dashboard_listener_stop
    if _dashboard_listener_stop is not None:
        _dashboard_listener_stop.set()
    if _dashboard_listener_task is not None:
        _dashboard_listener_task.cancel()
        with suppress(asyncio.CancelledError):
            await _dashboard_listener_task
    _dashboard_listener_task = None
    _dashboard_listener_stop = None
    _dashboard_subscribers.clear()


def format_admin_datetime(value, *, date_only: bool = False, include_seconds: bool = False, fallback: str = "-") -> str:
    """Format database/API timestamps in Samara time for the admin panel.

    Most legacy PostgreSQL columns are ``TIMESTAMP WITHOUT TIME ZONE`` and
    store UTC wall-clock values.  Treat naive values as UTC before converting
    them; timezone-aware values (including CloudPayments timestamps) are
    converted directly.
    """
    if value is None:
        return fallback

    parsed = value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return fallback
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            # Keep an unexpected provider format visible rather than failing
            # the whole admin page.
            return text
    elif isinstance(value, date) and not isinstance(value, datetime):
        parsed = datetime.combine(value, time.min)

    if not isinstance(parsed, datetime):
        return str(value)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(ADMIN_TIMEZONE)

    if date_only:
        return parsed.strftime("%d.%m.%Y")
    if include_seconds:
        return parsed.strftime("%d.%m.%Y %H:%M:%S")
    return parsed.strftime("%d.%m.%Y %H:%M")


def dashboard_day_start_utc_naive() -> datetime:
    """Return today's Samara midnight in the UTC-naive format used by DB columns."""
    samara_now = datetime.now(timezone.utc).astimezone(ADMIN_TIMEZONE)
    samara_midnight = datetime.combine(
        samara_now.date(),
        time.min,
        tzinfo=ADMIN_TIMEZONE,
    )
    return samara_midnight.astimezone(timezone.utc).replace(tzinfo=None)


def dashboard_now_utc_naive() -> datetime:
    """Return the current instant in the UTC-naive format used by DB columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def format_dashboard_number(value) -> str:
    """Format dashboard counters like the reference UI (space-separated thousands)."""
    return f"{int(value or 0):,}".replace(",", " ")


def format_subgram_balance(value) -> str:
    """Format a SubGram balance for the compact dashboard card."""
    if value is None:
        return "—"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{amount:,.2f}".replace(",", " ").replace(".", ",")


def format_dashboard_amount(value) -> str:
    """Format a monetary dashboard value without exposing provider decimals."""
    if value is None:
        return "—"
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return "—"
    return f"{amount:,.2f}".replace(",", " ").replace(".", ",")


async def get_dashboard_subgram_balance() -> dict:
    """Return a cached SubGram balance snapshot for the dashboard.

    A cache hit is served without network or database work.  The next request
    after the TTL refreshes the value once under a lock, so concurrent admins
    do not create a burst of identical SubGram API calls.
    """
    now = monotonic_clock.monotonic()
    if now < _subgram_balance_cache["expires_at"]:
        return dict(_subgram_balance_cache)

    async with _subgram_balance_lock:
        now = monotonic_clock.monotonic()
        if now < _subgram_balance_cache["expires_at"]:
            return dict(_subgram_balance_cache)

        try:
            response = await database.get_subgram_balance()
            ok = response.get("status") == "ok"
            value = response.get("balance") if ok else None
            error = None if ok else str(response.get("message") or "SubGram недоступен")
        except Exception as exc:
            ok = False
            value = None
            error = str(exc)

        _subgram_balance_cache.update(
            {
                "expires_at": monotonic_clock.monotonic() + SUBGRAM_DASHBOARD_CACHE_TTL,
                "value": value,
                "ok": ok,
                "error": error,
            }
        )
        return dict(_subgram_balance_cache)


def dashboard_sparkline_svg(values, chart_id: str) -> str:
    """Render a tiny inline SVG chart for today's hourly increments."""
    points = [max(0, int(value or 0)) for value in (values or [])]
    if not points:
        points = [0]
    max_value = max(max(points), 1)
    height = 36
    coordinates = []
    denominator = max(len(points) - 1, 1)
    for index, value in enumerate(points):
        x = index * 100 / denominator
        y = height - (value / max_value) * (height - 4) - 2
        coordinates.append((x, y))
    line_path = "M " + " ".join(
        f"{x:.2f},{y:.2f}" if index == 0 else f"L {x:.2f},{y:.2f}"
        for index, (x, y) in enumerate(coordinates)
    )
    area_path = f"{line_path} L 100,{height} L 0,{height} Z"
    return (
        f'<svg class="stat-chart" id="{chart_id}" viewBox="0 0 100 {height}" '
        f'preserveAspectRatio="none" role="img" aria-label="Динамика за сегодня">'
        f'<path class="stat-chart-fill" id="{chart_id}-fill" d="{area_path}"></path>'
        f'<path class="stat-chart-line" id="{chart_id}-line" d="{line_path}"></path>'
        "</svg>"
    )


DASHBOARD_STATS_QUERY = """
WITH hours AS (
    SELECT generate_series(
        $1::timestamp,
        date_trunc('hour', $2::timestamp),
        interval '1 hour'
    ) AS hour
),
users_hourly AS (
    SELECT date_trunc('hour', u.registered_at) AS hour, COUNT(*) AS value
    FROM users u
    WHERE u.registered_at >= $1
      AND u.registered_at <= $2
      AND NOT EXISTS (
          SELECT 1 FROM blocked_users b
          WHERE b.telegram_id = u.telegram_id
      )
      AND NOT EXISTS (
          SELECT 1 FROM unsubscribed_users x
          WHERE x.telegram_id = u.telegram_id
      )
    GROUP BY 1
),
messages_hourly AS (
    SELECT date_trunc('hour', created_at) AS hour, COUNT(*) AS value
    FROM messages
    WHERE created_at >= $1 AND created_at <= $2
    GROUP BY 1
),
subscriptions_hourly AS (
    SELECT date_trunc('hour', created_at) AS hour, COUNT(*) AS value
    FROM subscriptions
    WHERE created_at >= $1
      AND created_at <= $2
      AND (
          (status = 'active' AND (end_date IS NULL OR end_date > NOW()))
          OR (
              status = 'cancelled' AND end_date IS NOT NULL AND end_date > NOW()
              AND EXISTS (
                  SELECT 1 FROM unsubscribed_users AS archived
                  WHERE archived.telegram_id = subscriptions.user_id
                    AND archived.reason = 'manual'
              )
          )
      )
    GROUP BY 1
),
payment_visitors_hourly AS (
    SELECT date_trunc('hour', first_visited_at) AS hour, COUNT(*) AS value
    FROM payment_link_visits
    WHERE first_visited_at >= $1 AND first_visited_at <= $2
    GROUP BY 1
),
webhook_candidates AS (
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
),
webhook_completed AS (
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
),
successful_payments AS (
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
)
SELECT
    (
        SELECT COUNT(*)
        FROM users
        WHERE NOT EXISTS (
            SELECT 1 FROM blocked_users
            WHERE blocked_users.telegram_id = users.telegram_id
        )
        AND NOT EXISTS (
            SELECT 1 FROM unsubscribed_users
            WHERE unsubscribed_users.telegram_id = users.telegram_id
        )
    ) AS total_users,
    (SELECT COUNT(*) FROM messages) AS total_messages,
    (
        SELECT COUNT(*)
        FROM subscriptions
        WHERE (
            status = 'active' AND (end_date IS NULL OR end_date > NOW())
        ) OR (
            status = 'cancelled' AND end_date IS NOT NULL AND end_date > NOW()
            AND EXISTS (
                SELECT 1 FROM unsubscribed_users AS archived
                WHERE archived.telegram_id = subscriptions.user_id
                  AND archived.reason = 'manual'
            )
        )
    ) AS total_subscriptions,
    (SELECT COUNT(*) FROM payment_link_visits) AS payment_visitors,
    (SELECT COALESCE(SUM(amount_rub), 0)::NUMERIC FROM successful_payments) AS total_successful_amount,
    (
        SELECT COUNT(*)
        FROM users
        WHERE registered_at >= $1
          AND registered_at <= $2
          AND NOT EXISTS (
              SELECT 1 FROM blocked_users
              WHERE blocked_users.telegram_id = users.telegram_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM unsubscribed_users
              WHERE unsubscribed_users.telegram_id = users.telegram_id
          )
    ) AS users_today,
    (SELECT COUNT(*) FROM messages WHERE created_at >= $1 AND created_at <= $2) AS messages_today,
    (
        SELECT COUNT(*)
        FROM subscriptions
        WHERE created_at >= $1
          AND created_at <= $2
          AND (
              (status = 'active' AND (end_date IS NULL OR end_date > NOW()))
              OR (
                  status = 'cancelled' AND end_date IS NOT NULL AND end_date > NOW()
                  AND EXISTS (
                      SELECT 1 FROM unsubscribed_users AS archived
                      WHERE archived.telegram_id = subscriptions.user_id
                        AND archived.reason = 'manual'
                  )
              )
          )
    ) AS subscriptions_today,
    (SELECT COUNT(*) FROM payment_link_visits WHERE first_visited_at >= $1 AND first_visited_at <= $2) AS payment_visitors_today,
    COALESCE((SELECT array_agg(COALESCE(u.value, 0)::bigint ORDER BY h.hour)
              FROM hours h LEFT JOIN users_hourly u USING (hour)), ARRAY[]::bigint[]) AS users_today_chart,
    COALESCE((SELECT array_agg(COALESCE(m.value, 0)::bigint ORDER BY h.hour)
              FROM hours h LEFT JOIN messages_hourly m USING (hour)), ARRAY[]::bigint[]) AS messages_today_chart,
    COALESCE((SELECT array_agg(COALESCE(s.value, 0)::bigint ORDER BY h.hour)
              FROM hours h LEFT JOIN subscriptions_hourly s USING (hour)), ARRAY[]::bigint[]) AS subscriptions_today_chart,
    COALESCE((SELECT array_agg(COALESCE(p.value, 0)::bigint ORDER BY h.hour)
              FROM hours h LEFT JOIN payment_visitors_hourly p USING (hour)), ARRAY[]::bigint[]) AS payment_visitors_today_chart,
    EXISTS (
        SELECT 1
        FROM service_heartbeats
        WHERE service = 'worker'
          AND heartbeat_at >= NOW() - INTERVAL '2 minutes'
    ) AS worker_alive
"""


SUBSCRIPTION_STATUS_META = {
    "active": {
        "label": "Активна",
        "class_name": "subscription-status-active",
    },
    "retry_low": {
        "label": "Повторная оплата",
        "class_name": "subscription-status-retry",
    },
    "pending": {
        "label": "Ожидает оплаты",
        "class_name": "subscription-status-pending",
    },
    "cancelled": {
        "label": "Отменена",
        "class_name": "subscription-status-cancelled",
    },
    "expired": {
        "label": "Истекла",
        "class_name": "subscription-status-expired",
    },
}


WEBHOOK_SENSITIVE_KEYS = {
    "token",
    "cardtoken",
    "cardnumber",
    "cardcryptogrampacket",
    "password",
}


def redact_webhook_payload(value):
    """Hide payment credentials before rendering a webhook in the panel."""
    if isinstance(value, dict):
        return {
            key: "••••••••" if str(key).replace("_", "").lower() in WEBHOOK_SENSITIVE_KEYS
            else redact_webhook_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_webhook_payload(item) for item in value]
    return value


def subscription_status_meta(status) -> tuple[str, str, str]:
    """Return a safe display label and CSS class for a subscription status."""
    raw_status = str(status or "unknown").strip().lower() or "unknown"
    meta = SUBSCRIPTION_STATUS_META.get(raw_status)
    if meta:
        return raw_status, meta["label"], meta["class_name"]
    return raw_status, raw_status.replace("_", " ").capitalize(), "subscription-status-unknown"


def subscription_status_badge(status) -> str:
    """Render a consistent subscription status badge for the admin UI."""
    raw_status, label, class_name = subscription_status_meta(status)
    return (
        f'<span class="subscription-status {class_name}" '
        f'title="Технический статус: {escape(raw_status)}">'
        f'{escape(label)}</span>'
    )


async def ensure_admin_indexes() -> None:
    """Create indexes used by admin list pages when they are missing.

    The migration is idempotent and tolerates optional tables/columns from
    older installations, so the panel can start while the schema is upgraded.
    """
    statements = (
        "CREATE INDEX IF NOT EXISTS idx_users_registered_at ON users (registered_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages (created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_messages_sender_id ON messages (sender_id)",
        "CREATE INDEX IF NOT EXISTS idx_messages_recipient_id ON messages (recipient_id)",
        "CREATE INDEX IF NOT EXISTS idx_subscriptions_status_created_at ON subscriptions (status, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_subscriptions_user_created_at ON subscriptions (user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_payment_events_status_created_at ON payment_events (status, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_payment_events_user_created_at ON payment_events (user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_webhook_events_status_transaction_id ON webhook_events (status, transaction_id)",
        "CREATE INDEX IF NOT EXISTS idx_payment_link_visits_last_visited_at ON payment_link_visits (last_visited_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_broadcast_jobs_status_created_at ON broadcast_jobs (status, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_blocked_users_blocked_at ON blocked_users (blocked_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_unsubscribed_users_unsubscribed_at ON unsubscribed_users (unsubscribed_at DESC)",
    )
    conn = await database.get_conn()
    try:
        async with database.schema_migration_lock(conn):
            for statement in statements:
                try:
                    await conn.execute(statement)
                except Exception as exc:
                    # Older installations may not have every optional table/column.
                    print(f"⚠️ Не удалось создать индекс админки: {exc}", flush=True)
    finally:
        await database.release_conn(conn)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _dashboard_listener_task, _dashboard_listener_stop
    print("🚀 Инициализация БД...", flush=True)
    await database.init_pool()
    await database.ensure_broadcast_schema()
    await database.ensure_payment_link_visits_schema()
    conn = await database.get_conn()
    try:
        async with database.schema_migration_lock(conn):
            await database.ensure_webhook_events_schema(conn)
            await database.ensure_payment_events_schema(conn)
            await database.ensure_payment_events_user_fk_protection(conn)
            await database.ensure_admin_dashboard_notifications(conn)
    finally:
        await database.release_conn(conn)
    await ensure_admin_indexes()
    print("✅ БД готова", flush=True)
    _dashboard_listener_stop = asyncio.Event()
    _dashboard_listener_task = asyncio.create_task(_dashboard_listener())
    try:
        yield
    finally:
        await _stop_dashboard_listener()
        await database.close_pool()
        print("🛑 Соединения с БД закрыты", flush=True)
app = FastAPI(title="Admin Panel для Telegram Бота", lifespan=lifespan)

FAVICON_SVG = '''
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" aria-label="Anonimno">
  <rect x="3" y="3" width="58" height="58" rx="17" fill="#1fb89f"/>
  <rect x="13" y="18" width="38" height="28" rx="9" fill="#071019" stroke="#a4ffe7" stroke-width="2.5"/>
  <path d="M32 18v-6m0 0-4 4m4-4 4 4" fill="none" stroke="#a4ffe7" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="24" cy="31" r="3" fill="#a4ffe7"/>
  <circle cx="40" cy="31" r="3" fill="#a4ffe7"/>
  <path d="M23 39h18" fill="none" stroke="#a4ffe7" stroke-width="2.5" stroke-linecap="round"/>
</svg>
'''

# Inline SVG keeps the brand mark crisp on every screen size and avoids an
# additional static asset request for the admin panel.  The dark robot face
# also remains legible in both the light and dark themes.
ROBOT_LOGO_SVG = '''
<svg class="robot-logo" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" role="img" aria-label="Anonimno" focusable="false">
  <rect x="1" y="1" width="46" height="46" rx="14" fill="#1fb89f"/>
  <rect x="9" y="14" width="30" height="23" rx="7" fill="#071019" stroke="#a4ffe7" stroke-width="2"/>
  <path d="M24 14V9m0 0-3 3m3-3 3 3" fill="none" stroke="#a4ffe7" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="18" cy="25" r="2.5" fill="#a4ffe7"/>
  <circle cx="30" cy="25" r="2.5" fill="#a4ffe7"/>
  <path d="M17 32h14" fill="none" stroke="#a4ffe7" stroke-width="2" stroke-linecap="round"/>
</svg>
'''

LOGIN_PAGE = '''
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="icon" type="image/svg+xml" href="/admin-favicon-mail.svg">
    <title>Вход в админ-панель</title>
    <script>
        (() => {
            try {
                const savedTheme = localStorage.getItem('anonimno-admin-theme');
                // The dashboard follows the dark visual language of the product
                // by default.  The existing theme switch still lets an operator
                // opt into the light palette and remembers that choice.
                document.documentElement.dataset.theme = savedTheme === 'dark' || savedTheme === 'light' ? savedTheme : 'dark';
            } catch (_) {
                document.documentElement.dataset.theme = 'dark';
            }
        })();
    </script>
    <style>
        :root {
            color-scheme: light;
            --ink: #183332; --muted: #71817f; --line: #dce7e4; --accent: #118b7e; --accent-dark: #0d7168;
            --primary-bg: #118b7e; --primary-hover: #0d7168;
            --canvas: #f2f6f5; --surface: rgba(255,255,255,.96); --field: #fbfdfc; --field-focus: #ffffff;
            --label: #435b59; --error: #b54747; --login-border: rgba(24,51,50,.09); --login-shadow: rgba(26,63,60,.12);
        }
        :root[data-theme="dark"] {
            color-scheme: dark;
            --ink: #f2f7fb; --muted: #8ea2b5; --line: #213347; --accent: #2bd2b2; --accent-dark: #5de5c9;
            --primary-bg: #20b99e; --primary-hover: #27c9ad;
            --canvas: #071019; --surface: rgba(16,27,39,.97); --field: #0c1825; --field-focus: #122232;
            --label: #b7c8d5; --error: #ff727b; --login-border: rgba(93,229,201,.16); --login-shadow: rgba(0,0,0,.42);
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--canvas); color: var(--ink); display: grid; place-items: center; min-height: 100vh; padding: 24px; }
        body::before { content: ""; position: fixed; inset: 0; pointer-events: none; background: radial-gradient(circle at 18% 12%, rgba(17,139,126,.10), transparent 32%), radial-gradient(circle at 84% 82%, rgba(44,109,105,.08), transparent 28%); }
        .login-box {
            position: relative; z-index: 1; background: var(--surface); border-radius: 22px; padding: 38px; max-width: 430px; width: 100%; border: 1px solid var(--login-border); box-shadow: 0 24px 70px var(--login-shadow);
        }
        .brand-mark { width: 58px; height: 58px; margin: 0 auto 20px; display: grid; place-items: center; border-radius: 17px; color: white; background: var(--primary-bg); box-shadow: 0 10px 24px rgba(17,139,126,.22); }
        .brand-mark .robot-logo { width: 58px; height: 58px; display: block; }
        .login-box h1 { color: var(--ink); font-size: 27px; text-align: center; margin-bottom: 8px; letter-spacing: -.02em; }
        .login-box .subtitle { color: var(--muted); text-align: center; margin-bottom: 30px; font-size: 14px; line-height: 1.5; }
        .login-box label { display: block; color: var(--label); margin-bottom: 7px; font-weight: 650; font-size: 13px; }
        .login-box input[type="text"],
        .login-box input[type="password"] {
            width: 100%; padding: 12px 14px; border-radius: 11px; border: 1px solid var(--line); background: var(--field); color: var(--ink); font-size: 15px; transition: .2s ease; margin-bottom: 17px;
        }
        .login-box input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 4px rgba(17,139,126,.10); background: var(--field-focus); }
        .login-box button {
            width: 100%; padding: 13px; background: var(--primary-bg); color: white; border: none; border-radius: 11px; font-size: 15px; font-weight: 750; cursor: pointer; transition: .2s ease;
        }
        .login-box button:hover { background: var(--primary-hover); transform: translateY(-1px); box-shadow: 0 10px 24px rgba(17,139,126,.18); }
        .login-box .error { color: var(--error); text-align: center; margin-top: 14px; font-size: 13px; }
        @media (max-width: 480px) { .login-box { padding: 30px 22px; } }
    </style>
</head>
<body>
    <div class="login-box">
        <div class="brand-mark">ROBOT_LOGO_PLACEHOLDER</div>
        <h1>Панель управления</h1>
        <p class="subtitle">Войдите, чтобы управлять ботом и отслеживать ключевые показатели.</p>
        <form action="/admin/login" method="post">
            <label for="username">Логин</label>
            <input id="username" type="text" name="username" placeholder="Введите логин" autocomplete="username" required>
            <label for="password">Пароль</label>
            <input id="password" type="password" name="password" placeholder="Введите пароль" autocomplete="current-password" required>
            <button type="submit">Войти</button>
        </form>
        <div id="error" class="error"></div>
    </div>
</body>
</html>
'''

MENU = '''
<div class="sidebar-brand">
    <div class="brand-symbol">ROBOT_LOGO_PLACEHOLDER</div>
    <div><strong>Anonimno</strong><span>Панель управления</span></div>
</div>
<nav class="sidebar-nav" aria-label="Основная навигация">
    <div class="nav-section">
        <div class="nav-section-title">Основное</div>
        <a class="nav-link" data-nav href="/"><span class="nav-icon">⌂</span><span>Обзор</span></a>
        <a class="nav-link" data-nav href="/users"><span class="nav-icon">◎</span><span>Пользователи</span></a>
        <a class="nav-link" data-nav href="/messages"><span class="nav-icon">≋</span><span>Сообщения</span></a>
    </div>
    <div class="nav-section">
        <div class="nav-section-title">Монетизация</div>
        <a class="nav-link" data-nav href="/subscriptions"><span class="nav-icon">◇</span><span>Подписки</span></a>
        <a class="nav-link" data-nav href="/payments"><span class="nav-icon">₽</span><span>Платежи</span></a>
        <a class="nav-link" data-nav href="/payment-visits"><span class="nav-icon">↗</span><span>Переходы к оплате</span></a>
        <a class="nav-link" data-nav href="/subgram"><span class="nav-icon">◉</span><span>SubGram</span></a>
    </div>
    <div class="nav-section">
        <div class="nav-section-title">Коммуникации</div>
        <a class="nav-link" data-nav href="/broadcast"><span class="nav-icon">✦</span><span>Рассылки</span></a>
        <a class="nav-link" data-nav href="/blocked"><span class="nav-icon">⊘</span><span>Чёрный список</span></a>
        <a class="nav-link" data-nav href="/unsubscribed"><span class="nav-icon">○</span><span>Отписавшиеся</span></a>
    </div>
    <div class="nav-section">
        <div class="nav-section-title">Система</div>
        <a class="nav-link" data-nav href="/monitoring"><span class="nav-icon">⌁</span><span>Мониторинг</span></a>
        <a class="nav-link" data-nav href="/webhooks"><span class="nav-icon">⇄</span><span>Вебхуки</span></a>
        <a class="nav-link" data-nav href="/settings"><span class="nav-icon">⚙</span><span>Настройки</span></a>
    </div>
</nav>
<div class="sidebar-footer">
    <a class="nav-link logout-link" href="/admin/logout"><span class="nav-icon">↪</span><span>Выйти</span></a>
</div>
'''

LOGIN_PAGE = LOGIN_PAGE.replace('ROBOT_LOGO_PLACEHOLDER', ROBOT_LOGO_SVG)
MENU = MENU.replace('ROBOT_LOGO_PLACEHOLDER', ROBOT_LOGO_SVG)

BASE_HTML = '''
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="icon" type="image/svg+xml" href="/admin-favicon-mail.svg">
    <title>Anonimno — панель управления</title>
    <script>
        (() => {
            try {
                const savedTheme = localStorage.getItem('anonimno-admin-theme');
                const systemTheme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
                document.documentElement.dataset.theme = savedTheme === 'dark' || savedTheme === 'light' ? savedTheme : systemTheme;
            } catch (_) {
                document.documentElement.dataset.theme = 'light';
            }
        })();
    </script>
    <style>
        :root {
            color-scheme: light;
            --canvas: #f3f7f6; --surface: #ffffff; --surface-soft: #f8fbfa; --ink: #183332; --muted: #738380;
            --line: #dce7e4; --accent: #118b7e; --accent-dark: #0d7168; --accent-soft: #e8f5f2;
            --primary-bg: #118b7e; --primary-hover: #0d7168;
            --danger: #c34f55; --warning: #bb792d; --success: #2f8b66; --sidebar-width: 264px;
            --sidebar: #fbfdfc; --topbar: rgba(243,247,246,.88); --topbar-line: rgba(220,231,228,.9);
            --nav-title: #94a19f; --nav-text: #526765; --nav-hover: #f0f5f3; --logout: #9b4b4f;
            --line-strong: #bdcfcb; --row-line: #edf2f0; --row-hover: #fbfdfc; --field: #fbfdfc;
            --table-head: #f8fbfa; --table-text: #324947; --label: #465e5b; --button-surface: #ffffff; --button-hover: #f7faf9;
            --neutral-badge: #f1f4f3; --neutral-badge-text: #5d706d; --success-soft: #eef9f2; --success-line: #b9dfc9; --danger-soft: #fff5f5; --danger-line: #dbaeb1;
            --warning-soft: #fffaf3; --warning-hover: #fff5e8; --warning-line: #ead6b9;
            --card-shadow: 0 7px 24px rgba(34,72,68,.035); --card-shadow-hover: 0 10px 28px rgba(34,72,68,.06);
        }
        :root[data-theme="dark"] {
            color-scheme: dark;
            --canvas: #071019; --surface: #101b27; --surface-soft: #122232; --ink: #f2f7fb; --muted: #8ea2b5;
            --line: #213347; --accent: #2bd2b2; --accent-dark: #5de5c9; --accent-soft: #123c3d;
            --primary-bg: #20b99e; --primary-hover: #27c9ad;
            --danger: #ff727b; --warning: #ff9b3e; --success: #45d7b1;
            --sidebar: #08131f; --topbar: rgba(7,16,25,.90); --topbar-line: rgba(33,51,71,.9);
            --nav-title: #71879c; --nav-text: #c0cfdb; --nav-hover: #122536; --logout: #ff727b;
            --line-strong: #31506a; --row-line: #1d3043; --row-hover: #132638; --field: #0c1825;
            --table-head: #0c1825; --table-text: #d3dfe8; --label: #b7c8d5; --button-surface: #132333; --button-hover: #192d40;
            --neutral-badge: #1b2a3a; --neutral-badge-text: #b5c4d0; --success-soft: #103a38; --success-line: #1e7d6e; --danger-soft: #3a2029; --danger-line: #8b3c4c;
            --warning-soft: #3a2a1b; --warning-hover: #4a321d; --warning-line: #996329;
            --card-shadow: 0 10px 30px rgba(0,0,0,.24); --card-shadow-hover: 0 16px 38px rgba(0,0,0,.34);
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        html { background: var(--canvas); }
        body { font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--canvas); color: var(--ink); line-height: 1.45; }
        a { color: var(--accent); }
        .sidebar { position: fixed; inset: 0 auto 0 0; z-index: 40; width: var(--sidebar-width); display: flex; flex-direction: column; background: var(--sidebar); border-right: 1px solid var(--line); }
        .sidebar-brand { height: 76px; display: flex; align-items: center; gap: 12px; padding: 0 22px; border-bottom: 1px solid var(--line); }
        .brand-symbol { width: 38px; height: 38px; display: grid; place-items: center; border-radius: 11px; color: white; background: var(--primary-bg); font-weight: 800; box-shadow: 0 8px 18px rgba(17,139,126,.18); }
        .brand-symbol .robot-logo { width: 38px; height: 38px; display: block; }
        .sidebar-brand strong { display: block; font-size: 15px; letter-spacing: -.01em; }
        .sidebar-brand span { display: block; color: var(--muted); font-size: 11px; margin-top: 1px; }
        .sidebar-nav { flex: 1; overflow-y: auto; padding: 18px 14px; }
        .nav-section + .nav-section { margin-top: 19px; }
        .nav-section-title { padding: 0 11px 7px; color: var(--nav-title); font-size: 10px; font-weight: 800; text-transform: uppercase; letter-spacing: .11em; }
        .nav-link { display: flex; align-items: center; gap: 11px; min-height: 40px; padding: 8px 11px; border-radius: 10px; color: var(--nav-text); text-decoration: none; font-size: 13px; font-weight: 620; transition: .16s ease; }
        .nav-link span { color: inherit !important; text-shadow: none !important; }
        .nav-link:hover { color: var(--ink); background: var(--nav-hover); }
        .nav-link.active { color: var(--accent-dark); background: var(--accent-soft); }
        .nav-icon { width: 22px; text-align: center; font-size: 16px; font-weight: 500; }
        .sidebar-footer { padding: 14px; border-top: 1px solid var(--line); }
        .logout-link { color: var(--logout); }
        .main-shell { min-height: 100vh; margin-left: var(--sidebar-width); }
        .topbar { position: sticky; top: 0; z-index: 30; height: 76px; display: flex; align-items: center; justify-content: space-between; padding: 0 32px; background: var(--topbar); border-bottom: 1px solid var(--topbar-line); backdrop-filter: blur(14px); }
        .topbar-context { display: flex; align-items: center; gap: 10px; color: var(--muted); font-size: 13px; }
        .samara-clock { color: var(--ink); font-size: 13px; font-variant-numeric: tabular-nums; white-space: nowrap; }
        .samara-clock-label { color: var(--muted); margin-right: 6px; }
        .topbar-actions { display: flex; align-items: center; gap: 16px; margin-left: auto; }
        .live-dot { width: 8px; height: 8px; border-radius: 50%; background: #3ba77b; box-shadow: 0 0 0 4px rgba(59,167,123,.12); }
        .mobile-menu-button { display: none; width: 40px; height: 40px; padding: 0; border: 1px solid var(--line); border-radius: 10px; background: var(--button-surface); color: var(--ink); }
        .theme-toggle { min-height: 38px; padding: 8px 12px; border-color: var(--line); background: var(--button-surface); color: var(--ink); box-shadow: var(--card-shadow); }
        .theme-toggle:hover { border-color: var(--line-strong); background: var(--button-hover); color: var(--ink); }
        .theme-toggle-icon { width: 18px; font-size: 16px; line-height: 1; }
        .theme-toggle-label { font-size: 12px; }
        .sidebar-overlay { display: none; position: fixed; inset: 0; z-index: 35; background: rgba(10,35,33,.32); }
        .container { max-width: 1480px; margin: 0 auto; padding: 30px 32px 48px; }
        .page-title { font-size: 25px; font-weight: 760; margin-bottom: 22px; color: var(--ink); letter-spacing: -.025em; }
        .page-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; margin-bottom: 24px; }
        .page-heading h1 { font-size: 27px; line-height: 1.2; letter-spacing: -.03em; }
        .page-heading p { color: var(--muted); font-size: 13px; margin-top: 7px; }
        .page-actions { display: flex; flex-wrap: wrap; gap: 8px; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 14px; margin-bottom: 22px; }
        .stat-card { min-height: 128px; background: var(--surface); padding: 19px; border-radius: 15px; text-align: left; border: 1px solid var(--line); box-shadow: var(--card-shadow); }
        .stat-card:hover { border-color: var(--line-strong); box-shadow: var(--card-shadow-hover); }
        .stat-number { font-size: 29px; line-height: 1.1; font-weight: 770; color: var(--ink); letter-spacing: -.025em; }
        .stat-label { font-size: 12px; color: var(--muted); margin-top: 8px; }
        .stat-today { margin-top: 10px; color: var(--muted); font-size: 12px; font-weight: 650; }
        .stat-chart { display: block; width: 100%; height: 38px; margin-top: 13px; overflow: visible; }
        .stat-chart-line { fill: none; stroke: var(--card-accent, var(--accent)); stroke-width: 2.1; stroke-linecap: round; stroke-linejoin: round; vector-effect: non-scaling-stroke; }
        .stat-chart-fill { fill: var(--card-accent-soft, var(--accent-soft)); opacity: .72; }
        .stat-card .icon { width: 30px; height: 30px; margin-bottom: 17px; display: grid; place-items: center; border-radius: 9px; background: var(--accent-soft); color: var(--accent-dark); font-size: 14px; }
        :root[data-theme="dark"] .stat-card:nth-child(1) { --card-accent: #42e0b4; --card-accent-soft: rgba(42, 205, 167, .14); --card-tint: rgba(24, 119, 108, .12); }
        :root[data-theme="dark"] .stat-card:nth-child(2) { --card-accent: #5da3ff; --card-accent-soft: rgba(72, 142, 255, .14); --card-tint: rgba(31, 78, 147, .12); }
        :root[data-theme="dark"] .stat-card:nth-child(3) { --card-accent: #c17aff; --card-accent-soft: rgba(169, 91, 255, .15); --card-tint: rgba(91, 47, 140, .12); }
        :root[data-theme="dark"] .stat-card:nth-child(4) { --card-accent: #ff9b3e; --card-accent-soft: rgba(255, 145, 42, .15); --card-tint: rgba(143, 78, 28, .12); }
        :root[data-theme="dark"] .stat-card { background: linear-gradient(145deg, var(--surface) 0%, var(--card-tint, transparent) 100%); }
        :root[data-theme="dark"] .stat-card .icon { background: var(--card-accent-soft, var(--accent-soft)); color: var(--card-accent, var(--accent-dark)); }
        :root[data-theme="dark"] .stat-card:hover { border-color: color-mix(in srgb, var(--card-accent, var(--accent)) 48%, var(--line)); }
        .quick-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)) minmax(280px, .95fr); gap: 14px; align-items: start; }
        .system-status-grid { display: grid; gap: 9px; margin-top: 18px; }
        .system-status-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 11px 12px; border: 1px solid var(--line); border-radius: 11px; background: var(--surface-soft); font-size: 13px; }
        .status-pill { display: inline-flex; align-items: center; gap: 6px; font-weight: 750; font-size: 12px; }
        .status-pill::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
        .status-ok { color: var(--success); }
        .status-degraded { color: var(--warning); }
        .status-down { color: var(--danger); }
        .panel { background: var(--surface); border: 1px solid var(--line); border-radius: 15px; padding: 22px; box-shadow: var(--card-shadow); }
        .panel-title { font-size: 15px; font-weight: 740; margin-bottom: 6px; }
        .panel-copy { color: var(--muted); font-size: 13px; }
        .quick-links { display: grid; gap: 8px; margin-top: 18px; }
        .quick-link { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 13px; color: var(--ink); text-decoration: none; background: var(--surface-soft); border: 1px solid var(--line); border-radius: 11px; font-size: 13px; font-weight: 650; }
        .quick-link:hover { border-color: var(--line-strong); background: var(--accent-soft); }
        .subgram-dashboard-panel { align-self: start; }
        .subgram-dashboard-balance { margin-top: 5px; color: var(--ink); font-size: 30px; line-height: 1.1; font-weight: 780; letter-spacing: -.03em; }
        .subgram-dashboard-panel .status-pill { margin-top: 11px; }
        .subgram-dashboard-panel .quick-link { transition: .16s ease; }
        .subgram-dashboard-panel button.quick-link { font-family: inherit; text-align: left; }
        .subgram-dashboard-panel button.quick-link:disabled { opacity: .6; cursor: wait; }
        .subgram-stat-card { cursor: pointer; }
        .charges-stat-card { --card-accent: #5da3ff; --card-accent-soft: rgba(72, 142, 255, .14); --card-tint: rgba(31, 78, 147, .12); }
        .quick-grid > .panel { grid-column: 3; }
        .subgram-stat-card:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
        .table-container { background: var(--surface); border-radius: 15px; overflow-x: auto; border: 1px solid var(--line); box-shadow: var(--card-shadow); }
        table { width: 100%; min-width: 720px; border-collapse: collapse; font-size: 13px; }
        th { background: var(--table-head); color: var(--muted); font-weight: 750; font-size: 10px; text-transform: uppercase; letter-spacing: .07em; padding: 12px 14px; text-align: left; border-bottom: 1px solid var(--line); }
        td { padding: 12px 14px; border-bottom: 1px solid var(--row-line); font-size: 13px; color: var(--table-text); word-break: break-word; }
        tr:last-child td { border-bottom: none; }
        tr:hover td { background: var(--row-hover); }
        .badge { display: inline-block; padding: 4px 9px; border-radius: 999px; font-size: 10px; font-weight: 750; border: 1px solid transparent; white-space: nowrap; }
        .badge-text { background: var(--accent-soft); color: var(--accent-dark); border-color: var(--line); }
        .badge-photo, .badge-video, .badge-voice, .badge-sticker, .badge-video_note { background: var(--neutral-badge); color: var(--neutral-badge-text); border-color: var(--line); }
        .subscription-status { display: inline-flex; align-items: center; gap: 7px; min-height: 28px; padding: 5px 10px; border: 1px solid transparent; border-radius: 999px; font-size: 11px; font-weight: 760; line-height: 1; white-space: nowrap; }
        .subscription-status::before { content: ""; width: 7px; height: 7px; flex: 0 0 7px; border-radius: 50%; background: currentColor; }
        .subscription-status-active { color: var(--success); background: var(--success-soft); border-color: var(--success-line); }
        .subscription-status-retry { color: var(--warning); background: var(--warning-soft); border-color: var(--warning-line); }
        .subscription-status-pending { color: var(--muted); background: var(--neutral-badge); border-color: var(--line); }
        .subscription-status-cancelled, .subscription-status-expired { color: var(--danger); background: var(--danger-soft); border-color: var(--danger-line); }
        .subscription-status-unknown { color: var(--muted); background: var(--neutral-badge); border-color: var(--line); }
        .form-container { background: var(--surface); padding: 22px; border-radius: 15px; border: 1px solid var(--line); box-shadow: var(--card-shadow); }
        label { display: block; margin-bottom: 7px; color: var(--label); font-weight: 680; font-size: 13px; }
        textarea, input[type="text"] {
            width: 100%; padding: 11px 13px; border-radius: 11px; border: 1px solid var(--line); background: var(--field); color: var(--ink); font-size: 14px; font-family: inherit; transition: .18s ease;
        }
        textarea { min-height: 132px; resize: vertical; line-height: 1.55; }
        textarea:focus, input[type="text"]:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 4px rgba(17,139,126,.09); background: var(--surface); }
        button, .button { display: inline-flex; align-items: center; justify-content: center; gap: 7px; min-height: 39px; padding: 9px 15px; border: 1px solid var(--primary-bg); border-radius: 10px; background: var(--primary-bg); color: white; text-decoration: none; font-weight: 700; font-size: 13px; cursor: pointer; transition: .17s ease; width: auto; }
        button:hover, .button:hover { background: var(--primary-hover); border-color: var(--primary-hover); transform: translateY(-1px); }
        button:disabled { opacity: .55; cursor: wait; transform: none; }
        .button-secondary { color: var(--table-text); background: var(--button-surface); border-color: var(--line); }
        .button-secondary:hover { color: var(--ink); background: var(--button-hover); border-color: var(--line-strong); }
        .button-danger { color: var(--danger); background: var(--button-surface); border-color: var(--danger-line); }
        .button-danger:hover { color: var(--danger); background: var(--danger-soft); border-color: var(--danger-line); }
        .button-warning { color: var(--warning); background: var(--warning-soft); border-color: var(--warning-line); }
        .button-warning:hover { color: var(--warning); background: var(--warning-hover); border-color: var(--warning-line); }
        .mt-20 { margin-top: 15px; }
        .mb-20 { margin-bottom: 15px; }
        .empty { text-align: center; padding: 46px 18px; color: var(--muted); font-size: 13px; background: var(--surface); border: 1px dashed var(--line-strong); border-radius: 15px; }
        .result-box { background: var(--surface); padding: 22px; border-radius: 15px; border: 1px solid var(--line) !important; box-shadow: var(--card-shadow); }
        .result-box h2 { color: var(--ink) !important; font-size: 16px; margin-bottom: 18px; }
        .result-stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 15px; }
        .result-stats > div { padding: 13px; text-align: left; background: var(--surface-soft); border: 1px solid var(--line); border-radius: 11px; }
        .result-stats .number { font-size: 24px; font-weight: 760; color: var(--ink) !important; }
        .result-stats .label { color: var(--muted); font-size: 10px; margin-top: 5px; }
        .result-message { background: var(--surface-soft); padding: 12px; border-radius: 10px; margin-bottom: 15px; border: 1px solid var(--line); word-break: break-word; font-size: 13px; }
        .glow-button { display: inline-flex; align-items: center; justify-content: center; padding: 9px 15px; min-height: 39px; border: 1px solid var(--line) !important; border-radius: 10px; color: var(--table-text) !important; background: var(--button-surface); text-decoration: none; transition: .17s ease; text-align: center; font-size: 13px; font-weight: 700; width: auto; }
        .glow-button:hover { color: var(--ink) !important; background: var(--button-hover); border-color: var(--line-strong) !important; transform: translateY(-1px); }
        .progress-bar { width: 100%; height: 9px; background: var(--line); border-radius: 999px; overflow: hidden; margin: 14px 0 9px; }
        .progress-fill { height: 100%; background: var(--accent); transition: width .5s ease; border-radius: 999px; width: 0%; }
        .progress-text { color: var(--muted) !important; }
        .status-running { color: var(--accent); }
        .status-paused { color: var(--warning); }
        .status-completed { color: var(--success); }
        .status-pending { color: var(--muted); }
        .status-failed, .status-cancelled { color: var(--danger); }
        .detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 20px; }
        .detail-item { display: flex; justify-content: space-between; padding: 7px 0; border-bottom: 1px solid var(--row-line); }
        .detail-item .label { color: var(--muted); }
        .detail-item .value { color: var(--ink); font-weight: 650; }
        .updating { opacity: 0.5; transition: opacity 0.3s; }
        @media (max-width: 980px) {
            .sidebar { transform: translateX(-102%); transition: transform .22s ease; box-shadow: 16px 0 40px rgba(20,55,52,.13); }
            body.sidebar-open .sidebar { transform: translateX(0); }
            body.sidebar-open .sidebar-overlay { display: block; }
            .main-shell { margin-left: 0; }
            .mobile-menu-button { display: inline-flex; align-items: center; justify-content: center; }
            .topbar { height: 68px; padding: 0 20px; }
            .container { padding: 24px 20px 42px; }
            .quick-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .quick-grid > .panel { grid-column: 1 / -1; }
        }
        @media (max-width: 700px) {
            .page-heading { flex-direction: column; }
            .page-heading h1, .page-title { font-size: 22px; }
            .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
            .stat-card { min-height: 110px; padding: 15px; }
            .stat-number { font-size: 24px; }
            .stat-card .icon { margin-bottom: 12px; }
            .quick-grid { grid-template-columns: 1fr; }
            .quick-grid > .panel { grid-column: auto; grid-row: auto; }
            .result-stats { grid-template-columns: repeat(2, 1fr); }
            .form-container, .result-box, .panel { padding: 17px; }
            .detail-grid { grid-template-columns: 1fr; }
            .topbar-context span:last-child { display: none; }
            .samara-clock-label { display: none; }
            .topbar-actions { gap: 10px; }
            .theme-toggle-label { display: none; }
            .theme-toggle { width: 40px; padding: 0; }
        }
        @media (max-width: 430px) {
            .container { padding: 20px 14px 36px; }
            .stats { grid-template-columns: 1fr; }
            .stat-card { min-height: 96px; }
            .page-actions, .page-actions .button, .page-actions .glow-button { width: 100%; }
        }
    </style>
</head>
<body>
    <div class="sidebar-overlay" id="sidebar-overlay"></div>
    <aside class="sidebar" id="sidebar">MENU_PLACEHOLDER</aside>
    <main class="main-shell">
        <header class="topbar">
            <button class="mobile-menu-button" id="mobile-menu-button" type="button" aria-label="Открыть меню">☰</button>
            <div class="topbar-actions">
                <div class="topbar-context"><span class="live-dot"></span><span>Система работает</span></div>
                <div class="samara-clock" id="samara-clock" aria-label="Текущее Самарское время">
                    <span class="samara-clock-label">Самарское время</span><span id="samara-clock-value">--:--:--</span>
                </div>
                <button class="theme-toggle" id="theme-toggle" type="button" aria-pressed="false">
                    <span class="theme-toggle-icon" id="theme-toggle-icon" aria-hidden="true">☾</span>
                    <span class="theme-toggle-label" id="theme-toggle-label">Тёмная тема</span>
                </button>
            </div>
        </header>
        <div class="container"><div id="content">CONTENT_PLACEHOLDER</div></div>
    </main>
    <script>
        (() => {
            const path = window.location.pathname;
            document.querySelectorAll('[data-nav]').forEach((link) => {
                const target = link.getAttribute('href');
                const active = target === '/' ? path === '/' : path === target || path.startsWith(target + '/');
                link.classList.toggle('active', active);
                if (active) link.setAttribute('aria-current', 'page');
            });
            const openButton = document.getElementById('mobile-menu-button');
            const overlay = document.getElementById('sidebar-overlay');
            const closeSidebar = () => document.body.classList.remove('sidebar-open');
            openButton?.addEventListener('click', () => document.body.classList.toggle('sidebar-open'));
            overlay?.addEventListener('click', closeSidebar);
            document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeSidebar(); });

            const themeStorageKey = 'anonimno-admin-theme';
            const themeButton = document.getElementById('theme-toggle');
            const themeIcon = document.getElementById('theme-toggle-icon');
            const themeLabel = document.getElementById('theme-toggle-label');
            const renderThemeButton = () => {
                const dark = document.documentElement.dataset.theme === 'dark';
                themeIcon.textContent = dark ? '☀' : '☾';
                themeLabel.textContent = dark ? 'Светлая тема' : 'Тёмная тема';
                themeButton.setAttribute('aria-label', dark ? 'Включить светлую тему' : 'Включить тёмную тему');
                themeButton.setAttribute('title', dark ? 'Включить светлую тему' : 'Включить тёмную тему');
                themeButton.setAttribute('aria-pressed', dark ? 'true' : 'false');
            };
            themeButton?.addEventListener('click', () => {
                const nextTheme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
                document.documentElement.dataset.theme = nextTheme;
                try { localStorage.setItem(themeStorageKey, nextTheme); } catch (_) {}
                renderThemeButton();
            });
            renderThemeButton();

            const clock = document.getElementById('samara-clock-value');
            const updateSamaraClock = () => {
                if (!clock) return;
                clock.textContent = new Intl.DateTimeFormat('ru-RU', {
                    timeZone: 'Europe/Samara',
                    hour: '2-digit',
                    minute: '2-digit',
                    second: '2-digit',
                    hour12: false,
                }).format(new Date());
            };
            updateSamaraClock();
            window.setInterval(updateSamaraClock, 1000);
        })();
    </script>
    </div>
</body>
</html>
'''


def verify_password(username: str, password: str) -> bool:
    clean_username = username.replace('@', '').strip().lower()
    for admin_username, admin_password in ADMINS.items():
        if admin_username.lower() == clean_username:
            return admin_password == password
    return False

def create_session(username: str) -> str:
    token = str(uuid.uuid4())
    sessions[token] = {
        "username": username,
        "created_at": now_msk(),
        "expires_at": now_msk() + timedelta(hours=24)
    }
    return token

def get_session_user(token: str) -> dict:
    if token not in sessions:
        return None
    session = sessions[token]
    if session["expires_at"] < now_msk():
        del sessions[token]
        return None
    return session

sessions = {}

async def auth_middleware(request: Request, call_next):
    if request.url.path in {
        "/admin/login", "/favicon.svg", "/favicon.ico",
        "/admin-favicon-mail.svg", "/admin-favicon-mail.ico",
    }:
        return await call_next(request)
    
    session_token = request.cookies.get("admin_session")
    if not session_token:
        return RedirectResponse(url="/admin/login", status_code=303)
    
    session_data = get_session_user(session_token)
    if not session_data:
        response = RedirectResponse(url="/admin/login", status_code=303)
        response.delete_cookie("admin_session")
        return response
    
    request.state.admin_username = session_data["username"]
    return await call_next(request)

app.middleware("http")(auth_middleware)


@app.get("/favicon.svg", include_in_schema=False)
@app.get("/favicon.ico", include_in_schema=False)
@app.get("/admin-favicon-mail.svg", include_in_schema=False)
@app.get("/admin-favicon-mail.ico", include_in_schema=False)
async def favicon():
    return Response(
        content=FAVICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/admin/login", response_class=HTMLResponse)
async def login_page():
    return HTMLResponse(content=LOGIN_PAGE)

@app.post("/admin/login")
async def login(request: Request):
    form = await request.form()
    username = form.get("username")
    password = form.get("password")
    
    if not username or not password:
        return HTMLResponse(content=LOGIN_PAGE.replace(
            '<div id="error" class="error"></div>',
            '<div id="error" class="error">❌ Заполните все поля</div>'
        ))
    
    if not verify_password(username, password):
        return HTMLResponse(content=LOGIN_PAGE.replace(
            '<div id="error" class="error"></div>',
            '<div id="error" class="error">❌ Неверный ник или пароль</div>'
        ))
    
    token = create_session(username)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key="admin_session",
        value=token,
        httponly=True,
        max_age=86400,
        # SSH tunnels commonly expose the local endpoint over HTTP. Keep the
        # cookie Secure when the panel is behind HTTPS, but allow the tunnel.
        secure=request.url.scheme == "https",
        samesite="lax"
    )
    return response

@app.get("/admin/logout")
async def logout():
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie("admin_session")
    return response


@app.get("/health")
async def health_check():
    """Return a lightweight status suitable for monitoring services."""
    try:
        conn = await database.get_conn()
        try:
            await conn.fetchval("SELECT 1")
            worker_alive = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM service_heartbeats
                    WHERE service = 'worker'
                      AND heartbeat_at >= NOW() - INTERVAL '2 minutes'
                )
                """
            )
        finally:
            await database.release_conn(conn)
    except Exception as exc:
        print(f"❌ Health check: {exc}", flush=True)
        return JSONResponse(
            status_code=503,
            content={"status": "down", "database": "error", "worker": "unknown"},
        )

    status = "ok" if worker_alive else "degraded"
    return JSONResponse(
        status_code=200 if worker_alive else 503,
        content={
            "status": status,
            "database": "ok",
            "worker": "ok" if worker_alive else "stale",
        },
    )


@app.get("/monitoring/data")
async def monitoring_data():
    """Return a fresh server metrics snapshot for the monitoring page."""
    try:
        # Keep the short CPU sampling interval off the event loop. Memory and
        # disk values are instantaneous and can be read directly.
        cpu = await asyncio.to_thread(psutil.cpu_percent, interval=0.2)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        return JSONResponse(
            content={
                "ok": True,
                "cpu": round(float(cpu), 1),
                "memory": round(float(memory.percent), 1),
                "disk": round(float(disk.percent), 1),
                "updated_at": f"{format_admin_datetime(datetime.now(ADMIN_TIMEZONE), include_seconds=True)} Самара",
            },
            headers={"Cache-Control": "no-store, max-age=0"},
        )
    except Exception as exc:
        print(f"❌ Monitoring metrics: {exc}", flush=True)
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": "Не удалось получить показатели сервера"},
            headers={"Cache-Control": "no-store, max-age=0"},
        )


@app.get("/dashboard/data")
async def dashboard_data(request: Request):
    """Return one fresh dashboard snapshot after an SSE invalidation."""
    if request.query_params.get("refresh") == "1":
        _subgram_balance_cache["expires_at"] = 0.0
    try:
        conn = await database.get_conn()
        try:
            stats = await conn.fetchrow(
                DASHBOARD_STATS_QUERY,
                dashboard_day_start_utc_naive(),
                dashboard_now_utc_naive(),
            )
        finally:
            await database.release_conn(conn)

        subgram_snapshot = await get_dashboard_subgram_balance()

        return JSONResponse(
            content={
                "ok": True,
                "total_users": int(stats["total_users"] or 0),
                "users_today": int(stats["users_today"] or 0),
                "total_messages": int(stats["total_messages"] or 0),
                "messages_today": int(stats["messages_today"] or 0),
                "total_subscriptions": int(stats["total_subscriptions"] or 0),
                "subscriptions_today": int(stats["subscriptions_today"] or 0),
                "payment_visitors": int(stats["payment_visitors"] or 0),
                "payment_visitors_today": int(stats["payment_visitors_today"] or 0),
                "total_successful_amount": format_dashboard_amount(stats["total_successful_amount"]),
                "users_today_chart": list(stats["users_today_chart"] or []),
                "subscriptions_today_chart": list(stats["subscriptions_today_chart"] or []),
                "messages_today_chart": list(stats["messages_today_chart"] or []),
                "payment_visitors_today_chart": list(stats["payment_visitors_today_chart"] or []),
                "worker_alive": bool(stats["worker_alive"]),
                "subgram_balance": format_subgram_balance(subgram_snapshot.get("value")),
                "subgram_ok": bool(subgram_snapshot.get("ok")),
                "subgram_error": subgram_snapshot.get("error"),
            },
            headers={"Cache-Control": "no-store, max-age=0"},
        )
    except Exception as exc:
        print(f"❌ Dashboard counters: {exc}", flush=True)
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": "Не удалось получить показатели дашборда"},
            headers={"Cache-Control": "no-store, max-age=0"},
        )


@app.get("/dashboard/events")
async def dashboard_events(request: Request):
    """Stream dashboard invalidations to the browser using Server-Sent Events."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    _dashboard_subscribers.add(queue)
    stop_event = _dashboard_listener_stop

    async def event_stream():
        try:
            # Fetch a fresh snapshot immediately after connecting.  This also
            # covers events that happened while a browser was reconnecting.
            yield 'event: dashboard\ndata: {"reason":"connected"}\n\n'
            while True:
                if await request.is_disconnected():
                    break
                queue_task = asyncio.create_task(queue.get())
                disconnect_task = asyncio.create_task(request.is_disconnected())
                wait_tasks = {queue_task, disconnect_task}
                shutdown_task = None
                if stop_event is not None:
                    shutdown_task = asyncio.create_task(stop_event.wait())
                    wait_tasks.add(shutdown_task)
                try:
                    done, _ = await asyncio.wait(
                        wait_tasks,
                        timeout=20,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if shutdown_task is not None and shutdown_task in done:
                        break
                    if disconnect_task in done and disconnect_task.result():
                        break
                    if queue_task in done:
                        payload = queue_task.result()
                        safe_payload = json.dumps(
                            {"reason": payload or "changed"},
                            ensure_ascii=False,
                        )
                        yield f"event: dashboard\ndata: {safe_payload}\n\n"
                    else:
                        # Keep reverse proxies from closing an otherwise idle
                        # SSE connection.  A comment is ignored by EventSource.
                        yield ": heartbeat\n\n"
                finally:
                    for task in wait_tasks:
                        if not task.done():
                            task.cancel()
                    for task in wait_tasks:
                        with suppress(asyncio.CancelledError):
                            await task
        finally:
            _dashboard_subscribers.discard(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    total_users = 0
    users_today = 0
    total_messages = 0
    messages_today = 0
    total_subscriptions = 0
    subscriptions_today = 0
    payment_visitors = 0
    payment_visitors_today = 0
    total_successful_amount = 0
    users_today_chart = []
    subscriptions_today_chart = []
    messages_today_chart = []
    payment_visitors_today_chart = []
    worker_alive = False
    subgram_snapshot = {
        "value": None,
        "ok": False,
        "error": None,
    }
    
    try:
        conn = await database.get_conn()
        try:
            stats = await conn.fetchrow(
                DASHBOARD_STATS_QUERY,
                dashboard_day_start_utc_naive(),
                dashboard_now_utc_naive(),
            )
            total_users = stats["total_users"]
            users_today = stats["users_today"]
            total_messages = stats["total_messages"]
            messages_today = stats["messages_today"]
            total_subscriptions = stats["total_subscriptions"]
            subscriptions_today = stats["subscriptions_today"]
            payment_visitors = stats["payment_visitors"]
            payment_visitors_today = stats["payment_visitors_today"]
            total_successful_amount = stats["total_successful_amount"] or 0
            users_today_chart = list(stats["users_today_chart"] or [])
            subscriptions_today_chart = list(stats["subscriptions_today_chart"] or [])
            messages_today_chart = list(stats["messages_today_chart"] or [])
            payment_visitors_today_chart = list(stats["payment_visitors_today_chart"] or [])
            worker_alive = bool(stats["worker_alive"])
        finally:
            await database.release_conn(conn)
    except Exception as e:
        print(f"❌ Ошибка БД в дашборде: {e}", flush=True)
        import traceback
        traceback.print_exc()

    # SubGram is external to PostgreSQL; its cached snapshot is independent
    # from the database query so a temporary DB error does not hide the card.
    subgram_snapshot = await get_dashboard_subgram_balance()

    worker_status = "ok" if worker_alive else "degraded"
    worker_label = "Работает" if worker_alive else "Нет свежего сигнала"
    subgram_balance = format_subgram_balance(subgram_snapshot.get("value"))
    total_successful_amount_text = format_dashboard_amount(total_successful_amount)
    subgram_status_class = "ok" if subgram_snapshot.get("ok") else "degraded"
    subgram_status = (
        "Обновляется автоматически"
        if subgram_snapshot.get("ok")
        else "Не удалось получить баланс"
    )
    content = f'''
    <div class="page-heading">
        <div>
            <h1>Обзор</h1>
        </div>
    </div>
    <div class="stats">
        <div class="stat-card"><span class="icon">◎</span><div class="stat-number" id="dashboard-total-users">{format_dashboard_number(total_users)}</div><div class="stat-label">Доступные пользователи</div><div class="stat-today" id="dashboard-users-today">+{format_dashboard_number(users_today)} за сегодня</div>{dashboard_sparkline_svg(users_today_chart, 'dashboard-users-chart')}</div>
        <div class="stat-card"><span class="icon">◇</span><div class="stat-number" id="dashboard-total-subscriptions">{format_dashboard_number(total_subscriptions)}</div><div class="stat-label">Активные подписки</div><div class="stat-today" id="dashboard-subscriptions-today">+{format_dashboard_number(subscriptions_today)} за сегодня</div>{dashboard_sparkline_svg(subscriptions_today_chart, 'dashboard-subscriptions-chart')}</div>
        <div class="stat-card"><span class="icon">≋</span><div class="stat-number" id="dashboard-total-messages">{format_dashboard_number(total_messages)}</div><div class="stat-label">Сообщений отправлено</div><div class="stat-today" id="dashboard-messages-today">+{format_dashboard_number(messages_today)} за сегодня</div>{dashboard_sparkline_svg(messages_today_chart, 'dashboard-messages-chart')}</div>
        <div class="stat-card"><span class="icon">↗</span><div class="stat-number" id="dashboard-payment-visitors">{format_dashboard_number(payment_visitors)}</div><div class="stat-label">Перешли к оплате</div><div class="stat-today" id="dashboard-payment-visitors-today">+{format_dashboard_number(payment_visitors_today)} за сегодня</div>{dashboard_sparkline_svg(payment_visitors_today_chart, 'dashboard-payment-visitors-chart')}</div>
    </div>
    <div class="quick-grid">
        <div class="stat-card subgram-stat-card" role="button" tabindex="0" id="refresh-subgram-balance"><span class="icon">₽</span><div class="stat-number" id="dashboard-subgram-balance">{subgram_balance} ₽</div><div class="stat-label">Баланс SubGram</div></div>
        <div class="stat-card charges-stat-card"><span class="icon">↗</span><div class="stat-number" id="dashboard-total-successful-amount">{total_successful_amount_text} ₽</div><div class="stat-label">Общие списания</div></div>
        <section class="panel">
            <div class="panel-title">Состояние системы</div>
            <p class="panel-copy">Проверяется автоматически при открытии и обновляется раз в минуту.</p>
            <div class="system-status-grid">
                <div class="system-status-row"><span>Админ-панель</span><span class="status-pill status-ok" id="admin-health">Работает</span></div>
                <div class="system-status-row"><span>База данных</span><span class="status-pill status-ok" id="database-health">Подключена</span></div>
                <div class="system-status-row"><span>Worker рассылок</span><span class="status-pill status-{worker_status}" id="worker-health">{worker_label}</span></div>
            </div>
            <div class="quick-links">
                <a class="quick-link" href="/monitoring"><span>Открыть мониторинг сервера</span><span>→</span></a>
            </div>
        </section>
    </div>
    <script>
        async function refreshSystemHealth() {{
            try {{
                const response = await fetch('/health', {{ cache: 'no-store' }});
                const data = await response.json();
                const worker = document.getElementById('worker-health');
                const database = document.getElementById('database-health');
                worker.textContent = data.worker === 'ok' ? 'Работает' : 'Нет свежего сигнала';
                worker.className = 'status-pill status-' + (data.worker === 'ok' ? 'ok' : 'degraded');
                database.textContent = data.database === 'ok' ? 'Подключена' : 'Ошибка';
                database.className = 'status-pill status-' + (data.database === 'ok' ? 'ok' : 'down');
            }} catch (error) {{
                document.getElementById('database-health').textContent = 'Нет связи';
                document.getElementById('database-health').className = 'status-pill status-down';
            }}
        }}
        async function refreshDashboardStats(forceSubgram = false) {{
            try {{
                const response = await fetch('/dashboard/data' + (forceSubgram ? '?refresh=1' : ''), {{ cache: 'no-store' }});
                if (!response.ok) return;
                const data = await response.json();
                if (!data.ok) return;
                const updateChart = (chartId, values) => {{
                    const points = (values || []).map((value) => Math.max(0, Number(value) || 0));
                    if (!points.length) points.push(0);
                    const maxValue = Math.max(...points, 1);
                    const height = 36;
                    const denominator = Math.max(points.length - 1, 1);
                    const path = points.map((value, index) => {{
                        const x = index * 100 / denominator;
                        const y = height - (value / maxValue) * (height - 4) - 2;
                        return (index ? 'L ' : 'M ') + x.toFixed(2) + ',' + y.toFixed(2);
                    }}).join(' ');
                    const line = document.getElementById(chartId + '-line');
                    const fill = document.getElementById(chartId + '-fill');
                    if (line) line.setAttribute('d', path);
                    if (fill) fill.setAttribute('d', path + ' L 100,' + height + ' L 0,' + height + ' Z');
                }};
                document.getElementById('dashboard-total-users').textContent = data.total_users.toLocaleString('ru-RU');
                document.getElementById('dashboard-users-today').textContent = '+' + data.users_today.toLocaleString('ru-RU') + ' за сегодня';
                updateChart('dashboard-users-chart', data.users_today_chart);
                document.getElementById('dashboard-total-subscriptions').textContent = data.total_subscriptions.toLocaleString('ru-RU');
                document.getElementById('dashboard-subscriptions-today').textContent = '+' + data.subscriptions_today.toLocaleString('ru-RU') + ' за сегодня';
                updateChart('dashboard-subscriptions-chart', data.subscriptions_today_chart);
                document.getElementById('dashboard-total-messages').textContent = data.total_messages.toLocaleString('ru-RU');
                document.getElementById('dashboard-messages-today').textContent = '+' + data.messages_today.toLocaleString('ru-RU') + ' за сегодня';
                updateChart('dashboard-messages-chart', data.messages_today_chart);
                document.getElementById('dashboard-payment-visitors').textContent = data.payment_visitors.toLocaleString('ru-RU');
                document.getElementById('dashboard-payment-visitors-today').textContent = '+' + data.payment_visitors_today.toLocaleString('ru-RU') + ' за сегодня';
                updateChart('dashboard-payment-visitors-chart', data.payment_visitors_today_chart);
                const totalSuccessfulAmount = document.getElementById('dashboard-total-successful-amount');
                if (totalSuccessfulAmount) totalSuccessfulAmount.textContent = (data.total_successful_amount || '—') + ' ₽';
                const worker = document.getElementById('worker-health');
                worker.textContent = data.worker_alive ? 'Работает' : 'Нет свежего сигнала';
                worker.className = 'status-pill status-' + (data.worker_alive ? 'ok' : 'degraded');
                const subgramBalance = document.getElementById('dashboard-subgram-balance');
                const subgramStatus = document.getElementById('dashboard-subgram-status');
                if (subgramBalance) subgramBalance.textContent = (data.subgram_balance || '—') + ' ₽';
                if (subgramStatus) {{
                    subgramStatus.textContent = data.subgram_ok ? 'Обновляется автоматически' : 'Не удалось получить баланс';
                    subgramStatus.className = 'status-pill status-' + (data.subgram_ok ? 'ok' : 'degraded');
                    subgramStatus.title = data.subgram_error || '';
                }}
            }} catch (error) {{
                // Keep the last known counters visible during a temporary DB/network error.
            }}
        }}
        const refreshSubgramButton = document.getElementById('refresh-subgram-balance');
        if (refreshSubgramButton) refreshSubgramButton.addEventListener('click', async () => {{
            await refreshDashboardStats(true);
        }});
        const dashboardEvents = new EventSource('/dashboard/events');
        dashboardEvents.addEventListener('dashboard', refreshDashboardStats);
        // EventSource automatically reconnects after a network or proxy
        // interruption; the server sends a fresh invalidation on reconnect.
        dashboardEvents.onerror = () => {{
            // Keep the current values visible while the stream reconnects.
        }};
        window.setInterval(refreshSystemHealth, 60000);
        window.setInterval(refreshDashboardStats, 60000);
    </script>
    '''
    
    menu = MENU.replace('📊 Дашборд', '<span style="color:#7c3aed; text-shadow: 0 0 30px #7c3aed44;">📊 Дашборд</span>')
    html = BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', content)
    return HTMLResponse(content=html)


@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    rows = []
    total = 0
    page_size = 100
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except (TypeError, ValueError):
        page = 1

    try:
        conn = await database.get_conn()
        try:
            total = int(await conn.fetchval("SELECT COUNT(*) FROM users") or 0)
            total_pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, total_pages)
            rows = await conn.fetch(
                """
                SELECT * FROM users
                ORDER BY registered_at DESC NULLS LAST, telegram_id DESC
                LIMIT $1 OFFSET $2
                """,
                page_size,
                (page - 1) * page_size,
            )
        finally:
            await database.release_conn(conn)
    except Exception as e:
        print(f"❌ Ошибка загрузки пользователей: {e}")
        total_pages = 1

    html = f'<div class="page-title">👥 Пользователи</div>'
    html += f'<div class="stats"><div class="stat-card"><div class="stat-number">{total}</div><div class="stat-label">Всего пользователей</div></div></div>'

    if rows:
        html += '<div class="table-container"><table><tr><th>ID</th><th>Username</th><th>Имя</th><th>Регистрация</th></tr>'
        for row in rows:
            username = escape(str(row.get('username') or '-'))
            first_name = escape(str(row.get('first_name') or '-'))
            registered_at = row.get('registered_at')
            registered_text = format_admin_datetime(registered_at)
            html += f"<tr><td>{row['telegram_id']}</td><td>{username}</td><td>{first_name}</td><td>{registered_text}</td></tr>"
        html += '</table></div>'
        if total_pages > 1:
            previous = f'<a class="button button-secondary" href="/users?page={page - 1}">← Предыдущая</a>' if page > 1 else ''
            next_page = f'<a class="button button-secondary" href="/users?page={page + 1}">Следующая →</a>' if page < total_pages else ''
            html += f'<div class="page-actions" style="justify-content:space-between;margin-top:16px;"><span class="panel-copy">Страница {page} из {total_pages}</span><div style="display:flex;gap:8px;">{previous}{next_page}</div></div>'
    else:
        html += '<div class="empty">😕 Пользователей пока нет</div>'
    
    menu = MENU.replace('👥 Пользователи', '<span style="color:#7c3aed; text-shadow: 0 0 30px #7c3aed44;">👥 Пользователи</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))


@app.get("/messages", response_class=HTMLResponse)
async def messages_page(request: Request):
    rows = []
    total = 0
    page_size = 100
    total_pages = 1
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except (TypeError, ValueError):
        page = 1

    try:
        conn = await database.get_conn()
        try:
            total = int(await conn.fetchval("SELECT COUNT(*) FROM messages") or 0)
            total_pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, total_pages)
            rows = await conn.fetch("""
                SELECT m.id, m.content, m.content_type, m.created_at,
                       s.first_name as sender_name, s.username as sender_username,
                       r.first_name as recipient_name, r.username as recipient_username
                FROM messages m
                LEFT JOIN users s ON m.sender_id = s.telegram_id
                LEFT JOIN users r ON m.recipient_id = r.telegram_id
                ORDER BY m.created_at DESC NULLS LAST, m.id DESC
                LIMIT $1 OFFSET $2
            """, page_size, (page - 1) * page_size)
        finally:
            await database.release_conn(conn)
    except Exception as e:
        print(f"❌ Ошибка загрузки сообщений: {e}")
    
    html = '<div class="page-title">💬 Сообщения</div>'
    if rows:
        html += '<div class="table-container"><table><tr><th>ID</th><th>От</th><th>Кому</th><th>Тип</th><th>Сообщение</th><th>Дата</th></tr>'
        for row in rows:
            sender = row.get('sender_username') or row.get('sender_name') or 'Неизвестно'
            recipient = row.get('recipient_username') or row.get('recipient_name') or 'Неизвестно'
            html += f'''
            <tr>
                <td>{row['id']}</td>
                <td>@{sender}</td>
                <td>@{recipient}</td>
                <td><span class="badge badge-text">{row['content_type']}</span></td>
                <td>{row['content'][:50]}{'...' if len(row['content'] or '') > 50 else ''}</td>
                <td>{format_admin_datetime(row['created_at'])}</td>
            </tr>
            '''
        html += '</table></div>'

        first_item = (page - 1) * page_size + 1
        last_item = min(page * page_size, total)
        if total_pages > 1:
            previous = f'<a class="button button-secondary" href="/messages?page={page - 1}">← Предыдущая</a>' if page > 1 else ''
            next_page = f'<a class="button button-secondary" href="/messages?page={page + 1}">Следующая →</a>' if page < total_pages else ''
            html += f'<div class="page-actions" style="justify-content:space-between;margin-top:16px;"><span class="panel-copy">Показаны записи {first_item}–{last_item} из {total} · страница {page} из {total_pages}</span><div style="display:flex;gap:8px;">{previous}{next_page}</div></div>'
    else:
        html += '<div class="empty">😕 Сообщений пока нет</div>'
    
    menu = MENU.replace('💬 Сообщения', '<span style="color:#7c3aed; text-shadow: 0 0 30px #7c3aed44;">💬 Сообщения</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))


@app.get("/subscriptions", response_class=HTMLResponse)
async def subscriptions_page(request: Request):
    page_size = 100
    page = 1
    total_records = 0
    total_users = 0
    status_counts = {}
    rows = []

    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except (TypeError, ValueError):
        page = 1

    try:
        conn = await database.get_conn()
        try:
            total_records = int(await conn.fetchval(
                "SELECT COUNT(*) FROM subscriptions"
            ) or 0)
            total_users = int(await conn.fetchval(
                "SELECT COUNT(DISTINCT user_id) FROM subscriptions"
            ) or 0)
            status_rows = await conn.fetch(
                """
                SELECT status,
                       COUNT(*) AS records,
                       COUNT(DISTINCT user_id) AS users,
                       COUNT(*) FILTER (
                           WHERE (
                               status = 'active' AND (end_date IS NULL OR end_date > NOW())
                           ) OR (
                               status = 'cancelled' AND end_date IS NOT NULL AND end_date > NOW()
                               AND EXISTS (
                                   SELECT 1 FROM unsubscribed_users AS archived
                                   WHERE archived.telegram_id = subscriptions.user_id
                                     AND archived.reason = 'manual'
                               )
                           )
                       ) AS current_records,
                       COUNT(DISTINCT user_id) FILTER (
                           WHERE (
                               status = 'active' AND (end_date IS NULL OR end_date > NOW())
                           ) OR (
                               status = 'cancelled' AND end_date IS NOT NULL AND end_date > NOW()
                               AND EXISTS (
                                   SELECT 1 FROM unsubscribed_users AS archived
                                   WHERE archived.telegram_id = subscriptions.user_id
                                     AND archived.reason = 'manual'
                               )
                           )
                       ) AS current_users
                FROM subscriptions
                GROUP BY status
                ORDER BY status
                """
            )
            status_counts = {
                str(row["status"] or "unknown"): {
                    "records": int(row["records"] or 0),
                    "users": int(row["users"] or 0),
                    "current_records": int(row["current_records"] or 0),
                    "current_users": int(row["current_users"] or 0),
                }
                for row in status_rows
            }
            active_users = int(await conn.fetchval(
                """
                SELECT COUNT(DISTINCT user_id)
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
            ) or 0)
        finally:
            await database.release_conn(conn)

        total_pages = max(1, (total_records + page_size - 1) // page_size)
        page = min(page, total_pages)
        rows = await database.get_all_subscriptions(
            limit=page_size,
            offset=(page - 1) * page_size,
        )
    except Exception as e:
        print(f"❌ Ошибка загрузки подписок: {e}")

        total_pages = 1

    active_users = locals().get("active_users", 0)
    retry_low_users = status_counts.get("retry_low", {}).get("users", 0)

    html = f'<div class="page-title">📋 Подписки</div>'
    html += f'''
    <div class="stats">
        <div class="stat-card"><div class="stat-number">{total_users}</div><div class="stat-label">Уникальных пользователей</div></div>
        <div class="stat-card"><div class="stat-number">{total_records}</div><div class="stat-label">Всего записей подписок</div></div>
        <div class="stat-card"><div class="stat-number">{active_users}</div><div class="stat-label">Действующих подписок</div></div>
        <div class="stat-card"><div class="stat-number">{retry_low_users}</div><div class="stat-label">Ожидают повторной оплаты</div></div>
    </div>
    '''

    if status_counts:
        status_labels = {
            "active": "Активна",
            "retry_low": "Повторная оплата",
            "pending": "Ожидает оплаты",
            "cancelled": "Отменена",
            "expired": "Истекла",
        }
        summary = " · ".join(
            f"{escape(status_labels.get(status, status))}: {data['users']} польз."
            for status, data in status_counts.items()
        )
        html += f'<p class="panel-copy" style="margin:0 0 16px;">По статусам: {summary}</p>'

    if rows:
        html += '''
        <div class="table-container">
            <table>
                <tr>
                    <th>ID</th>
                    <th>Пользователь</th>
                    <th>План</th>
                    <th>Начало</th>
                    <th>Окончание</th>
                    <th>Успешно списано</th>
                    <th>Статус</th>
                    <th>Действие</th>
                </tr>
        '''
        for row in rows:
            raw_status, _, _ = subscription_status_meta(row.get('status'))
            end_date_str = format_admin_datetime(row['end_date'], date_only=True, fallback='∞')
            successful_amount = row.get('successful_amount') or 0
            try:
                successful_amount_text = f"{float(successful_amount):,.2f}".replace(',', ' ').replace('.', ',') + ' ₽'
            except (TypeError, ValueError):
                successful_amount_text = '0,00 ₽'

            block_btn = ''
            if raw_status in ['active', 'pending']:
                block_btn = f'''
                <button onclick="blockSubscription({row['user_id']})"
                        style="padding:4px 12px; border:1px solid #ff4488; border-radius:6px; background:transparent; color:#ff4488; cursor:pointer; font-size:11px;">
                    🚫 Заблокировать
                </button>
                '''
            
            html += f'''
            <tr>
                <td>{row['id']}</td>
                <td>@{escape(str(row.get('username') or row.get('first_name') or row['user_id']))}</td>
                <td>{escape(str(row.get('plan') or '-'))}</td>
                <td>{format_admin_datetime(row['start_date'], date_only=True)}</td>
                <td>{end_date_str}</td>
                <td><strong>{successful_amount_text}</strong></td>
                <td>{subscription_status_badge(raw_status)}</td>
                <td>{block_btn}</td>
            </tr>
            '''
        html += '</table></div>'

        first_item = (page - 1) * page_size + 1
        last_item = min(page * page_size, total_records)
        previous = f'<a class="button button-secondary" href="/subscriptions?page={page - 1}">← Предыдущая</a>' if page > 1 else ''
        next_page = f'<a class="button button-secondary" href="/subscriptions?page={page + 1}">Следующая →</a>' if page < total_pages else ''
        html += f'<div class="page-actions" style="justify-content:space-between;margin-top:16px;"><span class="panel-copy">Показаны записи {first_item}–{last_item} из {total_records} · страница {page} из {total_pages}</span><div style="display:flex;gap:8px;">{previous}{next_page}</div></div>'

        html += '''
        <script>
            async function blockSubscription(userId) {
                if (!confirm('⚠️ Вы уверены, что хотите ЗАБЛОКИРОВАТЬ подписку пользователя #' + userId + '?')) return;
                
                const btn = event.target;
                btn.textContent = '⏳...';
                btn.disabled = true;
                
                try {
                    const response = await fetch('/subscription/block/' + userId, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' }
                    });
                    const data = await response.json();
                    if (data.success) {
                        alert('✅ ' + data.message);
                        window.location.reload();
                    } else {
                        alert('❌ Ошибка: ' + data.error);
                    }
                } catch (e) {
                    alert('❌ Ошибка: ' + e.message);
                } finally {
                    btn.textContent = '🚫 Заблокировать';
                    btn.disabled = false;
                }
            }
        </script>
        '''
    else:
        html += '<div class="empty">😕 Подписок пока нет</div>'
    
    menu = MENU.replace('📋 Подписки', '<span style="color:#7c3aed; text-shadow: 0 0 30px #7c3aed44;">📋 Подписки</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))


@app.get("/blocked", response_class=HTMLResponse)
async def blocked_page(request: Request):
    rows = []
    total = 0
    page_size = 100
    total_pages = 1

    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except (TypeError, ValueError):
        page = 1

    try:
        conn = await database.get_conn()
        try:
            total = int(await conn.fetchval("SELECT COUNT(*) FROM blocked_users") or 0)
            total_pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, total_pages)
            rows = await conn.fetch(
                """
                SELECT * FROM blocked_users
                ORDER BY blocked_at DESC NULLS LAST, telegram_id DESC
                LIMIT $1 OFFSET $2
                """,
                page_size,
                (page - 1) * page_size,
            )
        finally:
            await database.release_conn(conn)
    except Exception as e:
        print(f"❌ Ошибка загрузки ЧС: {e}")
    
    html = f'<div class="page-title">🚫 Чёрный список</div>'
    html += f'<div class="stats"><div class="stat-card"><div class="stat-number">{total}</div><div class="stat-label">Заблокировали бота</div></div></div>'
    
    if rows:
        html += '<div class="table-container"><table><tr><th>ID</th><th>Username</th><th>Имя</th><th>Заблокирован</th></tr>'
        for row in rows:
            html += f"<tr><td>{row['telegram_id']}</td><td>{row.get('username') or '-'}</td><td>{row.get('first_name') or '-'}</td><td>{format_admin_datetime(row['blocked_at'])}</td></tr>"
        html += '</table></div>'

        first_item = (page - 1) * page_size + 1
        last_item = min(page * page_size, total)
        if total_pages > 1:
            previous = f'<a class="button button-secondary" href="/blocked?page={page - 1}">← Предыдущая</a>' if page > 1 else ''
            next_page = f'<a class="button button-secondary" href="/blocked?page={page + 1}">Следующая →</a>' if page < total_pages else ''
            html += f'<div class="page-actions" style="justify-content:space-between;margin-top:16px;"><span class="panel-copy">Показаны записи {first_item}–{last_item} из {total} · страница {page} из {total_pages}</span><div style="display:flex;gap:8px;">{previous}{next_page}</div></div>'
    else:
        html += '<div class="empty">😕 Пользователей в ЧС нет</div>'
    
    menu = MENU.replace('🚫 ЧС', '<span style="color:#7c3aed; text-shadow: 0 0 30px #7c3aed44;">🚫 ЧС</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))


@app.get("/unsubscribed", response_class=HTMLResponse)
async def unsubscribed_page(request: Request):
    total = 0
    users = []
    page_size = 100
    total_pages = 1
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except (TypeError, ValueError):
        page = 1

    try:
        conn = await database.get_conn()
        try:
            total = int(await conn.fetchval("SELECT COUNT(*) FROM unsubscribed_users") or 0)
            total_pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, total_pages)
            rows = await conn.fetch("""
                SELECT * FROM unsubscribed_users
                ORDER BY unsubscribed_at DESC
                LIMIT $1 OFFSET $2
            """, page_size, (page - 1) * page_size)
            users = [dict(row) for row in rows]
        finally:
            await database.release_conn(conn)
    except Exception as e:
        print(f"❌ Ошибка загрузки отписавшихся: {e}")
    
    html = f'''
    <div class="page-title">🚫 Отписавшиеся пользователи</div>
    
    <div class="stats">
        <div class="stat-card">
            <div class="stat-number">{total}</div>
            <div class="stat-label">Всего отписавшихся</div>
        </div>
    </div>
    '''
    
    if users:
        html += '''
        <div class="table-container">
            <table>
                <tr>
                    <th>ID</th>
                    <th>Username</th>
                    <th>Имя</th>
                    <th>Дата отписки</th>
                    <th>Причина</th>
                    <th>Токен</th>
                    <th>Действие</th>
                </tr>
        '''
        for row in users:
            reason_labels = {
                'manual': '👤 Самостоятельно',
                'admin_block': '🚫 Админ',
                'expired': '⏰ Истекла'
            }
            raw_reason = row.get('reason', 'manual')
            if str(raw_reason).startswith('card_unavailable_'):
                reason_code = str(raw_reason).rsplit('_', 1)[-1]
                reason = f'💳 Карта недоступна (код {escape(reason_code)})'
            else:
                reason = reason_labels.get(raw_reason, raw_reason)
            
            token = row.get('card_token', '')
            token_display = token[:20] + '...' if token and len(token) > 20 else token or '—'
            
            # Кнопка восстановления
            restore_btn = f'''
            <button onclick="restoreUser(event, {row['telegram_id']})"
                    style="padding:4px 12px; border:1px solid #7c3aed; border-radius:6px; background:transparent; color:#7c3aed; cursor:pointer; font-size:11px;">
                🔄 Восстановить
            </button>
            '''
            
            html += f'''
            <tr>
                <td>{row['telegram_id']}</td>
                <td>{row.get('username') or '-'}</td>
                <td>{row.get('first_name') or '-'}</td>
                <td>{format_admin_datetime(row['unsubscribed_at'])}</td>
                <td>{reason}</td>
                <td><span style="font-family:monospace; font-size:11px; color:#8899aa;">{token_display}</span></td>
                <td>{restore_btn}</td>
            </tr>
        '''
        html += '</table></div>'

        first_item = (page - 1) * page_size + 1
        last_item = min(page * page_size, total)
        if total_pages > 1:
            previous = f'<a class="button button-secondary" href="/unsubscribed?page={page - 1}">← Предыдущая</a>' if page > 1 else ''
            next_page = f'<a class="button button-secondary" href="/unsubscribed?page={page + 1}">Следующая →</a>' if page < total_pages else ''
            html += f'<div class="page-actions" style="justify-content:space-between;margin-top:16px;"><span class="panel-copy">Показаны записи {first_item}–{last_item} из {total} · страница {page} из {total_pages}</span><div style="display:flex;gap:8px;">{previous}{next_page}</div></div>'

        html += '''
        <script>
            async function restoreUser(event, userId) {
                if (!confirm('⚠️ Вы уверены, что хотите восстановить подписку пользователя #' + userId + '?')) return;

                const btn = event.target;
                btn.textContent = '⏳...';
                btn.disabled = true;
                
                try {
                    const response = await fetch('/unsubscribed/restore/' + userId, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' }
                    });
                    const data = await response.json();
                    if (data.success) {
                        alert('✅ ' + data.message);
                        window.location.reload();
                    } else {
                        alert('❌ Ошибка: ' + data.error);
                    }
                } catch (e) {
                    alert('❌ Ошибка: ' + e.message);
                } finally {
                    btn.textContent = '🔄 Восстановить';
                    btn.disabled = false;
                }
            }
        </script>
        '''
    else:
        html += '<div class="empty">😕 Отписавшихся пока нет</div>'
    
    html += '''
    <div style="margin-top: 20px;">
        <a href="/unsubscribed" class="glow-button" style="flex:0 1 auto; width:auto; padding: 8px 16px;">🔄 Обновить</a>
    </div>
    '''
    
    menu = MENU.replace('🚫 Отписались', '<span style="color:#7c3aed; text-shadow: 0 0 30px #7c3aed44;">🚫 Отписались</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))

@app.get("/webhooks", response_class=HTMLResponse)
async def webhooks_page(request: Request):
    rows = []
    total = 0
    page_size = 25
    total_pages = 1
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except (TypeError, ValueError):
        page = 1

    try:
        conn = await database.get_conn()
        try:
            total = int(await conn.fetchval("SELECT COUNT(*) FROM webhook_events") or 0)
            total_pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, total_pages)
            rows = await conn.fetch(
                """
                SELECT id, received_at, event_type, transaction_id,
                       account_id, status, is_recurrent, payload
                FROM webhook_events
                ORDER BY received_at DESC, id DESC
                LIMIT $1 OFFSET $2
                """,
                page_size,
                (page - 1) * page_size,
            )
        finally:
            await database.release_conn(conn)
    except Exception as e:
        print(f"❌ Ошибка загрузки вебхуков: {e}", flush=True)

    html = f'''
    <div class="page-title">📨 Вебхуки от CloudPayments</div>
    <div style="display: flex; gap: 15px; margin-bottom: 20px; flex-wrap: wrap;">
        <div class="stat-card" style="flex:1; min-width:120px;">
            <div class="stat-number">{total}</div>
            <div class="stat-label">Всего вебхуков</div>
        </div>
    </div>
    <div style="margin-bottom: 15px;">
        <a href="/webhooks?page={page}" class="glow-button" style="flex:0 1 auto; width:auto; padding: 8px 16px;">🔄 Обновить</a>
    </div>
    '''

    if rows:
        html += '<div class="table-container"><table><tr><th>Время</th><th>Событие</th><th>Транзакция</th><th>Аккаунт</th><th>Статус</th><th>Тип</th><th>Payload</th></tr>'
        for row in rows:
            event_type = escape(str(row.get("event_type") or "pay"))
            transaction_id = escape(str(row.get("transaction_id") or "—"))
            account_id = escape(str(row.get("account_id") or "—"))
            status = str(row.get("status") or "—")
            status_class = {
                "completed": "status-ok",
                "declined": "status-down",
                "failed": "status-down",
            }.get(status.lower(), "status-degraded")
            recurrent = "Рекуррентный" if row.get("is_recurrent") else "Первичный"
            payload = row.get("payload")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (TypeError, ValueError):
                    pass
            payload_text = json.dumps(
                redact_webhook_payload(payload),
                ensure_ascii=False,
                indent=2,
                default=str,
            )
            html += f'''
            <tr>
                <td>{format_admin_datetime(row.get("received_at"), include_seconds=True)}</td>
                <td>{event_type}</td>
                <td>{transaction_id}</td>
                <td>{account_id}</td>
                <td><span class="status-pill {status_class}">{escape(status)}</span></td>
                <td>{recurrent}</td>
                <td><details><summary>Показать</summary><pre style="white-space:pre-wrap;word-break:break-word;max-width:520px;">{escape(payload_text)}</pre></details></td>
            </tr>
            '''
        html += '</table></div>'
        if total_pages > 1:
            previous = f'<a class="button button-secondary" href="/webhooks?page={page - 1}">← Предыдущая</a>' if page > 1 else ''
            next_page = f'<a class="button button-secondary" href="/webhooks?page={page + 1}">Следующая →</a>' if page < total_pages else ''
            html += f'<div class="page-actions" style="justify-content:space-between;margin-top:16px;"><span class="panel-copy">Страница {page} из {total_pages}</span><div style="display:flex;gap:8px;">{previous}{next_page}</div></div>'
    else:
        html += '<div class="empty">📭 Вебхуков пока нет. После следующего запроса CloudPayments запись появится здесь.</div>'

    menu = MENU.replace('📨 Вебхуки', '<span style="color:#ffaa44; text-shadow: 0 0 30px #ffaa4444;">📨 Вебхуки</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))


@app.get("/monitoring", response_class=HTMLResponse)
async def monitoring_page(request: Request):
    content = '''
    <div class="page-heading">
        <div>
            <h1>📊 Мониторинг сервера</h1>
            <p>Показатели обновляются автоматически каждые 30 секунд.</p>
        </div>
        <div class="page-actions">
            <button id="monitoring-refresh" type="button" class="button-secondary">↻ Обновить сейчас</button>
        </div>
    </div>
    <div class="stats">
        <div class="stat-card"><div class="stat-number" id="monitoring-cpu">—</div><div class="stat-label">CPU</div></div>
        <div class="stat-card"><div class="stat-number" id="monitoring-memory">—</div><div class="stat-label">Память</div></div>
        <div class="stat-card"><div class="stat-number" id="monitoring-disk">—</div><div class="stat-label">Диск</div></div>
    </div>
    <div class="panel monitoring-meta" aria-live="polite">
        <span>Последнее обновление: <strong id="monitoring-updated">загрузка…</strong></span>
        <span id="monitoring-error" class="status-degraded" role="status"></span>
    </div>
    <script>
        (() => {
            const cpuElement = document.getElementById('monitoring-cpu');
            const memoryElement = document.getElementById('monitoring-memory');
            const diskElement = document.getElementById('monitoring-disk');
            const updatedElement = document.getElementById('monitoring-updated');
            const errorElement = document.getElementById('monitoring-error');
            const refreshButton = document.getElementById('monitoring-refresh');
            let isRefreshing = false;

            async function refreshMonitoring() {
                if (isRefreshing) return;
                isRefreshing = true;
                refreshButton.disabled = true;
                errorElement.textContent = '';
                try {
                    const response = await fetch('/monitoring/data', {
                        cache: 'no-store',
                        headers: { 'Accept': 'application/json' }
                    });
                    const data = await response.json();
                    if (!response.ok || !data.ok) {
                        throw new Error(data.error || 'Не удалось обновить показатели');
                    }
                    cpuElement.textContent = `${Number(data.cpu).toFixed(1)}%`;
                    memoryElement.textContent = `${Number(data.memory).toFixed(1)}%`;
                    diskElement.textContent = `${Number(data.disk).toFixed(1)}%`;
                    updatedElement.textContent = data.updated_at || 'только что';
                } catch (error) {
                    errorElement.textContent = `Ошибка обновления: ${error.message}`;
                } finally {
                    isRefreshing = false;
                    refreshButton.disabled = false;
                }
            }

            refreshButton.addEventListener('click', refreshMonitoring);
            refreshMonitoring();
            const refreshTimer = window.setInterval(refreshMonitoring, 30000);
            window.addEventListener('pagehide', () => window.clearInterval(refreshTimer), { once: true });
        })();
    </script>
    <style>
        .monitoring-meta { display: flex; justify-content: space-between; gap: 16px; color: var(--muted); font-size: 12px; }
        .monitoring-meta strong { color: var(--ink); font-weight: 700; }
        @media (max-width: 600px) { .monitoring-meta { align-items: flex-start; flex-direction: column; } }
    </style>
    '''
    
    menu = MENU.replace('📊 Мониторинг', '<span style="color:#7c3aed; text-shadow: 0 0 30px #7c3aed44;">📊 Мониторинг</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', content))

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    try:
        current_value = await database.get_setting("show_payment_checkboxes", "true")
        confirmation_enabled = str(current_value).lower() == "true"
        status_label = "Включено" if confirmation_enabled else "Отключено"
        status_class = "status-ok" if confirmation_enabled else "status-down"
    except Exception as exc:
        print(f"⚠️ Не удалось получить настройку подтверждения: {exc}", flush=True)
        confirmation_enabled = None
        status_label = "Не удалось проверить"
        status_class = "status-degraded"

    html = '''
    <div class="page-title">⚙️ Настройки</div>
    <div class="form-container">
        <h3 style="margin-bottom: 7px;">Подтверждение оплаты</h3>
        <p style="color: var(--muted); font-size: 13px; margin-bottom: 18px;">Показывает пользователю элементы подтверждения после оплаты.</p>
        <div class="system-status-row" style="margin-bottom: 18px;">
            <span>Текущий статус</span>
            <span id="confirmation-status" class="status-pill __STATUS_CLASS__">__STATUS_LABEL__</span>
        </div>
        <div class="setting-actions">
            <button id="enable-confirmation" onclick="setSetting('true')" class="glow-button setting-choice __ENABLE_ACTIVE__" aria-pressed="__ENABLE_PRESSED__">✅ Включить</button>
            <button id="disable-confirmation" onclick="setSetting('false')" class="glow-button setting-choice __DISABLE_ACTIVE__" aria-pressed="__DISABLE_PRESSED__">❌ Отключить</button>
        </div>
        <div id="setting-feedback" class="setting-feedback" role="status" aria-live="polite"></div>
    </div>
    <style>
        .setting-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
        .setting-choice { width: 100%; }
        .setting-choice.is-active { background: var(--accent-soft); border-color: var(--accent) !important; color: var(--accent-dark) !important; box-shadow: 0 0 0 3px rgba(17, 139, 126, .08); }
        #disable-confirmation.is-active { background: var(--danger-soft); border-color: var(--danger-line) !important; color: var(--danger) !important; box-shadow: 0 0 0 3px rgba(184, 75, 81, .07); }
        .setting-feedback { min-height: 20px; margin-top: 12px; color: var(--success); font-size: 12px; font-weight: 650; }
        .setting-feedback.is-error { color: var(--danger); }
        @media (max-width: 560px) { .setting-actions { grid-template-columns: 1fr; } }
    </style>
    <script>
        const enableButton = document.getElementById('enable-confirmation');
        const disableButton = document.getElementById('disable-confirmation');
        const statusElement = document.getElementById('confirmation-status');
        const feedbackElement = document.getElementById('setting-feedback');

        function renderSetting(enabled) {
            statusElement.textContent = enabled ? 'Включено' : 'Отключено';
            statusElement.className = 'status-pill ' + (enabled ? 'status-ok' : 'status-down');
            enableButton.classList.toggle('is-active', enabled);
            disableButton.classList.toggle('is-active', !enabled);
            enableButton.setAttribute('aria-pressed', enabled ? 'true' : 'false');
            disableButton.setAttribute('aria-pressed', enabled ? 'false' : 'true');
        }

        async function setSetting(value) {
            const enabled = value === 'true';
            enableButton.disabled = true;
            disableButton.disabled = true;
            feedbackElement.textContent = 'Сохраняем…';
            feedbackElement.classList.remove('is-error');
            try {
                const response = await fetch('/api/settings/update', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ key: 'show_payment_checkboxes', value: value })
                });
                const data = await response.json();
                if (!response.ok || !data.success) {
                    throw new Error(data.error || 'Не удалось сохранить настройку');
                }
                renderSetting(enabled);
                feedbackElement.textContent = '✓ Настройка сохранена';
            } catch (e) {
                feedbackElement.textContent = 'Ошибка: ' + e.message;
                feedbackElement.classList.add('is-error');
            } finally {
                enableButton.disabled = false;
                disableButton.disabled = false;
            }
        }
    </script>
    '''
    html = (html
        .replace('__STATUS_CLASS__', status_class)
        .replace('__STATUS_LABEL__', status_label)
        .replace('__ENABLE_ACTIVE__', 'is-active' if confirmation_enabled is True else '')
        .replace('__DISABLE_ACTIVE__', 'is-active' if confirmation_enabled is False else '')
        .replace('__ENABLE_PRESSED__', 'true' if confirmation_enabled is True else 'false')
        .replace('__DISABLE_PRESSED__', 'true' if confirmation_enabled is False else 'false'))
    menu = MENU.replace('⚙️ Настройки', '<span style="color:#7c3aed; text-shadow: 0 0 30px #7c3aed44;">⚙️ Настройки</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))


@app.post("/api/settings/update")
async def update_setting(request: Request):
    try:
        body = await request.json()
        key = body.get("key")
        value = str(body.get("value", "")).lower()

        if key != "show_payment_checkboxes":
            return JSONResponse(
                content={"success": False, "error": "Неизвестная настройка"},
                status_code=400,
            )
        if value not in {"true", "false"}:
            return JSONResponse(
                content={"success": False, "error": "Некорректное значение"},
                status_code=400,
            )

        await database.set_setting(key, value)
        return JSONResponse(content={"success": True, "value": value})
    except Exception as exc:
        print(f"❌ Не удалось сохранить настройку подтверждения: {exc}", flush=True)
        return JSONResponse(
            content={"success": False, "error": "Не удалось сохранить настройку"},
            status_code=500,
        )


@app.get("/broadcast", response_class=HTMLResponse)
async def broadcast_page(request: Request):
    total_users = 0
    job_id = request.query_params.get("job")
    job_data = None
    
    try:
        conn = await database.get_conn()
        try:
            # Показываем фактическую аудиторию рассылки, а не общее число
            # записей в users. Заблокированные и отписавшиеся пользователи
            # всё равно исключаются при создании broadcast_job_recipients.
            total_users = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM users
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM blocked_users
                    WHERE blocked_users.telegram_id = users.telegram_id
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM unsubscribed_users
                    WHERE unsubscribed_users.telegram_id = users.telegram_id
                )
                """
            )
            
            if not job_id:
                row = await conn.fetchrow(
                    "SELECT id FROM broadcast_jobs WHERE status IN ('running', 'pending', 'paused') ORDER BY id DESC LIMIT 1"
                )
                if row:
                    job_id = str(row['id'])
            
            if job_id:
                job_id = int(job_id)
                row = await conn.fetchrow(
                    "SELECT * FROM broadcast_jobs WHERE id = $1",
                    job_id
                )
                if row:
                    job_data = dict(row)
        finally:
            await database.release_conn(conn)
    except:
        pass
    
    result_html = ""
    
    if job_data:
        total = job_data.get('total_users', 0)
        sent = job_data.get('sent_users', 0)
        failed = job_data.get('failed_users', 0)
        blocked = job_data.get('blocked_users', 0)
        progress = job_data.get('progress', 0)
        processed = sent + failed + blocked
        remaining = max(0, total - processed)
        status_text = job_data.get('status', 'pending')
        
        status_labels = {
            'pending': '⏳ Ожидание',
            'running': '🔄 Выполняется',
            'paused': '⏸️ Приостановлено',
            'completed': '✅ Завершено',
            'failed': '❌ Ошибка',
            'cancelled': '⛔ Остановлена',
            'archived': '📦 Архив'
        }
        status_label = status_labels.get(status_text, status_text)
        status_class = f"status-{status_text}"
        
        result_html = f'''
        <div class="result-box" id="broadcast-stats">
            <div class="page-heading" style="margin-bottom:18px;">
                <div>
                    <h2>Рассылка #{job_id} · <span class="status-value {status_class}">{status_label}</span></h2>
                    <p>Статус автоматически обновляется каждые 5 секунд.</p>
                </div>
            </div>
            <div class="result-stats">
                <div class="stat-total"><div class="number">{total}</div><div class="label">Получателей</div></div>
                <div class="stat-sent"><div class="number">{sent}</div><div class="label">Отправлено</div></div>
                <div class="stat-failed"><div class="number">{failed}</div><div class="label">Ошибок</div></div>
                <div class="stat-remaining"><div class="number">{remaining}</div><div class="label">Осталось</div></div>
            </div>
            <div class="progress-bar">
                <div class="progress-fill" style="width:{min(progress, 100)}%;"></div>
            </div>
            <p class="progress-text" style="font-size:12px;">{progress:.1f}% · Обработано {processed} из {total}</p>
            
            <div class="page-actions" style="margin-top:18px;">
                <button onclick="pauseBroadcast()" id="pauseBtn" class="button-warning"
                        style="display: {'inline-flex' if status_text in ('running', 'pending') else 'none'};">
                    Поставить на паузу
                </button>
                <button onclick="resumeBroadcast()" id="resumeBtn" class="button-success"
                        style="display: {'inline-flex' if status_text == 'paused' else 'none'};">
                    Продолжить рассылку
                </button>
                <button onclick="archiveBroadcast()" id="archiveBtn" class="button-danger"
                        style="display: {'none' if status_text == 'archived' else 'inline-flex'};">
                    Архивировать задачу
                </button>
                <a href="/broadcast" class="glow-button">Обновить</a>
                <a href="/broadcast/list" class="glow-button">Открыть историю</a>
            </div>
        </div>

        <script>
            let jobId = {job_id};

            async function updateStatus() {{
                try {{
                    const response = await fetch(`/broadcast/status/${{jobId}}`);
                    const data = await response.json();

                    if (data.success) {{
                        document.querySelector('.stat-total .number').textContent = data.total_users;
                        document.querySelector('.stat-sent .number').textContent = data.sent_users;
                        document.querySelector('.stat-failed .number').textContent = data.failed_users;
                        const remaining = data.total_users - data.sent_users - data.failed_users - (data.blocked_users || 0);
                        document.querySelector('.stat-remaining .number').textContent = Math.max(0, remaining);
                        document.querySelector('.progress-fill').style.width = data.progress + '%';
                        document.querySelector('.progress-text').textContent = data.progress.toFixed(1) + '% - Обработано: ' + (data.sent_users + data.failed_users + (data.blocked_users || 0)) + ' из ' + data.total_users;

                        const statusLabel = {{
                            'pending': '⏳ Ожидание',
                            'running': '🔄 Выполняется',
                            'paused': '⏸️ Приостановлено',
                            'completed': '✅ Завершено',
                            'failed': '❌ Ошибка',
                            'cancelled': '⛔ Остановлена',
                            'archived': '📦 Архив'
                        }};
                        document.querySelector('.status-value').textContent = statusLabel[data.status] || data.status;
                        document.querySelector('.status-value').className = 'status-value status-' + data.status;

                        const pauseBtn = document.getElementById('pauseBtn');
                        if (pauseBtn) {{
                            pauseBtn.style.display = ['running', 'pending'].includes(data.status) ? 'inline-flex' : 'none';
                        }}
                        const resumeBtn = document.getElementById('resumeBtn');
                        if (resumeBtn) {{
                            resumeBtn.style.display = data.status === 'paused' ? 'inline-flex' : 'none';
                        }}
                    }}
                }} catch (e) {{
                    console.error('Ошибка обновления статуса:', e);
                }}
            }}

            async function pauseBroadcast() {{
                if (!confirm('⏸️ Вы уверены, что хотите поставить рассылку #${{jobId}} на паузу?')) return;
                
                const btn = document.getElementById('pauseBtn');
                btn.textContent = '⏳ Пауза...';
                btn.disabled = true;
                
                try {{
                    const response = await fetch(`/broadcast/pause/${{jobId}}`, {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }}
                    }});
                    const data = await response.json();
                    if (data.success) {{
                        alert('✅ ' + data.message);
                        window.location.reload();
                    }} else {{
                        alert('❌ Ошибка: ' + data.error);
                    }}
                }} catch (e) {{
                    alert('❌ Ошибка: ' + e.message);
                }} finally {{
                    btn.textContent = 'Поставить на паузу';
                    btn.disabled = false;
                }}
            }}

            async function resumeBroadcast() {{
                if (!confirm('▶️ Продолжить рассылку #${{jobId}}?')) return;

                const btn = document.getElementById('resumeBtn');
                btn.textContent = '⏳ Возобновление...';
                btn.disabled = true;

                try {{
                    const response = await fetch(`/broadcast/resume/${{jobId}}`, {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }}
                    }});
                    const data = await response.json();
                    if (data.success) {{
                        alert('✅ ' + data.message);
                        window.location.reload();
                    }} else {{
                        alert('❌ Ошибка: ' + data.error);
                    }}
                }} catch (e) {{
                    alert('❌ Ошибка: ' + e.message);
                }} finally {{
                    btn.textContent = 'Продолжить рассылку';
                    btn.disabled = false;
                }}
            }}

            async function archiveBroadcast() {{
                if (!confirm('📦 Переместить рассылку #${{jobId}} в архив? История сохранится.')) return;

                const btn = document.getElementById('archiveBtn');
                btn.textContent = '⏳ Архивация...';
                btn.disabled = true;

                try {{
                    const response = await fetch(`/broadcast/archive/${{jobId}}`, {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }}
                    }});
                    const data = await response.json();
                    if (data.success) {{
                        alert('✅ ' + data.message);
                        window.location.href = '/broadcast/list';
                    }} else {{
                        alert('❌ Ошибка: ' + data.error);
                    }}
                }} catch (e) {{
                    alert('❌ Ошибка: ' + e.message);
                }} finally {{
                    btn.textContent = 'Архивировать задачу';
                    btn.disabled = false;
                }}
            }}

            // Обновляем каждые 5 секунд
            setInterval(updateStatus, 5000);
            updateStatus();
        </script>
        '''
    
    html = f'''
    <div class="page-heading">
        <div>
            <h1>Рассылки</h1>
            <p>Создавайте сообщения и отслеживайте выполнение каждой задачи.</p>
        </div>
        <div class="page-actions">
            <a href="/broadcast/list" class="button button-secondary">История рассылок</a>
        </div>
    </div>
    <div class="stats">
        <div class="stat-card"><span class="icon">◎</span><div class="stat-number">{total_users}</div><div class="stat-label">Доступно получателей</div></div>
    </div>
    {result_html}
    <div class="form-container" style="margin-top:14px;">
        <div class="panel-title">Новая рассылка</div>
        <p class="panel-copy" style="margin-bottom:18px;">Сообщение будет поставлено в очередь для всех доступных пользователей.</p>
        <form action="/broadcast/send" method="post">
            <label for="message">Текст сообщения</label>
            <textarea id="message" name="message" rows="5" placeholder="Введите текст рассылки..." required></textarea>
            <div class="mt-20">
                <button type="submit">Запустить рассылку</button>
            </div>
        </form>
    </div>
    '''
    
    menu = MENU.replace('📢 Рассылка', '<span style="color:#7c3aed; text-shadow: 0 0 30px #7c3aed44;">📢 Рассылка</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))

@app.post("/broadcast/send")
async def send_broadcast(request: Request, message: str = Form(...)):
    if not message or len(message.strip()) == 0:
        return RedirectResponse(url="/broadcast?error=empty", status_code=303)

    try:
        async with database.connection() as conn:
            async with conn.transaction():
                job_id = await conn.fetchval(
                    """
                    INSERT INTO broadcast_jobs (message, status, created_at)
                    VALUES ($1, 'pending', NOW())
                    RETURNING id
                    """,
                    message.strip(),
                )
                await conn.execute(
                    """
                    INSERT INTO broadcast_job_recipients (job_id, user_id)
                    SELECT $1, users.telegram_id
                    FROM users
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM unsubscribed_users
                        WHERE unsubscribed_users.telegram_id = users.telegram_id
                    )
                    AND NOT EXISTS (
                        SELECT 1
                        FROM blocked_users
                        WHERE blocked_users.telegram_id = users.telegram_id
                    )
                    ON CONFLICT (job_id, user_id) DO NOTHING
                    """,
                    job_id,
                )
                total_users = await conn.fetchval(
                    "SELECT COUNT(*) FROM broadcast_job_recipients WHERE job_id = $1",
                    job_id,
                )
                if not total_users:
                    await conn.execute("DELETE FROM broadcast_jobs WHERE id = $1", job_id)
                    return RedirectResponse(url="/broadcast?error=no_users", status_code=303)
                await conn.execute(
                    "UPDATE broadcast_jobs SET total_users = $2 WHERE id = $1",
                    job_id,
                    total_users,
                )
    except Exception as e:
        print(f"❌ Ошибка отправки рассылки: {e}")
        return RedirectResponse(url="/broadcast?error=send_failed", status_code=303)
    
    return RedirectResponse(url="/broadcast?success=1", status_code=303)


@app.get("/broadcast/status/{job_id}")
async def get_broadcast_status(job_id: int):
    try:
        conn = await database.get_conn()
        try:
            row = await conn.fetchrow(
                "SELECT status, total_users, sent_users, failed_users, blocked_users, progress FROM broadcast_jobs WHERE id = $1",
                job_id
            )
            if row:
                return {
                    "success": True,
                    "status": row['status'],
                    "total_users": row['total_users'],
                    "sent_users": row['sent_users'],
                    "failed_users": row['failed_users'],
                    "blocked_users": row['blocked_users'],
                    "progress": float(row['progress']) if row['progress'] else 0
                }
            return {"success": False, "error": "Job not found"}
        finally:
            await database.release_conn(conn)
    except Exception as e:
        print(f"❌ Ошибка получения статуса рассылки: {e}")
        return {"success": False, "error": str(e)}


async def get_job_status(job_id: int):
    async with database.connection() as conn:
        row = await conn.fetchrow("SELECT * FROM broadcast_jobs WHERE id = $1", job_id)
        return dict(row) if row else None

@app.post("/broadcast/pause/{job_id}")
async def pause_broadcast(job_id: int):
    async with database.connection() as conn:
        async with conn.transaction():
            job = await conn.fetchrow("SELECT status FROM broadcast_jobs WHERE id = $1 FOR UPDATE", job_id)
            if not job:
                return JSONResponse(
                    content={"success": False, "error": "Задача не найдена"},
                    status_code=404
                )

            if job['status'] not in ('running', 'pending'):
                return JSONResponse(
                    content={"success": False, "error": f"Нельзя поставить на паузу задачу со статусом {job['status']}"},
                    status_code=400
                )

            await conn.execute(
                "UPDATE broadcast_jobs SET status = 'paused', paused_at = NOW() WHERE id = $1",
                job_id
            )
            await conn.execute(
                """
                UPDATE broadcast_job_recipients
                SET status = 'pending', updated_at = NOW()
                WHERE job_id = $1 AND status = 'processing'
                """,
                job_id,
            )
        
        return JSONResponse(
            content={
                "success": True,
                "message": f"Рассылка #{job_id} поставлена на паузу",
                "job_id": job_id
            }
        )

@app.post("/broadcast/resume/{job_id}")
async def resume_broadcast(job_id: int):
    """Resume a paused broadcast and return any leased recipients to the queue."""
    async with database.connection() as conn:
        async with conn.transaction():
            job = await conn.fetchrow(
                "SELECT status FROM broadcast_jobs WHERE id = $1 FOR UPDATE",
                job_id,
            )
            if not job:
                return JSONResponse(
                    content={"success": False, "error": "Задача не найдена"},
                    status_code=404,
                )

            if job["status"] != "paused":
                return JSONResponse(
                    content={
                        "success": False,
                        "error": f"Нельзя возобновить задачу со статусом {job['status']}",
                    },
                    status_code=400,
                )

            await conn.execute(
                """
                UPDATE broadcast_jobs
                SET status = 'pending', paused_at = NULL, heartbeat_at = NULL
                WHERE id = $1
                """,
                job_id,
            )
            await conn.execute(
                """
                UPDATE broadcast_job_recipients
                SET status = 'pending', updated_at = NOW()
                WHERE job_id = $1 AND status = 'processing'
                """,
                job_id,
            )

        return JSONResponse(
            content={
                "success": True,
                "message": f"Рассылка #{job_id} возобновлена",
                "job_id": job_id,
            }
        )

@app.post("/broadcast/archive/{job_id}")
async def archive_broadcast(job_id: int):
    async with database.connection() as conn:
        async with conn.transaction():
            job = await conn.fetchrow("SELECT id FROM broadcast_jobs WHERE id = $1 FOR UPDATE", job_id)
            if not job:
                return JSONResponse(
                    content={"success": False, "error": "Задача не найдена"},
                    status_code=404
                )

            await conn.execute(
                """
                UPDATE broadcast_jobs
                SET status = 'archived', archived_at = NOW(), heartbeat_at = NULL
                WHERE id = $1
                """,
                job_id,
            )
            await conn.execute(
                """
                UPDATE broadcast_job_recipients
                SET status = 'cancelled', updated_at = NOW()
                WHERE job_id = $1 AND status = 'processing'
                """,
                job_id,
            )

        return JSONResponse(
            content={
                "success": True,
                "message": f"Рассылка #{job_id} перемещена в архив",
                "job_id": job_id
            }
        )

@app.post("/subscription/block/{user_id}")
async def block_subscription(user_id: int):
    """Блокирует подписку пользователя (меняет статус на 'cancelled')"""
    try:
        conn = await database.get_conn()
        try:
            # Проверяем, есть ли подписка
            row = await conn.fetchrow(
                "SELECT id, status FROM subscriptions WHERE user_id = $1 ORDER BY created_at DESC LIMIT 1",
                user_id
            )
            if not row:
                return JSONResponse(
                    content={"success": False, "error": "Подписка не найдена"},
                    status_code=404
                )
            
            if row['status'] == 'cancelled':
                return JSONResponse(
                    content={"success": False, "error": "Подписка уже заблокирована"},
                    status_code=400
                )
            
            # Сохраняем полный снимок подписки и переводим её в cancelled
            # одной транзакцией внутри database.unsubscribe_user.
            user_data = await database.get_user(user_id)
            success = await database.unsubscribe_user(
                user_id,
                user_data.get('username', '') if user_data else '',
                user_data.get('first_name', '') if user_data else '',
                'admin_block'
            )
            if not success:
                return JSONResponse(
                    content={"success": False, "error": "Не удалось сохранить данные отписки"},
                    status_code=500
                )
            
            return JSONResponse(
                content={"success": True, "message": f"Подписка пользователя {user_id} заблокирована"}
            )
        finally:
            await database.release_conn(conn)
    except Exception as e:
        print(f"❌ Ошибка блокировки подписки: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )


@app.get("/broadcast/list", response_class=HTMLResponse)
async def broadcast_list(request: Request):
    try:
        conn = await database.get_conn()
        try:
            rows = await conn.fetch("""
                SELECT * FROM broadcast_jobs 
                ORDER BY created_at DESC 
                LIMIT 50
            """)
        finally:
            await database.release_conn(conn)
    except Exception as e:
        print(f"❌ Ошибка загрузки истории: {e}")
        rows = []
    
    html = '''
    <div class="page-heading">
        <div>
            <h1>История рассылок</h1>
            <p>Последние 50 задач с итоговыми статусами и прогрессом.</p>
        </div>
        <div class="page-actions">
            <a href="/broadcast" class="button">Новая рассылка</a>
        </div>
    </div>
    '''
    
    if rows:
        html += '<div class="table-container"><table><tr><th>ID</th><th>Статус</th><th>Всего</th><th>Отправлено</th><th>Ошибок</th><th>Прогресс</th><th>Создана</th><th>Действие</th></tr>'
        for row in rows:
            status_color = {
                'pending': '#8899aa',
                'running': '#7c3aed',
                'paused': '#ffaa44',
                'completed': '#44ff88',
                'failed': '#ff4488',
                'cancelled': '#ff6644',
                'archived': '#708090'
            }.get(row['status'], '#8899aa')
            
            status_labels = {
                'pending': '⏳ Ожидание',
                'running': '🔄 Выполняется',
                'paused': '⏸️ Пауза',
                'completed': '✅ Завершено',
                'failed': '❌ Ошибка',
                'cancelled': '⛔ Остановлена',
                'archived': '📦 Архив'
            }.get(row['status'], row['status'])
            
            html += f'''
            <tr>
                <td>{row['id']}</td>
                <td><span style="color:{status_color};">{status_labels}</span></td>
                <td>{row['total_users']}</td>
                <td>{row['sent_users']}</td>
                <td>{row['failed_users']}</td>
                <td>{row['progress']:.1f}%</td>
                <td>{format_admin_datetime(row['created_at'])}</td>
                <td><a href="/broadcast?job={row['id']}" style="font-weight:700; text-decoration:none;">Подробнее →</a></td>
            </tr>
            '''
        html += '</table></div>'
    else:
        html += '<div class="empty">😕 Рассылок пока нет</div>'
    
    menu = MENU.replace('📢 Рассылка', '<span style="color:#7c3aed; text-shadow: 0 0 30px #7c3aed44;">📢 Рассылка</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))

@app.post("/unsubscribed/restore/{user_id}")
async def restore_subscription(user_id: int):
    try:
        result = await database.restore_unsubscribed_user(user_id)
        if result is None:
            return JSONResponse(
                content={"success": False, "error": "Пользователь не найден в отписавшихся"},
                status_code=404,
            )

        restored_count = result["restored_count"]
        return JSONResponse(
            content={
                "success": True,
                "message": f"Подписка пользователя {user_id} восстановлена ({restored_count} записей)",
                "restored_count": restored_count,
            }
        )
    except Exception as e:
        print(f"❌ Ошибка восстановления подписки: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500,
        )

@app.post("/subscription/block/{user_id}")
async def block_subscription(user_id: int):
    try:
        conn = await database.get_conn()
        try:
            # ===== 1. Проверяем, есть ли подписка =====
            row = await conn.fetchrow(
                "SELECT id, card_token, subscription_id, status FROM subscriptions WHERE user_id = $1 ORDER BY created_at DESC LIMIT 1",
                user_id
            )
            if not row:
                return JSONResponse(
                    content={"success": False, "error": "Подписка не найдена"},
                    status_code=404
                )
            
            if row['status'] == 'cancelled':
                return JSONResponse(
                    content={"success": False, "error": "Подписка уже заблокирована"},
                    status_code=400
                )
            
            user_data = await database.get_user(user_id)
            username = user_data.get('username', '') if user_data else ''
            first_name = user_data.get('first_name', '') if user_data else ''
            
            subscription_id = row.get('subscription_id')
            if subscription_id:
                try:
                    from cloudpayments import CloudPaymentsClient
                    cp_client = CloudPaymentsClient()
                    result = await cp_client.cancel_subscription(subscription_id)
                    if result["success"]:
                        print(f"✅ Отменена подписка в CloudPayments для {user_id}")
                    else:
                        print(f"❌ Ошибка отмены подписки в CloudPayments: {result.get('error')}")
                except Exception as e:
                    print(f"❌ Ошибка при отмене подписки: {e}")
            
            # Сохраняем полный снимок подписки и переводим её в cancelled
            # централизованно, чтобы ручная блокировка не теряла поля.
            success = await database.unsubscribe_user(
                user_id,
                username,
                first_name,
                'admin_block'
            )
            if not success:
                return JSONResponse(
                    content={"success": False, "error": "Не удалось сохранить данные отписки"},
                    status_code=500
                )
            
            return JSONResponse(
                content={"success": True, "message": f"Подписка пользователя {user_id} заблокирована"}
            )
        finally:
            await database.release_conn(conn)
    except Exception as e:
        print(f"❌ Ошибка блокировки подписки: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )

@app.get("/subgram", response_class=HTMLResponse)
async def subgram_page(request: Request):
    try:
        print("🔥 SUBGRAM PAGE STARTED")
        balance_data = await database.get_subgram_balance()
        print(f"🔥 balance_data: {balance_data}")
        
        balance = 0
        bots_info = []
        error_message = None
        
        if balance_data.get("status") == "ok":
            balance = balance_data.get("balance", 0)
            bots_info = balance_data.get("bots_info", [])
        else:
            error_message = balance_data.get("message", "Неизвестная ошибка")

        bot_info = bots_info[0] if bots_info else None

        html = f'''
        <div class="page-title">📊 SubGram Статистика</div>

        <div class="stats">
            <div class="stat-card">
                <div class="stat-number">{balance} ₽</div>
                <div class="stat-label">Баланс аккаунта</div>
            </div>
        '''

        if bot_info:
            html += f'''
            <div class="stat-card">
                <div class="stat-number">{bot_info.get("total_followers", 0)}</div>
                <div class="stat-label">Подписчиков приведено</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">{bot_info.get("revenue", 0)} ₽</div>
                <div class="stat-label">Заработано</div>
            </div>
            '''

        html += '</div>'

        if error_message:
            html += f'<div class="empty" style="color:#ff4488;">❌ Ошибка: {error_message}</div>'

        if not bots_info:
            html += '<div class="empty">😕 Нет данных по ботам или они не подключены.</div>'
        
        menu = MENU.replace('📊 SubGram', '<span style="color:#ffaa44; text-shadow: 0 0 30px #ffaa4444;">📊 SubGram</span>')
        return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))
        
    except Exception as e:
        print(f"🔥 КРИТИЧЕСКАЯ ОШИБКА: {e}")
        import traceback
        traceback.print_exc()
        return HTMLResponse(content=f"<h1>❌ Ошибка</h1><pre>{traceback.format_exc()}</pre>")

@app.get("/payment-visits", response_class=HTMLResponse)
async def payment_link_visits_page(request: Request):
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except (TypeError, ValueError):
        page = 1

    page_size = 25
    total_people = 0
    total_visits = 0
    rows = []
    error = None

    try:
        conn = await database.get_conn()
        try:
            total_people = int(await conn.fetchval(
                "SELECT COUNT(*) FROM payment_link_visits"
            ) or 0)
            total_visits = int(await conn.fetchval(
                "SELECT COALESCE(SUM(visit_count), 0) FROM payment_link_visits"
            ) or 0)
            rows = await conn.fetch(
                """
                SELECT p.user_id, p.visit_count, p.first_visited_at, p.last_visited_at,
                       u.username, u.first_name
                FROM payment_link_visits p
                LEFT JOIN users u ON u.telegram_id = p.user_id
                ORDER BY p.last_visited_at DESC
                LIMIT $1 OFFSET $2
                """,
                page_size,
                (page - 1) * page_size,
            )
        finally:
            await database.release_conn(conn)
    except Exception as exc:
        print(f"❌ Ошибка загрузки переходов к оплате: {exc}", flush=True)
        error = "Не удалось загрузить переходы к оплате"

    total_pages = max(1, (total_people + page_size - 1) // page_size)
    if page > total_pages:
        page = total_pages

    html = f'''
    <div class="page-heading">
        <div>
            <h1>↗ Переходы к оплате</h1>
            <p>Пользователи, которые открывали свою ссылку оплаты. Переход не означает успешную оплату.</p>
        </div>
        <div class="page-actions">
            <a class="button button-secondary" href="/payment-visits">Обновить</a>
        </div>
    </div>
    <div class="stats">
        <div class="stat-card"><div class="stat-number">{total_people}</div><div class="stat-label">Уникальных пользователей</div></div>
        <div class="stat-card"><div class="stat-number">{total_visits}</div><div class="stat-label">Всего переходов</div></div>
    </div>
    '''

    if error:
        html += f'<div class="empty">⚠️ {escape(error)}</div>'
    elif rows:
        html += '''
        <div class="table-container">
            <table>
                <tr><th>Пользователь</th><th>Telegram ID</th><th>Переходов</th><th>Первый переход</th><th>Последний переход</th></tr>
        '''
        for row in rows:
            username = row.get("username")
            first_name = row.get("first_name")
            if username:
                person = "@" + escape(str(username))
            elif first_name:
                person = escape(str(first_name))
            else:
                person = "Без имени"
            user_id = escape(str(row["user_id"]))
            first_visit = format_admin_datetime(row["first_visited_at"], fallback="—")
            last_visit = format_admin_datetime(row["last_visited_at"], fallback="—")
            html += f'''
                <tr>
                    <td><strong>{person}</strong></td>
                    <td>{user_id}</td>
                    <td>{int(row["visit_count"] or 0)}</td>
                    <td>{first_visit}</td>
                    <td>{last_visit}</td>
                </tr>
            '''
        html += '</table></div>'
        previous = f'<a class="button button-secondary" href="/payment-visits?page={page - 1}">← Предыдущая</a>' if page > 1 else ''
        next_page = f'<a class="button button-secondary" href="/payment-visits?page={page + 1}">Следующая →</a>' if page < total_pages else ''
        html += f'<div class="page-actions" style="justify-content:space-between;margin-top:16px;"><span class="panel-copy">Страница {page} из {total_pages}</span><div style="display:flex;gap:8px;">{previous}{next_page}</div></div>'
    else:
        html += '<div class="empty">Переходов к оплате пока нет.</div>'

    menu = MENU.replace('Переходы к оплате', '<span style="color:#7c3aed; text-shadow: 0 0 30px #7c3aed44;">Переходы к оплате</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))


@app.get("/payments", response_class=HTMLResponse)
async def payments_page(request: Request):
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except (TypeError, ValueError):
        page = 1

    page_size = 20
    payments = []
    all_payments = []
    has_next_page = False
    api_error = None
    try:
        now = datetime.now(ADMIN_TIMEZONE)
        period_end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        period_start = period_end - timedelta(days=1)

        # All user payments must belong to a user known by the bot. Starting
        # from the first local record avoids thousands of empty API requests
        # while still covering the complete history of this project.
        conn = await database.get_conn()
        try:
            first_record = await conn.fetchval(
                """
                SELECT LEAST(
                    COALESCE((SELECT MIN(registered_at) FROM users), NOW()),
                    COALESCE((SELECT MIN(created_at) FROM subscriptions), NOW())
                )
                """
            )
        finally:
            await database.release_conn(conn)

        if first_record:
            if first_record.tzinfo is None:
                # Legacy TIMESTAMP columns store UTC wall-clock values.
                first_record = first_record.replace(tzinfo=timezone.utc)
            period_start = first_record.astimezone(now.tzinfo).replace(
                hour=0, minute=0, second=0, microsecond=0
            ) - timedelta(days=1)

        result = await CloudPaymentsClient().list_payments_all_time(
            created_date_gte=period_start,
            created_date_lte=period_end,
        )
        if result.get("success"):
            all_payments = result.get("payments", [])
            page_offset = (page - 1) * page_size
            payments = all_payments[page_offset:page_offset + page_size]
            has_next_page = page_offset + page_size < len(all_payments)
        else:
            api_error = result.get("error") or "Не удалось получить операции"
    except Exception as e:
        print(f"❌ Ошибка загрузки операций CloudPayments: {e}", flush=True)
        api_error = "CloudPayments временно недоступен"

    successful_payments = sum(1 for item in all_payments if item.get("Status") == "Completed")
    failed_payments = len(all_payments) - successful_payments

    html = f'''
    <div class="page-heading">
        <div>
            <h1>💳 Платежи CloudPayments</h1>
            <p>Все операции из CloudPayments за всё время. Показаны успешные и неуспешные транзакции.</p>
        </div>
        <div class="page-actions">
            <a class="button button-secondary" href="/payments">Обновить</a>
        </div>
    </div>

    <div class="stats">
        <div class="stat-card"><div class="stat-number" style="color:var(--success);">{successful_payments}</div><div class="stat-label">✅ Успешных за всё время</div></div>
        <div class="stat-card"><div class="stat-number" style="color:var(--danger);">{failed_payments}</div><div class="stat-label">❌ Неуспешных за всё время</div></div>
    </div>
    '''

    if api_error:
        html += f'<div class="empty">⚠️ Не удалось получить операции CloudPayments: {escape(str(api_error))}</div>'
    elif payments:
        html += '''
        <div class="table-container">
            <table>
                <tr><th>Пользователь</th><th>Сумма</th><th>Статус</th><th>Дата</th><th>TransactionId</th></tr>
        '''
        for payment in payments:
            account_id = escape(str(payment.get("AccountId") or "—"))
            transaction_id = escape(str(payment.get("TransactionId") or "—"))
            status = str(payment.get("Status") or "Unknown")
            status_text = "✅ Успешный" if status == "Completed" else f"❌ {status}"
            status_class = "status-ok" if status == "Completed" else "status-down"
            amount = float(payment.get("Amount") or 0)
            created_at = format_admin_datetime(
                payment.get("CreatedDateIso") or payment.get("CreatedDate"),
                fallback="—",
            )
            created_at = escape(created_at)
            html += f'''
                <tr>
                    <td><strong>{account_id}</strong></td>
                    <td>{amount:.2f} ₽</td>
                    <td><span class="status-pill {status_class}">{escape(status_text)}</span></td>
                    <td>{created_at}</td>
                    <td>{transaction_id}</td>
                </tr>
            '''
        html += '</table></div>'
        previous = f'<a class="button button-secondary" href="/payments?page={page - 1}">← Предыдущая</a>' if page > 1 else ''
        next_page = f'<a class="button button-secondary" href="/payments?page={page + 1}">Следующая →</a>' if has_next_page else ''
        html += f'<div class="page-actions" style="justify-content:space-between;margin-top:16px;"><span class="panel-copy">Страница {page}</span><div style="display:flex;gap:8px;">{previous}{next_page}</div></div>'
    else:
        html += '<div class="empty">За всё время операций CloudPayments нет.</div>'

    menu = MENU.replace('💳 Платежи', '<span style="color:#7c3aed; text-shadow: 0 0 30px #7c3aed44;">💳 Платежи</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=ADMIN_BIND_HOST, port=ADMIN_PORT)
