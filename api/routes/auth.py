from fastapi import APIRouter, Depends, Body
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from api.deps import get_db, get_current_user
from models.user import User
import services.auth as svc

router = APIRouter(prefix="/auth", tags=["auth"])


# ── request / response schemas ────────────────────────────────────────────────

class RegisterIn(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    email: EmailStr
    password: str = Field(..., min_length=8)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


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


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.post("/register", status_code=201)
def register(body: RegisterIn, db: Session = Depends(get_db)):
    return svc.register(db, body.name, body.email, body.password)


@router.post("/verify-email")
def verify_email(body: VerifyEmailIn, db: Session = Depends(get_db)):
    return svc.verify_email(db, body.user_id, body.code)


@router.post("/resend-otp")
def resend_otp(body: ResendOTPIn, db: Session = Depends(get_db)):
    return svc.resend_otp(db, body.user_id, body.type)


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    return svc.login(db, body.email, body.password)


@router.post("/refresh")
def refresh(body: RefreshIn, db: Session = Depends(get_db)):
    return svc.refresh_access_token(db, body.refresh_token)


@router.post("/google")
def google_auth(body: GoogleAuthIn, db: Session = Depends(get_db)):
    return svc.google_sign_in(db, body.access_token)


@router.post("/forgot-password")
def forgot_password(body: ForgotPasswordIn, db: Session = Depends(get_db)):
    return svc.request_password_reset(db, body.email)


@router.post("/reset-password")
def reset_password(body: ResetPasswordIn, db: Session = Depends(get_db)):
    return svc.reset_password(db, body.user_id, body.code, body.new_password)


@router.get("/me")
def me(current_user: User = Depends(get_current_user)):
    return {
        "id":             current_user.id,
        "name":           current_user.name,
        "email":          current_user.email,
        "plan":           current_user.plan,
        "email_verified": current_user.email_verified,
        "avatar_url":     current_user.avatar_url,
    }
