from pydantic import BaseModel


class CheckoutIn(BaseModel):
    plan:       str
    origin:     str | None = None
    promo_code: str | None = None


class PortalIn(BaseModel):
    origin: str | None = None


class PromoCodeIn(BaseModel):
    code: str
    plan: str | None = None


class SetupCheckoutIn(BaseModel):
    return_path: str = "/pdf-to-audio"
    origin: str | None = None


class AttachPaymentMethodIn(BaseModel):
    payment_method_id: str


class SubscribeIn(BaseModel):
    payment_method_id: str | None = None
    plan: str
    interval: str = "month"
    promo_code: str | None = None
