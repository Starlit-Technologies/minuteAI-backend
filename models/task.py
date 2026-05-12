from sqlalchemy import Column, String, Text, DateTime, ForeignKey, func

from db.base import Base


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
