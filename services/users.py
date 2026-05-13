from pathlib import Path

from fastapi import HTTPException, UploadFile, status
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from models.job import JobRecord
from models.user import SubscriptionPlan, User

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

_FALLBACK_LIMITS = {
    "free":  {"meetings": 3,   "hours": 5,   "workspaces": 1, "members": 3},
    "pro":   {"meetings": 50,  "hours": 100, "workspaces": 3, "members": 10},
    "team":  {"meetings": 200, "hours": 500, "workspaces": None, "members": None},
}


def _plan_limits(db: Session, plan_name: str) -> dict:
    row = db.query(SubscriptionPlan).filter(
        SubscriptionPlan.name == plan_name,
        SubscriptionPlan.is_active == True,
    ).first()
    if row:
        return {
            "meetings":   row.meetings_per_month,    # None = unlimited
            "hours":      row.audio_hours_per_month, # None = unlimited
            "workspaces": row.max_workspaces,        # None = unlimited
            "members":    row.max_members,           # None = unlimited
        }
    return _FALLBACK_LIMITS.get(plan_name, _FALLBACK_LIMITS["free"])


def update_profile(db: Session, user: User, name: str, email: str) -> dict:
    if email != user.email:
        if db.query(User).filter(User.email == email, User.id != user.id).first():
            raise HTTPException(status.HTTP_409_CONFLICT, "Email already in use.")
        user.email = email
        user.email_verified = False
    user.name = name
    db.commit()
    return {"id": user.id, "name": user.name, "email": user.email}


def change_password(db: Session, user: User, current_password: str, new_password: str) -> dict:
    if not _pwd.verify(current_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect.")
    user.password_hash = _pwd.hash(new_password)
    db.commit()
    return {"message": "Password updated."}


def update_preferences(db: Session, user: User, timezone: str | None, language: str | None, theme: str | None) -> dict:
    if timezone is not None:
        user.timezone = timezone
    if language is not None:
        user.language = language
    if theme is not None:
        user.theme = theme
    db.commit()
    return {"timezone": user.timezone, "language": user.language, "theme": user.theme}


def get_preferences(user: User) -> dict:
    return {
        "timezone": user.timezone or "UTC",
        "language": user.language or "en",
        "theme":    user.theme    or "system",
    }


async def upload_avatar(db: Session, user: User, file: UploadFile) -> dict:
    ALLOWED = {"image/jpeg", "image/png", "image/webp"}
    if file.content_type not in ALLOWED:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unsupported file type. Use JPEG, PNG, or WebP.")

    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "File too large. Maximum 5 MB.")

    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[file.content_type]
    filename = f"{user.id}.{ext}"
    avatar_dir = Path("storage/avatars")
    avatar_dir.mkdir(parents=True, exist_ok=True)

    with open(avatar_dir / filename, "wb") as f:
        f.write(content)

    avatar_url = f"/static/avatars/{filename}"
    user.avatar_url = avatar_url
    db.commit()
    return {"avatar_url": avatar_url}


def get_usage(db: Session, user: User) -> dict:
    done_jobs = (
        db.query(JobRecord)
        .filter(JobRecord.user_id == user.id, JobRecord.status == "done")
        .all()
    )
    limits = _plan_limits(db, user.plan or "free")
    return {
        "plan":            user.plan or "free",
        "meetings_used":   len(done_jobs),
        "meetings_limit":  limits["meetings"],    # None = unlimited
        "hours_used":      0,
        "hours_limit":     limits["hours"],       # None = unlimited
        "max_workspaces":  limits["workspaces"],  # None = unlimited
        "max_members":     limits["members"],     # None = unlimited
    }
