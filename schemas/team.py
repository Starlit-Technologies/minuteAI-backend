from typing import Optional
from pydantic import BaseModel, EmailStr


class InviteIn(BaseModel):
    email: EmailStr
    role:  str = "member"
    name:  Optional[str] = None


class UpdateRoleIn(BaseModel):
    role: str
