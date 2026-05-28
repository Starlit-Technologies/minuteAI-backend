from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from models.user import TeamMember, User, Workspace


def ws_dict(ws: Workspace, is_owner: bool, member_count: int) -> dict:
    return {
        "id":           ws.id,
        "name":         ws.name,
        "owner_id":     ws.owner_id,
        "is_owner":     is_owner,
        "member_count": member_count,
        "created_at":   ws.created_at.isoformat() if ws.created_at else None,
    }


def member_dict(m: TeamMember, linked_user: Optional[User] = None) -> dict:
    name = linked_user.name if linked_user else (m.name or m.invited_email.split("@")[0].title())
    initials = "".join(w[0] for w in name.split()[:2]).upper()
    return {
        "id":         m.id,
        "name":       name,
        "email":      m.invited_email,
        "role":       m.role,
        "status":     m.status,
        "initials":   initials,
        "avatar_url": linked_user.avatar_url if linked_user else None,
        "joined_at":  m.created_at.isoformat() if m.created_at else None,
    }


def bot_dict(bot) -> dict:
    return {
        "id":           bot.id,
        "workspace_id": bot.workspace_id,
        "email":        bot.email,
        "display_name": bot.display_name,
        "provider":     bot.provider,
        "status":       bot.status,
        "kind":         bot.kind,
        "created_at":   bot.created_at.isoformat() if bot.created_at else None,
    }


def get_accessible_workspace(workspace_id: str, current_user: User, db: Session) -> Workspace:
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if not ws:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found.")
    if ws.owner_id == current_user.id:
        return ws
    membership = db.query(TeamMember).filter(
        TeamMember.workspace_id == workspace_id,
        TeamMember.user_id == current_user.id,
        TeamMember.status == "active",
    ).first()
    if not membership:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a member of this workspace.")
    return ws
