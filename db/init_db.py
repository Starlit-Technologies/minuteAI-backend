from sqlalchemy import text

from db.base import engine, SessionLocal


def create_tables():
    import models.user            # noqa: F401  (includes SubscriptionPlan)
    import models.job             # noqa: F401
    import models.task            # noqa: F401
    import models.notification    # noqa: F401
    import models.meeting_bot     # noqa: F401
    import models.bot_account     # noqa: F401
    import models.connected_account  # noqa: F401
    import models.consent         # noqa: F401
    import models.meeting_thread  # noqa: F401
    from db.base import Base
    Base.metadata.create_all(bind=engine)


def _apply_migration(conn, sql: str):
    try:
        conn.execute(text(sql))
        conn.commit()
    except Exception:
        conn.rollback()


def _migrate_tasks_job_id_nullable():
    if engine.dialect.name != "sqlite":
        return
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
    if engine.dialect.name == "postgresql":
        migrations = [
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS user_id TEXT",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS starred BOOLEAN NOT NULL DEFAULT false",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS timezone TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS language TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS theme TEXT",
            "ALTER TABLE team_members ADD COLUMN IF NOT EXISTS invite_token TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMP",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_reminder_sent_at TIMESTAMP",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT",
            "ALTER TABLE team_members ADD COLUMN IF NOT EXISTS workspace_id TEXT",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS workspace_id TEXT",
            "ALTER TABLE meeting_bot_sessions ADD COLUMN IF NOT EXISTS title TEXT",
            "ALTER TABLE meeting_bot_sessions ADD COLUMN IF NOT EXISTS duration_minutes INTEGER",
            "ALTER TABLE meeting_bot_sessions ADD COLUMN IF NOT EXISTS bot_account_id TEXT",
            "ALTER TABLE meeting_bot_sessions ADD COLUMN IF NOT EXISTS calendar_event_id TEXT",
            "ALTER TABLE meeting_bot_sessions ADD COLUMN IF NOT EXISTS source TEXT",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS config_json TEXT",
            "ALTER TABLE meeting_bot_sessions ADD COLUMN IF NOT EXISTS logs_json TEXT",
            "CREATE TABLE IF NOT EXISTS connected_accounts (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, provider TEXT NOT NULL, access_token TEXT NOT NULL, refresh_token TEXT, token_expiry TIMESTAMP, email TEXT, auto_join BOOLEAN NOT NULL DEFAULT true, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE (user_id, provider))",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMP",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_completed BOOLEAN NOT NULL DEFAULT false",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS title TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS tools_json TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS meeting_join TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS recap_to TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS source TEXT",
            "CREATE TABLE IF NOT EXISTS subscription_plans (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL, description TEXT, price_monthly INTEGER NOT NULL DEFAULT 0, currency TEXT NOT NULL DEFAULT 'usd', interval TEXT NOT NULL DEFAULT 'month', features_json TEXT, meetings_per_month INTEGER, audio_hours_per_month INTEGER, stripe_product_id TEXT, stripe_price_id TEXT, is_active BOOLEAN NOT NULL DEFAULT true, is_popular BOOLEAN NOT NULL DEFAULT false, sort_order INTEGER NOT NULL DEFAULT 0, created_by_id TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
            "ALTER TABLE subscription_plans ADD COLUMN IF NOT EXISTS meetings_per_month INTEGER",
            "ALTER TABLE subscription_plans ADD COLUMN IF NOT EXISTS audio_hours_per_month INTEGER",
            "ALTER TABLE subscription_plans ADD COLUMN IF NOT EXISTS max_workspaces INTEGER",
            "ALTER TABLE subscription_plans ADD COLUMN IF NOT EXISTS max_members INTEGER",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS r2_audio_key TEXT",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS speaker_map_json TEXT",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS transcript_segments_json TEXT",
            "ALTER TABLE meeting_bot_sessions ADD COLUMN IF NOT EXISTS attendees_json TEXT",
            "ALTER TABLE connected_accounts ADD COLUMN IF NOT EXISTS settings_json TEXT",
            "CREATE TABLE IF NOT EXISTS consent_events (id TEXT PRIMARY KEY, user_id TEXT, job_id TEXT, session_id TEXT, event_type TEXT NOT NULL, legal_basis TEXT NOT NULL DEFAULT 'contract', user_name TEXT, user_email TEXT, ip_address TEXT, consented_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS push_token TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS two_factor_enabled BOOLEAN NOT NULL DEFAULT false",
            "ALTER TABLE subscription_plans ADD COLUMN IF NOT EXISTS credits_per_month INTEGER",
            # Meeting Thread system
            "CREATE TABLE IF NOT EXISTS meeting_threads (id TEXT PRIMARY KEY, workspace_id TEXT, created_by TEXT NOT NULL, name TEXT NOT NULL, participants_hash TEXT NOT NULL, participants_json TEXT, is_locked BOOLEAN NOT NULL DEFAULT false, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
            "CREATE TABLE IF NOT EXISTS thread_sessions (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES meeting_threads(id) ON DELETE CASCADE, job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE, session_number INTEGER NOT NULL DEFAULT 1, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
            # Task PM-sync fields
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS external_id TEXT",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS external_provider TEXT",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS synced_at TIMESTAMP",
            # Meeting thread category
            "ALTER TABLE meeting_threads ADD COLUMN IF NOT EXISTS category TEXT",
            # Public share token for bot session live-view page
            "ALTER TABLE meeting_bot_sessions ADD COLUMN IF NOT EXISTS share_token TEXT",
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_meeting_bot_sessions_share_token ON meeting_bot_sessions (share_token)",
        ]
    else:
        migrations = [
            "ALTER TABLE jobs ADD COLUMN user_id TEXT",
            "ALTER TABLE jobs ADD COLUMN starred INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN timezone TEXT",
            "ALTER TABLE users ADD COLUMN language TEXT",
            "ALTER TABLE users ADD COLUMN theme TEXT",
            "ALTER TABLE team_members ADD COLUMN invite_token TEXT",
            "ALTER TABLE users ADD COLUMN stripe_customer_id TEXT",
            "ALTER TABLE users ADD COLUMN stripe_subscription_id TEXT",
            "ALTER TABLE users ADD COLUMN trial_ends_at DATETIME",
            "ALTER TABLE users ADD COLUMN trial_reminder_sent_at DATETIME",
            "ALTER TABLE users ADD COLUMN avatar_url TEXT",
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
            "ALTER TABLE jobs ADD COLUMN config_json TEXT",
            "ALTER TABLE meeting_bot_sessions ADD COLUMN logs_json TEXT",
            "CREATE TABLE IF NOT EXISTS bot_accounts (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL UNIQUE, email TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL DEFAULT 'MinuteAI Recorder', profile_dir TEXT NOT NULL, provider TEXT NOT NULL DEFAULT 'google', status TEXT NOT NULL DEFAULT 'needs_setup', kind TEXT NOT NULL DEFAULT 'dedicated', created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
            "CREATE TABLE IF NOT EXISTS admin_grants (id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, created_by_id TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
            "CREATE TABLE IF NOT EXISTS promo_codes (id TEXT PRIMARY KEY, code TEXT NOT NULL UNIQUE, description TEXT, discount_type TEXT NOT NULL DEFAULT 'percent', discount_value INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, max_uses INTEGER, uses_count INTEGER NOT NULL DEFAULT 0, expires_at DATETIME, created_by_id TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
            "CREATE TABLE IF NOT EXISTS promo_code_redemptions (id TEXT PRIMARY KEY, promo_code_id TEXT NOT NULL, user_id TEXT, checkout_session_id TEXT NOT NULL UNIQUE, subscription_id TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
            "CREATE TABLE IF NOT EXISTS connected_accounts (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, provider TEXT NOT NULL, access_token TEXT NOT NULL, refresh_token TEXT, token_expiry DATETIME, email TEXT, auto_join INTEGER NOT NULL DEFAULT 1, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE (user_id, provider))",
            "ALTER TABLE users ADD COLUMN last_login_at DATETIME",
            "ALTER TABLE users ADD COLUMN onboarding_completed INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN role TEXT",
            "ALTER TABLE users ADD COLUMN title TEXT",
            "ALTER TABLE users ADD COLUMN tools_json TEXT",
            "ALTER TABLE users ADD COLUMN meeting_join TEXT",
            "ALTER TABLE users ADD COLUMN recap_to TEXT",
            "ALTER TABLE users ADD COLUMN source TEXT",
            "CREATE TABLE IF NOT EXISTS subscription_plans (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL, description TEXT, price_monthly INTEGER NOT NULL DEFAULT 0, currency TEXT NOT NULL DEFAULT 'usd', interval TEXT NOT NULL DEFAULT 'month', features_json TEXT, meetings_per_month INTEGER, audio_hours_per_month INTEGER, stripe_product_id TEXT, stripe_price_id TEXT, is_active INTEGER NOT NULL DEFAULT 1, is_popular INTEGER NOT NULL DEFAULT 0, sort_order INTEGER NOT NULL DEFAULT 0, created_by_id TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
            "ALTER TABLE subscription_plans ADD COLUMN meetings_per_month INTEGER",
            "ALTER TABLE subscription_plans ADD COLUMN audio_hours_per_month INTEGER",
            "ALTER TABLE subscription_plans ADD COLUMN max_workspaces INTEGER",
            "ALTER TABLE subscription_plans ADD COLUMN max_members INTEGER",
            "ALTER TABLE jobs ADD COLUMN r2_audio_key TEXT",
            "ALTER TABLE jobs ADD COLUMN speaker_map_json TEXT",
            "ALTER TABLE jobs ADD COLUMN transcript_segments_json TEXT",
            "ALTER TABLE meeting_bot_sessions ADD COLUMN attendees_json TEXT",
            "ALTER TABLE connected_accounts ADD COLUMN settings_json TEXT",
            "CREATE TABLE IF NOT EXISTS consent_events (id TEXT PRIMARY KEY, user_id TEXT, job_id TEXT, session_id TEXT, event_type TEXT NOT NULL, legal_basis TEXT NOT NULL DEFAULT 'contract', user_name TEXT, user_email TEXT, ip_address TEXT, consented_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
            "ALTER TABLE users ADD COLUMN push_token TEXT",
            "ALTER TABLE users ADD COLUMN two_factor_enabled INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE subscription_plans ADD COLUMN credits_per_month INTEGER",
            # Meeting Thread system
            "CREATE TABLE IF NOT EXISTS meeting_threads (id TEXT PRIMARY KEY, workspace_id TEXT, created_by TEXT NOT NULL, name TEXT NOT NULL, participants_hash TEXT NOT NULL, participants_json TEXT, is_locked INTEGER NOT NULL DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
            "CREATE TABLE IF NOT EXISTS thread_sessions (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES meeting_threads(id) ON DELETE CASCADE, job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE, session_number INTEGER NOT NULL DEFAULT 1, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
            # Task PM-sync fields
            "ALTER TABLE tasks ADD COLUMN external_id TEXT",
            "ALTER TABLE tasks ADD COLUMN external_provider TEXT",
            "ALTER TABLE tasks ADD COLUMN synced_at DATETIME",
            # Meeting thread category
            "ALTER TABLE meeting_threads ADD COLUMN category TEXT",
            # Public share token for bot session live-view page
            "ALTER TABLE meeting_bot_sessions ADD COLUMN share_token TEXT",
        ]
    with engine.connect() as conn:
        for sql in migrations:
            _apply_migration(conn, sql)
    _migrate_tasks_job_id_nullable()


def seed_default_workspaces():
    import uuid as _uuid_mod
    from models.user import User, Workspace, TeamMember
    from models.job import JobRecord
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

        unassigned = session.query(JobRecord).filter(JobRecord.workspace_id.is_(None), JobRecord.user_id.isnot(None)).all()
        for job in unassigned:
            ws = session.query(Workspace).filter(Workspace.owner_id == job.user_id).first()
            if ws:
                job.workspace_id = ws.id
        session.commit()


def recover_stuck_jobs():
    from models.job import JobRecord
    in_progress = {"uploading", "transcribing", "formatting", "generating"}
    with SessionLocal() as session:
        stuck = session.query(JobRecord).filter(JobRecord.status.in_(in_progress)).all()
        for record in stuck:
            record.status = "error"
            record.message = "Server restarted during processing — click Retry to resume."
        if stuck:
            session.commit()


def recover_stuck_bot_sessions():
    from models.meeting_bot import MeetingBotSessionRecord
    stuck_states = {"preparing", "joining", "waiting_for_host", "recording", "processing"}
    with SessionLocal() as session:
        stuck = session.query(MeetingBotSessionRecord).filter(
            MeetingBotSessionRecord.status.in_(stuck_states)
        ).all()
        for record in stuck:
            record.status = "failed"
            record.failure_reason = "Server restarted while the bot was active — please start a new session."
        if stuck:
            session.commit()
