"""
Cloudflare R2 storage service.

Uses the S3-compatible API via boto3. All functions are safe to call even when
R2 is not configured — they raise a RuntimeError so callers can decide to fall
back to local storage or surface a helpful error message.

Required environment variables:
    R2_ACCOUNT_ID          – Cloudflare account ID
    R2_ACCESS_KEY_ID       – R2 API token (Access Key ID)
    R2_SECRET_ACCESS_KEY   – R2 API token (Secret Access Key)
    R2_BUCKET_NAME         – Bucket name (e.g. "minutesai-audio")
    R2_PUBLIC_URL          – Optional. Public bucket base URL for direct links.
                             Leave unset to use presigned URLs instead.
"""

import os
import tempfile
from pathlib import Path
from typing import Optional

_s3_client = None


def _is_configured() -> bool:
    return all([
        os.environ.get("R2_ACCOUNT_ID"),
        os.environ.get("R2_ACCESS_KEY_ID"),
        os.environ.get("R2_SECRET_ACCESS_KEY"),
        os.environ.get("R2_BUCKET_NAME"),
    ])


def _get_client():
    global _s3_client
    if _s3_client is None:
        import boto3
        _s3_client = boto3.client(
            "s3",
            endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
        )
    return _s3_client


def _bucket() -> str:
    return os.environ["R2_BUCKET_NAME"]


# ── Public API ────────────────────────────────────────────────────────────────

def upload_file(local_path: str, r2_key: str, content_type: Optional[str] = None) -> str:
    """
    Upload a local file to R2.

    Returns the R2 key (use get_presigned_url or get_public_url to serve it).
    Raises RuntimeError if R2 is not configured.
    """
    if not _is_configured():
        raise RuntimeError("R2 is not configured. Set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME.")

    extra = {}
    if content_type:
        extra["ContentType"] = content_type

    client = _get_client()
    client.upload_file(local_path, _bucket(), r2_key, ExtraArgs=extra if extra else None)
    return r2_key


def download_to_temp(r2_key: str, suffix: str = "") -> str:
    """
    Download an R2 object to a temporary file.

    Returns the path to the temp file. Caller is responsible for deleting it.
    Raises RuntimeError if R2 is not configured.
    """
    if not _is_configured():
        raise RuntimeError("R2 is not configured.")

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.close()
    _get_client().download_file(_bucket(), r2_key, tmp.name)
    return tmp.name


def get_presigned_url(r2_key: str, expires_in: int = 3600) -> str:
    """
    Generate a time-limited presigned URL to serve a private R2 object.

    expires_in: seconds until the URL expires (default 1 hour).
    """
    if not _is_configured():
        raise RuntimeError("R2 is not configured.")

    url = _get_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": _bucket(), "Key": r2_key},
        ExpiresIn=expires_in,
    )
    return url


def get_public_url(r2_key: str) -> str:
    """
    Return a direct public URL for an R2 object.

    Only works when the bucket has public access enabled and R2_PUBLIC_URL is set.
    Falls back to a presigned URL if R2_PUBLIC_URL is not configured.
    """
    public_base = os.environ.get("R2_PUBLIC_URL", "").rstrip("/")
    if public_base:
        return f"{public_base}/{r2_key}"
    return get_presigned_url(r2_key)


def delete_file(r2_key: str) -> None:
    """Delete an object from R2. Silently ignores if not configured."""
    if not _is_configured():
        return
    try:
        _get_client().delete_object(Bucket=_bucket(), Key=r2_key)
    except Exception:
        pass


def object_exists(r2_key: str) -> bool:
    """Return True if the R2 key exists in the bucket."""
    if not _is_configured():
        return False
    try:
        _get_client().head_object(Bucket=_bucket(), Key=r2_key)
        return True
    except Exception:
        return False


def r2_key_for_audio(job_id: str, suffix: str) -> str:
    """Canonical R2 key for a meeting audio file."""
    return f"audio/{job_id}{suffix}"


def r2_key_for_output(job_id: str, fmt: str, template: Optional[str] = None) -> str:
    """Canonical R2 key for a generated document (docx / pdf)."""
    if template:
        return f"outputs/{job_id}_{template}.{fmt}"
    return f"outputs/{job_id}.{fmt}"
