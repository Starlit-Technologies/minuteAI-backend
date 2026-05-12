from typing import Optional
from pydantic import BaseModel, EmailStr


class CreateWorkspaceIn(BaseModel):
    name: str


class RenameWorkspaceIn(BaseModel):
    name: str


class InviteIn(BaseModel):
    email: EmailStr
    role:  str = "member"
    name:  Optional[str] = None


class UpdateRoleIn(BaseModel):
    role: str
