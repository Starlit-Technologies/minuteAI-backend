from typing import Optional
from pydantic import BaseModel


class CreateTaskIn(BaseModel):
    title:        str
    workspace_id: Optional[str] = None
    job_id:       Optional[str] = None
    assignee_id:  Optional[str] = None
    status:       str = "todo"
    deadline:     Optional[str] = None


class UpdateTaskIn(BaseModel):
    status:      Optional[str] = None
    assignee_id: Optional[str] = None
    title:       Optional[str] = None
    deadline:    Optional[str] = None
    job_id:      Optional[str] = None
