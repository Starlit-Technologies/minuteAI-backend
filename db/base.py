import os
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

_db_path = Path(__file__).parent.parent / "storage" / "minuteai.db"


def _database_url() -> str:
    configured = os.environ.get("DATABASE_URL", "").strip()
    if not configured:
        return f"sqlite:///{_db_path}"
    if configured.startswith("postgres://"):
        return configured.replace("postgres://", "postgresql://", 1)
    return configured


DATABASE_URL = _database_url()

_is_sqlite = DATABASE_URL.startswith("sqlite")
_connect_args = {"check_same_thread": False} if _is_sqlite else {}
_engine_kwargs = {"pool_pre_ping": True} if not _is_sqlite else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, **_engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()
