from typing import Optional
from pydantic import BaseModel


class UpdateAdminUserIn(BaseModel):
    name:           Optional[str]  = None
    plan:           Optional[str]  = None
    email_verified: Optional[bool] = None


class UpdateAdminWorkspaceIn(BaseModel):
    name: str


class UpdateAdminTaskIn(BaseModel):
    status:   Optional[str] = None
    deadline: Optional[str] = None


class CreateAdminGrantIn(BaseModel):
    email: str


class CreatePromoCodeIn(BaseModel):
    code:           str
    description:    Optional[str] = None
    discount_type:  str = "percent"
    discount_value: int
    active:         bool = True
    max_uses:       Optional[int] = None
    expires_at:     Optional[str] = None


class UpdatePromoCodeIn(BaseModel):
    description:    Optional[str]  = None
    discount_type:  Optional[str]  = None
    discount_value: Optional[int]  = None
    active:         Optional[bool] = None
    max_uses:       Optional[int]  = None
    expires_at:     Optional[str]  = None


class CreatePlanIn(BaseModel):
    name:                  str
    display_name:          str
    description:           Optional[str]       = None
    price_monthly:         int                 = 0   # cents
    currency:              str                 = "usd"
    interval:              str                 = "month"
    features:              list[str]           = []
    meetings_per_month:    Optional[int]       = None  # None = unlimited
    audio_hours_per_month: Optional[int]       = None  # None = unlimited
    max_workspaces:        Optional[int]       = None  # None = unlimited
    max_members:           Optional[int]       = None  # None = unlimited per workspace
    is_popular:            bool                = False
    sort_order:            int                 = 0
    sync_stripe:           bool                = True  # create product+price in Stripe


class UpdatePlanIn(BaseModel):
    display_name:          Optional[str]       = None
    description:           Optional[str]       = None
    price_monthly:         Optional[int]       = None
    currency:              Optional[str]       = None
    interval:              Optional[str]       = None
    features:              Optional[list[str]] = None
    meetings_per_month:    Optional[int]       = None  # None = unlimited
    audio_hours_per_month: Optional[int]       = None  # None = unlimited
    max_workspaces:        Optional[int]       = None  # None = unlimited
    max_members:           Optional[int]       = None  # None = unlimited per workspace
    is_active:             Optional[bool]      = None
    is_popular:            Optional[bool]      = None
    sort_order:            Optional[int]       = None
    sync_stripe:           bool                = False  # update Stripe product metadata
