import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from middleware.auth import get_db, get_current_user
from models.bot_account import ensure_workspace_bot_account
from models.user import User, Workspace, TeamMember
from schemas.workspaces import CreateWorkspaceIn, InviteIn, RenameWorkspaceIn, UpdateRoleIn
from services.users import _plan_limits
from services.workspaces import bot_dict, get_accessible_workspace, member_dict, ws_dict

router = APIRouter(prefix="/workspaces", tags=["workspaces"])

VALID_ROLES = {"admin", "member", "viewer"}


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
        mc = db.query(TeamMember).filter(TeamMember.workspace_id == ws.id, TeamMember.status == "active").count() + 1
        result.append(ws_dict(ws, ws.owner_id == current_user.id, mc))
    return result


@router.post("", status_code=201)
def create_workspace(
    body: CreateWorkspaceIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    plan  = current_user.plan or "free"
    limits = _plan_limits(db, plan)
    ws_limit = limits.get("workspaces", 1)
    if ws_limit is not None:
        count = db.query(Workspace).filter(Workspace.owner_id == current_user.id).count()
        if count >= ws_limit:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Your plan allows up to {ws_limit} workspace(s). Upgrade to create more.",
            )
    ws = Workspace(name=body.name.strip(), owner_id=current_user.id)
    db.add(ws)
    db.commit()
    db.refresh(ws)
    ensure_workspace_bot_account(ws.id)
    return ws_dict(ws, True, 1)


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
    return ws_dict(ws, True, mc)


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


@router.get("/{workspace_id}/members")
def list_members(
    workspace_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = get_accessible_workspace(workspace_id, current_user, db)
    owner = db.query(User).filter(User.id == ws.owner_id).first()
    result = []
    if owner:
        initials = "".join(w[0] for w in (owner.name or "O").split()[:2]).upper()
        result.append({
            "id": "owner", "name": owner.name, "email": owner.email,
            "role": "owner", "status": "active", "initials": initials,
            "avatar_url": owner.avatar_url, "joined_at": None,
        })
    members = db.query(TeamMember).filter(TeamMember.workspace_id == workspace_id).order_by(TeamMember.created_at).all()
    for m in members:
        linked = db.query(User).filter(User.id == m.user_id).first() if m.user_id else None
        result.append(member_dict(m, linked))
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

    # Enforce member limit based on workspace owner's plan
    ws_owner = db.query(User).filter(User.id == ws.owner_id).first()
    owner_plan = (ws_owner.plan if ws_owner else None) or "free"
    member_limit = _plan_limits(db, owner_plan).get("members")
    if member_limit is not None:
        current_count = db.query(TeamMember).filter(
            TeamMember.workspace_id == workspace_id,
            TeamMember.status.in_(["active", "pending"]),
        ).count()
        if current_count >= member_limit:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Your plan allows up to {member_limit} member(s) per workspace. Upgrade to invite more.",
            )

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
            to_email=body.email, inviter_name=current_user.name or current_user.email,
            invitee_name=invitee_name, role=body.role,
            has_account=linked_user is not None, invite_token=member.invite_token,
            workspace_name=ws.name,
        )
    except Exception:
        pass

    return member_dict(member, linked_user)


@router.get("/{workspace_id}/bot-account")
def get_workspace_bot(
    workspace_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_accessible_workspace(workspace_id, current_user, db)
    bot = ensure_workspace_bot_account(workspace_id)
    if not bot:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recorder bot account not found.")
    result = bot_dict(bot)
    # Override with the real Google account email configured in the admin bot setup
    try:
        from models.bot_config import load_bot_config, profile_status as _profile_status
        cfg = load_bot_config()
        ps = _profile_status(cfg)
        if ps.get("signed_in_email"):
            result["email"] = ps["signed_in_email"]
    except Exception:
        pass
    return result


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
