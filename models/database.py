import os
from pathlib import Path
from sqlalchemy import create_engine, Column, String, Text, DateTime, Boolean, Integer, ForeignKey, func, text
from sqlalchemy.orm import declarative_base, sessionmaker

_db_path = Path(__file__).parent.parent / "storage" / "minuteai.db"
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{_db_path}")

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()


class TaskRecord(Base):
    __tablename__ = "tasks"

    id           = Column(String,  primary_key=True)
    job_id       = Column(String,  ForeignKey("jobs.id",   ondelete="CASCADE"),   nullable=True,  index=True)
    workspace_id = Column(String,  nullable=True,  index=True)
    title        = Column(Text,    nullable=False)
    owner_name   = Column(String,  nullable=True)
    assignee_id  = Column(String,  ForeignKey("users.id",  ondelete="SET NULL"),  nullable=True,  index=True)
    status       = Column(String,  nullable=False, default="todo")
    deadline     = Column(String,  nullable=True)
    created_at   = Column(DateTime, server_default=func.now())


class JobRecord(Base):
    __tablename__ = "jobs"

    id           = Column(String,  primary_key=True)
    user_id      = Column(String,  nullable=True, index=True)
    status       = Column(String,  nullable=False, default="uploading")
    message      = Column(Text,    nullable=True)
    email        = Column(String,  nullable=True)
    audio_path   = Column(String,  nullable=True)
    transcript   = Column(Text,    nullable=True)
    minutes_json = Column(Text,    nullable=True)
    docx_path    = Column(String,  nullable=True)
    pdf_path     = Column(String,  nullable=True)
    starred      = Column(Boolean, nullable=False, default=False)
    workspace_id = Column(String,  nullable=True, index=True)
    created_at   = Column(DateTime, server_default=func.now())


class NotificationRecord(Base):
    __tablename__ = "notifications"

    id         = Column(String,  primary_key=True)
    user_id    = Column(String,  nullable=False, index=True)
    type       = Column(String,  nullable=False)
    title      = Column(Text,    nullable=False)
    body       = Column(Text,    nullable=True)
    link       = Column(String,  nullable=True)
    read       = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, server_default=func.now())


class MeetingBotSessionRecord(Base):
    __tablename__ = "meeting_bot_sessions"

    id             = Column(String,  primary_key=True)
    user_id        = Column(String,  nullable=False, index=True)
    workspace_id   = Column(String,  nullable=True, index=True)
    bot_account_id = Column(String,  nullable=True, index=True)
    calendar_event_id = Column(String, nullable=True, index=True)
    source         = Column(String,  nullable=True)
    platform       = Column(String,  nullable=False)
    meeting_url    = Column(Text,    nullable=False)
    title          = Column(Text,    nullable=True)
    bot_name       = Column(String,  nullable=False, default="MinuteAI Recorder")
    status         = Column(String,  nullable=False, default="queued", index=True)
    scheduled_for  = Column(DateTime, nullable=True)
    duration_minutes = Column(Integer, nullable=True)
    started_at     = Column(DateTime, nullable=True)
    ended_at       = Column(DateTime, nullable=True)
    failure_reason = Column(Text,    nullable=True)
    recording_path = Column(String,  nullable=True)
    job_id         = Column(String,  nullable=True, index=True)
    created_at     = Column(DateTime, server_default=func.now())


class BotAccountRecord(Base):
    __tablename__ = "bot_accounts"

    id           = Column(String, primary_key=True)
    workspace_id = Column(String, nullable=False, unique=True, index=True)
    email        = Column(String, nullable=False, unique=True, index=True)
    display_name = Column(String, nullable=False, default="MinuteAI Recorder")
    profile_dir  = Column(String, nullable=False)
    provider     = Column(String, nullable=False, default="google")
    status       = Column(String, nullable=False, default="needs_setup", index=True)
    kind         = Column(String, nullable=False, default="dedicated")
    created_at   = Column(DateTime, server_default=func.now())


def create_tables():
    import models.user  # noqa: F401
    Base.metadata.create_all(bind=engine)


def _migrate_tasks_job_id_nullable():
    """Make tasks.job_id nullable — requires table recreation in SQLite (idempotent)."""
    with engine.connect() as conn:
        try:
            rows = conn.execute(text("PRAGMA table_info(tasks)")).fetchall()
            job_id_notnull = next((r[3] for r in rows if r[1] == "job_id"), None)
            if job_id_notnull == 1:
                conn.execute(text("""
                    CREATE TABLE tasks_new (
                        id TEXT PRIMARY KEY,
                        job_id TEXT REFERENCES jobs(id) ON DELETE CASCADE,
                        workspace_id TEXT,
                        title TEXT NOT NULL,
                        owner_name TEXT,
                        assignee_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                        status TEXT NOT NULL DEFAULT 'todo',
                        deadline TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """))
                conn.execute(text("INSERT INTO tasks_new SELECT * FROM tasks"))
                conn.execute(text("DROP TABLE tasks"))
                conn.execute(text("ALTER TABLE tasks_new RENAME TO tasks"))
                conn.commit()
        except Exception:
            pass


def run_migrations():
    """Add new columns to existing tables (SQLite-safe, idempotent)."""
    migrations = [
        "ALTER TABLE jobs ADD COLUMN user_id TEXT",
        "ALTER TABLE jobs ADD COLUMN starred INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN timezone TEXT",
        "ALTER TABLE users ADD COLUMN language TEXT",
        "ALTER TABLE users ADD COLUMN theme TEXT",
        "ALTER TABLE team_members ADD COLUMN invite_token TEXT",
        "ALTER TABLE users ADD COLUMN stripe_customer_id TEXT",
        "ALTER TABLE users ADD COLUMN stripe_subscription_id TEXT",
        "ALTER TABLE users ADD COLUMN avatar_url TEXT",
        # workspaces
        "CREATE TABLE IF NOT EXISTS workspaces (id TEXT PRIMARY KEY, name TEXT NOT NULL, owner_id TEXT NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
        "ALTER TABLE team_members ADD COLUMN workspace_id TEXT",
        "ALTER TABLE jobs ADD COLUMN workspace_id TEXT",
        "CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, workspace_id TEXT, title TEXT NOT NULL, owner_name TEXT, assignee_id TEXT, status TEXT NOT NULL DEFAULT 'todo', deadline TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
        "CREATE TABLE IF NOT EXISTS notifications (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, type TEXT NOT NULL, title TEXT NOT NULL, body TEXT, link TEXT, read INTEGER NOT NULL DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
        "CREATE TABLE IF NOT EXISTS meeting_bot_sessions (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, workspace_id TEXT, platform TEXT NOT NULL, meeting_url TEXT NOT NULL, title TEXT, bot_name TEXT NOT NULL DEFAULT 'MinuteAI Recorder', status TEXT NOT NULL DEFAULT 'queued', scheduled_for DATETIME, duration_minutes INTEGER, started_at DATETIME, ended_at DATETIME, failure_reason TEXT, recording_path TEXT, job_id TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
        "ALTER TABLE meeting_bot_sessions ADD COLUMN title TEXT",
        "ALTER TABLE meeting_bot_sessions ADD COLUMN duration_minutes INTEGER",
        "ALTER TABLE meeting_bot_sessions ADD COLUMN bot_account_id TEXT",
        "ALTER TABLE meeting_bot_sessions ADD COLUMN calendar_event_id TEXT",
        "ALTER TABLE meeting_bot_sessions ADD COLUMN source TEXT",
        "CREATE TABLE IF NOT EXISTS bot_accounts (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL UNIQUE, email TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL DEFAULT 'MinuteAI Recorder', profile_dir TEXT NOT NULL, provider TEXT NOT NULL DEFAULT 'google', status TEXT NOT NULL DEFAULT 'needs_setup', kind TEXT NOT NULL DEFAULT 'dedicated', created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
        "CREATE TABLE IF NOT EXISTS admin_grants (id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, created_by_id TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
        "CREATE TABLE IF NOT EXISTS promo_codes (id TEXT PRIMARY KEY, code TEXT NOT NULL UNIQUE, description TEXT, discount_type TEXT NOT NULL DEFAULT 'percent', discount_value INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, max_uses INTEGER, uses_count INTEGER NOT NULL DEFAULT 0, expires_at DATETIME, created_by_id TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
        "CREATE TABLE IF NOT EXISTS promo_code_redemptions (id TEXT PRIMARY KEY, promo_code_id TEXT NOT NULL, user_id TEXT, checkout_session_id TEXT NOT NULL UNIQUE, subscription_id TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                pass  # column/table already exists
    _migrate_tasks_job_id_nullable()


def seed_default_workspaces():
    """Create a default workspace for every user that doesn't have one.
    Also link any TeamMember rows that have no workspace_id yet."""
    import uuid as _uuid_mod
    from models.user import User, Workspace, TeamMember
    with SessionLocal() as session:
        all_users = session.query(User).all()
        for user in all_users:
            exists = session.query(Workspace).filter(Workspace.owner_id == user.id).first()
            if not exists:
                first = (user.name or "My").split()[0]
                session.add(Workspace(
                    id=str(_uuid_mod.uuid4()),
                    name=f"{first}'s Workspace",
                    owner_id=user.id,
                ))
        session.commit()

        orphaned = session.query(TeamMember).filter(TeamMember.workspace_id.is_(None)).all()
        for m in orphaned:
            ws = session.query(Workspace).filter(Workspace.owner_id == m.owner_id).first()
            if ws:
                m.workspace_id = ws.id
        session.commit()

        # Backfill existing jobs that have no workspace_id
        unassigned = session.query(JobRecord).filter(JobRecord.workspace_id.is_(None), JobRecord.user_id.isnot(None)).all()
        for job in unassigned:
            ws = session.query(Workspace).filter(Workspace.owner_id == job.user_id).first()
            if ws:
                job.workspace_id = ws.id
        session.commit()


def recover_stuck_jobs():
    """
    On startup, any job that was mid-pipeline when the server stopped is now
    orphaned — no thread is running it. Mark them as error so the frontend
    shows the Retry button, which resumes from the last completed step.
    """
    in_progress = {"uploading", "transcribing", "formatting", "generating"}
    with SessionLocal() as session:
        stuck = session.query(JobRecord).filter(JobRecord.status.in_(in_progress)).all()
        for record in stuck:
            record.status = "error"
            record.message = "Server restarted during processing — click Retry to resume."
        if stuck:
            session.commit()
