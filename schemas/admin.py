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
