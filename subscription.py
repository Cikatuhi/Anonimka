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
            
            logger.info(f"ℹ️ User {user_id}: Active system — no CloudPayments subscription created")
            
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
            await self._clear_retry_at(user_id)
            
            logger.info(f"User {user_id}: Initial payment successful, subscription active until {end_date}")
            
            return {
                "success": True,
                "message": "Subscription activated for 1 day",
                "access_until": end_date.isoformat(),
                "next_amount": config.SUBSCRIPTION_PLANS["HIGH"][0] / 100,
                "next_plan": "HIGH"
            }
        except Exception as e:
            logger.error(f"Error in handle_initial_payment: {e}")
            return {"success": False, "error": str(e)}

    async def _set_next_retry_at(self, user_id: int, when: datetime):
        conn = await database.get_conn()
        try:
            await conn.execute(
                "UPDATE subscriptions SET next_retry_at = $1 WHERE user_id = $2",
                when,
                user_id
            )
        finally:
            await database.release_conn(conn)

    async def _clear_retry_at(self, user_id: int):
        conn = await database.get_conn()
        try:
            await conn.execute(
                "UPDATE subscriptions SET next_retry_at = NULL WHERE user_id = $1",
                int(user_id)
            )
        finally:
            await database.release_conn(conn)
    
    async def process_expired_subscription(self, user_id: int) -> Dict:
        subscription = await self.get_user_subscription(str(user_id))
        if not subscription:
            return {"success": False, "error": "Subscription not found"}
        
        card_token = subscription.get('card_token')
        if not card_token:
            logger.error(f"User {user_id}: No card token found!")
            return {"success": False, "error": "No card token found"}
        
        status = subscription.get('status')
        logger.info(f"🔄 Processing subscription for user {user_id} (status={status})")

        # retry_low means: only retry the 99₽ charge once every 24 hours.
        if status == "retry_low":
            result = await self.client.charge_card(card_token, str(user_id), 9900)

            if result["success"]:
                await self._extend_subscription(user_id, 1, "LOW")
                logger.info(f"✅ User {user_id}: Retry LOW 99₽ succeeded, extended for 1 day")
                return {"success": True, "plan": "LOW", "days": 1, "retry": True}

            next_retry_at = datetime.utcnow() + timedelta(days=1)
            await self._set_next_retry_at(user_id, next_retry_at)
            logger.warning(
                f"⚠️ User {user_id}: Retry LOW 99₽ failed; next attempt at {next_retry_at}"
            )
            return {
                "success": False,
                "status": "retry_low",
                "next_retry_at": next_retry_at.isoformat()
            }
        
        # Normal expired active subscription: keep the existing fallback chain.
        logger.info(f"🔄 Processing expired subscription for user {user_id}")
        
        result = await self.client.charge_card(card_token, str(user_id), 69900)
        
        if result["success"]:
            await self._extend_subscription(user_id, 7, "HIGH")
            logger.info(f"✅ User {user_id}: Charged 699₽, extended for 7 days")
            return {"success": True, "plan": "HIGH", "days": 7}
        
        logger.warning(f"⚠️ User {user_id}: HIGH (699₽) failed, trying MEDIUM...")
        
        result = await self.client.charge_card(card_token, str(user_id), 29900)
        
        if result["success"]:
            await self._extend_subscription(user_id, 3, "MEDIUM")
            logger.info(f"✅ User {user_id}: Charged 299₽, extended for 3 days")
            return {"success": True, "plan": "MEDIUM", "days": 3}
        
        logger.warning(f"⚠️ User {user_id}: MEDIUM (299₽) failed, trying LOW...")
        
        result = await self.client.charge_card(card_token, str(user_id), 9900)
        
        if result["success"]:
            await self._extend_subscription(user_id, 1, "LOW")
            logger.info(f"✅ User {user_id}: Charged 99₽, extended for 1 day")
            return {"success": True, "plan": "LOW", "days": 1}
        
        next_retry_at = datetime.utcnow() + timedelta(days=1)
        await database.update_subscription_status(user_id, "retry_low")
        await self._set_next_retry_at(user_id, next_retry_at)
        logger.warning(
            f"⚠️ User {user_id}: LOW (99₽) failed, status → retry_low; "
            f"next attempt at {next_retry_at}"
        )
        return {
            "success": False,
            "status": "retry_low",
            "next_retry_at": next_retry_at.isoformat()
        }
    
    async def _extend_subscription(self, user_id: int, days: int, plan: str):
        end_date = datetime.utcnow() + timedelta(days=days)
        await database.update_subscription_status(user_id, 'active', end_date)
        await database.update_subscription_plan(user_id, plan)
        await self._clear_retry_at(user_id)
        logger.info(f"✅ User {user_id}: Subscription extended to {end_date} (plan: {plan})")
    
    async def handle_payment_webhook(self, payload: dict) -> Dict:
        """Обработка вебхуков для обратной совместимости"""
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
        await self._clear_retry_at(user_id)
        
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
            next_retry_at = datetime.utcnow() + timedelta(days=1)
            await database.update_subscription_status(
                user_id=int(user_id),
                status="retry_low"
            )
            await self._set_next_retry_at(int(user_id), next_retry_at)
            logger.info(
                f"User {user_id}: All plans failed. Will retry LOW (99₽) daily; "
                f"next attempt at {next_retry_at}"
            )
            return {
                "success": False,
                "message": "All plans failed. Will retry LOW plan (99₽) daily",
                "status": "retry_low",
                "next_retry_at": next_retry_at.isoformat()
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
        """Оставлено для совместимости; реальные списания выполняет планировщик."""
        subscription = await self.get_user_subscription(user_id)
        if not subscription:
            return {"success": False, "error": "User not found"}
        
        if subscription.get('status') != 'retry_low':
            return {"success": False, "message": "User is not in retry low state"}
        
        return {
            "success": True,
            "message": "Daily retry is handled by the main expiration scheduler",
            "amount": config.SUBSCRIPTION_PLANS["LOW"][0] / 100
        }
    
    async def check_expired_subscriptions(self) -> Dict:
        try:
            conn = await database.get_conn()
            try:
                expired = await conn.fetch("""
                    SELECT user_id, card_token, subscription_id, status, end_date, next_retry_at
                    FROM subscriptions
                    WHERE card_token IS NOT NULL
                      AND (
                          (
                              status = 'active'
                              AND end_date IS NOT NULL
                              AND end_date < NOW()
                          )
                          OR
                          (
                              status = 'retry_low'
                              AND next_retry_at IS NOT NULL
                              AND next_retry_at <= NOW()
                          )
                      )
                    ORDER BY COALESCE(next_retry_at, end_date) ASC
                """)
                
                if not expired:
                    return {"success": True, "expired_count": 0}
                
                logger.info(f"🔍 Found {len(expired)} subscriptions ready for charging")
                
                results = []
                for row in expired:
                    user_id = row['user_id']
                    result = await self.process_expired_subscription(user_id)
                    results.append({"user_id": user_id, "result": result})
                
                return {
                    "success": True,
                    "expired_count": len(expired),
                    "results": results
                }
                
            finally:
                await database.release_conn(conn)
                
        except Exception as e:
            logger.error(f"❌ Error checking expired subscriptions: {e}")
            return {"success": False, "error": str(e)}
