import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Callable

from sqlalchemy import Column, String, Text, Boolean, DateTime, func

from db.base import Base, SessionLocal


class JobRecord(Base):
    __tablename__ = "jobs"

    id           = Column(String,  primary_key=True)
    user_id      = Column(String,  nullable=True, index=True)
    status       = Column(String,  nullable=False, default="uploading")
    message      = Column(Text,    nullable=True)
    email        = Column(String,  nullable=True)
    audio_path   = Column(String,  nullable=True)
    r2_audio_key = Column(String,  nullable=True)
    transcript      = Column(Text,    nullable=True)
    transcript_segments_json = Column(Text, nullable=True)
    minutes_json    = Column(Text,    nullable=True)
    speaker_map_json = Column(Text,   nullable=True)
    docx_path       = Column(String,  nullable=True)
    pdf_path        = Column(String,  nullable=True)
    starred         = Column(Boolean, nullable=False, default=False)
    workspace_id    = Column(String,  nullable=True, index=True)
    config_json     = Column(Text,    nullable=True)
    created_at      = Column(DateTime, server_default=func.now())


@dataclass
class Job:
    id: str
    status: str = "uploading"
    message: Optional[str] = None
    email: Optional[str] = None
    user_id: Optional[str] = None
    audio_path: Optional[str] = None
    r2_audio_key: Optional[str] = None
    transcript: Optional[str] = None
    transcript_segments: Optional[list] = None
    minutes: Optional[dict] = None
    speaker_map: Optional[dict] = None
    docx_path: Optional[str] = None
    pdf_path: Optional[str] = None
    starred: bool = False
    workspace_id: Optional[str] = None
    job_config: Optional[dict] = None
    created_at: datetime = field(default_factory=datetime.utcnow)


def _record_to_job(r: JobRecord) -> Job:
    config_raw = getattr(r, "config_json", None)
    return Job(
        id=r.id, status=r.status, message=r.message, email=r.email,
        user_id=getattr(r, "user_id", None),
        audio_path=r.audio_path,
        r2_audio_key=getattr(r, "r2_audio_key", None),
        transcript=r.transcript,
        transcript_segments=json.loads(r.transcript_segments_json) if getattr(r, "transcript_segments_json", None) else None,
        minutes=json.loads(r.minutes_json) if r.minutes_json else None,
        speaker_map=json.loads(r.speaker_map_json) if getattr(r, "speaker_map_json", None) else None,
        docx_path=r.docx_path, pdf_path=r.pdf_path,
        starred=bool(getattr(r, "starred", False)),
        workspace_id=getattr(r, "workspace_id", None),
        job_config=json.loads(config_raw) if config_raw else None,
    )


class JobStore:
    def __init__(self):
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
                config_json=json.dumps(job.job_config) if job.job_config else None,
            )
            session.add(record)
            session.commit()
        with self._lock:
            self._cache[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            if job_id in self._cache:
                return self._cache[job_id]
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

        db_kwargs = {k: v for k, v in kwargs.items() if k not in ("minutes", "speaker_map", "transcript_segments")}
        if "minutes" in kwargs:
            db_kwargs["minutes_json"] = json.dumps(kwargs["minutes"]) if kwargs["minutes"] else None
        if "speaker_map" in kwargs:
            db_kwargs["speaker_map_json"] = json.dumps(kwargs["speaker_map"]) if kwargs["speaker_map"] else None
        if "transcript_segments" in kwargs:
            db_kwargs["transcript_segments_json"] = json.dumps(kwargs["transcript_segments"]) if kwargs["transcript_segments"] else None
        with SessionLocal() as session:
            record = session.get(JobRecord, job_id)
            if record:
                for k, v in db_kwargs.items():
                    if hasattr(record, k):
                        setattr(record, k, v)
                session.commit()

        if self._notify:
            self._notify(job_id, payload)


store = JobStore()
