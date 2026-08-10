import base64
import httpx
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
import logging

from config import config

logger = logging.getLogger(__name__)

class CloudPaymentsClient:
    def __init__(self):
        self.base_url = config.API_URL
        self.public_id = config.PUBLIC_ID
        self.api_secret = config.API_SECRET
        self.auth = base64.b64encode(f"{self.public_id}:{self.api_secret}".encode()).decode()
    
    async def charge_card(self, token: str, account_id: str, amount: int) -> Dict[str, Any]:
        url = f"{self.base_url}/payments/tokens/charge"
        
        payload = {
            "Token": token,
            "AccountId": account_id,
            "Amount": amount / 100,
            "Currency": "RUB",
            "InvoiceId": f"sub_{account_id}_{int(datetime.utcnow().timestamp())}",
            "Description": f"Subscription payment for user {account_id}",
        }
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers={
                        "Authorization": f"Basic {self.auth}",
                        "Content-Type": "application/json"
                    }
                )

            logger.info(
                "CloudPayments charge response: HTTP %s, body=%s",
                response.status_code,
                response.text
            )
            
            if response.status_code == 200:
                data = response.json()
                logger.info(
                    "CloudPayments charge parsed response: Success=%s, Message=%s, Model=%s",
                    data.get("Success"),
                    data.get("Message"),
                    data.get("Model")
                )
                if data.get("Success"):
                    return {
                        "success": True,
                        "data": data.get("Model", {}),
                        "message": "Payment initiated, waiting for webhook confirmation"
                    }
                else:
                    return {
                        "success": False,
                        "error": data.get("Message", "Unknown error"),
                        "data": data.get("Model", {})
                    }
            else:
                logger.error(
                    "CloudPayments charge HTTP error: status=%s, body=%s",
                    response.status_code,
                    response.text
                )
                return {
                    "success": False,
                    "error": f"HTTP {response.status_code}: {response.text}"
                }
        except Exception as e:
            logger.error(f"Error charging card: {e}")
            return {"success": False, "error": str(e)}
    
    async def create_subscription(self,
                                  token: str,
                                  account_id: str,
                                  amount: int,
                                  interval_days: int = 1,
                                  max_periods: Optional[int] = None) -> Dict[str, Any]:
        logger.warning("create_subscription() вызван, но активная система отключена")
        return {"success": False, "error": "Internal subscriptions are disabled"}
    
    async def update_subscription(self,
                                  subscription_id: str,
                                  amount: Optional[int] = None,
                                  max_periods: Optional[int] = None,
                                  status: Optional[str] = None,
                                  start_date: Optional[datetime] = None) -> Dict[str, Any]:
        logger.warning("update_subscription() вызван, но активная система отключена")
        return {"success": False, "error": "Internal subscriptions are disabled"}
    
    async def cancel_subscription(self, subscription_id: str) -> Dict[str, Any]:
        logger.warning("cancel_subscription() вызван, но активная система отключена")
        return {"success": False, "error": "Internal subscriptions are disabled"}
    
    async def get_subscription_status(self, subscription_id: str) -> Dict[str, Any]:
        logger.warning("get_subscription_status() вызван, но активная система отключена")
        return {"success": False, "error": "Internal subscriptions are disabled"}
