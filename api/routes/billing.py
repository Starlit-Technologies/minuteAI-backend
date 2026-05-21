import os

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from middleware.auth import get_db, get_current_user
from models.user import PromoCode, PromoCodeRedemption, SubscriptionPlan, User
from schemas.billing import CheckoutIn, PortalIn, PromoCodeIn, SetupCheckoutIn, AttachPaymentMethodIn, SubscribeIn
import services.billing as svc

router = APIRouter(prefix="/billing", tags=["billing"])

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
APP_URL        = os.environ.get("APP_URL", "http://localhost:5174")


@router.get("/plans")
def list_plans(db: Session = Depends(get_db)):
    import json as _json
    db_plans = (
        db.query(SubscriptionPlan)
        .filter(SubscriptionPlan.is_active == True)
        .order_by(SubscriptionPlan.sort_order, SubscriptionPlan.created_at)
        .all()
    )
    if db_plans:
        result = []
        for p in db_plans:
            features = []
            if p.features_json:
                try:
                    features = _json.loads(p.features_json)
                except Exception:
                    pass
            result.append({
                "name":                  p.name,
                "display_name":          p.display_name,
                "description":           p.description or "",
                "price_monthly":         p.price_monthly,
                "currency":              (p.currency or "usd").lower(),
                "interval":              p.interval or "month",
                "features":              features,
                "meetings_per_month":    p.meetings_per_month,
                "audio_hours_per_month": p.audio_hours_per_month,
                "stripe_price_id":       p.stripe_price_id,
                "is_popular":            bool(p.is_popular),
                "sort_order":            p.sort_order or 0,
            })
        return result

    # Fall back to Stripe-based listing when no DB plans exist
    if not stripe.api_key:
        return []
    plans = []
    for plan_key, price_id in svc.PRICE_IDS.items():
        if not price_id:
            continue
        try:
            price   = stripe.Price.retrieve(price_id, expand=["product"])
            product = price.product
            if getattr(product, "active", False) is False:
                continue
            raw_features = getattr(product, "marketing_features", None) or []
            features = [
                f.name if hasattr(f, "name") else f.get("name", "")
                for f in raw_features
            ]
            recurring = getattr(price, "recurring", None)
            interval  = getattr(recurring, "interval", None) if recurring else None
            plans.append({
                "name":          plan_key,
                "display_name":  getattr(product, "name", plan_key.title()),
                "description":   getattr(product, "description", "") or "",
                "price_monthly": price.unit_amount or 0,
                "currency":      (price.currency or "gbp").lower(),
                "interval":      interval,
                "features":      features,
                "stripe_price_id": price_id,
                "is_popular":    False,
                "meetings_per_month": None,
                "audio_hours_per_month": None,
            })
        except Exception:
            continue
    return sorted(plans, key=lambda p: p["price_monthly"])


@router.post("/checkout")
def create_checkout(
    body: CheckoutIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    price = svc.get_price_for_plan(body.plan, db)
    customer_id = svc.get_or_create_customer(db, current_user)
    base = (body.origin or APP_URL).rstrip("/")
    metadata = {"user_id": current_user.id}
    checkout_kwargs = {}

    if body.promo_code:
        promo = svc.validate_promo_code(db, body.promo_code)
        checkout_kwargs["discounts"] = [svc.build_checkout_discount(promo, price)]
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
    promo = svc.validate_promo_code(db, body.code)
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
        response.update(svc.promo_preview(promo, svc.get_price_for_plan(body.plan, db)))
    return response


@router.get("/subscription")
def get_subscription(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not current_user.stripe_customer_id or not stripe.api_key:
        return {"status": "free", "plan": current_user.plan}

    try:
        sub_id = current_user.stripe_subscription_id
        if sub_id:
            sub = stripe.Subscription.retrieve(sub_id, expand=["items.data.price"])
        else:
            subs = stripe.Subscription.list(
                customer=current_user.stripe_customer_id,
                status="all", limit=10,
                expand=["data.items.data.price"],
            )
            subs_data = [s for s in subs.data if s.status in ("active", "trialing", "past_due")]
            if not subs_data:
                return {"status": "free", "plan": current_user.plan}
            sub = subs_data[0]

        if sub.status not in ("active", "trialing", "past_due"):
            return {"status": sub.status, "plan": current_user.plan}

        price = sub.items.data[0].price
        product = stripe.Product.retrieve(
            price.product if isinstance(price.product, str) else price.product.id
        )

        item = sub.items.data[0]
        period_start = getattr(sub, "current_period_start", None) or getattr(item, "current_period_start", None)
        period_end   = getattr(sub, "current_period_end",   None) or getattr(item, "current_period_end",   None)

        card = None
        pms = stripe.PaymentMethod.list(customer=current_user.stripe_customer_id, type="card", limit=1)
        if pms.data:
            pm_card = getattr(pms.data[0], "card", None)
            if pm_card:
                card = {
                    "brand": getattr(pm_card, "brand", ""), "last4": getattr(pm_card, "last4", ""),
                    "exp_month": getattr(pm_card, "exp_month", ""), "exp_year": getattr(pm_card, "exp_year", ""),
                }

        recurring = getattr(price, "recurring", None)
        return {
            "id":                   sub.id,
            "plan":                 svc.plan_name_from_price_id(price.id, db, default=current_user.plan),
            "plan_name":            getattr(product, "name", current_user.plan.title()),
            "status":               sub.status,
            "current_period_start": period_start,
            "current_period_end":   period_end,
            "cancel_at_period_end": getattr(sub, "cancel_at_period_end", False),
            "cancel_at":            getattr(sub, "cancel_at", None),
            "trial_end":            getattr(sub, "trial_end", None),
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
    if not current_user.stripe_customer_id or not stripe.api_key:
        return {"plan": current_user.plan}

    try:
        subs = stripe.Subscription.list(
            customer=current_user.stripe_customer_id,
            status="all", limit=10,
            expand=["data.items.data.price"],
        )
        subs_data = [s for s in subs.data if s.status in ("active", "trialing", "past_due")]
        if not subs_data:
            if current_user.plan != "free":
                current_user.plan = "free"
                current_user.stripe_subscription_id = None
                db.commit()
            return {"plan": current_user.plan}

        sub      = subs_data[0]
        price_id = sub.items.data[0].price.id
        plan     = svc.plan_name_from_price_id(price_id, db, default=current_user.plan)
        current_user.plan = plan
        current_user.stripe_subscription_id = sub.id
        if getattr(sub, "trial_end", None):
            from datetime import datetime
            current_user.trial_ends_at = datetime.utcfromtimestamp(sub.trial_end)
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
                "id": inv.id, "amount": f"£{inv.amount_paid / 100:.2f}",
                "date": inv.created, "status": inv.status,
                "pdf": inv.invoice_pdf,
                "desc": (inv.lines.data[0].description or "Subscription") if inv.lines.data else "Subscription",
            }
            for inv in invoices.data
        ]
    except stripe.error.StripeError:
        return []


@router.get("/payment-method")
def get_payment_method(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not current_user.stripe_customer_id or not stripe.api_key:
        return {"card": None}
    try:
        pms = stripe.PaymentMethod.list(
            customer=current_user.stripe_customer_id, type="card", limit=1
        )
        if not pms.data:
            return {"card": None}
        pm   = pms.data[0]
        card = getattr(pm, "card", None)
        if not card:
            return {"card": None}
        return {
            "card": {
                "payment_method_id": pm.id,
                "brand":     card.brand,
                "last4":     card.last4,
                "exp_month": card.exp_month,
                "exp_year":  card.exp_year,
            }
        }
    except stripe.error.StripeError:
        return {"card": None}


def _get_default_or_first_card(customer_id: str):
    customer = stripe.Customer.retrieve(customer_id)
    default_pm = getattr(getattr(customer, "invoice_settings", None), "default_payment_method", None)
    if default_pm:
        return default_pm if isinstance(default_pm, str) else default_pm.id
    pms = stripe.PaymentMethod.list(customer=customer_id, type="card", limit=1)
    return pms.data[0].id if pms.data else None


@router.delete("/payment-method")
def remove_payment_method(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.plan != "free":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Cancel your subscription before removing your payment method.",
        )
    if not current_user.stripe_customer_id or not stripe.api_key:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No billing account found.")
    try:
        pms = stripe.PaymentMethod.list(
            customer=current_user.stripe_customer_id, type="card", limit=10
        )
        for pm in pms.data:
            stripe.PaymentMethod.detach(pm.id)
    except stripe.error.StripeError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    return {"ok": True}


@router.get("/has-payment-method")
def has_payment_method(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.plan != "free":
        return {"connected": True}
    if not current_user.stripe_customer_id or not stripe.api_key:
        return {"connected": False}
    try:
        pms = stripe.PaymentMethod.list(
            customer=current_user.stripe_customer_id, type="card", limit=1
        )
        return {"connected": bool(pms.data)}
    except stripe.error.StripeError:
        return {"connected": False}


@router.post("/attach-payment-method")
def attach_payment_method(
    body: AttachPaymentMethodIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not stripe.api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Billing not configured.")
    customer_id = svc.get_or_create_customer(db, current_user)
    try:
        stripe.PaymentMethod.attach(body.payment_method_id, customer=customer_id)
        stripe.Customer.modify(
            customer_id,
            invoice_settings={"default_payment_method": body.payment_method_id},
        )
        trial = svc.ensure_signup_trial(db, current_user, customer_id, body.payment_method_id)
    except stripe.error.CardError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, e.user_message or "Card declined.")
    except stripe.error.StripeError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    return {"ok": True, "trial": trial}


@router.post("/subscribe")
def subscribe_to_plan(
    body: SubscribeIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not stripe.api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Billing not configured.")
    price = svc.get_price_for_plan(body.plan, db)
    customer_id = svc.get_or_create_customer(db, current_user)
    try:
        payment_method_id = body.payment_method_id
        if payment_method_id:
            stripe.PaymentMethod.attach(payment_method_id, customer=customer_id)
            stripe.Customer.modify(
                customer_id,
                invoice_settings={"default_payment_method": payment_method_id},
            )
        else:
            payment_method_id = _get_default_or_first_card(customer_id)
            if not payment_method_id:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Add a payment method before upgrading.")

        sub_kwargs: dict = {
            "customer": customer_id,
            "items": [{"price": price.id}],
            "default_payment_method": payment_method_id,
            "payment_behavior": "default_incomplete",
            "payment_settings": {"save_default_payment_method": "on_subscription"},
            "expand": ["latest_invoice.payment_intent"],
            "metadata": {"user_id": current_user.id},
        }
        if body.promo_code:
            promo = svc.validate_promo_code(db, body.promo_code)
            sub_kwargs["discounts"] = [svc.build_checkout_discount(promo, price)]
            sub_kwargs["metadata"]["promo_code"] = promo.code

        sub = stripe.Subscription.create(**sub_kwargs)
    except stripe.error.CardError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, e.user_message or "Card declined.")
    except stripe.error.StripeError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))

    invoice = sub.latest_invoice
    pi = getattr(invoice, "payment_intent", None) if invoice else None

    if pi and getattr(pi, "status", None) == "requires_action":
        return {
            "status": "requires_action",
            "subscription_id": sub.id,
            "client_secret": pi.client_secret,
        }

    if pi and getattr(pi, "status", None) == "requires_payment_method":
        stripe.Subscription.cancel(sub.id)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Payment failed. Please check your card details.")

    plan_name = svc.plan_name_from_price_id(price.id, db, default=body.plan)
    current_user.plan = plan_name
    current_user.stripe_subscription_id = sub.id
    db.commit()
    return {"status": "active", "subscription_id": sub.id}


@router.post("/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig     = request.headers.get("stripe-signature", "")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid webhook signature.")

    etype = event["type"]
    data  = event["data"]["object"]

    if etype == "checkout.session.completed":
        user_id    = data.get("metadata", {}).get("user_id")
        sub_id     = data.get("subscription")
        promo_code = data.get("metadata", {}).get("promo_code")
        if user_id and sub_id:
            sub      = stripe.Subscription.retrieve(sub_id)
            price_id = sub["items"]["data"][0]["price"]["id"]
            plan     = svc.plan_name_from_price_id(price_id, db)
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                user.plan = plan
                user.stripe_subscription_id = sub_id
                db.commit()
            if promo_code:
                promo = db.query(PromoCode).filter(PromoCode.code == promo_code).first()
                if promo:
                    svc.record_promo_redemption(db, promo, user_id, data.get("id"), sub_id)

    elif etype in ("customer.subscription.updated", "customer.subscription.deleted"):
        sub_id = data["id"]
        user   = db.query(User).filter(User.stripe_subscription_id == sub_id).first()
        if user:
            if etype == "customer.subscription.deleted" or data["status"] in ("canceled", "unpaid"):
                user.plan = "free"
                user.stripe_subscription_id = None
            else:
                price_id = data["items"]["data"][0]["price"]["id"]
                user.plan = svc.plan_name_from_price_id(price_id, db, default=user.plan)
                if data.get("trial_end"):
                    from datetime import datetime
                    user.trial_ends_at = datetime.utcfromtimestamp(data["trial_end"])
            db.commit()

    return {"received": True}
