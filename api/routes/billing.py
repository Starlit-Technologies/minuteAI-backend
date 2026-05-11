import os
from datetime import datetime

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.deps import get_db, get_current_user
from models.user import PromoCode, PromoCodeRedemption, User

router = APIRouter(prefix="/billing", tags=["billing"])

stripe.api_key     = os.environ.get("STRIPE_SECRET_KEY", "")
WEBHOOK_SECRET     = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
APP_URL            = os.environ.get("APP_URL", "http://localhost:5174")

PRICE_IDS = {
    "pro":  os.environ.get("STRIPE_PRO_PRICE_ID",  ""),
    "team": os.environ.get("STRIPE_TEAM_PRICE_ID", ""),
}
PLAN_FROM_PRICE = {v: k for k, v in PRICE_IDS.items() if v}


# ── helpers ───────────────────────────────────────────────────────────────────

def _fmt_price(amount: int, currency: str) -> str:
    symbols = {"gbp": "£", "usd": "$", "eur": "€"}
    sym = symbols.get(currency.lower(), currency.upper())
    val = amount / 100
    return f"{sym}{int(val)}" if val == int(val) else f"{sym}{val:.2f}"

def _get_or_create_customer(user: User, db: Session) -> str:
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


def _get_price_for_plan(plan: str):
    price_id = PRICE_IDS.get(plan)
    if not price_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid plan or Stripe not configured.")
    try:
        return stripe.Price.retrieve(price_id, expand=["product"])
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Unable to retrieve billing plan: {exc}")


def _validate_promo_code(db: Session, raw_code: str) -> PromoCode:
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


def _promo_preview(promo: PromoCode, price) -> dict:
    amount = price.unit_amount or 0
    currency = (price.currency or "gbp").upper()
    if promo.discount_type == "percent":
        discount_amount = round(amount * (promo.discount_value / 100))
        display = f"{promo.discount_value}% off"
    else:
        discount_amount = min(amount, promo.discount_value)
        display = _fmt_price(promo.discount_value, price.currency or "gbp")
        display = f"{display} off"

    final_amount = max(amount - discount_amount, 0)
    return {
        "code": promo.code,
        "description": promo.description,
        "discount_type": promo.discount_type,
        "discount_value": promo.discount_value,
        "display_discount": display,
        "amount": amount,
        "discount_amount": discount_amount,
        "final_amount": final_amount,
        "currency": currency,
        "expires_at": promo.expires_at.isoformat() if promo.expires_at else None,
        "remaining_uses": None if promo.max_uses is None else max(promo.max_uses - promo.uses_count, 0),
    }


def _build_checkout_discount(promo: PromoCode, price):
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


# ── schemas ───────────────────────────────────────────────────────────────────

class CheckoutIn(BaseModel):
    plan:   str            # "pro" | "team"
    origin: str | None = None  # window.location.origin from the browser
    promo_code: str | None = None


class PortalIn(BaseModel):
    origin: str | None = None


class PromoCodeIn(BaseModel):
    code: str
    plan: str | None = None


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.get("/plans")
def list_plans():
    """Fetch paid plan details directly from Stripe products."""
    if not stripe.api_key:
        return []
    plans = []
    for plan_key, price_id in PRICE_IDS.items():
        if not price_id:
            continue
        try:
            price   = stripe.Price.retrieve(price_id, expand=["product"])
            product = price.product

            if getattr(product, "active", False) is False:
                continue

            # marketing_features is a list of StripeObject with a .name attr
            raw_features = getattr(product, "marketing_features", None) or []
            features = [
                f.name if hasattr(f, "name") else f.get("name", "")
                for f in raw_features
            ]

            recurring = getattr(price, "recurring", None)
            interval  = getattr(recurring, "interval", None) if recurring else None

            plans.append({
                "key":         plan_key,
                "price_id":    price_id,
                "name":        getattr(product, "name", plan_key.title()),
                "description": getattr(product, "description", "") or "",
                "price":       _fmt_price(price.unit_amount or 0, price.currency or "gbp"),
                "amount":      price.unit_amount or 0,
                "currency":    (price.currency or "gbp").upper(),
                "interval":    interval,
                "features":    features,
            })
        except Exception:
            continue
    return sorted(plans, key=lambda p: p["amount"])


@router.post("/checkout")
def create_checkout(
    body: CheckoutIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    price = _get_price_for_plan(body.plan)

    customer_id = _get_or_create_customer(current_user, db)
    base = (body.origin or APP_URL).rstrip("/")
    metadata = {"user_id": current_user.id}
    checkout_kwargs = {}

    if body.promo_code:
        promo = _validate_promo_code(db, body.promo_code)
        checkout_kwargs["discounts"] = [_build_checkout_discount(promo, price)]
        metadata["promo_code"] = promo.code

    session = stripe.checkout.Session.create(
        customer=customer_id,
        mode="subscription",
        line_items=[{"price": price.id, "quantity": 1}],
        success_url=f"{base}/billing?success=1",
        cancel_url=f"{base}/billing?canceled=1",
        metadata=metadata,
        allow_promotion_codes=False,
        **checkout_kwargs,
    )
    return {"url": session.url}


@router.post("/promocode/validate")
def validate_promocode(
    body: PromoCodeIn,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    promo = _validate_promo_code(db, body.code)
    response = {
        "code": promo.code,
        "description": promo.description,
        "discount_type": promo.discount_type,
        "discount_value": promo.discount_value,
        "display_discount": f"{promo.discount_value}% off" if promo.discount_type == "percent" else "Fixed discount",
        "expires_at": promo.expires_at.isoformat() if promo.expires_at else None,
        "remaining_uses": None if promo.max_uses is None else max(promo.max_uses - promo.uses_count, 0),
    }
    if body.plan:
        response.update(_promo_preview(promo, _get_price_for_plan(body.plan)))
    return response


@router.get("/subscription")
def get_subscription(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return detailed subscription info for the manage-subscription page."""
    if not current_user.stripe_customer_id or not stripe.api_key:
        return {"status": "free", "plan": current_user.plan}

    try:
        # Prefer stored subscription ID for a direct retrieve
        sub_id = current_user.stripe_subscription_id
        if sub_id:
            sub = stripe.Subscription.retrieve(sub_id, expand=["items.data.price"])
        else:
            subs = stripe.Subscription.list(
                customer=current_user.stripe_customer_id,
                status="active",
                limit=1,
                expand=["data.items.data.price"],
            )
            if not subs.data:
                return {"status": "free", "plan": current_user.plan}
            sub = subs.data[0]

        if sub.status not in ("active", "trialing", "past_due"):
            return {"status": sub.status, "plan": current_user.plan}

        price = sub.items.data[0].price
        product = stripe.Product.retrieve(
            price.product if isinstance(price.product, str) else price.product.id
        )

        # Period dates — newer Stripe API versions put them on the subscription item
        item        = sub.items.data[0]
        period_start = (
            getattr(sub,  "current_period_start", None) or
            getattr(item, "current_period_start", None)
        )
        period_end   = (
            getattr(sub,  "current_period_end",   None) or
            getattr(item, "current_period_end",   None)
        )

        # Payment method — list all cards on the customer, most reliable approach
        card = None
        pms = stripe.PaymentMethod.list(customer=current_user.stripe_customer_id, type="card", limit=1)
        if pms.data:
            pm_card = getattr(pms.data[0], "card", None)
            if pm_card:
                card = {
                    "brand":     getattr(pm_card, "brand",     ""),
                    "last4":     getattr(pm_card, "last4",     ""),
                    "exp_month": getattr(pm_card, "exp_month", ""),
                    "exp_year":  getattr(pm_card, "exp_year",  ""),
                }

        recurring = getattr(price, "recurring", None)
        return {
            "id":                   sub.id,
            "plan":                 PLAN_FROM_PRICE.get(price.id, current_user.plan),
            "plan_name":            getattr(product, "name", current_user.plan.title()),
            "status":               sub.status,
            "current_period_start": period_start,
            "current_period_end":   period_end,
            "cancel_at_period_end": getattr(sub, "cancel_at_period_end", False),
            "cancel_at":            getattr(sub, "cancel_at", None),
            "amount":               price.unit_amount or 0,
            "currency":             price.currency or "gbp",
            "interval":             getattr(recurring, "interval", None) if recurring else None,
            "card":                 card,
        }
    except Exception as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))


@router.post("/sync")
def sync_subscription(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Pull the user's active Stripe subscription and update their plan in DB."""
    if not current_user.stripe_customer_id or not stripe.api_key:
        return {"plan": current_user.plan}

    try:
        subs = stripe.Subscription.list(
            customer=current_user.stripe_customer_id,
            status="active",
            limit=1,
            expand=["data.items.data.price"],
        )
        if not subs.data:
            # No active subscription — revert to free if they cancelled
            if current_user.plan != "free":
                current_user.plan = "free"
                current_user.stripe_subscription_id = None
                db.commit()
            return {"plan": current_user.plan}

        sub      = subs.data[0]
        price_id = sub.items.data[0].price.id
        plan     = PLAN_FROM_PRICE.get(price_id, current_user.plan)

        current_user.plan = plan
        current_user.stripe_subscription_id = sub.id
        db.commit()
        return {"plan": plan}
    except stripe.error.StripeError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))


@router.post("/portal")
def create_portal(
    body: PortalIn = PortalIn(),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not current_user.stripe_customer_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No billing account found.")
    base = (body.origin or APP_URL).rstrip("/")
    session = stripe.billing_portal.Session.create(
        customer=current_user.stripe_customer_id,
        return_url=f"{base}/billing",
    )
    return {"url": session.url}


@router.get("/invoices")
def list_invoices(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not current_user.stripe_customer_id or not stripe.api_key:
        return []
    try:
        invoices = stripe.Invoice.list(customer=current_user.stripe_customer_id, limit=24)
        return [
            {
                "id":     inv.id,
                "amount": f"£{inv.amount_paid / 100:.2f}",
                "date":   inv.created,
                "status": inv.status,
                "pdf":    inv.invoice_pdf,
                "desc":   (inv.lines.data[0].description or "Subscription") if inv.lines.data else "Subscription",
            }
            for inv in invoices.data
        ]
    except stripe.error.StripeError:
        return []


@router.post("/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig     = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid webhook signature.")

    etype = event["type"]
    data  = event["data"]["object"]

    if etype == "checkout.session.completed":
        user_id = data.get("metadata", {}).get("user_id")
        sub_id  = data.get("subscription")
        promo_code = data.get("metadata", {}).get("promo_code")
        if user_id and sub_id:
            sub      = stripe.Subscription.retrieve(sub_id)
            price_id = sub["items"]["data"][0]["price"]["id"]
            plan     = PLAN_FROM_PRICE.get(price_id, "pro")
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                user.plan = plan
                user.stripe_subscription_id = sub_id
                db.commit()
            if promo_code and not db.query(PromoCodeRedemption).filter(
                PromoCodeRedemption.checkout_session_id == data.get("id")
            ).first():
                promo = db.query(PromoCode).filter(PromoCode.code == promo_code).first()
                if promo:
                    promo.uses_count += 1
                    db.add(PromoCodeRedemption(
                        promo_code_id=promo.id,
                        user_id=user_id,
                        checkout_session_id=data.get("id"),
                        subscription_id=sub_id,
                    ))
                    db.commit()

    elif etype in ("customer.subscription.updated", "customer.subscription.deleted"):
        sub_id = data["id"]
        user   = db.query(User).filter(User.stripe_subscription_id == sub_id).first()
        if user:
            if etype == "customer.subscription.deleted" or data["status"] in ("canceled", "unpaid"):
                user.plan = "free"
                user.stripe_subscription_id = None
            else:
                price_id = data["items"]["data"][0]["price"]["id"]
                user.plan = PLAN_FROM_PRICE.get(price_id, user.plan)
            db.commit()

    return {"received": True}
