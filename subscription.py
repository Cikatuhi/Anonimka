import logging
from typing import Dict, Optional, List
from datetime import datetime, timedelta
from time_utils import now_msk, to_db_timestamp
import sys

sys.path.append('/root/Anonimno-Artem-')
import database

from config import config
from cloudpayments import CloudPaymentsClient

logger = logging.getLogger(__name__)

CARD_UNAVAILABLE_REASON_CODES = {5014, 5054, 5062, 5041}


def _normalise_reason_code(value) -> int | None:
    """Convert CloudPayments' numeric reason code to a stable integer."""
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _card_unavailable_reason_code(value) -> int | None:
    code = _normalise_reason_code(value)
    return code if code in CARD_UNAVAILABLE_REASON_CODES else None


class SubscriptionManager:
    """Token-charge subscription manager.

    Responsibility split:
    - scheduler: only initiates charges and moves to the next attempt after an
      immediate CloudPayments API rejection;
    - webhook: is the only place that grants/extends access after a payment
      result is confirmed by CloudPayments.
    """

    PENDING_CHARGE_HOURS = 24

    def __init__(self):
        self.client = CloudPaymentsClient()
        self.plan_amounts = {
            "HIGH": config.SUBSCRIPTION_PLANS["HIGH"][0],
            "MEDIUM": config.SUBSCRIPTION_PLANS["MEDIUM"][0],
            "LOW": config.SUBSCRIPTION_PLANS["LOW"][0],
        }
        self.plan_days = {
            "HIGH": config.SUBSCRIPTION_PLANS["HIGH"][1],
            "MEDIUM": config.SUBSCRIPTION_PLANS["MEDIUM"][1],
            "LOW": config.SUBSCRIPTION_PLANS["LOW"][1],
        }
        self.fallback_plan = {"HIGH": "MEDIUM", "MEDIUM": "LOW", "LOW": None}
        self.next_plan_after_success = {"HIGH": "HIGH", "MEDIUM": "HIGH", "LOW": "MEDIUM"}

    async def get_user_subscription(self, user_id: str) -> Optional[dict]:
        try:
            return await database.get_subscription_by_user_id(int(user_id))
        except Exception as e:
            logger.error(f"Error getting subscription for user {user_id}: {e}")
            return None

    async def get_all_users_in_retry_low(self) -> List[str]:
        try:
            users = await database.get_all_users_in_retry_low()
            return [str(user) for user in users]
        except Exception as e:
            logger.error(f"Error getting retry_low users: {e}")
            return []

    async def create_user_subscription(self, user_id: str, card_token: str) -> dict:
        try:
            await database.create_subscription(
                user_id=int(user_id),
                plan="HIGH",
                status="inactive",
                end_date=None,
                card_token=card_token,
            )
            return await self.get_user_subscription(user_id)
        except Exception as e:
            logger.error(f"Error creating subscription for user {user_id}: {e}")
            return None

    async def handle_initial_payment(self, user_id: str, card_token: str) -> Dict:
        try:
            subscription = await self.get_user_subscription(user_id)
            if not subscription:
                subscription = await self.create_user_subscription(user_id, card_token)

            if not card_token:
                return {"success": False, "error": "No card token provided. Please pay first."}

            end_date = now_msk() + timedelta(days=1)
            await database.update_subscription_status(int(user_id), "active", end_date)
            await database.update_subscription_plan(int(user_id), "HIGH")
            await self._clear_retry_at(user_id)

            return {
                "success": True,
                "message": "Subscription activated for 1 day",
                "access_until": end_date.isoformat(),
                "next_amount": self.plan_amounts["HIGH"] / 100,
                "next_plan": "HIGH",
            }
        except Exception as e:
            logger.error(f"Error in handle_initial_payment: {e}")
            return {"success": False, "error": str(e)}

    async def _set_next_retry_at(self, user_id: int, when: datetime):
        conn = await database.get_conn()
        try:
            await conn.execute(
                "UPDATE subscriptions SET next_retry_at = $1 WHERE user_id = $2",
                when,
                int(user_id),
            )
        finally:
            await database.release_conn(conn)

    async def _clear_retry_at(self, user_id: int):
        conn = await database.get_conn()
        try:
            await conn.execute(
                "UPDATE subscriptions SET next_retry_at = NULL WHERE user_id = $1",
                int(user_id),
            )
        finally:
            await database.release_conn(conn)

    async def _claim_charge_attempt(self, user_id: int, status: str) -> bool:
        """Atomically claim one charge attempt.

        This prevents the scheduler endpoint and the APScheduler job from
        charging the same expired subscription concurrently.
        """
        conn = await database.get_conn()
        try:
            row = await conn.fetchrow(
                """
                UPDATE subscriptions
                SET next_retry_at = NOW() + INTERVAL '24 hours'
                WHERE user_id = $1
                  AND status = $2
                  AND (
                      (
                          status = 'active'
                          AND end_date IS NOT NULL
                          AND end_date < NOW()
                          AND (next_retry_at IS NULL OR next_retry_at <= NOW())
                      )
                      OR
                      (
                          status = 'retry_low'
                          AND next_retry_at IS NOT NULL
                          AND next_retry_at <= NOW()
                      )
                  )
                RETURNING user_id
                """,
                int(user_id),
                status,
            )
            return row is not None
        finally:
            await database.release_conn(conn)

    async def _set_plan_for_next_attempt(self, user_id: int, plan: str):
        conn = await database.get_conn()
        try:
            await conn.execute(
                "UPDATE subscriptions SET plan = $1, next_retry_at = NULL WHERE user_id = $2",
                plan,
                int(user_id),
            )
        finally:
            await database.release_conn(conn)

    async def _move_to_retry_low(self, user_id: int):
        next_retry_at = now_msk() + timedelta(days=1)
        conn = await database.get_conn()
        try:
            await conn.execute(
                """
                UPDATE subscriptions
                SET status = 'retry_low', plan = 'LOW', next_retry_at = $1
                WHERE user_id = $2
                """,
                next_retry_at,
                int(user_id),
            )
        finally:
            await database.release_conn(conn)
        return next_retry_at

    async def process_expired_subscription(self, user_id: int) -> Dict:
        """Initiate charges only. Access is never extended here."""
        subscription = await self.get_user_subscription(str(user_id))
        if not subscription:
            return {"success": False, "error": "Subscription not found"}

        card_token = subscription.get("card_token")
        if not card_token:
            return {"success": False, "error": "No card token found"}

        status = subscription.get("status")
        if status not in ("active", "retry_low"):
            return {"success": False, "status": status, "message": "Subscription is not chargeable"}

        # retry_low is deliberately isolated: only LOW/99₽ and only once per 24h.
        if status == "retry_low":
            plan = "LOW"
            if not await self._claim_charge_attempt(user_id, "retry_low"):
                return {"success": False, "status": "retry_low", "message": "Charge attempt already claimed"}

            result = await self.client.charge_card(card_token, str(user_id), self.plan_amounts[plan])
            if result["success"]:
                logger.info(
                    f"⏳ User {user_id}: LOW 99₽ charge accepted; waiting for webhook confirmation"
                )
                return {
                    "success": True,
                    "status": "payment_pending",
                    "plan": "LOW",
                    "amount": 99,
                    "message": "Charge initiated; waiting for webhook",
                }

            reason_code = _card_unavailable_reason_code(result.get("reason_code"))
            if reason_code is not None:
                reason = f"card_unavailable_{reason_code}"
                archived = await database.unsubscribe_user(
                    int(user_id),
                    reason=reason,
                )
                if not archived:
                    logger.error(
                        "User %s: failed to archive subscription after card-unavailable code %s",
                        user_id,
                        reason_code,
                    )
                    return {
                        "success": False,
                        "status": "card_unavailable",
                        "reason_code": reason_code,
                        "error": "Failed to archive subscription",
                    }
                logger.warning(
                    "User %s: card unavailable (reason code %s); subscription archived",
                    user_id,
                    reason_code,
                )
                return {
                    "success": False,
                    "status": "unsubscribed",
                    "reason": reason,
                    "reason_code": reason_code,
                }

            next_retry_at = now_msk() + timedelta(days=1)
            await self._set_next_retry_at(user_id, next_retry_at)
            logger.warning(f"⚠️ User {user_id}: Retry LOW 99₽ failed; next attempt at {next_retry_at}")
            return {
                "success": False,
                "status": "retry_low",
                "next_retry_at": next_retry_at.isoformat(),
            }

        # Normal expired subscription: follow HIGH -> MEDIUM -> LOW.
        plan = subscription.get("plan") or "HIGH"
        if plan not in self.plan_amounts:
            plan = "HIGH"

        while plan:
            if not await self._claim_charge_attempt(user_id, "active"):
                return {"success": False, "message": "Charge attempt already claimed"}

            amount = self.plan_amounts[plan]
            result = await self.client.charge_card(card_token, str(user_id), amount)

            if result["success"]:
                logger.info(
                    f"⏳ User {user_id}: {plan} ({amount / 100:.0f}₽) charge accepted; waiting for webhook"
                )
                return {
                    "success": True,
                    "status": "payment_pending",
                    "plan": plan,
                    "amount": amount / 100,
                    "message": "Charge initiated; waiting for webhook",
                }

            reason_code = _card_unavailable_reason_code(result.get("reason_code"))
            if reason_code is not None:
                reason = f"card_unavailable_{reason_code}"
                archived = await database.unsubscribe_user(
                    int(user_id),
                    reason=reason,
                )
                if not archived:
                    logger.error(
                        "User %s: failed to archive subscription after card-unavailable code %s",
                        user_id,
                        reason_code,
                    )
                    return {
                        "success": False,
                        "status": "card_unavailable",
                        "reason_code": reason_code,
                        "error": "Failed to archive subscription",
                    }
                logger.warning(
                    "User %s: card unavailable (reason code %s); subscription archived",
                    user_id,
                    reason_code,
                )
                return {
                    "success": False,
                    "status": "unsubscribed",
                    "reason": reason,
                    "reason_code": reason_code,
                }

            next_plan = self.fallback_plan[plan]
            if next_plan:
                await self._set_plan_for_next_attempt(user_id, next_plan)
                logger.warning(
                    f"⚠️ User {user_id}: {plan} ({amount / 100:.0f}₽) rejected immediately; plan → {next_plan}"
                )
                plan = next_plan
                subscription = await self.get_user_subscription(str(user_id))
                if not subscription:
                    return {"success": False, "error": "Subscription disappeared during fallback"}
                card_token = subscription.get("card_token") or card_token
                continue

            next_retry_at = await self._move_to_retry_low(user_id)
            logger.warning(
                f"⚠️ User {user_id}: LOW 99₽ rejected immediately; status → retry_low, next attempt {next_retry_at}"
            )
            return {
                "success": False,
                "status": "retry_low",
                "next_retry_at": next_retry_at.isoformat(),
            }

        return {"success": False, "error": "No charge plan available"}

    async def _process_recurrent_webhook_transaction(self, payload: dict) -> Dict:
        """Process one recurrent webhook atomically and idempotently."""
        user_id = payload.get("AccountId")
        transaction_id = payload.get("TransactionId")
        if not user_id:
            return {"success": False, "error": "No AccountId"}
        if transaction_id is None:
            return {"success": False, "error": "No TransactionId in recurrent webhook"}

        try:
            amount = int(round(float(payload.get("Amount", 0)) * 100))
        except (TypeError, ValueError):
            return {"success": False, "error": "Invalid Amount"}

        status = payload.get("Status")
        reason_code = _card_unavailable_reason_code(payload.get("ReasonCode"))
        card_token = payload.get("CardToken")
        transaction_key = str(transaction_id)

        conn = await database.get_conn()
        try:
            async with conn.transaction():
                # Serialize concurrent deliveries of the same CloudPayments transaction.
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", transaction_key)

                existing = await conn.fetchrow(
                    "SELECT transaction_id FROM payment_events WHERE transaction_id = $1",
                    transaction_key,
                )
                if existing:
                    logger.info(f"ℹ️ Transaction {transaction_key} already processed; webhook ignored")
                    return {"success": True, "duplicate": True, "message": "Transaction already processed"}

                subscription = await conn.fetchrow(
                    """
                    SELECT * FROM subscriptions
                    WHERE user_id = $1
                    ORDER BY created_at DESC
                    LIMIT 1
                    FOR UPDATE
                    """,
                    int(user_id),
                )
                if not subscription:
                    return {"success": False, "error": "Subscription not found"}

                if card_token:
                    await conn.execute(
                        "UPDATE subscriptions SET card_token = $1 WHERE user_id = $2",
                        card_token,
                        int(user_id),
                    )

                await conn.execute(
                    """
                    INSERT INTO payment_events (transaction_id, user_id, amount, status)
                    VALUES ($1, $2, $3, $4)
                    """,
                    transaction_key,
                    int(user_id),
                    amount,
                    status,
                )

                # These codes mean that the saved card cannot be charged.
                # Archive the complete subscription snapshot and stop all
                # HIGH -> MEDIUM -> LOW fallback attempts.
                if status != "Completed" and reason_code is not None:
                    reason = f"card_unavailable_{reason_code}"
                    archived = await database.unsubscribe_user(
                        int(user_id),
                        reason=reason,
                        conn=conn,
                    )
                    if not archived:
                        raise RuntimeError(
                            f"Failed to archive subscription for reason code {reason_code}"
                        )
                    logger.warning(
                        "User %s: recurrent transaction %s reported card unavailable "
                        "(reason code %s); subscription archived",
                        user_id,
                        transaction_key,
                        reason_code,
                    )
                    return {
                        "success": False,
                        "status": "unsubscribed",
                        "reason": reason,
                        "reason_code": reason_code,
                        "transaction_id": transaction_key,
                    }

                if status == "Completed":
                    plan_by_amount = {
                        self.plan_amounts["HIGH"]: "HIGH",
                        self.plan_amounts["MEDIUM"]: "MEDIUM",
                        self.plan_amounts["LOW"]: "LOW",
                    }
                    paid_plan = plan_by_amount.get(amount)
                    if not paid_plan:
                        return {"success": False, "error": f"Unknown amount: {amount}"}

                    days = self.plan_days[paid_plan]
                    next_plan = self.next_plan_after_success[paid_plan]
                    end_date = now_msk() + timedelta(days=days)

                    await conn.execute(
                        """
                        UPDATE subscriptions
                        SET status = 'active',
                            end_date = $1,
                            plan = $2,
                            next_retry_at = NULL
                        WHERE user_id = $3
                        """,
                        to_db_timestamp(end_date),
                        next_plan,
                        int(user_id),
                    )

                    logger.info(
                        f"User {user_id}: ✅ recurrent transaction {transaction_key} completed: "
                        f"{amount / 100:.0f}₽, +{days} days, next plan {next_plan}"
                    )
                    return {
                        "success": True,
                        "message": "Payment successful; subscription extended",
                        "transaction_id": transaction_key,
                        "paid_plan": paid_plan,
                        "next_plan": next_plan,
                        "amount": amount / 100,
                        "days": days,
                        "access_until": end_date.isoformat(),
                    }

                # A webhook-confirmed decline releases the scheduler claim and
                # selects the next plan. The scheduler will initiate that charge.
                plan_by_amount = {
                    self.plan_amounts["HIGH"]: "HIGH",
                    self.plan_amounts["MEDIUM"]: "MEDIUM",
                    self.plan_amounts["LOW"]: "LOW",
                }
                failed_plan = plan_by_amount.get(amount)
                if not failed_plan:
                    return {"success": False, "error": f"Unknown amount: {amount}"}

                next_plan = self.fallback_plan[failed_plan]
                if next_plan:
                    await conn.execute(
                        """
                        UPDATE subscriptions
                        SET status = 'active', plan = $1, next_retry_at = NULL
                        WHERE user_id = $2
                        """,
                        next_plan,
                        int(user_id),
                    )
                    logger.info(
                        f"User {user_id}: ❌ transaction {transaction_key} declined for {failed_plan}; "
                        f"next plan → {next_plan}"
                    )
                    return {
                        "success": False,
                        "status": "next_plan",
                        "transaction_id": transaction_key,
                        "failed_plan": failed_plan,
                        "next_plan": next_plan,
                        "next_amount": self.plan_amounts[next_plan] / 100,
                    }

                next_retry_at = now_msk() + timedelta(days=1)
                await conn.execute(
                    """
                    UPDATE subscriptions
                    SET status = 'retry_low', plan = 'LOW', next_retry_at = $1
                    WHERE user_id = $2
                    """,
                    next_retry_at,
                    int(user_id),
                )
                logger.info(
                    f"User {user_id}: ❌ transaction {transaction_key} declined for LOW; "
                    f"retry_low next attempt at {next_retry_at}"
                )
                return {
                    "success": False,
                    "status": "retry_low",
                    "transaction_id": transaction_key,
                    "next_retry_at": next_retry_at.isoformat(),
                }
        finally:
            await database.release_conn(conn)

    async def handle_payment_webhook(self, payload: dict) -> Dict:
        """Webhook is the single authority for recurrent payment results."""
        user_id = payload.get("AccountId")
        is_recurrent = payload.get("IsRecurrent", False)

        if not user_id:
            return {"success": False, "error": "No AccountId"}


        if not is_recurrent:
            return {"success": True, "message": "Initial payment processed"}

        return await self._process_recurrent_webhook_transaction(payload)

    async def handle_daily_low_retry(self, user_id: str) -> Dict:
        subscription = await self.get_user_subscription(user_id)
        if not subscription:
            return {"success": False, "error": "User not found"}
        if subscription.get("status") != "retry_low":
            return {"success": False, "message": "User is not in retry low state"}
        return {
            "success": True,
            "message": "Daily retry is handled by the main expiration scheduler",
            "amount": self.plan_amounts["LOW"] / 100,
        }

    async def check_expired_subscriptions(self) -> Dict:
        try:
            conn = await database.get_conn()
            try:
                rows = await conn.fetch(
                    """
                    SELECT user_id, card_token, subscription_id, status, end_date, next_retry_at, plan
                    FROM subscriptions
                    WHERE card_token IS NOT NULL
                      AND (
                          (
                              status = 'active'
                              AND end_date IS NOT NULL
                              AND end_date < NOW()
                              AND (next_retry_at IS NULL OR next_retry_at <= NOW())
                          )
                          OR
                          (
                              status = 'retry_low'
                              AND next_retry_at IS NOT NULL
                              AND next_retry_at <= NOW()
                          )
                      )
                    ORDER BY COALESCE(next_retry_at, end_date) ASC
                    """
                )
                if not rows:
                    return {"success": True, "expired_count": 0}

                logger.info(f"🔍 Found {len(rows)} subscriptions ready for charging")
                results = []
                for row in rows:
                    user_id = row["user_id"]
                    result = await self.process_expired_subscription(user_id)
                    results.append({"user_id": user_id, "result": result})

                return {"success": True, "expired_count": len(rows), "results": results}
            finally:
                await database.release_conn(conn)
        except Exception as e:
            logger.error(f"❌ Error checking expired subscriptions: {e}")
            return {"success": False, "error": str(e)}
