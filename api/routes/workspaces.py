import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from api.deps import get_db, get_current_user
from models.user import User, Workspace, TeamMember
from models.bot_account import ensure_workspace_bot_account

router = APIRouter(prefix="/workspaces", tags=["workspaces"])

WORKSPACE_LIMITS = {"free": 1, "pro": 3, "team": 999}
VALID_ROLES = {"admin", "member", "viewer"}


# ── schemas ───────────────────────────────────────────────────────────────────

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


# ── helpers ───────────────────────────────────────────────────────────────────

def _ws_dict(ws: Workspace, is_owner: bool, member_count: int) -> dict:
    return {
        "id":           ws.id,
        "name":         ws.name,
        "owner_id":     ws.owner_id,
        "is_owner":     is_owner,
        "member_count": member_count,
        "created_at":   ws.created_at.isoformat() if ws.created_at else None,
    }


def _member_dict(m: TeamMember, linked_user: Optional[User] = None) -> dict:
    name     = linked_user.name if linked_user else (m.name or m.invited_email.split("@")[0].title())
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


def _bot_dict(bot) -> dict:
    return {
        "id": bot.id,
        "workspace_id": bot.workspace_id,
        "email": bot.email,
        "display_name": bot.display_name,
        "provider": bot.provider,
        "status": bot.status,
        "kind": bot.kind,
        "created_at": bot.created_at.isoformat() if bot.created_at else None,
    }


def _get_accessible_workspace(workspace_id: str, current_user: User, db: Session) -> Workspace:
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


# ── invite token lookup (public, no auth) — must be before /{workspace_id} ───

@router.get("/invite/{token}")
def get_invite(token: str, db: Session = Depends(get_db)):
    member = db.query(TeamMember).filter(TeamMember.invite_token == token).first()
    if not member:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invite not found or already used.")
    ws    = db.query(Workspace).filter(Workspace.id == member.workspace_id).first() if member.workspace_id else None
    owner = db.query(User).filter(User.id == member.owner_id).first()
    return {
        "inviter_name":   owner.name  if owner else "Someone",
        "inviter_email":  owner.email if owner else "",
        "workspace_id":   ws.id   if ws else None,
        "workspace_name": ws.name if ws else "a workspace",
        "role":           member.role,
        "invited_email":  member.invited_email,
        "status":         member.status,
        "has_account":    member.user_id is not None,
    }


# ── workspace CRUD ────────────────────────────────────────────────────────────

@router.get("")
def list_workspaces(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    owned = db.query(Workspace).filter(Workspace.owner_id == current_user.id).all()

    member_ws_ids = [
        r[0] for r in db.query(TeamMember.workspace_id).filter(
            TeamMember.user_id == current_user.id,
            TeamMember.status == "active",
            TeamMember.workspace_id.isnot(None),
        ).all()
    ]
    member_wss = db.query(Workspace).filter(
        Workspace.id.in_(member_ws_ids),
        Workspace.owner_id != current_user.id,
    ).all() if member_ws_ids else []

    result = []
    for ws in owned + member_wss:
        # +1 for owner
        mc = db.query(TeamMember).filter(TeamMember.workspace_id == ws.id, TeamMember.status == "active").count() + 1
        result.append(_ws_dict(ws, ws.owner_id == current_user.id, mc))
    return result


@router.post("", status_code=201)
def create_workspace(
    body: CreateWorkspaceIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    plan  = current_user.plan or "free"
    limit = WORKSPACE_LIMITS.get(plan, 1)
    count = db.query(Workspace).filter(Workspace.owner_id == current_user.id).count()
    if count >= limit:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Your {plan} plan allows up to {limit} workspace(s). Upgrade to create more.",
        )
    ws = Workspace(name=body.name.strip(), owner_id=current_user.id)
    db.add(ws)
    db.commit()
    db.refresh(ws)
    ensure_workspace_bot_account(ws.id)
    return _ws_dict(ws, True, 1)


@router.patch("/{workspace_id}")
def rename_workspace(
    workspace_id: str,
    body: RenameWorkspaceIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = db.query(Workspace).filter(Workspace.id == workspace_id, Workspace.owner_id == current_user.id).first()
    if not ws:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found.")
    ws.name = body.name.strip()
    db.commit()
    db.refresh(ws)
    mc = db.query(TeamMember).filter(TeamMember.workspace_id == ws.id, TeamMember.status == "active").count() + 1
    return _ws_dict(ws, True, mc)


@router.delete("/{workspace_id}", status_code=204)
def delete_workspace(
    workspace_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = db.query(Workspace).filter(Workspace.id == workspace_id, Workspace.owner_id == current_user.id).first()
    if not ws:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found.")
    total = db.query(Workspace).filter(Workspace.owner_id == current_user.id).count()
    if total <= 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot delete your only workspace.")
    db.delete(ws)
    db.commit()


# ── members ───────────────────────────────────────────────────────────────────

@router.get("/{workspace_id}/members")
def list_members(
    workspace_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = _get_accessible_workspace(workspace_id, current_user, db)

    owner = db.query(User).filter(User.id == ws.owner_id).first()
    result = []
    if owner:
        initials = "".join(w[0] for w in (owner.name or "O").split()[:2]).upper()
        result.append({
            "id":         "owner",
            "name":       owner.name,
            "email":      owner.email,
            "role":       "owner",
            "status":     "active",
            "initials":   initials,
            "avatar_url": owner.avatar_url,
            "joined_at":  None,
        })

    members = db.query(TeamMember).filter(TeamMember.workspace_id == workspace_id).order_by(TeamMember.created_at).all()
    for m in members:
        linked = db.query(User).filter(User.id == m.user_id).first() if m.user_id else None
        result.append(_member_dict(m, linked))
    return result


@router.post("/{workspace_id}/invite", status_code=201)
def invite_member(
    workspace_id: str,
    body: InviteIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = db.query(Workspace).filter(Workspace.id == workspace_id, Workspace.owner_id == current_user.id).first()
    if not ws:
        # allow admins too
        m = db.query(TeamMember).filter(
            TeamMember.workspace_id == workspace_id,
            TeamMember.user_id == current_user.id,
            TeamMember.role == "admin",
            TeamMember.status == "active",
        ).first()
        ws = db.query(Workspace).filter(Workspace.id == workspace_id).first() if m else None
    if not ws:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the workspace owner or admins can invite members.")

    if body.role not in VALID_ROLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Role must be one of: {', '.join(VALID_ROLES)}")
    if body.email == current_user.email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot invite yourself.")
    if body.email == (db.query(User).filter(User.id == ws.owner_id).first() or User()).email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This person is already the workspace owner.")

    existing = db.query(TeamMember).filter(
        TeamMember.workspace_id == workspace_id,
        TeamMember.invited_email == body.email,
    ).first()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "This person has already been invited to this workspace.")

    linked_user = db.query(User).filter(User.email == body.email).first()
    member = TeamMember(
        workspace_id  = workspace_id,
        owner_id      = ws.owner_id,
        invited_email = body.email,
        role          = body.role,
        name          = body.name,
        user_id       = linked_user.id if linked_user else None,
        status        = "active" if linked_user else "pending",
        invite_token  = secrets.token_urlsafe(32),
    )
    db.add(member)
    db.commit()
    db.refresh(member)

    invitee_name = (linked_user.name if linked_user else body.name) or body.email.split("@")[0].title()
    try:
        from services.auth_email import send_team_invite
        send_team_invite(
            to_email       = body.email,
            inviter_name   = current_user.name or current_user.email,
            invitee_name   = invitee_name,
            role           = body.role,
            has_account    = linked_user is not None,
            invite_token   = member.invite_token,
            workspace_name = ws.name,
        )
    except Exception:
        pass

    return _member_dict(member, linked_user)


@router.get("/{workspace_id}/bot-account")
def get_workspace_bot(
    workspace_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_accessible_workspace(workspace_id, current_user, db)
    bot = ensure_workspace_bot_account(workspace_id)
    if not bot:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recorder bot account not found.")
    return _bot_dict(bot)


@router.patch("/{workspace_id}/members/{member_id}")
def update_member_role(
    workspace_id: str,
    member_id: str,
    body: UpdateRoleIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = db.query(Workspace).filter(Workspace.id == workspace_id, Workspace.owner_id == current_user.id).first()
    if not ws:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the workspace owner can change roles.")
    if body.role not in VALID_ROLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Role must be one of: {', '.join(VALID_ROLES)}")
    member = db.query(TeamMember).filter(TeamMember.id == member_id, TeamMember.workspace_id == workspace_id).first()
    if not member:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found.")
    member.role = body.role
    db.commit()
    return {"id": member.id, "role": member.role}


@router.delete("/{workspace_id}/members/{member_id}", status_code=204)
def remove_member(
    workspace_id: str,
    member_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = db.query(Workspace).filter(Workspace.id == workspace_id, Workspace.owner_id == current_user.id).first()
    if not ws:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the workspace owner can remove members.")
    member = db.query(TeamMember).filter(TeamMember.id == member_id, TeamMember.workspace_id == workspace_id).first()
    if not member:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found.")
    db.delete(member)
    db.commit()
