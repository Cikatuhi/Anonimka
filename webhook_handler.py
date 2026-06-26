from fastapi import APIRouter, Request, HTTPException
import json
import logging

from subscription import SubscriptionManager

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
        
        amount_rub = model.get("Amount", 0)
        
        payload = {
            "AccountId": model.get("AccountId"),
            "Amount": amount_rub,  # Оставляем в рублях для совместимости
            "Status": model.get("Status"),
            "TransactionId": model.get("TransactionId"),
            "SubscriptionId": model.get("SubscriptionId"),
            "IsRecurrent": model.get("IsRecurrent", False),
            "ReasonCode": model.get("ReasonCode"),
            "CardToken": model.get("Token")
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
    subscription = subscription_manager.get_user_subscription(user_id)
    
    if not subscription:
        return {
            "success": False,
            "message": "User not found"
        }
    
    return {
        "success": True,
        "user_id": user_id,
        "status": subscription.status,
        "current_plan": subscription.current_plan.value if subscription.current_plan else None,
        "current_amount": subscription.current_amount / 100 if subscription.current_amount else 0,
        "active_until": subscription.active_until.isoformat() if subscription.active_until else None,
        "successful_payments": subscription.successful_payments,
        "failed_payments": subscription.failed_payments,
        "cascade_attempts": subscription.cascade_attempts
    }

@router.post("/subscription/cancel/{user_id}")
async def cancel_subscription(user_id: str):
    subscription = subscription_manager.get_user_subscription(user_id)
    if not subscription:
        return {"success": False, "message": "User not found"}
    
    if subscription.subscription_id:
        result = await subscription_manager.client.cancel_subscription(subscription.subscription_id)
        if result["success"]:
            subscription.status = SubscriptionStatus.CANCELLED
            return {"success": True, "message": "Subscription cancelled"}
        else:
            return {"success": False, "error": result.get("error")}
    
    return {"success": False, "message": "No subscription to cancel"}