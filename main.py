from fastapi import FastAPI
import uvicorn
import logging
from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from webhook_handler import router
from subscription import SubscriptionManager
from config import config

logging.getLogger('apscheduler').setLevel(logging.INFO)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

subscription_manager = SubscriptionManager()
scheduler = AsyncIOScheduler()

async def process_expired_subscriptions_job():

    logger.info("🔄 Checking for expired subscriptions...")
    result = await subscription_manager.check_expired_subscriptions()
    
    if result.get("success"):
        count = result.get("expired_count", 0)
        if count > 0:
            logger.info(f"✅ Processed {count} expired subscriptions")
            for r in result.get("results", []):
                logger.info(f"  - User {r['user_id']}: {r['result']}")
        else:
            logger.info("✅ No expired subscriptions found")
    else:
        logger.error(f"❌ Error: {result.get('error')}")

async def retry_low_job():

    logger.info("🔄 Processing retry_low users...")
    
    users = await subscription_manager.get_all_users_in_retry_low()
    
    if not users:
        logger.info("✅ No retry_low users found")
        return
    
    logger.info(f"🔍 Found {len(users)} users in retry_low")
    
    for user_id in users:
        result = await subscription_manager.process_expired_subscription(int(user_id))
        logger.info(f"📊 Result for {user_id}: {result}")

async def daily_retry_job():
    logger.info("Running daily retry job for all users in RETRY_LOW status")
    users = await subscription_manager.get_all_users_in_retry_low()
    
    if not users:
        logger.info("No users in RETRY_LOW status")
        return
    
    logger.info(f"Found {len(users)} users in RETRY_LOW status")
    
    for user_id in users:
        try:
            result = await subscription_manager.handle_daily_low_retry(user_id)
            if result.get("success"):
                logger.info(f"Daily retry triggered for user {user_id}")
            else:
                logger.warning(f"Failed to trigger daily retry for user {user_id}: {result.get('error')}")
        except Exception as e:
            logger.error(f"Error processing daily retry for user {user_id}: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):

    print("🔥🔥🔥 LIFESPAN IS RUNNING 🔥🔥🔥")
    logger.info("🚀 LIFESPAN STARTED")
    
    
    scheduler.add_job(
        process_expired_subscriptions_job,
        trigger=IntervalTrigger(minutes=5),
        id="process_expired",
        replace_existing=True
    )
    
    scheduler.add_job(
        retry_low_job,
        trigger=IntervalTrigger(hours=24),
        id="retry_low",
        replace_existing=True
    )
    
    scheduler.add_job(
        daily_retry_job,
        trigger=CronTrigger(hour=0, minute=0),
        id="daily_retry",
        replace_existing=True
    )
    
    scheduler.start()
    logger.info("🚀 Scheduler started")
    logger.info("  - Expired check: every 5 minutes (ACTIVE CHARGING)")
    logger.info("  - Retry LOW: every 24 hours")
    logger.info("  - Daily retry: 00:00 every day (legacy)")
    
    yield
    
    scheduler.shutdown()
    logger.info("🛑 Scheduler stopped")

app = FastAPI(
    title="CloudPayments Subscription Bot",
    description="Active subscription management with automatic charging",
    version="3.0.0",
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
        "scheduler_running": scheduler.running,
        "retry_low_count": len(retry_low_users)
    }

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=config.HOST,
        port=config.PORT,
        reload=True
    )
