import asyncio
import html
import os
import time

import httpx
from dotenv import load_dotenv

import database

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
BATCH_SIZE = 50
STATUS_CHECK_INTERVAL_SECONDS = 1.0
HEARTBEAT_INTERVAL_SECONDS = 30.0
STALE_AFTER_MINUTES = 15
MAX_SEND_ATTEMPTS = 3
RETRY_BASE_SECONDS = 1.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
VALENTINE_CUSTOM_EMOJI_ID = "5472019095106886003"


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


async def touch_job_heartbeat(job_id: int) -> None:
    """Keep an active broadcast lease alive during slow Telegram requests."""
    async with database.connection() as conn:
        await conn.execute(
            """
            UPDATE broadcast_jobs
            SET heartbeat_at = NOW()
            WHERE id = $1 AND status = 'running'
            """,
            job_id,
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


async def get_blocked_user_ids(user_ids: list[int]) -> set[int]:
    """Load the blocked marker for a whole leased batch in one query."""
    if not user_ids:
        return set()

    async with database.connection() as conn:
        rows = await conn.fetch(
            """
            SELECT telegram_id
            FROM blocked_users
            WHERE telegram_id = ANY($1::BIGINT[])
            """,
            user_ids,
        )
    return {int(row["telegram_id"]) for row in rows}


async def record_recipient_results(
    job_id: int,
    results: list[tuple[int, str, str | None, str | None]],
) -> dict[str, int]:
    """Persist a leased batch and advance job counters in one transaction.

    Only rows that are still ``processing`` are counted.  This protects the
    counters when an administrator pauses a job or stale-work recovery
    releases a lease while Telegram requests are still finishing.
    """
    if not results:
        return {}

    user_ids = [user_id for user_id, _, _, _ in results]
    statuses = [status for _, status, _, _ in results]
    error_codes = [error_code for _, _, error_code, _ in results]
    error_messages = [((error_message or "")[:500] or None) for _, _, _, error_message in results]

    async with database.connection() as conn:
        async with conn.transaction():
            changed = await conn.fetch(
                """
                WITH updates(user_id, status, error_code, error_message) AS (
                    SELECT *
                    FROM UNNEST(
                        $2::BIGINT[],
                        $3::TEXT[],
                        $4::TEXT[],
                        $5::TEXT[]
                    )
                ), changed AS (
                    UPDATE broadcast_job_recipients AS recipients
                    SET status = updates.status,
                        error_code = updates.error_code,
                        error_message = updates.error_message,
                        sent_at = CASE
                            WHEN updates.status = 'sent' THEN COALESCE(recipients.sent_at, NOW())
                            ELSE recipients.sent_at
                        END,
                        updated_at = NOW()
                    FROM updates
                    WHERE recipients.job_id = $1
                      AND recipients.user_id = updates.user_id
                      AND recipients.status = 'processing'
                    RETURNING recipients.status
                )
                SELECT status, COUNT(*)::INTEGER AS count
                FROM changed
                GROUP BY status
                """,
                job_id,
                user_ids,
                statuses,
                error_codes,
                error_messages,
            )
            counts = {str(row["status"]): int(row["count"]) for row in changed}
            sent = counts.get("sent", 0)
            failed = counts.get("failed", 0)
            blocked = counts.get("blocked", 0)
            if sent or failed or blocked:
                await conn.execute(
                    """
                    UPDATE broadcast_jobs AS jobs
                    SET sent_users = COALESCE(jobs.sent_users, 0) + $2,
                        failed_users = COALESCE(jobs.failed_users, 0) + $3,
                        blocked_users = COALESCE(jobs.blocked_users, 0) + $4,
                        progress = CASE
                            WHEN COALESCE(jobs.total_users, 0) > 0 THEN LEAST(
                                100.0,
                                (
                                    COALESCE(jobs.sent_users, 0) + $2
                                    + COALESCE(jobs.failed_users, 0) + $3
                                    + COALESCE(jobs.blocked_users, 0) + $4
                                ) * 100.0 / jobs.total_users
                            )
                            ELSE 0
                        END,
                        heartbeat_at = CASE
                            WHEN jobs.status = 'running' THEN NOW()
                            ELSE jobs.heartbeat_at
                        END
                    WHERE jobs.id = $1
                    """,
                    job_id,
                    sent,
                    failed,
                    blocked,
                )
            return counts


async def record_recipient_result(
    job_id: int,
    user_id: int,
    status: str,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    # Keep the single-result helper for compatibility with older call sites;
    # the worker uses record_recipient_results() for normal batch processing.
    await record_recipient_results(
        job_id,
        [(user_id, status, error_code, error_message)],
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
        f'<tg-emoji emoji-id="{VALENTINE_CUSTOM_EMOJI_ID}">💌</tg-emoji> '
        "<b>Вам пришло новое анонимное сообщение!</b>\n\n"
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

            await touch_service_heartbeat()
            blocked_ids = await get_blocked_user_ids([row["user_id"] for row in batch])
            results = []
            stopped_status = None
            next_status_check = time.monotonic() + STATUS_CHECK_INTERVAL_SECONDS
            next_heartbeat = time.monotonic() + HEARTBEAT_INTERVAL_SECONDS

            for row in batch:
                user_id = row["user_id"]
                if time.monotonic() >= next_heartbeat:
                    await touch_service_heartbeat()
                    await touch_job_heartbeat(job_id)
                    next_heartbeat = time.monotonic() + HEARTBEAT_INTERVAL_SECONDS
                if time.monotonic() >= next_status_check:
                    status_check = await get_job_status(job_id)
                    stopped_status = status_check if status_check else "missing"
                    next_status_check = time.monotonic() + STATUS_CHECK_INTERVAL_SECONDS
                if stopped_status and stopped_status != "running":
                    break
                try:
                    # A recipient may have been marked blocked after the job
                    # was created (or may belong to an older job whose
                    # audience was captured before blocked_users filtering).
                    # Mark it as blocked without calling Telegram.  This keeps
                    # the job progressing while preserving the user's
                    # subscription and billing data.
                    if user_id in blocked_ids:
                        results.append((user_id, "blocked", None, None))
                        continue
                    response = await send_telegram_message(
                        client,
                        user_id,
                        message_text,
                        keyboard,
                    )
                    if response.status_code == 200:
                        results.append((user_id, "sent", None, None))
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
                    results.append(
                        (
                            user_id,
                            result_status,
                            str(response.status_code),
                            description,
                        )
                    )
                except Exception as exc:
                    results.append((user_id, "failed", "exception", str(exc)))
                finally:
                    # Keep broadcast traffic below Telegram's global limit.
                    # 40 ms between attempts is at most 25 attempts per second.
                    await asyncio.sleep(0.04)

            await record_recipient_results(job_id, results)
            if stopped_status and stopped_status != "running":
                release_status = "pending" if stopped_status in {"paused", "pending"} else "cancelled"
                await release_processing(job_id, release_status)
                await sync_job_counts(job_id)
                print(f"⏸️ Задача {job_id} остановлена со статусом {stopped_status}", flush=True)
                return


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
