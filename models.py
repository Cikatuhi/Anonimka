from pydantic import BaseModel
from typing import Optional
from enum import Enum
from datetime import datetime

class PlanType(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

class SubscriptionStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    RETRY_LOW = "retry_low"  # Ежедневные попытки списать 99₽
    CANCELLED = "cancelled"

class UserSubscription(BaseModel):
    user_id: str
    subscription_id: Optional[str] = None
    card_token: Optional[str] = None
    
    current_plan: PlanType = PlanType.HIGH
    current_amount: int = 69900
    
    status: SubscriptionStatus = SubscriptionStatus.INACTIVE
    
    active_until: Optional[datetime] = None
    
    cascade_attempts: int = 0
    has_tried_high: bool = False
    has_tried_medium: bool = False
    has_tried_low: bool = False
    
    successful_payments: int = 0
    failed_payments: int = 0
    last_payment_amount: Optional[int] = None
    last_failed_date: Optional[datetime] = None
    
    class Config:
        use_enum_values = True

class WebhookPayload(BaseModel):
    transaction_id: int
    account_id: str
    amount: float  # в рублях
    status: str
    subscription_id: Optional[str] = None
    reason_code: Optional[int] = None
    card_token: Optional[str] = None
    is_recurrent: bool = False