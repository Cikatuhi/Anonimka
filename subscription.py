import json
from typing import Dict, Optional, List
from datetime import datetime, timedelta
import logging
import sys
import os

sys.path.append('/root/Anonimno-Artem-')
import database

from config import config
from models import PlanType, SubscriptionStatus
from cloudpayments import CloudPaymentsClient

logger = logging.getLogger(__name__)

class SubscriptionManager:
    
    def __init__(self):
        self.client = CloudPaymentsClient()
        
        self.plan_amounts = {
            "HIGH": config.SUBSCRIPTION_PLANS["HIGH"][0],
            "MEDIUM": config.SUBSCRIPTION_PLANS["MEDIUM"][0],
            "LOW": config.SUBSCRIPTION_PLANS["LOW"][0]
        }
        self.plan_days = {
            "HIGH": config.SUBSCRIPTION_PLANS["HIGH"][1],
            "MEDIUM": config.SUBSCRIPTION_PLANS["MEDIUM"][1],
            "LOW": config.SUBSCRIPTION_PLANS["LOW"][1]
        }
    
    async def get_user_subscription(self, user_id: str) -> Optional[dict]:
        try:
            user_id_int = int(user_id)
            return await database.get_subscription_by_user_id(user_id_int)
        except Exception as e:
            logger.error(f"Error getting subscription for user {user_id}: {e}")
            return None
    
    async def get_all_users_in_retry_low(self) -> List[str]:
        try:
            users = await database.get_all_users_in_retry_low()
            return [str(user) for user in users]
        except Exception as e:
            logger.error(f"Error getting retry_low users: {e}")
            return []
    
    async def create_user_subscription(self, user_id: str, card_token: str) -> dict:
        try:
            user_id_int = int(user_id)
            await database.create_subscription(
                user_id=user_id_int,
                plan="HIGH",
                status="inactive",
                end_date=None,
                card_token=card_token
            )
            logger.info(f"Created new subscription for user {user_id}")
            return await self.get_user_subscription(user_id)
        except Exception as e:
            logger.error(f"Error creating subscription for user {user_id}: {e}")
            return None
    
    async def handle_initial_payment(self, user_id: str, card_token: str) -> Dict:
        try:
            subscription = await self.get_user_subscription(user_id)
            if not subscription:
                subscription = await self.create_user_subscription(user_id, card_token)
            
            if not card_token:
                logger.error(f"User {user_id}: No card token provided!")
                return {
                    "success": False,
                    "error": "No card token provided. Please pay first."
                }
            
            result = await self.client.create_subscription(
                token=card_token,
                account_id=user_id,
                amount=config.SUBSCRIPTION_PLANS["HIGH"][0],
                interval_days=config.BILLING_INTERVAL_DAYS,
                max_periods=1
            )
            
            if result["success"]:
                await database.save_cloudpayments_subscription_id(
                    user_id=int(user_id),
                    subscription_id=result["subscription_id"]
                )
                
                end_date = datetime.utcnow() + timedelta(days=1)
                await database.update_subscription_status(
                    user_id=int(user_id),
                    status="active",
                    end_date=end_date
                )
                await database.update_subscription_plan(
                    user_id=int(user_id),
                    plan="HIGH"
                )
                
                logger.info(f"User {user_id}: Initial payment successful, subscription active until {end_date}")
                
                return {
                    "success": True,
                    "message": "Subscription activated for 1 day",
                    "subscription_id": result["subscription_id"],
                    "access_until": end_date.isoformat(),
                    "next_amount": config.SUBSCRIPTION_PLANS["HIGH"][0] / 100,
                    "next_plan": "HIGH"
                }
            else:
                logger.error(f"User {user_id}: Failed to create subscription: {result.get('error')}")
                return {
                    "success": False,
                    "error": result.get("error", "Failed to create subscription")
                }
        except Exception as e:
            logger.error(f"Error in handle_initial_payment: {e}")
            return {"success": False, "error": str(e)}
    
    async def handle_payment_webhook(self, payload: dict) -> Dict:
        user_id = payload.get("AccountId")
        if not user_id:
            logger.error("No AccountId in webhook")
            return {"success": False, "error": "No AccountId"}
        
        subscription = await self.get_user_subscription(user_id)
        if not subscription:
            logger.error(f"Subscription not found for user {user_id}")
            return {"success": False, "error": "Subscription not found"}
        
        is_successful = payload.get("Status") == "Completed"
        amount = int(payload.get("Amount", 0) * 100)
        is_recurrent = payload.get("IsRecurrent", False)
        card_token = payload.get("CardToken")
        
        if card_token:
            await database.save_card_token(int(user_id), card_token)
        
        if not is_recurrent:
            logger.info(f"User {user_id}: Initial payment processed")
            return {"success": True, "message": "Initial payment processed"}
        
        if is_successful:
            return await self._handle_successful_payment(subscription, amount, user_id)
        else:
            return await self._handle_failed_payment(subscription, amount, user_id)
    
    async def _handle_successful_payment(self, subscription: dict, amount: int, user_id: str) -> Dict:
        if amount == config.SUBSCRIPTION_PLANS["HIGH"][0]:
            days = self.plan_days["HIGH"]
            next_plan = "HIGH"
            next_amount = config.SUBSCRIPTION_PLANS["HIGH"][0]
            plan_name = "HIGH (699₽)"
        elif amount == config.SUBSCRIPTION_PLANS["MEDIUM"][0]:
            days = self.plan_days["MEDIUM"]
            next_plan = "HIGH"
            next_amount = config.SUBSCRIPTION_PLANS["HIGH"][0]
            plan_name = "MEDIUM (299₽)"
        elif amount == config.SUBSCRIPTION_PLANS["LOW"][0]:
            days = self.plan_days["LOW"]
            next_plan = "MEDIUM"
            next_amount = config.SUBSCRIPTION_PLANS["MEDIUM"][0]
            plan_name = "LOW (99₽)"
        else:
            logger.warning(f"User {user_id}: Unknown amount: {amount}")
            return {"success": False, "error": f"Unknown amount: {amount}"}
        
        end_date = datetime.utcnow() + timedelta(days=days)
        await database.update_subscription_status(
            user_id=int(user_id),
            status="active",
            end_date=end_date
        )
        await database.update_subscription_plan(
            user_id=int(user_id),
            plan=next_plan
        )
        
        card_token = subscription.get('card_token')
        
        if card_token:
            result = await self.client.create_subscription(
                token=card_token,
                account_id=user_id,
                amount=next_amount,
                interval_days=config.BILLING_INTERVAL_DAYS,
                max_periods=1
            )
            
            if result["success"]:
                await database.save_cloudpayments_subscription_id(
                    user_id=int(user_id),
                    subscription_id=result["subscription_id"]
                )
                logger.info(f"User {user_id}: New subscription created with card token")
            else:
                logger.error(f"User {user_id}: Failed to create subscription with card token: {result.get('error')}")
        else:
            logger.warning(f"User {user_id}: ❌ No card token found! Cannot create recurring subscription")
        
        logger.info(f"User {user_id}: ✅ {plan_name} paid successfully. Access for {days} days until {end_date}")
        
        return {
            "success": True,
            "message": f"Payment successful! Access for {days} days",
            "plan": plan_name,
            "amount": amount / 100,
            "access_until": end_date.isoformat(),
            "next_amount": next_amount / 100,
            "next_plan": next_plan
        }
    
    async def _handle_failed_payment(self, subscription: dict, amount: int, user_id: str) -> Dict:
        current_amount = amount
        
        logger.info(f"User {user_id}: ❌ Payment failed: {current_amount/100}₽")
        
        if current_amount == config.SUBSCRIPTION_PLANS["HIGH"][0]:
            next_amount = config.SUBSCRIPTION_PLANS["MEDIUM"][0]
            next_plan = "MEDIUM"
        elif current_amount == config.SUBSCRIPTION_PLANS["MEDIUM"][0]:
            next_amount = config.SUBSCRIPTION_PLANS["LOW"][0]
            next_plan = "LOW"
        elif current_amount == config.SUBSCRIPTION_PLANS["LOW"][0]:
            await database.update_subscription_status(
                user_id=int(user_id),
                status="retry_low"
            )
            logger.info(f"User {user_id}: All plans failed. Will retry LOW (99₽) daily forever")
            return {
                "success": False,
                "message": "All plans failed. Will retry LOW plan (99₽) daily forever",
                "status": "retry_low_daily_forever"
            }
        else:
            return {"success": False, "error": f"Unknown amount: {current_amount}"}
        
        subscription_id = subscription.get('subscription_id')
        if subscription_id:
            await self.client.update_subscription(
                subscription_id=subscription_id,
                amount=next_amount,
                max_periods=1
            )
        
        await database.update_subscription_plan(
            user_id=int(user_id),
            plan=next_plan
        )
        
        logger.info(f"User {user_id}: Attempting {next_plan} plan ({next_amount/100}₽)")
        
        return {
            "success": False,
            "message": f"Attempting {next_plan} plan ({next_amount/100}₽)",
            "next_amount": next_amount / 100,
            "next_plan": next_plan
        }
    
    async def handle_daily_low_retry(self, user_id: str) -> Dict:
        subscription = await self.get_user_subscription(user_id)
        if not subscription:
            return {"success": False, "error": "User not found"}
        
        if subscription.get('status') != 'retry_low':
            return {"success": False, "message": "User is not in retry low state"}
        
        subscription_id = subscription.get('subscription_id')
        if subscription_id:
            await self.client.update_subscription(
                subscription_id=subscription_id,
                amount=config.SUBSCRIPTION_PLANS["LOW"][0],
                max_periods=None
            )
        
        logger.info(f"User {user_id}: Daily retry for LOW plan (99₽) - infinite attempts")
        
        return {
            "success": True,
            "message": "Daily retry for LOW plan (99₽)",
            "amount": config.SUBSCRIPTION_PLANS["LOW"][0] / 100
        }

    async def check_expired_subscriptions(self) -> Dict:
        """
        Проверяет истекшие подписки и деактивирует их
        Для рекуррентных платежей:
        - Если end_date < NOW() → статус → inactive
        - Если статус retry_low → уже обрабатывается daily_retry_job
        
        Returns:
            Dict: {
                "success": bool,
                "expired_count": int,
                "expired_users": list
            }
        """
        try:
            conn = await database.get_conn()
            try:
                expired = await conn.fetch("""
                    SELECT user_id, end_date, plan, subscription_id 
                    FROM subscriptions 
                    WHERE status = 'active' 
                    AND end_date IS NOT NULL 
                    AND end_date < NOW()
                """)
                
                if not expired:
                    logger.info("✅ No expired subscriptions found")
                    return {"success": True, "expired_count": 0, "expired_users": []}
                
                logger.info(f"🔍 Found {len(expired)} expired subscriptions")
                
                expired_users = []
                
                for row in expired:
                    user_id = row['user_id']
                    end_date = row['end_date']
                    plan = row['plan']
                    subscription_id = row.get('subscription_id')
                    
                    if subscription_id:
                        cp_status = await self.client.get_subscription_status(subscription_id)
                        
                        if cp_status.get("success"):
                            cp_status_value = cp_status.get("status")
                            
                            if cp_status_value in ["Active", "Pending"]:
                                logger.info(f"User {user_id}: CloudPayments subscription still active, skipping")
                                continue
                    
                    await database.update_subscription_status(
                        user_id=user_id,
                        status='inactive',
                        end_date=end_date
                    )
                    
                    expired_users.append(user_id)
                    logger.info(f"⏰ User {user_id}: Subscription expired (plan: {plan}, ended: {end_date})")
                
                return {
                    "success": True,
                    "expired_count": len(expired_users),
                    "expired_users": expired_users
                }
                
            finally:
                await database.release_conn(conn)
                
        except Exception as e:
            logger.error(f"❌ Error checking expired subscriptions: {e}")
            return {"success": False, "error": str(e)}
