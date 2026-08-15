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
                "CloudPayments charge response: HTTP %s",
                response.status_code
            )
            
            if response.status_code == 200:
                data = response.json()
                logger.info(
                    "CloudPayments charge parsed response: Success=%s, Message=%s",
                    data.get("Success"),
                    data.get("Message")
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
                    "CloudPayments charge HTTP error: status=%s",
                    response.status_code
                )
                return {
                    "success": False,
                    "error": f"HTTP {response.status_code}"
                }
        except Exception as e:
            logger.error(f"Error charging card: {e}")
            return {"success": False, "error": str(e)}

    async def list_payments(
        self,
        payment_date: str,
        page: int = 1,
        count: int = 20,
    ) -> Dict[str, Any]:
        """Return CloudPayments transactions for one calendar date.

        CloudPayments exposes both completed and declined operations through
        ``/payments/list``.  The admin panel deliberately reads this data on
        demand instead of treating the local webhook journal as the source of
        truth for historical transactions.
        """
        url = f"{self.base_url}/payments/list"
        payload = {
            "Date": payment_date,
            "TimeZone": "MSK",
            "Page": page,
            "Count": count,
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers={
                        "Authorization": f"Basic {self.auth}",
                        "Content-Type": "application/json",
                    },
                )

            if response.status_code != 200:
                logger.error(
                    "CloudPayments payments/list HTTP error: status=%s",
                    response.status_code,
                )
                return {
                    "success": False,
                    "error": f"CloudPayments HTTP {response.status_code}",
                }

            data = response.json()
            if not data.get("Success"):
                return {
                    "success": False,
                    "error": data.get("Message") or "CloudPayments не вернул список транзакций",
                }

            model = data.get("Model") or []
            if isinstance(model, dict):
                model = model.get("Payments") or model.get("Items") or []

            return {
                "success": True,
                "payments": model if isinstance(model, list) else [],
                "page": page,
                "count": count,
            }
        except Exception as e:
            logger.error("Error loading CloudPayments transactions: %s", e)
            return {"success": False, "error": str(e)}

    async def list_payments_period(
        self,
        created_date_gte: str,
        created_date_lte: str,
        page_number: int = 1,
        statuses: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        """Return CloudPayments transactions for an arbitrary period.

        The v2 endpoint returns up to 100 transactions per page and includes
        all statuses by default, including successful and failed operations.
        """
        url = f"{self.base_url}/v2/payments/list"
        payload = {
            "PageNumber": max(1, page_number),
            "CreatedDateGte": created_date_gte,
            "CreatedDateLte": created_date_lte,
            "TimeZone": "MSK",
        }
        if statuses:
            payload["Statuses"] = statuses

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers={
                        "Authorization": f"Basic {self.auth}",
                        "Content-Type": "application/json",
                    },
                )

            if response.status_code != 200:
                logger.error(
                    "CloudPayments v2 payments/list HTTP error: status=%s",
                    response.status_code,
                )
                return {
                    "success": False,
                    "error": f"CloudPayments HTTP {response.status_code}",
                }

            data = response.json()
            if not data.get("Success"):
                return {
                    "success": False,
                    "error": data.get("Message") or "CloudPayments не вернул список транзакций",
                }

            model = data.get("Model") or []
            if isinstance(model, dict):
                model = model.get("Payments") or model.get("Items") or []

            payments = model if isinstance(model, list) else []
            return {
                "success": True,
                "payments": payments,
                "page": max(1, page_number),
                "count": len(payments),
                "has_more": len(payments) >= 100,
            }
        except Exception as e:
            logger.error("Error loading CloudPayments transactions for period: %s", e)
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
