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
