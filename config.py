import os
from dataclasses import dataclass
from typing import Dict, Tuple
from dotenv import load_dotenv

load_dotenv()

@dataclass
class Config:

    PUBLIC_ID: str = os.getenv("CLOUDPAYMENTS_PUBLIC_ID", "test_api_00000000000000000000002")
    API_SECRET: str = os.getenv("CLOUDPAYMENTS_API_SECRET", "test_api_00000000000000000000002")
    API_URL: str = "https://api.cloudpayments.ru"
    
    # Планы подписки (сумма в копейках, срок в днях)
    SUBSCRIPTION_PLANS: Dict[str, Tuple[int, int]] = {
        "HIGH": (69900, 7),   # 699.00 RUB -> 7 дней
        "MEDIUM": (29900, 3), # 299.00 RUB -> 3 дня
        "LOW": (9900, 1),     # 99.00 RUB -> 1 день
    }
    
    # Установочный платеж (в копейках)
    INITIAL_PAYMENT: int = 500  # 5.00 RUB
    
    # Интервал списания в днях (ежедневно)
    BILLING_INTERVAL_DAYS: int = 1
    
    WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", "https://your-domain.com/webhook")
    
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", 8000))

config = Config()
