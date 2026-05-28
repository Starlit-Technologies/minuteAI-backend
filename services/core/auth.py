import os
import json
import logging
import secrets
import string
import urllib.request
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# ── Disposable email blocklist ─────────────────────────────────────────────────

_DISPOSABLE_DOMAINS: frozenset = frozenset()

def _load_disposable_domains() -> None:
    global _DISPOSABLE_DOMAINS
    url = (
        "https://raw.githubusercontent.com/disposable-email-domains/"
        "disposable-email-domains/master/disposable_email_blocklist.conf"
    )
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            _DISPOSABLE_DOMAINS = frozenset(
                line.strip().lower()
                for line in resp.read().decode().splitlines()
                if line.strip() and not line.startswith("#")
            )
        log.info("Loaded %d disposable email domains", len(_DISPOSABLE_DOMAINS))
    except Exception as exc:
        log.warning("Could not fetch disposable domain list (%s); using fallback", exc)
        _DISPOSABLE_DOMAINS = frozenset({
            "mailinator.com", "guerrillamail.com", "guerrillamailblock.com",
            "guerrillamail.info", "guerrillamail.biz", "guerrillamail.de",
            "grr.la", "sharklasers.com", "spam4.me", "tempmail.com",
            "throwaway.email", "trashmail.com", "trashmail.me", "trashmail.at",
            "yopmail.com", "yopmail.fr", "fakeinbox.com", "maildrop.cc",
            "dispostable.com", "mailnull.com", "spamgourmet.com",
            "tempr.email", "discard.email", "temp-mail.org", "getnada.com",
            "mailtemp.info", "emailondeck.com", "getairmail.com",
        })

_load_disposable_domains()

from fastapi import HTTPException, status
from passlib.context import CryptContext
from jose import jwt, JWTError
from sqlalchemy.orm import Session

from models.user import User, OTPCode, TeamMember
from services.notifications.auth_email import send_verification_otp, send_password_reset_otp, send_login_otp

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

JWT_SECRET          = os.environ.get("JWT_SECRET", "change-me-in-production")
GOOGLE_CLIENT_ID    = os.environ.get("GOOGLE_CLIENT_ID", "")
JWT_ALGORITHM       = "HS256"
ACCESS_TOKEN_TTL    = timedelta(hours=24)
REFRESH_TOKEN_TTL   = timedelta(days=30)
OTP_TTL             = timedelta(minutes=15)
OTP_MAX_ATTEMPTS    = 5
OTP_RESEND_COOLDOWN = timedelta(seconds=60)


# ── helpers ──────────────────────────────────────────────────────────────────

def _hash(pw: str) -> str:
    return _pwd.hash(pw)


def _verify(pw: str, hashed: str) -> bool:
    return _pwd.verify(pw, hashed)


def _gen_otp() -> str:
    return "".join(secrets.choice(string.digits) for _ in range(6))


def _make_token(payload: dict, ttl: timedelta) -> str:
    exp = datetime.now(timezone.utc) + ttl
    return jwt.encode({**payload, "exp": exp}, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _access_token(user_id: str) -> str:
    return _make_token({"sub": user_id, "type": "access"}, ACCESS_TOKEN_TTL)


def _refresh_token(user_id: str) -> str:
    return _make_token({"sub": user_id, "type": "refresh"}, REFRESH_TOKEN_TTL)


def _token_pair(user_id: str) -> dict:
    return {
        "access_token": _access_token(user_id),
        "refresh_token": _refresh_token(user_id),
        "token_type": "bearer",
    }


def _user_dict(user: User) -> dict:
    from services.core.billing import check_payment_method
    return {
        "id":                   user.id,
        "name":                 user.name,
        "email":                user.email,
        "plan":                 user.plan,
        "avatar_url":           user.avatar_url,
        "last_login_at":        user.last_login_at.isoformat() if user.last_login_at else None,
        "onboarding_completed": bool(user.onboarding_completed),
        "two_factor_enabled":   bool(user.two_factor_enabled),
        "role":                 user.role,
        "title":                user.title,
        "language":             user.language,
        "has_payment_method":   check_payment_method(user),
        "source":               user.source,
        "trial_ends_at":        user.trial_ends_at.isoformat() if user.trial_ends_at else None,
        "trial_reminder_sent_at": user.trial_reminder_sent_at.isoformat() if user.trial_reminder_sent_at else None,
    }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── registration ──────────────────────────────────────────────────────────────

def register(db: Session, name: str, email: str, password: str) -> dict:
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists.")

    domain = email.rsplit("@", 1)[-1].lower()
    if domain in _DISPOSABLE_DOMAINS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Disposable email addresses are not allowed. Please use a real email address.",
        )

    user = User(name=name, email=email, password_hash=_hash(password))
    db.add(user)
    db.flush()  # get user.id before OTP insert

    otp = _gen_otp()
    db.add(OTPCode(
        user_id=user.id,
        code=otp,
        type="verify_email",
        expires_at=_utcnow() + OTP_TTL,
    ))
    db.commit()

    try:
        send_verification_otp(email, name, otp)
    except Exception as e:
        log.error("Failed to send verification email to %s: %s", email, e)

    return {"message": "Account created. Check your email for a verification code.", "user_id": user.id}


# ── email verification ────────────────────────────────────────────────────────

def verify_email(db: Session, user_id: str, code: str) -> dict:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")
    if user.email_verified:
        return {**_token_pair(user.id), "user": _user_dict(user)}

    record = (
        db.query(OTPCode)
        .filter(OTPCode.user_id == user_id, OTPCode.type == "verify_email", OTPCode.used == False)
        .order_by(OTPCode.created_at.desc())
        .first()
    )
    if not record:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No active verification code. Request a new one.")

    if record.attempts >= OTP_MAX_ATTEMPTS:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Request a new code.")

    record.attempts += 1
    db.flush()

    if record.expires_at < _utcnow():
        db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Code expired. Request a new one.")

    if record.code != code:
        db.commit()
        remaining = OTP_MAX_ATTEMPTS - record.attempts
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Incorrect code. {remaining} attempt{'s' if remaining != 1 else ''} left.")

    record.used = True
    user.email_verified = True
    user.last_login_at  = _utcnow()

    # Link any pending team invitations sent to this email
    pending_invites = db.query(TeamMember).filter(
        TeamMember.invited_email == user.email,
        TeamMember.status == "pending",
    ).all()
    for inv in pending_invites:
        inv.user_id = user.id
        inv.status  = "active"

    db.commit()

    return {**_token_pair(user.id), "user": _user_dict(user)}


# ── resend OTP ────────────────────────────────────────────────────────────────

def _create_otp(db: Session, user: User, otp_type: str) -> str:
    otp = _gen_otp()
    db.add(OTPCode(
        user_id=user.id,
        code=otp,
        type=otp_type,
        expires_at=_utcnow() + OTP_TTL,
    ))
    return otp


def _latest_active_otp(db: Session, user_id: str, otp_type: str):
    return (
        db.query(OTPCode)
        .filter(OTPCode.user_id == user_id, OTPCode.type == otp_type, OTPCode.used == False)
        .order_by(OTPCode.created_at.desc())
        .first()
    )


def _verify_otp_record(db: Session, user_id: str, otp_type: str, code: str) -> None:
    record = _latest_active_otp(db, user_id, otp_type)
    if not record:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No active code. Request a new one.")

    if record.attempts >= OTP_MAX_ATTEMPTS:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Request a new code.")

    record.attempts += 1
    db.flush()

    if record.expires_at < _utcnow():
        db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Code expired. Request a new one.")

    if record.code != code:
        db.commit()
        remaining = OTP_MAX_ATTEMPTS - record.attempts
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Incorrect code. {remaining} attempt{'s' if remaining != 1 else ''} left.")

    record.used = True


def resend_otp(db: Session, user_id: str, otp_type: str) -> dict:
    if otp_type not in {"verify_email", "reset_password", "login_2fa"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unsupported code type.")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")

    last = _latest_active_otp(db, user_id, otp_type)
    if last and last.created_at > _utcnow() - OTP_RESEND_COOLDOWN:
        wait = int((OTP_RESEND_COOLDOWN - (_utcnow() - last.created_at)).total_seconds())
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"Please wait {wait}s before requesting a new code.")

    otp = _create_otp(db, user, otp_type)
    db.commit()

    try:
        if otp_type == "verify_email":
            send_verification_otp(user.email, user.name, otp)
        elif otp_type == "reset_password":
            send_password_reset_otp(user.email, user.name, otp)
        else:
            send_login_otp(user.email, user.name, otp)
    except Exception as e:
        log.error("Failed to send OTP email to %s: %s", user.email, e)

    return {"message": "A new code has been sent to your email."}


# ── login ─────────────────────────────────────────────────────────────────────

def login(db: Session, email: str, password: str) -> dict:
    user = db.query(User).filter(User.email == email).first()
    if not user or not _verify(password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password.")
    if not user.email_verified:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Please verify your email before signing in.",
            headers={"X-User-Id": user.id},
        )
    if user.two_factor_enabled:
        otp = _create_otp(db, user, "login_2fa")
        db.commit()
        try:
            send_login_otp(user.email, user.name, otp)
        except Exception as e:
            log.error("Failed to send 2FA email to %s: %s", user.email, e)
        return {
            "requires_2fa": True,
            "user_id": user.id,
            "email": user.email,
            "message": "Enter the security code sent to your email.",
        }
    user.last_login_at = _utcnow()
    db.commit()
    return {**_token_pair(user.id), "user": _user_dict(user)}


# ── token refresh ─────────────────────────────────────────────────────────────

def verify_login_2fa(db: Session, user_id: str, code: str) -> dict:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")
    if not user.two_factor_enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Two-factor authentication is not enabled.")

    _verify_otp_record(db, user.id, "login_2fa", code)
    user.last_login_at = _utcnow()
    db.commit()
    return {**_token_pair(user.id), "user": _user_dict(user)}


def start_2fa_setup(db: Session, user: User, current_password) -> dict:
    if user.source != "google":
        if not current_password or not _verify(current_password, user.password_hash):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect.")
    if user.two_factor_enabled:
        return {"message": "Two-factor authentication is already enabled.", "two_factor_enabled": True}

    otp = _create_otp(db, user, "enable_2fa")
    db.commit()
    try:
        send_login_otp(user.email, user.name, otp)
    except Exception as e:
        log.error("Failed to send 2FA setup email to %s: %s", user.email, e)
    return {"message": "A security code has been sent to your email."}


def confirm_2fa_setup(db: Session, user: User, code: str) -> dict:
    if user.two_factor_enabled:
        return {"message": "Two-factor authentication is already enabled.", "two_factor_enabled": True}

    _verify_otp_record(db, user.id, "enable_2fa", code)
    user.two_factor_enabled = True
    db.commit()
    return {"message": "Two-factor authentication enabled.", "two_factor_enabled": True}


def disable_2fa(db: Session, user: User, current_password) -> dict:
    if user.source != "google":
        if not current_password or not _verify(current_password, user.password_hash):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect.")
    user.two_factor_enabled = False
    db.query(OTPCode).filter(
        OTPCode.user_id == user.id,
        OTPCode.type.in_(["login_2fa", "enable_2fa"]),
        OTPCode.used == False,
    ).update({"used": True}, synchronize_session=False)
    db.commit()
    return {"message": "Two-factor authentication disabled.", "two_factor_enabled": False}


def refresh_access_token(db: Session, token: str) -> dict:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired refresh token.")

    if payload.get("type") != "refresh":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not a refresh token.")

    user = db.query(User).filter(User.id == payload["sub"]).first()
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found.")

    return {**_token_pair(user.id), "token_type": "bearer"}


# ── password reset ────────────────────────────────────────────────────────────

def request_password_reset(db: Session, email: str) -> dict:
    user = db.query(User).filter(User.email == email).first()
    # always return success to avoid user enumeration
    if not user:
        return {"message": "If that email exists, a reset code has been sent."}

    last = (
        db.query(OTPCode)
        .filter(OTPCode.user_id == user.id, OTPCode.type == "reset_password", OTPCode.used == False)
        .order_by(OTPCode.created_at.desc())
        .first()
    )
    if last and last.created_at > _utcnow() - OTP_RESEND_COOLDOWN:
        return {"message": "If that email exists, a reset code has been sent."}

    otp = _gen_otp()
    db.add(OTPCode(
        user_id=user.id,
        code=otp,
        type="reset_password",
        expires_at=_utcnow() + OTP_TTL,
    ))
    db.commit()

    try:
        send_password_reset_otp(user.email, user.name, otp)
    except Exception as e:
        log.error("Failed to send password reset email to %s: %s", user.email, e)

    return {"message": "If that email exists, a reset code has been sent.", "user_id": user.id}


def google_sign_in(db: Session, access_token: str) -> dict:
    """Verify a Google OAuth access token via userinfo endpoint, create account if needed."""
    try:
        req = urllib.request.Request(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            info = json.loads(resp.read())
    except Exception:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid Google token.")

    email = info.get("email")
    if not email:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Could not retrieve email from Google.")

    name       = info.get("name") or email.split("@")[0].replace(".", " ").title()
    avatar_url = info.get("picture")

    user = db.query(User).filter(User.email == email).first()
    if user:
        if user.two_factor_enabled:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Please sign in with email and password to complete two-factor authentication.",
            )
        if avatar_url and not user.avatar_url:
            user.avatar_url = avatar_url
        user.last_login_at = _utcnow()
        db.commit()
    else:
        user = User(
            name=name,
            email=email,
            password_hash=_hash(secrets.token_urlsafe(32)),
            email_verified=True,
            avatar_url=avatar_url,
            source="google",
        )
        db.add(user)
        db.flush()
        pending = db.query(TeamMember).filter(
            TeamMember.invited_email == email,
            TeamMember.status == "pending",
        ).all()
        for inv in pending:
            inv.user_id = user.id
            inv.status  = "active"
        db.commit()

    return {**_token_pair(user.id), "user": _user_dict(user)}


def reset_password(db: Session, user_id: str, code: str, new_password: str) -> dict:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")

    record = (
        db.query(OTPCode)
        .filter(OTPCode.user_id == user_id, OTPCode.type == "reset_password", OTPCode.used == False)
        .order_by(OTPCode.created_at.desc())
        .first()
    )
    if not record:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No active reset code. Request a new one.")

    if record.attempts >= OTP_MAX_ATTEMPTS:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Request a new code.")

    record.attempts += 1
    db.flush()

    if record.expires_at < _utcnow():
        db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Code expired. Request a new one.")

    if record.code != code:
        db.commit()
        remaining = OTP_MAX_ATTEMPTS - record.attempts
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Incorrect code. {remaining} attempt{'s' if remaining != 1 else ''} left.")

    record.used = True
    user.password_hash = _hash(new_password)
    db.commit()

    return {**_token_pair(user.id), "user": _user_dict(user)}
