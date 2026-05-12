from pathlib import Path

from fastapi import HTTPException, UploadFile, status
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from models.job import JobRecord
from models.user import User

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

PLAN_LIMITS = {
    "free":  {"meetings": 3,   "hours": 5},
    "pro":   {"meetings": 50,  "hours": 100},
    "team":  {"meetings": 200, "hours": 500},
}


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
    limits = PLAN_LIMITS.get(user.plan, PLAN_LIMITS["free"])
    return {
        "plan":           user.plan,
        "meetings_used":  len(done_jobs),
        "meetings_limit": limits["meetings"],
        "hours_used":     0,
        "hours_limit":    limits["hours"],
    }
