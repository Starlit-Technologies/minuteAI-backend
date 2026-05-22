from typing import Optional
from pydantic import BaseModel, EmailStr, Field


class RegisterIn(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    email: EmailStr
    password: str = Field(..., min_length=8)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class VerifyLogin2FAIn(BaseModel):
    user_id: str
    code: str = Field(..., min_length=6, max_length=6)


class Start2FASetupIn(BaseModel):
    current_password: Optional[str] = None  # not required for Google-authed users


class Confirm2FASetupIn(BaseModel):
    code: str = Field(..., min_length=6, max_length=6)


class Disable2FAIn(BaseModel):
    current_password: Optional[str] = None  # not required for Google-authed users


class VerifyEmailIn(BaseModel):
    user_id: str
    code: str = Field(..., min_length=6, max_length=6)


class ResendOTPIn(BaseModel):
    user_id: str
    type: str = "verify_email"


class RefreshIn(BaseModel):
    refresh_token: str


class GoogleAuthIn(BaseModel):
    access_token: str


class ForgotPasswordIn(BaseModel):
    email: EmailStr


class ResetPasswordIn(BaseModel):
    user_id: str
    code: str = Field(..., min_length=6, max_length=6)
    new_password: str = Field(..., min_length=8)
