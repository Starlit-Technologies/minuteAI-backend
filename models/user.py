import uuid
from datetime import datetime
from sqlalchemy import Column, String, Boolean, DateTime, Integer, ForeignKey, func, UniqueConstraint
from db.base import Base


def _uuid():
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id             = Column(String, primary_key=True, default=_uuid)
    name           = Column(String, nullable=False)
    email          = Column(String, nullable=False, unique=True, index=True)
    password_hash  = Column(String, nullable=False)
    email_verified = Column(Boolean, nullable=False, default=False)
    plan           = Column(String, nullable=False, default="free")
    timezone              = Column(String, nullable=True)
    language              = Column(String, nullable=True)
    theme                 = Column(String, nullable=True)
    stripe_customer_id    = Column(String, nullable=True, unique=True, index=True)
    stripe_subscription_id = Column(String, nullable=True)
    avatar_url            = Column(String, nullable=True)
    last_login_at         = Column(DateTime, nullable=True)
    created_at            = Column(DateTime, server_default=func.now())
    updated_at     = Column(DateTime, server_default=func.now(), onupdate=datetime.utcnow)


class OTPCode(Base):
    __tablename__ = "otp_codes"

    id         = Column(String, primary_key=True, default=_uuid)
    user_id    = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    code       = Column(String(6), nullable=False)
    type       = Column(String, nullable=False)   # "verify_email" | "reset_password"
    expires_at = Column(DateTime, nullable=False)
    used       = Column(Boolean, nullable=False, default=False)
    attempts   = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, server_default=func.now())


class Workspace(Base):
    __tablename__ = "workspaces"

    id         = Column(String, primary_key=True, default=_uuid)
    name       = Column(String, nullable=False)
    owner_id   = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime, server_default=func.now())


class TeamMember(Base):
    __tablename__ = "team_members"

    id            = Column(String, primary_key=True, default=_uuid)
    workspace_id  = Column(String, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True, index=True)
    owner_id      = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id       = Column(String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    invited_email = Column(String, nullable=False)
    name          = Column(String, nullable=True)
    role          = Column(String, nullable=False, default="member")
    status        = Column(String, nullable=False, default="pending")
    invite_token  = Column(String, nullable=True, unique=True, index=True)
    created_at    = Column(DateTime, server_default=func.now())


class AdminGrant(Base):
    __tablename__ = "admin_grants"

    id             = Column(String, primary_key=True, default=_uuid)
    email          = Column(String, nullable=False, unique=True, index=True)
    created_by_id  = Column(String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at     = Column(DateTime, server_default=func.now())


class PromoCode(Base):
    __tablename__ = "promo_codes"

    id             = Column(String, primary_key=True, default=_uuid)
    code           = Column(String, nullable=False, unique=True, index=True)
    description    = Column(String, nullable=True)
    discount_type  = Column(String, nullable=False, default="percent")
    discount_value = Column(Integer, nullable=False, default=0)
    active         = Column(Boolean, nullable=False, default=True)
    max_uses       = Column(Integer, nullable=True)
    uses_count     = Column(Integer, nullable=False, default=0)
    expires_at     = Column(DateTime, nullable=True)
    created_by_id  = Column(String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at     = Column(DateTime, server_default=func.now())


class PromoCodeRedemption(Base):
    __tablename__ = "promo_code_redemptions"

    id                  = Column(String, primary_key=True, default=_uuid)
    promo_code_id       = Column(String, ForeignKey("promo_codes.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id             = Column(String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    checkout_session_id = Column(String, nullable=False, unique=True, index=True)
    subscription_id     = Column(String, nullable=True, index=True)
    created_at          = Column(DateTime, server_default=func.now())
