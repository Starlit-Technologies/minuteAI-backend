# Backward-compat shim — import from canonical locations instead.
from db.base import Base, engine, SessionLocal, DATABASE_URL  # noqa: F401
from models.job import JobRecord                               # noqa: F401
from models.task import TaskRecord                             # noqa: F401
from models.notification import NotificationRecord             # noqa: F401
from models.meeting_bot import MeetingBotSessionRecord         # noqa: F401
from models.bot_account import BotAccountRecord                # noqa: F401
from models.connected_account import ConnectedAccountRecord    # noqa: F401
from db.init_db import (                                       # noqa: F401
    create_tables,
    run_migrations,
    seed_default_workspaces,
    recover_stuck_jobs,
)
