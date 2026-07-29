from fastapi import FastAPI
import uvicorn
import logging
from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime

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

async def daily_retry_job():
    logger.info("Running daily retry job for all users in RETRY_LOW status (infinite attempts)")
    
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
            
async def check_expired_subscriptions_job():

    logger.info("🔄 Running expired subscriptions check...")
    result = await subscription_manager.check_expired_subscriptions()
    
    if result.get("success"):
        count = result.get("expired_count", 0)
        if count > 0:
            logger.info(f"✅ Deactivated {count} expired subscriptions")
        else:
            logger.debug("✅ No expired subscriptions found")
    else:
        logger.error(f"❌ Error checking expired subscriptions: {result.get('error')}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.add_job(
        daily_retry_job,
        trigger=CronTrigger(hour=0, minute=0),
        id="daily_retry",
        replace_existing=True
    )
    
    scheduler.add_job(
        check_expired_subscriptions_job,
        trigger='interval',
        minutes=5,
        id="check_expired",
        replace_existing=True,
        next_run_time=datetime.now() 
    )
    
    scheduler.start()
    logger.info("🚀 Scheduler started")
    logger.info("  - Daily retry: 00:00 every day")
    logger.info("  - Expired check: every 5 minutes")
    
    yield
    
    scheduler.shutdown()
    logger.info("🛑 Scheduler stopped")


app = FastAPI(
    title="CloudPayments Subscription Bot",
    description="Telegram bot subscription management with cascade payments and infinite retries",
    version="2.1.0",
    lifespan=lifespan
)

app.include_router(router)

@app.get("/")
async def root():
    return {
        "status": "ok",
        "message": "CloudPayments Cascade Subscription Bot is running",
        "retry_policy": "Infinite retries for LOW plan (99₽)",
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
