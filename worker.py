import asyncio
import html
import os

import httpx
from dotenv import load_dotenv

import database

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
BATCH_SIZE = 50
STALE_AFTER_MINUTES = 15
MAX_SEND_ATTEMPTS = 3
RETRY_BASE_SECONDS = 1.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


async def send_telegram_message(client, user_id: int, message_text: str, keyboard: dict):
    """Send one message, retrying transient Telegram/network failures."""
    payload = {
        "chat_id": user_id,
        "text": message_text,
        "parse_mode": "HTML",
        "reply_markup": keyboard,
    }
    last_error = None

    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            response = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json=payload,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            last_error = exc
            if attempt == MAX_SEND_ATTEMPTS:
                raise
            await asyncio.sleep(RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
            continue

        if response.status_code not in RETRYABLE_STATUS_CODES:
            return response
        if attempt == MAX_SEND_ATTEMPTS:
            return response

        delay = RETRY_BASE_SECONDS * (2 ** (attempt - 1))
        if response.status_code == 429:
            try:
                retry_after = response.json().get("parameters", {}).get("retry_after")
                if retry_after is not None:
                    delay = max(delay, float(retry_after))
            except (ValueError, TypeError):
                pass
        await asyncio.sleep(min(delay, 60.0))

    if last_error:
        raise last_error
    raise RuntimeError("Telegram request failed without a response")

async def get_job_status(job_id: int) -> str | None:
    async with database.connection() as conn:
        return await conn.fetchval(
            "SELECT status FROM broadcast_jobs WHERE id = $1",
            job_id,
        )


async def touch_service_heartbeat() -> None:
    """Record that this worker process is alive for the admin health check."""
    async with database.connection() as conn:
        await conn.execute(
            """
            INSERT INTO service_heartbeats (service, heartbeat_at)
            VALUES ('worker', NOW())
            ON CONFLICT (service)
            DO UPDATE SET heartbeat_at = EXCLUDED.heartbeat_at
            """
        )


async def recover_stale_work() -> None:
    """Make jobs abandoned by a crashed worker available again.

    Recipient rows are the idempotency barrier: already sent rows remain sent,
    while rows left in ``processing`` are returned to ``pending`` after the
    lease expires.
    """
    async with database.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE broadcast_jobs
                SET status = 'pending', started_at = NULL, heartbeat_at = NULL
                WHERE status = 'running'
                  AND COALESCE(heartbeat_at, started_at) <
                      NOW() - ($1 * INTERVAL '1 minute')
                """,
                STALE_AFTER_MINUTES,
            )
            await conn.execute(
                """
                UPDATE broadcast_job_recipients
                SET status = 'pending', updated_at = NOW()
                FROM broadcast_jobs
                WHERE broadcast_jobs.id = broadcast_job_recipients.job_id
                  AND broadcast_jobs.status IN ('running', 'pending', 'paused')
                  AND broadcast_job_recipients.status = 'processing'
                  AND updated_at <
                      NOW() - ($1 * INTERVAL '1 minute')
                """,
                STALE_AFTER_MINUTES,
            )


async def claim_next_job():
    """Atomically claim one pending job, even with multiple worker processes."""
    async with database.connection() as conn:
        async with conn.transaction():
            return await conn.fetchrow(
                """
                WITH next_job AS (
                    SELECT id
                    FROM broadcast_jobs
                    WHERE status = 'pending'
                    ORDER BY created_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE broadcast_jobs AS jobs
                SET status = 'running',
                    started_at = COALESCE(jobs.started_at, NOW()),
                    heartbeat_at = NOW()
                FROM next_job
                WHERE jobs.id = next_job.id
                RETURNING jobs.*
                """
            )


async def claim_recipient_batch(job_id: int, limit: int = BATCH_SIZE):
    """Lease a bounded batch of pending recipients for this job."""
    async with database.connection() as conn:
        async with conn.transaction():
            return await conn.fetch(
                """
                WITH picked AS (
                    SELECT job_id, user_id
                    FROM broadcast_job_recipients
                    WHERE job_id = $1 AND status = 'pending'
                    ORDER BY user_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT $2
                )
                UPDATE broadcast_job_recipients AS recipients
                SET status = 'processing',
                    attempts = recipients.attempts + 1,
                    updated_at = NOW()
                FROM picked
                WHERE recipients.job_id = picked.job_id
                  AND recipients.user_id = picked.user_id
                RETURNING recipients.user_id
                """,
                job_id,
                limit,
            )


async def record_recipient_result(
    job_id: int,
    user_id: int,
    status: str,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    async with database.connection() as conn:
        await conn.execute(
            """
            UPDATE broadcast_job_recipients
            SET status = $3,
                error_code = $4,
                error_message = $5,
                sent_at = CASE WHEN $3 = 'sent' THEN NOW() ELSE sent_at END,
                updated_at = NOW()
            WHERE job_id = $1 AND user_id = $2
            """,
            job_id,
            user_id,
            status,
            error_code,
            (error_message or "")[:500] or None,
        )
        await conn.execute(
            "UPDATE broadcast_jobs SET heartbeat_at = NOW() WHERE id = $1 AND status = 'running'",
            job_id,
        )


async def release_processing(job_id: int, status: str = "pending") -> None:
    """Release a leased batch when an admin pauses or archives a job."""
    async with database.connection() as conn:
        await conn.execute(
            """
            UPDATE broadcast_job_recipients
            SET status = $2, updated_at = NOW()
            WHERE job_id = $1 AND status = 'processing'
            """,
            job_id,
            status,
        )


async def sync_job_counts(job_id: int, final_status: str | None = None):
    """Recalculate counters from recipient rows so restarts cannot lose progress."""
    async with database.connection() as conn:
        stats = await conn.fetchrow(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE status = 'sent') AS sent,
                COUNT(*) FILTER (WHERE status = 'failed') AS failed,
                COUNT(*) FILTER (WHERE status = 'blocked') AS blocked,
                COUNT(*) FILTER (WHERE status = 'pending') AS pending,
                COUNT(*) FILTER (WHERE status = 'processing') AS processing,
                COUNT(*) FILTER (WHERE status = 'cancelled') AS cancelled
            FROM broadcast_job_recipients
            WHERE job_id = $1
            """,
            job_id,
        )
        job_total = await conn.fetchval(
            "SELECT total_users FROM broadcast_jobs WHERE id = $1", job_id
        )
        total = max(int(job_total or 0), int(stats["total"] or 0))
        processed = (
            int(stats["sent"] or 0)
            + int(stats["failed"] or 0)
            + int(stats["blocked"] or 0)
            + int(stats["cancelled"] or 0)
        )
        progress = 100 if total and processed >= total else (processed / total * 100 if total else 0)
        await conn.execute(
            """
            UPDATE broadcast_jobs
            SET total_users = $2,
                sent_users = $3,
                failed_users = $4,
                blocked_users = $5,
                progress = $6,
                heartbeat_at = CASE
                    WHEN status = 'running' THEN NOW()
                    ELSE heartbeat_at
                END
            WHERE id = $1
            """,
            job_id,
            total,
            stats["sent"],
            stats["failed"],
            stats["blocked"],
            progress,
        )
        if final_status:
            await conn.execute(
                """
                UPDATE broadcast_jobs
                SET status = $2, finished_at = NOW(), heartbeat_at = NULL
                WHERE id = $1 AND status = 'running'
                """,
                job_id,
                final_status,
            )
        return stats


async def process_job(job):
    job_id = job["id"]
    message_text = (
        "✉️ Вам пришло анонимное сообщение:\n\n"
        f"{html.escape(str(job['message']), quote=False)}"
    )
    keyboard = {
        "inline_keyboard": [
            [{"text": "✍️ Ответить", "callback_data": "reply_anon"}],
            [{
                "text": "Узнать кто это",
                "callback_data": "who_is_it",
                "style": "primary",
                "icon_custom_emoji_id": "5424885441100782420",
            }],
        ]
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            current_status = await get_job_status(job_id)
            if current_status in {"paused", "cancelled", "archived"}:
                await release_processing(job_id, "pending" if current_status == "paused" else "cancelled")
                await sync_job_counts(job_id)
                print(f"⏸️ Задача {job_id} остановлена со статусом {current_status}", flush=True)
                return
            if current_status != "running":
                return

            batch = await claim_recipient_batch(job_id)
            if not batch:
                stats = await sync_job_counts(job_id)
                if not stats["pending"] and not stats["processing"]:
                    await sync_job_counts(job_id, final_status="completed")
                    print(f"✅ Задача {job_id} завершена", flush=True)
                    return
                await asyncio.sleep(1)
                continue

            for row in batch:
                user_id = row["user_id"]
                if await get_job_status(job_id) != "running":
                    break
                try:
                    await touch_service_heartbeat()
                    response = await send_telegram_message(
                        client,
                        user_id,
                        message_text,
                        keyboard,
                    )
                    if response.status_code == 200:
                        await record_recipient_result(job_id, user_id, "sent")
                        continue

                    try:
                        payload = response.json()
                    except ValueError:
                        payload = {}
                    description = str(payload.get("description") or response.text)
                    lowered = description.lower()
                    if response.status_code == 403 or "bot was blocked" in lowered or "forbidden" in lowered:
                        result_status = "blocked"
                        user_data = await database.get_user(user_id)
                        if user_data:
                            await database.add_blocked_user(
                                user_id,
                                user_data.get("username", ""),
                                user_data.get("first_name", ""),
                            )
                    else:
                        result_status = "failed"
                    await record_recipient_result(
                        job_id,
                        user_id,
                        result_status,
                        str(response.status_code),
                        description,
                    )
                except Exception as exc:
                    await record_recipient_result(job_id, user_id, "failed", "exception", str(exc))
                finally:
                    # Keep broadcast traffic below Telegram's global limit.
                    # 40 ms between attempts is at most 25 attempts per second.
                    await asyncio.sleep(0.04)

            await sync_job_counts(job_id)


async def main_loop():
    await database.ensure_broadcast_schema()
    await touch_service_heartbeat()
    await recover_stale_work()
    print("🚀 Воркер запущен, ждём задачи...", flush=True)
    while True:
        try:
            await touch_service_heartbeat()
            await recover_stale_work()
            job = await claim_next_job()
            if job:
                print(f"📨 Найдена задача {job['id']}, начинаем обработку...", flush=True)
                await process_job(job)
            else:
                await asyncio.sleep(5)
        except Exception as exc:
            print(f"❌ Ошибка worker: {exc}", flush=True)
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main_loop())
