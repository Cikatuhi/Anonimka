from fastapi import APIRouter, Request, HTTPException
import json
import logging
from datetime import datetime, timedelta
from subscription import SubscriptionManager
import database

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
            logger.info(f"Ignoring event: {event_type}")
            return {"status": "ignored"}
        
        model = body.get("Model", {})
        
        account_id = model.get("AccountId")
        status = model.get("Status")
        amount_rub = model.get("Amount", 0)
        is_recurrent = model.get("IsRecurrent", False)
        card_token = model.get("Token") 
        
        if status == "Completed" and account_id:
            try:
                user_id = int(account_id)
                logger.info(f"✅ Успешная оплата для пользователя {user_id}")
                
                existing_subscription = await database.get_subscription_by_user_id(user_id)
                
                if existing_subscription:
                    end_date = datetime.now() + timedelta(days=7)
                    await database.update_subscription_status(user_id, 'active', end_date)
                    await database.update_subscription_plan(user_id, 'HIGH')
                    
                    if card_token:
                        await database.save_card_token(user_id, card_token)
                    
                    logger.info(f"✅ Обновлена подписка для пользователя {user_id}")
                else:
                    end_date = datetime.now() + timedelta(days=7)
                    await database.create_subscription(
                        user_id=user_id,
                        plan='HIGH',
                        status='active',
                        end_date=end_date,
                        card_token=card_token,
                        subscription_id=model.get("SubscriptionId")
                    )
                    logger.info(f"✅ Создана подписка для пользователя {user_id} с токеном карты")
                
                try:
                    await database.remove_from_unsubscribed(user_id)
                    logger.info(f"✅ Пользователь {user_id} удалён из отписавшихся")
                except Exception as e:
                    logger.warning(f"⚠️ Не удалось удалить из отписавшихся: {e}")
                
                return {"status": "ok", "message": "Subscription activated"}
                
            except Exception as e:
                logger.error(f"❌ Ошибка выдачи подписки: {e}")
        
        payload = {
            "AccountId": account_id,
            "Amount": amount_rub,
            "Status": status,
            "TransactionId": model.get("TransactionId"),
            "SubscriptionId": model.get("SubscriptionId"),
            "IsRecurrent": is_recurrent,
            "ReasonCode": model.get("ReasonCode"),
            "CardToken": card_token
        }
        
        result = await subscription_manager.handle_payment_webhook(payload)
        logger.info(f"Webhook processed for user {payload.get('AccountId')}: {result.get('message', '')}")
        
        return {"status": "processed", "result": result}
        
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


@router.post("/subscription/daily-retry/{user_id}")
async def trigger_daily_retry(user_id: str):
    result = await subscription_manager.handle_daily_low_retry(user_id)
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
        "card_token": subscription.get('card_token'),  # ← Показываем, что токен есть
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
