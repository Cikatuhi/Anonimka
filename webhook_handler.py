from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
import json
import logging
from datetime import datetime, timedelta
from urllib.parse import parse_qsl
from subscription import SubscriptionManager, CARD_UNAVAILABLE_REASON_CODES, _normalise_reason_code
from time_utils import now_msk
import database
from config import config

logger = logging.getLogger(__name__)
router = APIRouter()
subscription_manager = SubscriptionManager()


def plan_from_amount(amount_rub) -> str | None:
    """Map a successful payment amount to the access plan it purchased."""
    try:
        amount = int(round(float(amount_rub) * 100))
    except (TypeError, ValueError):
        return None

    plans = config.SUBSCRIPTION_PLANS
    if amount == plans["HIGH"][0]:
        return "HIGH"
    if amount == plans["MEDIUM"][0]:
        return "MEDIUM"
    if amount == plans["LOW"][0]:
        return "LOW"
    return None


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


async def _read_cloudpayments_payload(request: Request) -> dict:
    """Accept both the legacy JSON envelope and native CloudPayments POSTs.

    CloudPayments sends the payload as a flat form when the notification
    format is set to ``CloudPayments``.  Older deployments of this project
    expected a JSON object containing ``EventType`` and ``Model``.
    """
    raw_body = await request.body()
    if not raw_body:
        return {}

    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        payload = json.loads(raw_body)
        if not isinstance(payload, dict):
            raise ValueError("Webhook JSON must be an object")
        return payload

    return dict(parse_qsl(raw_body.decode("utf-8"), keep_blank_values=True))


def _normalise_cloudpayments_payload(body: dict) -> tuple[str | None, dict]:
    """Return event type and payment model for either supported format."""
    model = body.get("Model")
    if isinstance(model, str):
        try:
            model = json.loads(model)
        except json.JSONDecodeError:
            model = None

    if not isinstance(model, dict):
        model = body

    event_type = body.get("EventType") or body.get("Type")
    return str(event_type).strip().lower() if event_type else None, model


async def _journal_webhook_event(
    body: dict,
    *,
    event_type: str | None,
    transaction_id,
    account_id,
    status,
    is_recurrent: bool,
) -> None:
    """Persist a received payload without affecting webhook acknowledgement."""
    conn = None
    try:
        conn = await database.get_conn()
        await database.record_webhook_event(
            conn,
            payload=body,
            event_type=event_type,
            transaction_id=transaction_id,
            account_id=account_id,
            status=status,
            is_recurrent=is_recurrent,
        )
    except Exception:
        # A database journal outage must not make CloudPayments retry a
        # payment that was otherwise processed successfully.
        logger.exception("Failed to persist CloudPayments webhook in webhook_events")
    finally:
        if conn is not None:
            await database.release_conn(conn)


@router.post("/webhook")
async def handle_webhook(request: Request):
    try:
        body = await _read_cloudpayments_payload(request)
        event_type, model = _normalise_cloudpayments_payload(body)

        account_id = model.get("AccountId")
        status = model.get("Status")
        amount_rub = model.get("Amount", 0)
        transaction_id = model.get("TransactionId")
        raw_reason_code = model.get("ReasonCode")
        if raw_reason_code is None:
            raw_reason_code = body.get("ReasonCode")
        reason_code = _normalise_reason_code(raw_reason_code)
        is_recurrent = _as_bool(model.get("IsRecurrent", False))
        invoice_id = model.get("InvoiceId") or body.get("InvoiceId")

        # Token charges created by this project use a stable invoice prefix.
        # CloudPayments normally supplies IsRecurrent, but some Pay
        # notifications omit it while retaining InvoiceId.
        if not is_recurrent and str(invoice_id or "").startswith("sub_"):
            is_recurrent = True

        logger.info(
            "Received webhook: event_type=%s transaction_id=%s account_id=%s status=%s is_recurrent=%s",
            event_type or "pay",
            transaction_id,
            account_id,
            status,
            is_recurrent,
        )

        # A Pay notification in native CloudPayments format has no EventType:
        # the event is identified by the configured callback URL.  Ignore
        # explicitly unrelated events, but acknowledge them to prevent retries.
        if event_type not in {None, "payment", "pay", "recurrent"}:
            # CloudPayments expects the acknowledgement body to be exactly
            # the documented JSON response.  Keep internal details in logs,
            # not in the callback response.
            await _journal_webhook_event(
                body,
                event_type=event_type,
                transaction_id=transaction_id,
                account_id=account_id,
                status=status,
                is_recurrent=is_recurrent,
            )
            return JSONResponse({"code": 0})

        card_token = model.get("Token")
        subscription_id = model.get("SubscriptionId")

        # Token charges are reported through Pay.  Some CloudPayments payloads
        # do not flag them as IsRecurrent, so classify a pending retry_low
        # subscription by its server-side state rather than by the form format.
        if not is_recurrent and status != "Completed" and account_id and transaction_id is not None:
            try:
                existing = await database.get_subscription_by_user_id(int(account_id))
                is_recurrent = bool(
                    existing
                    and (
                        existing.get("status") == "retry_low"
                        or (
                            reason_code in CARD_UNAVAILABLE_REASON_CODES
                            and status != "Completed"
                        )
                    )
                )
            except (TypeError, ValueError):
                is_recurrent = False

        await _journal_webhook_event(
            body,
            event_type=event_type,
            transaction_id=transaction_id,
            account_id=account_id,
            status=status,
            is_recurrent=is_recurrent,
        )

        # Recurrent payments must be processed by SubscriptionManager first.
        # Do not let the generic successful-payment handler below overwrite
        # MEDIUM/LOW with HIGH.
        if is_recurrent:
            payload = {
                "AccountId": account_id,
                "Amount": amount_rub,
                "Status": status,
                "TransactionId": transaction_id,
                "SubscriptionId": subscription_id,
                "IsRecurrent": True,
                "ReasonCode": reason_code,
                "CardToken": card_token,
            }

            result = await subscription_manager.handle_payment_webhook(payload)
            logger.info(
                f"Recurrent payment processed for user {account_id}: "
                f"{result.get('message', '')}"
            )
            return JSONResponse({"code": 0})

        # Initial successful payment: this is the only generic webhook path
        # that establishes HIGH access regardless of the amount field.
        if status == "Completed" and account_id:
            try:
                user_id = int(account_id)
                logger.info(
                    f"Successful initial payment for user {user_id}, "
                    f"amount: {amount_rub}₽"
                )

                existing_subscription = await database.get_subscription_by_user_id(user_id)

                if existing_subscription:
                    end_date = now_msk() + timedelta(days=1)
                    await database.update_subscription_status(
                        user_id, "active", end_date
                    )
                    await database.update_subscription_plan(user_id, "HIGH")

                    if card_token:
                        await database.save_card_token(user_id, card_token)
                        logger.info(f"Card token saved for user {user_id}")

                    logger.info(
                        f"User {user_id}: no CloudPayments subscription created "
                        f"(active token-charge system)"
                    )
                else:
                    end_date = now_msk() + timedelta(days=1)
                    await database.create_subscription(
                        user_id=user_id,
                        plan="HIGH",
                        status="active",
                        end_date=end_date,
                        card_token=card_token,
                        subscription_id=None,
                    )
                    logger.info(f"Created subscription for user {user_id}")

                await database.remove_from_unsubscribed(user_id)
                return JSONResponse({"code": 0})

            except Exception as e:
                logger.error(f"Subscription activation error: {e}")
                return JSONResponse({"code": 0})

        return JSONResponse({"code": 0})

    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        logger.error("Invalid webhook payload received")
        raise HTTPException(status_code=400, detail="Invalid webhook payload")
    except Exception as e:
        logger.error(f"Error processing webhook: {e}", exc_info=True)
        return JSONResponse({"code": 0})


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


@router.post("/subscription/process-expired")
async def process_expired_subscription_endpoint():
    result = await subscription_manager.check_expired_subscriptions()
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
        "status": subscription.get("status"),
        "plan": subscription.get("plan"),
        "end_date": subscription.get("end_date").isoformat() if subscription.get("end_date") else None,
        "card_token": subscription.get("card_token"),
        "subscription_id": subscription.get("subscription_id"),
        "created_at": subscription.get("created_at").isoformat() if subscription.get("created_at") else None
    }


@router.post("/subscription/cancel/{user_id}")
async def cancel_subscription(user_id: str):
    subscription = await subscription_manager.get_user_subscription(user_id)

    if not subscription:
        return {"success": False, "message": "User not found"}

    subscription_id = subscription.get("subscription_id")
    if subscription_id:
        result = await subscription_manager.client.cancel_subscription(subscription_id)
        if result["success"]:
            await database.update_subscription_status(int(user_id), "cancelled")
            return {"success": True, "message": "Subscription cancelled"}
        else:
            return {"success": False, "error": result.get("error")}

    return {"success": False, "message": "No subscription to cancel"}


@router.post("/subscription/check-expired")
async def check_expired_subscriptions():
    result = await subscription_manager.check_expired_subscriptions()
    return result
