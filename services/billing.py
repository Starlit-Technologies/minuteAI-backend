import os
from datetime import datetime
from typing import Optional

import stripe
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from models.user import PromoCode, PromoCodeRedemption, SubscriptionPlan, User

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
APP_URL        = os.environ.get("APP_URL", "http://localhost:5174")

# Env-var fallback price IDs (used when DB has no plans yet)
PRICE_IDS = {
    "pro":  os.environ.get("STRIPE_PRO_PRICE_ID",  ""),
    "team": os.environ.get("STRIPE_TEAM_PRICE_ID", ""),
}
PLAN_FROM_PRICE = {v: k for k, v in PRICE_IDS.items() if v}


def fmt_price(amount: int, currency: str) -> str:
    symbols = {"gbp": "£", "usd": "$", "eur": "€"}
    sym = symbols.get(currency.lower(), currency.upper())
    val = amount / 100
    return f"{sym}{int(val)}" if val == int(val) else f"{sym}{val:.2f}"


def get_or_create_customer(db: Session, user: User) -> str:
    if user.stripe_customer_id:
        return user.stripe_customer_id
    customer = stripe.Customer.create(
        email=user.email,
        name=user.name,
        metadata={"user_id": user.id},
    )
    user.stripe_customer_id = customer.id
    db.commit()
    return customer.id


def _db_price_id_for_plan(plan: str, db: Optional[Session]) -> Optional[str]:
    if not db:
        return None
    row = db.query(SubscriptionPlan).filter(
        SubscriptionPlan.name == plan,
        SubscriptionPlan.is_active == True,
        SubscriptionPlan.stripe_price_id.isnot(None),
    ).first()
    return row.stripe_price_id if row else None


def get_price_for_plan(plan: str, db: Optional[Session] = None):
    price_id = _db_price_id_for_plan(plan, db) or PRICE_IDS.get(plan)
    if not price_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid plan or Stripe not configured.")
    try:
        return stripe.Price.retrieve(price_id, expand=["product"])
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Unable to retrieve billing plan: {exc}")


def plan_name_from_price_id(price_id: str, db: Optional[Session] = None, default: str = "pro") -> str:
    if db:
        row = db.query(SubscriptionPlan).filter(SubscriptionPlan.stripe_price_id == price_id).first()
        if row:
            return row.name
    return PLAN_FROM_PRICE.get(price_id, default)


def validate_promo_code(db: Session, raw_code: str) -> PromoCode:
    code = (raw_code or "").strip().upper()
    if not code:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Promo code is required.")
    promo = db.query(PromoCode).filter(PromoCode.code == code).first()
    if not promo:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Promo code not found.")
    if not promo.active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This promo code is inactive.")
    if promo.expires_at and promo.expires_at <= datetime.utcnow():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This promo code has expired.")
    if promo.max_uses is not None and promo.uses_count >= promo.max_uses:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This promo code has reached its usage limit.")
    if promo.discount_value <= 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This promo code is invalid.")
    return promo


def promo_preview(promo: PromoCode, price) -> dict:
    amount = price.unit_amount or 0
    currency = (price.currency or "gbp").upper()
    if promo.discount_type == "percent":
        discount_amount = round(amount * (promo.discount_value / 100))
        display = f"{promo.discount_value}% off"
    else:
        discount_amount = min(amount, promo.discount_value)
        display = f"{fmt_price(promo.discount_value, price.currency or 'gbp')} off"
    final_amount = max(amount - discount_amount, 0)
    return {
        "code":             promo.code,
        "description":      promo.description,
        "discount_type":    promo.discount_type,
        "discount_value":   promo.discount_value,
        "display_discount": display,
        "amount":           amount,
        "discount_amount":  discount_amount,
        "final_amount":     final_amount,
        "currency":         currency,
        "expires_at":       promo.expires_at.isoformat() if promo.expires_at else None,
        "remaining_uses":   None if promo.max_uses is None else max(promo.max_uses - promo.uses_count, 0),
    }


def build_checkout_discount(promo: PromoCode, price) -> dict:
    params = {
        "duration": "once",
        "name": promo.code,
        "metadata": {"promo_code": promo.code},
    }
    if promo.discount_type == "percent":
        params["percent_off"] = promo.discount_value
    else:
        params["amount_off"] = min(price.unit_amount or 0, promo.discount_value)
        params["currency"] = (price.currency or "gbp").lower()
    coupon = stripe.Coupon.create(**params)
    return {"coupon": coupon.id}


def record_promo_redemption(db: Session, promo: PromoCode, user_id: str, checkout_session_id: str, subscription_id: str):
    if db.query(PromoCodeRedemption).filter(
        PromoCodeRedemption.checkout_session_id == checkout_session_id
    ).first():
        return
    promo.uses_count += 1
    db.add(PromoCodeRedemption(
        promo_code_id=promo.id,
        user_id=user_id,
        checkout_session_id=checkout_session_id,
        subscription_id=subscription_id,
    ))
    db.commit()
