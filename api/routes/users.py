import os
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from api.deps import get_db, get_current_user
from models.user import User
from models.database import JobRecord
from passlib.context import CryptContext

router = APIRouter(prefix="/users", tags=["users"])
_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

PLAN_LIMITS = {
    "free":  {"meetings": 3,  "hours": 5},
    "pro":   {"meetings": 50, "hours": 100},
    "team":  {"meetings": 200, "hours": 500},
}


# ── schemas ───────────────────────────────────────────────────────────────────

class UpdateProfileIn(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    email: EmailStr

class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8)

class UpdatePrefsIn(BaseModel):
    timezone: str | None = None
    language: str | None = None
    theme: str | None = None


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.patch("/me")
def update_profile(
    body: UpdateProfileIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if body.email != current_user.email:
        if db.query(User).filter(User.email == body.email, User.id != current_user.id).first():
            raise HTTPException(status.HTTP_409_CONFLICT, "Email already in use.")
        current_user.email = body.email
        current_user.email_verified = False
    current_user.name = body.name
    db.commit()
    return {"id": current_user.id, "name": current_user.name, "email": current_user.email}


@router.post("/me/password")
def change_password(
    body: ChangePasswordIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not _pwd.verify(body.current_password, current_user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect.")
    current_user.password_hash = _pwd.hash(body.new_password)
    db.commit()
    return {"message": "Password updated."}


@router.patch("/me/preferences")
def update_preferences(
    body: UpdatePrefsIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if body.timezone is not None:
        current_user.timezone = body.timezone
    if body.language is not None:
        current_user.language = body.language
    if body.theme is not None:
        current_user.theme = body.theme
    db.commit()
    return {
        "timezone": current_user.timezone,
        "language": current_user.language,
        "theme": current_user.theme,
    }


@router.get("/me/preferences")
def get_preferences(current_user: User = Depends(get_current_user)):
    return {
        "timezone": current_user.timezone or "UTC",
        "language": current_user.language or "en",
        "theme":    current_user.theme    or "system",
    }


@router.post("/me/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ALLOWED = {"image/jpeg", "image/png", "image/webp"}
    if file.content_type not in ALLOWED:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unsupported file type. Use JPEG, PNG, or WebP.")

    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "File too large. Maximum 5 MB.")

    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[file.content_type]
    filename  = f"{current_user.id}.{ext}"
    avatar_dir = Path("storage/avatars")
    avatar_dir.mkdir(parents=True, exist_ok=True)

    with open(avatar_dir / filename, "wb") as f:
        f.write(content)

    avatar_url = f"/static/avatars/{filename}"
    current_user.avatar_url = avatar_url
    db.commit()
    return {"avatar_url": avatar_url}


@router.get("/me/usage")
def get_usage(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    done_jobs = (
        db.query(JobRecord)
        .filter(JobRecord.user_id == current_user.id, JobRecord.status == "done")
        .all()
    )
    limits = PLAN_LIMITS.get(current_user.plan, PLAN_LIMITS["free"])
    return {
        "plan": current_user.plan,
        "meetings_used": len(done_jobs),
        "meetings_limit": limits["meetings"],
        "hours_used": 0,
        "hours_limit": limits["hours"],
    }
