from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from middleware.auth import get_admin_user, get_db
from models.anomaly import ApiAnomalyRecord
from models.user import User

router = APIRouter(prefix="/admin/anomalies", tags=["admin-anomalies"])


# ── Serialiser ────────────────────────────────────────────────────────────────

def _row(a: ApiAnomalyRecord) -> dict:
    return {
        "id":               a.id,
        "endpoint":         a.endpoint,
        "method":           a.method,
        "status_code":      a.status_code,
        "error_type":       a.error_type,
        "error_message":    a.error_message,
        "stack_trace":      a.stack_trace,
        "request_body":     a.request_body,
        "user_id":          a.user_id,
        "user_email":       a.user_email,
        "ip_address":       a.ip_address,
        "resolved":         a.resolved,
        "resolved_at":      a.resolved_at.isoformat() if a.resolved_at else None,
        "resolved_by_email":a.resolved_by_email,
        "notes":            a.notes,
        "count":            a.count,
        "first_seen_at":    a.first_seen_at.isoformat() if a.first_seen_at else None,
        "last_seen_at":     a.last_seen_at.isoformat()  if a.last_seen_at  else None,
        "created_at":       a.created_at.isoformat()    if a.created_at    else None,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/stats")
def anomaly_stats(
    db: Session = Depends(get_db),
    _:  User    = Depends(get_admin_user),
):
    """Summary counters + 7-day trend + top error types + top endpoints."""
    total    = db.query(func.count(ApiAnomalyRecord.id)).scalar() or 0
    open_ct  = db.query(func.count(ApiAnomalyRecord.id)).filter(ApiAnomalyRecord.resolved == False).scalar() or 0  # noqa: E712
    res_ct   = db.query(func.count(ApiAnomalyRecord.id)).filter(ApiAnomalyRecord.resolved == True).scalar()  or 0  # noqa: E712
    critical = db.query(func.count(ApiAnomalyRecord.id)).filter(
        ApiAnomalyRecord.status_code >= 500,
        ApiAnomalyRecord.resolved == False,  # noqa: E712
    ).scalar() or 0

    since = datetime.utcnow() - timedelta(days=7)
    daily = (
        db.query(
            func.date(ApiAnomalyRecord.created_at).label("day"),
            func.count(ApiAnomalyRecord.id).label("count"),
        )
        .filter(ApiAnomalyRecord.created_at >= since)
        .group_by(func.date(ApiAnomalyRecord.created_at))
        .order_by(func.date(ApiAnomalyRecord.created_at))
        .all()
    )

    top_errors = (
        db.query(
            ApiAnomalyRecord.error_type,
            func.count(ApiAnomalyRecord.id).label("count"),
        )
        .filter(ApiAnomalyRecord.resolved == False)  # noqa: E712
        .group_by(ApiAnomalyRecord.error_type)
        .order_by(func.count(ApiAnomalyRecord.id).desc())
        .limit(5)
        .all()
    )

    top_endpoints = (
        db.query(
            ApiAnomalyRecord.endpoint,
            func.sum(ApiAnomalyRecord.count).label("hits"),
        )
        .filter(ApiAnomalyRecord.resolved == False)  # noqa: E712
        .group_by(ApiAnomalyRecord.endpoint)
        .order_by(func.sum(ApiAnomalyRecord.count).desc())
        .limit(5)
        .all()
    )

    return {
        "total":    total,
        "open":     open_ct,
        "resolved": res_ct,
        "critical": critical,
        "daily":    [{"day": str(r.day), "count": r.count} for r in daily],
        "top_errors":    [{"error_type": r.error_type, "count": r.count} for r in top_errors],
        "top_endpoints": [{"endpoint": r.endpoint, "hits": int(r.hits)} for r in top_endpoints],
    }


@router.get("")
def list_anomalies(
    q:           Optional[str] = Query(None),
    resolved:    Optional[str] = Query(None),   # "open" | "resolved" | None = all
    method:      Optional[str] = Query(None),
    status_code: Optional[int] = Query(None),
    skip:  int = Query(0,   ge=0),
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db),
    _:  User    = Depends(get_admin_user),
):
    qs = db.query(ApiAnomalyRecord)

    if resolved == "open":
        qs = qs.filter(ApiAnomalyRecord.resolved == False)  # noqa: E712
    elif resolved == "resolved":
        qs = qs.filter(ApiAnomalyRecord.resolved == True)   # noqa: E712

    if method:
        qs = qs.filter(ApiAnomalyRecord.method == method.upper())

    if status_code:
        qs = qs.filter(ApiAnomalyRecord.status_code == status_code)

    if q:
        like = f"%{q}%"
        qs = qs.filter(
            or_(
                ApiAnomalyRecord.endpoint.ilike(like),
                ApiAnomalyRecord.error_type.ilike(like),
                ApiAnomalyRecord.error_message.ilike(like),
                ApiAnomalyRecord.user_email.ilike(like),
            )
        )

    total   = qs.count()
    records = qs.order_by(ApiAnomalyRecord.last_seen_at.desc()).offset(skip).limit(limit).all()
    return {"total": total, "items": [_row(r) for r in records]}


@router.get("/{anomaly_id}")
def get_anomaly(
    anomaly_id: str,
    db: Session = Depends(get_db),
    _:  User    = Depends(get_admin_user),
):
    record = db.query(ApiAnomalyRecord).filter(ApiAnomalyRecord.id == anomaly_id).first()
    if not record:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Anomaly not found.")
    return _row(record)


class UpdateAnomalyIn(BaseModel):
    resolved: Optional[bool] = None
    notes:    Optional[str]  = None


@router.patch("/{anomaly_id}")
def update_anomaly(
    anomaly_id: str,
    body:  UpdateAnomalyIn,
    db:    Session = Depends(get_db),
    admin: User    = Depends(get_admin_user),
):
    record = db.query(ApiAnomalyRecord).filter(ApiAnomalyRecord.id == anomaly_id).first()
    if not record:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Anomaly not found.")

    if body.resolved is not None:
        record.resolved = body.resolved
        if body.resolved:
            record.resolved_at        = datetime.utcnow()
            record.resolved_by_id     = admin.id
            record.resolved_by_email  = admin.email
        else:
            record.resolved_at        = None
            record.resolved_by_id     = None
            record.resolved_by_email  = None

    if body.notes is not None:
        record.notes = body.notes

    db.commit()
    return _row(record)


@router.delete("/{anomaly_id}", status_code=204)
def delete_anomaly(
    anomaly_id: str,
    db: Session = Depends(get_db),
    _:  User    = Depends(get_admin_user),
):
    record = db.query(ApiAnomalyRecord).filter(ApiAnomalyRecord.id == anomaly_id).first()
    if not record:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Anomaly not found.")
    db.delete(record)
    db.commit()


# ── Bulk actions ──────────────────────────────────────────────────────────────

@router.post("/bulk/resolve-all")
def resolve_all_open(
    db:    Session = Depends(get_db),
    admin: User    = Depends(get_admin_user),
):
    now   = datetime.utcnow()
    count = (
        db.query(ApiAnomalyRecord)
        .filter(ApiAnomalyRecord.resolved == False)  # noqa: E712
        .update({
            "resolved":           True,
            "resolved_at":        now,
            "resolved_by_id":     admin.id,
            "resolved_by_email":  admin.email,
        }, synchronize_session=False)
    )
    db.commit()
    return {"resolved": count}


@router.delete("/bulk/resolved")
def clear_resolved(
    db: Session = Depends(get_db),
    _:  User    = Depends(get_admin_user),
):
    count = (
        db.query(ApiAnomalyRecord)
        .filter(ApiAnomalyRecord.resolved == True)  # noqa: E712
        .delete(synchronize_session=False)
    )
    db.commit()
    return {"deleted": count}
