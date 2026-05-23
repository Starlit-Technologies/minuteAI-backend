from sqlalchemy import Column, String, Text, DateTime, ForeignKey, func

from db.base import Base


class TaskRecord(Base):
    __tablename__ = "tasks"

    id                = Column(String,  primary_key=True)
    job_id            = Column(String,  ForeignKey("jobs.id",   ondelete="CASCADE"),   nullable=True,  index=True)
    workspace_id      = Column(String,  nullable=True,  index=True)
    title             = Column(Text,    nullable=False)
    owner_name        = Column(String,  nullable=True)
    assignee_id       = Column(String,  ForeignKey("users.id",  ondelete="SET NULL"),  nullable=True,  index=True)
    status            = Column(String,  nullable=False, default="todo")
    deadline          = Column(String,  nullable=True)    # ISO date string (YYYY-MM-DD) after normalization
    # External PM tool sync — populated when tasks are pushed to Linear, Notion, Jira, etc.
    external_id       = Column(String,  nullable=True)    # ID in the external tool
    external_provider = Column(String,  nullable=True)    # "linear" | "notion" | "jira" | "asana" | ...
    synced_at         = Column(DateTime, nullable=True)
    created_at        = Column(DateTime, server_default=func.now())
