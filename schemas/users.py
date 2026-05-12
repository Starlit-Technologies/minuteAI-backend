from pydantic import BaseModel, EmailStr, Field


class UpdateProfileIn(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    email: EmailStr


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8)


class UpdatePrefsIn(BaseModel):
    timezone: str | None = None
    language: str | None = None
    theme: str | None = None
