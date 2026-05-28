"""
AnomalyMiddleware — captures 5xx responses and unhandled exceptions, deduplicates
by (endpoint, error_type) and persists them as ApiAnomalyRecord rows.
"""
import asyncio
import os
import traceback
import uuid
from datetime import datetime

from jose import JWTError, jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

_JWT_SECRET    = os.environ.get("JWT_SECRET", "change-me-in-production")
_JWT_ALGORITHM = "HS256"

# Paths we never track (health checks, static files, API docs)
_SKIP_PREFIXES = ("/health", "/static", "/docs", "/openapi.json", "/redoc")


def _should_track(path: str) -> bool:
    return not any(path.startswith(p) for p in _SKIP_PREFIXES)


def _extract_user(request: Request):
    """Best-effort JWT parse — returns (user_id, email) or (None, None)."""
    try:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None, None
        payload = jwt.decode(auth[7:], _JWT_SECRET, algorithms=[_JWT_ALGORITHM])
        return payload.get("sub"), payload.get("email")
    except (JWTError, Exception):
        return None, None


def _sync_log(
    endpoint:      str,
    method:        str,
    status_code:   int,
    error_type:    str,
    error_message: str,
    stack_trace:   str | None,
    user_id:       str | None,
    user_email:    str | None,
    ip_address:    str | None,
) -> None:
    """Synchronous DB write — safe to run in a thread executor."""
    try:
        from db.base import SessionLocal
        from models.anomaly import ApiAnomalyRecord

        db = SessionLocal()
        try:
            now = datetime.utcnow()
            existing = (
                db.query(ApiAnomalyRecord)
                .filter(
                    ApiAnomalyRecord.endpoint   == endpoint,
                    ApiAnomalyRecord.error_type == error_type,
                    ApiAnomalyRecord.resolved   == False,  # noqa: E712
                )
                .first()
            )
            if existing:
                existing.count        += 1
                existing.last_seen_at  = now
                # Backfill user / stack trace if we get more context later
                if user_id and not existing.user_id:
                    existing.user_id    = user_id
                    existing.user_email = user_email
                if stack_trace and not existing.stack_trace:
                    existing.stack_trace = stack_trace[:5000]
            else:
                db.add(ApiAnomalyRecord(
                    id            = str(uuid.uuid4()),
                    endpoint      = endpoint,
                    method        = method,
                    status_code   = status_code,
                    error_type    = error_type,
                    error_message = (error_message or "")[:2000],
                    stack_trace   = stack_trace[:5000] if stack_trace else None,
                    user_id       = user_id,
                    user_email    = user_email,
                    ip_address    = ip_address,
                    first_seen_at = now,
                    last_seen_at  = now,
                    created_at    = now,
                ))
            db.commit()
        finally:
            db.close()
    except Exception:
        pass  # Never let anomaly logging crash the app


class AnomalyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not _should_track(path):
            return await call_next(request)

        method     = request.method
        ip         = request.client.host if request.client else None
        user_id, user_email = _extract_user(request)
        loop       = asyncio.get_running_loop()

        try:
            response = await call_next(request)
        except Exception as exc:
            tb = traceback.format_exc()
            loop.run_in_executor(
                None, _sync_log,
                path, method, 500,
                type(exc).__name__, str(exc), tb,
                user_id, user_email, ip,
            )
            raise

        if response.status_code >= 500:
            loop.run_in_executor(
                None, _sync_log,
                path, method, response.status_code,
                "ServerError", f"HTTP {response.status_code}", None,
                user_id, user_email, ip,
            )

        return response
