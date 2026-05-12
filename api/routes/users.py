import io
import json
import zipfile

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from middleware.auth import get_db, get_current_user
from models.job import JobRecord
from models.user import User
from schemas.users import ChangePasswordIn, UpdatePrefsIn, UpdateProfileIn
import services.users as svc

router = APIRouter(prefix="/users", tags=["users"])


@router.patch("/me")
def update_profile(
    body: UpdateProfileIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return svc.update_profile(db, current_user, body.name, body.email)


@router.post("/me/password")
def change_password(
    body: ChangePasswordIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return svc.change_password(db, current_user, body.current_password, body.new_password)


@router.patch("/me/preferences")
def update_preferences(
    body: UpdatePrefsIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return svc.update_preferences(db, current_user, body.timezone, body.language, body.theme)


@router.get("/me/preferences")
def get_preferences(current_user: User = Depends(get_current_user)):
    return svc.get_preferences(current_user)


@router.post("/me/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await svc.upload_avatar(db, current_user, file)


@router.get("/me/usage")
def get_usage(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return svc.get_usage(db, current_user)


@router.get("/me/export")
def export_data(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    jobs = (
        db.query(JobRecord)
        .filter(
            JobRecord.user_id == current_user.id,
            JobRecord.status == "done",
            JobRecord.minutes_json.isnot(None),
        )
        .order_by(JobRecord.created_at.desc())
        .all()
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for job in jobs:
            try:
                minutes = json.loads(job.minutes_json)
            except Exception:
                continue
            title = (minutes.get("title") or f"meeting_{job.id[:8]}").strip()
            safe  = "".join(c if c.isalnum() or c in " -_" else "_" for c in title)[:60].strip()
            date  = job.created_at.strftime("%Y-%m-%d") if job.created_at else "unknown"
            zf.writestr(f"{date}_{safe}.json", json.dumps(minutes, indent=2, ensure_ascii=False))

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="minuteai_export.zip"'},
    )
