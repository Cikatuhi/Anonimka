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
        "Amount": amount / 100,  # В рублях
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
            
        if response.status_code == 200:
            data = response.json()я
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
        url = f"{self.base_url}/subscriptions/create"
        start_date = (datetime.utcnow() + timedelta(days=interval_days)).isoformat() + "Z"
        
        payload = {
            "Token": token,
            "AccountId": account_id,
            "Description": f"Subscription for user {account_id}",
            "Amount": amount / 100,
            "Currency": "RUB",
            "RequireConfirmation": False,
            "StartDate": start_date,
            "Interval": "Day",
            "Period": interval_days,
        }
        
        if max_periods:
            payload["MaxPeriods"] = max_periods
        
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
                
            if response.status_code == 200:
                data = response.json()
                if data.get("Success"):
                    return {
                        "success": True,
                        "subscription_id": data["Model"]["Id"],
                        "subscription_data": data["Model"]
                    }
                else:
                    return {
                        "success": False,
                        "error": data.get("Message", "Unknown error")
                    }
            else:
                return {
                    "success": False,
                    "error": f"HTTP {response.status_code}: {response.text}"
                }
        except Exception as e:
            logger.error(f"Error creating subscription: {e}")
            return {"success": False, "error": str(e)}
    
    async def update_subscription(self, 
                                  subscription_id: str, 
                                  amount: Optional[int] = None,
                                  max_periods: Optional[int] = None,
                                  status: Optional[str] = None,
                                  start_date: Optional[datetime] = None) -> Dict[str, Any]:
        url = f"{self.base_url}/subscriptions/update/{subscription_id}"
        payload = {}
        if amount is not None:
            payload["Amount"] = amount / 100
        if max_periods is not None:
            payload["MaxPeriods"] = max_periods
        if status is not None:
            payload["Status"] = status
        if start_date is not None:
            payload["StartDate"] = start_date.isoformat() + "Z"
            
        if not payload:
            return {"success": True, "message": "No updates needed"}
        
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
                
            if response.status_code == 200:
                data = response.json()
                return {
                    "success": data.get("Success", False),
                    "data": data.get("Model", {})
                }
            else:
                return {
                    "success": False,
                    "error": f"HTTP {response.status_code}: {response.text}"
                }
        except Exception as e:
            logger.error(f"Error updating subscription: {e}")
            return {"success": False, "error": str(e)}
    
    async def cancel_subscription(self, subscription_id: str) -> Dict[str, Any]:
        url = f"{self.base_url}/subscriptions/cancel/{subscription_id}"
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    url,
                    headers={
                        "Authorization": f"Basic {self.auth}",
                        "Content-Type": "application/json"
                    }
                )
                
            if response.status_code == 200:
                data = response.json()
                return {
                    "success": data.get("Success", False),
                    "data": data.get("Model", {})
                }
            else:
                return {
                    "success": False,
                    "error": f"HTTP {response.status_code}: {response.text}"
                }
        except Exception as e:
            logger.error(f"Error cancelling subscription: {e}")
            return {"success": False, "error": str(e)}
    
    async def get_subscription_status(self, subscription_id: str) -> Dict[str, Any]:
        url = f"{self.base_url}/subscriptions/get/{subscription_id}"
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    url,
                    headers={
                        "Authorization": f"Basic {self.auth}",
                        "Content-Type": "application/json"
                    }
                )
                
            if response.status_code == 200:
                data = response.json()
                if data.get("Success"):
                    model = data["Model"]
                    return {
                        "success": True,
                        "status": model.get("Status"),
                        "amount": model.get("Amount"),
                        "next_payment": model.get("NextPaymentDate"),
                        "data": model
                    }
                else:
                    return {
                        "success": False,
                        "error": data.get("Message", "Unknown error")
                    }
            else:
                return {
                    "success": False,
                    "error": f"HTTP {response.status_code}: {response.text}"
                }
        except Exception as e:
            logger.error(f"Error getting subscription status: {e}")
            return {"success": False, "error": str(e)}
