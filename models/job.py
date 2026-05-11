import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Callable

from models.database import SessionLocal, JobRecord


@dataclass
class Job:
    id: str
    status: str = "uploading"
    message: Optional[str] = None
    email: Optional[str] = None
    user_id: Optional[str] = None
    audio_path: Optional[str] = None
    transcript: Optional[str] = None
    minutes: Optional[dict] = None
    docx_path: Optional[str] = None
    pdf_path: Optional[str] = None
    starred: bool = False
    workspace_id: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)


def _record_to_job(r: JobRecord) -> Job:
    return Job(
        id=r.id, status=r.status, message=r.message, email=r.email,
        user_id=getattr(r, "user_id", None),
        audio_path=r.audio_path, transcript=r.transcript,
        minutes=json.loads(r.minutes_json) if r.minutes_json else None,
        docx_path=r.docx_path, pdf_path=r.pdf_path,
        starred=bool(getattr(r, "starred", False)),
        workspace_id=getattr(r, "workspace_id", None),
    )


class JobStore:
    def __init__(self):
        # In-memory cache for fast WebSocket reads — DB is the source of truth
        self._cache: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._notify: Optional[Callable[[str, dict], None]] = None

    def set_notify(self, callback: Callable[[str, dict], None]):
        self._notify = callback

    def create(self, job: Job) -> Job:
        with SessionLocal() as session:
            record = JobRecord(
                id=job.id, status=job.status, email=job.email,
                user_id=job.user_id, audio_path=job.audio_path,
                workspace_id=job.workspace_id,
            )
            session.add(record)
            session.commit()
        with self._lock:
            self._cache[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        # Try cache first
        with self._lock:
            if job_id in self._cache:
                return self._cache[job_id]
        # Fall back to DB (e.g. after server restart)
        with SessionLocal() as session:
            record = session.get(JobRecord, job_id)
            if not record:
                return None
            job = _record_to_job(record)
        with self._lock:
            self._cache[job_id] = job
        return job

    def update(self, job_id: str, **kwargs) -> None:
        with self._lock:
            job = self._cache.get(job_id)
            if not job:
                return
            for k, v in kwargs.items():
                setattr(job, k, v)
            payload = {
                "status": job.status,
                "message": job.message,
                "transcript": job.transcript,
            }

        # Persist to DB
        db_kwargs = {k: v for k, v in kwargs.items() if k != "minutes"}
        if "minutes" in kwargs:
            db_kwargs["minutes_json"] = json.dumps(kwargs["minutes"]) if kwargs["minutes"] else None
        with SessionLocal() as session:
            record = session.get(JobRecord, job_id)
            if record:
                for k, v in db_kwargs.items():
                    if hasattr(record, k):
                        setattr(record, k, v)
                session.commit()

        if self._notify:
            self._notify(job_id, payload)


# Singleton
store = JobStore()
