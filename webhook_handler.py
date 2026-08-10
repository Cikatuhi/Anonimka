from fastapi import APIRouter, Request, HTTPException
import json
import logging
from datetime import datetime, timedelta
from subscription import SubscriptionManager
import database
from config import config

logger = logging.getLogger(__name__)
router = APIRouter()
subscription_manager = SubscriptionManager()

@router.post("/webhook")
async def handle_webhook(request: Request):
    try:
        body = await request.json()
        logger.info(f"Received webhook: {body}")
        
        event_type = body.get("EventType")
        if event_type not in ["Payment", "Recurrent"]:
            return {"status": "ignored"}
        
        model = body.get("Model", {})
        
        account_id = model.get("AccountId")
        status = model.get("Status")
        amount_rub = model.get("Amount", 0)
        is_recurrent = model.get("IsRecurrent", False)
        card_token = model.get("Token")
        subscription_id = model.get("SubscriptionId")
        
        if status == "Completed" and account_id:
            try:
                user_id = int(account_id)
                logger.info(f"✅ Успешная оплата для пользователя {user_id}, сумма: {amount_rub}₽")
                
                existing_subscription = await database.get_subscription_by_user_id(user_id)
                
                if existing_subscription:
                    end_date = datetime.now() + timedelta(days=1)
                    await database.update_subscription_status(user_id, 'active', end_date)
                    await database.update_subscription_plan(user_id, 'HIGH')
                    
                    if card_token:
                        await database.save_card_token(user_id, card_token)
                        logger.info(f"✅ Card token saved for user {user_id}")
                    
    logger.info(f"ℹ️ CloudPayments subscription not created (active system)")
                    
                else:
                    end_date = datetime.now() + timedelta(days=1)
                    await database.create_subscription(
                        user_id=user_id,
                        plan='HIGH',
                        status='active',
                        end_date=end_date,
                        card_token=card_token,
                        subscription_id=subscription_id
                    )
                    logger.info(f"✅ Создана подписка для пользователя {user_id}")
                
                await database.remove_from_unsubscribed(user_id)
                return {"status": "ok", "message": "Subscription activated"}
                
            except Exception as e:
                logger.error(f"❌ Ошибка выдачи подписки: {e}")
                return {"status": "error", "message": str(e)}
        
        if is_recurrent:
            payload = {
                "AccountId": account_id,
                "Amount": amount_rub,
                "Status": status,
                "TransactionId": model.get("TransactionId"),
                "SubscriptionId": subscription_id,
                "IsRecurrent": is_recurrent,
                "ReasonCode": model.get("ReasonCode"),
                "CardToken": card_token
            }
            
            result = await subscription_manager.handle_payment_webhook(payload)
            logger.info(f"Recurrent payment processed for user {account_id}: {result.get('message', '')}")
            return {"status": "processed", "result": result}
        
        return {"status": "ok"}
        
    except json.JSONDecodeError:
        logger.error("Invalid JSON received")
        raise HTTPException(status_code=400, detail="Invalid JSON")
    except Exception as e:
        logger.error(f"Error processing webhook: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}

@router.post("/subscription/init")
async def initialize_subscription(request: Request):
    try:
        body = await request.json()
        user_id = body.get("user_id")
        card_token = body.get("card_token")
        
        if not user_id or not card_token:
            raise HTTPException(status_code=400, detail="Missing user_id or card_token")
        
        result = await subscription_manager.handle_initial_payment(user_id, card_token)
        return result
        
    except Exception as e:
        logger.error(f"Error initializing subscription: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/subscription/process-expired")
async def process_expired_subscription_endpoint():
    result = await subscription_manager.check_expired_subscriptions()
    return result

@router.get("/subscription/{user_id}")
async def get_subscription_status(user_id: str):
    subscription = await subscription_manager.get_user_subscription(user_id)
    
    if not subscription:
        return {
            "success": False,
            "message": "User not found"
        }
    
    return {
        "success": True,
        "user_id": user_id,
        "status": subscription.get('status'),
        "plan": subscription.get('plan'),
        "end_date": subscription.get('end_date').isoformat() if subscription.get('end_date') else None,
        "card_token": subscription.get('card_token'),
        "subscription_id": subscription.get('subscription_id'),
        "created_at": subscription.get('created_at').isoformat() if subscription.get('created_at') else None
    }

@router.post("/subscription/cancel/{user_id}")
async def cancel_subscription(user_id: str):
    subscription = await subscription_manager.get_user_subscription(user_id)
    
    if not subscription:
        return {"success": False, "message": "User not found"}
    
    subscription_id = subscription.get('subscription_id')
    if subscription_id:
        result = await subscription_manager.client.cancel_subscription(subscription_id)
        if result["success"]:
            await database.update_subscription_status(int(user_id), 'cancelled')
            return {"success": True, "message": "Subscription cancelled"}
        else:
            return {"success": False, "error": result.get("error")}
    
    return {"success": False, "message": "No subscription to cancel"}

@router.post("/subscription/check-expired")
async def check_expired_subscriptions():
    result = await subscription_manager.check_expired_subscriptions()
    return result
