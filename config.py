import os
from dataclasses import dataclass, field
from typing import Dict, Tuple
from dotenv import load_dotenv

load_dotenv()


def env_flag(name: str, default: bool) -> bool:
    """Read a conventional true/false environment flag."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:

    PUBLIC_ID: str = os.getenv("CLOUDPAYMENTS_PUBLIC_ID", "pk_cfc9c99ee447f06de34f6cd148a22")
    API_SECRET: str = os.environ["CLOUDPAYMENTS_API_SECRET"]
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

    # Lets the HTTP service accept CloudPayments notifications while manual
    # reconciliation is in progress, without running any automatic charges.
    PAYMENT_SCHEDULER_ENABLED: bool = field(
        default_factory=lambda: env_flag("PAYMENT_SCHEDULER_ENABLED", True)
    )

    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", 8000))

config = Config()
