from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from middleware.auth import get_db, get_current_user
from models.user import User
from schemas.auth import (
    Confirm2FASetupIn, Disable2FAIn,
    ForgotPasswordIn, GoogleAuthIn, LoginIn, RefreshIn,
    RegisterIn, ResendOTPIn, ResetPasswordIn, Start2FASetupIn,
    VerifyEmailIn, VerifyLogin2FAIn,
)
import services.core.auth as svc
import services.core.billing as billing_svc

router = APIRouter(prefix="/auth", tags=["auth"])


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


@router.post("/login/2fa")
def verify_login_2fa(body: VerifyLogin2FAIn, db: Session = Depends(get_db)):
    return svc.verify_login_2fa(db, body.user_id, body.code)


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


@router.post("/2fa/start")
def start_2fa_setup(
    body: Start2FASetupIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return svc.start_2fa_setup(db, current_user, body.current_password)


@router.post("/2fa/confirm")
def confirm_2fa_setup(
    body: Confirm2FASetupIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return svc.confirm_2fa_setup(db, current_user, body.code)


@router.post("/2fa/disable")
def disable_2fa(
    body: Disable2FAIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return svc.disable_2fa(db, current_user, body.current_password)


@router.get("/me")
def me(current_user: User = Depends(get_current_user)):
    return {
        "id":                   current_user.id,
        "name":                 current_user.name,
        "email":                current_user.email,
        "plan":                 current_user.plan,
        "email_verified":       current_user.email_verified,
        "avatar_url":           current_user.avatar_url,
        "last_login_at":        current_user.last_login_at.isoformat() if current_user.last_login_at else None,
        "onboarding_completed": bool(current_user.onboarding_completed),
        "two_factor_enabled":   bool(current_user.two_factor_enabled),
        "role":                 current_user.role,
        "title":                current_user.title,
        "language":             current_user.language,
        "has_payment_method":   billing_svc.check_payment_method(current_user),
        "source":               current_user.source,
        "trial_ends_at":        current_user.trial_ends_at.isoformat() if current_user.trial_ends_at else None,
        "trial_reminder_sent_at": current_user.trial_reminder_sent_at.isoformat() if current_user.trial_reminder_sent_at else None,
    }
