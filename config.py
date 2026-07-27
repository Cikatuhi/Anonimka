import os
from dataclasses import dataclass, field
from typing import Dict, Tuple
from dotenv import load_dotenv

load_dotenv()

@dataclass
class Config:

    PUBLIC_ID: str = os.getenv("CLOUDPAYMENTS_PUBLIC_ID", "pk_cfc9c99ee447f06de34f6cd148a22")
    API_SECRET: str = os.getenv("CLOUDPAYMENTS_API_SECRET", "b4a73077265e848f6c300c6ab661a4bb")
    API_URL: str = "https://api.cloudpayments.ru"

    SUBSCRIPTION_PLANS: Dict[str, Tuple[int, int]] = field(
        default_factory=lambda: { 
            "HIGH": (69900, 7),   
            "MEDIUM": (29900, 3), 
            "LOW": (9900, 1),    
        }
    )
    
    # Установочный платеж (в копейках)
    INITIAL_PAYMENT: int = 500  # 5.00 RUB

    BILLING_INTERVAL_DAYS: int = 1
    
    WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", "https://your-domain.com/webhook")
    
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", 8000))

config = Config()
