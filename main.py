from fastapi import FastAPI
import uvicorn
import logging
from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from time_utils import now_msk

import database
from webhook_handler import router
from subscription import SubscriptionManager
from config import config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

subscription_manager = SubscriptionManager()
scheduler = AsyncIOScheduler()


async def process_expired_subscriptions_job():
    logger.info("🔄 Checking for expired subscriptions and retry_low charges...")
    result = await subscription_manager.check_expired_subscriptions()

    if result.get("success"):
        count = result.get("expired_count", 0)
        if count > 0:
            logger.info(f"✅ Processed {count} subscriptions ready for charging")
            for r in result.get("results", []):
                logger.info(f"  - User {r['user_id']}: {r['result']}")
        else:
            logger.info("✅ No subscriptions ready for charging")
    else:
        logger.error(f"❌ Error: {result.get('error')}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🔥🔥🔥 LIFESPAN IS RUNNING")
    logger.info("🚀 LIFESPAN STARTED")

    # Migrations for the active token-charge system. Safe to run on every startup.
    conn = await database.get_conn()
    try:
        await database.ensure_message_retention_schema(conn)
        await database.ensure_webhook_events_schema(conn)
        await conn.execute("""
            ALTER TABLE subscriptions
            ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMP WITH TIME ZONE
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_subscriptions_next_retry_at
            ON subscriptions(next_retry_at)
        """)
        await database.ensure_subscription_user_fk_protection(conn)
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
        logger.info("✅ retry_low/payment_events migrations checked")
    finally:
        await database.release_conn(conn)

    if config.PAYMENT_SCHEDULER_ENABLED:
        scheduler.add_job(
            process_expired_subscriptions_job,
            trigger=IntervalTrigger(minutes=5),
            id="process_expired",
            replace_existing=True
        )
        scheduler.start()
        logger.info("🚀 Scheduler started")
        logger.info("  - Expired ACTIVE check: every 5 minutes")
        logger.info("  - RETRY_LOW: handled by the same 5-minute check, but only when next_retry_at <= NOW()")
    else:
        logger.warning(
            "🛡️ PAYMENT_SCHEDULER_ENABLED=false: webhook API is active; automatic charges are disabled"
        )

    logger.info("  - Subscription extension: webhook only")
    logger.info("  - Recurrent webhook idempotency: payment_events.transaction_id UNIQUE")

    yield

    if scheduler.running:
        scheduler.shutdown()
        logger.info("🛑 Scheduler stopped")


app = FastAPI(
    title="CloudPayments Subscription Bot",
    description="Active subscription management with automatic charging",
    version="3.1.0",
    lifespan=lifespan
)

app.include_router(router)


@app.get("/")
async def root():
    return {
        "status": "ok",
        "message": "Active Subscription Bot is running",
        "system": "ACTIVE (code initiates charges)",
        "retry_policy": "Retry LOW (99₽) every 24 hours",
        "payment_flow": "scheduler initiates charge; webhook extends subscription",
        "plans": {
            "HIGH": {"amount": "699.00 RUB", "days": 7},
            "MEDIUM": {"amount": "299.00 RUB", "days": 3},
            "LOW": {"amount": "99.00 RUB", "days": 1}
        },
        "initial_payment": f"{config.INITIAL_PAYMENT/100} RUB"
    }


@app.get("/health")
async def health_check():
    retry_low_users = await subscription_manager.get_all_users_in_retry_low()
    return {
        "status": "healthy",
        "scheduler_enabled": config.PAYMENT_SCHEDULER_ENABLED,
        "scheduler_running": scheduler.running,
        "retry_low_count": len(retry_low_users)
    }


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=config.HOST,
        port=config.PORT,
        reload=False
    )
