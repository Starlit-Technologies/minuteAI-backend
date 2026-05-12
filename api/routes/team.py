import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from middleware.auth import get_db, get_current_user
from models.user import User, TeamMember
from schemas.team import InviteIn, UpdateRoleIn
from services.auth_email import send_team_invite
from services.team import member_dict

router = APIRouter(prefix="/team", tags=["team"])

VALID_ROLES = {"admin", "member", "viewer"}


@router.get("/invite/{token}")
def get_invite(token: str, db: Session = Depends(get_db)):
    member = db.query(TeamMember).filter(TeamMember.invite_token == token).first()
    if not member:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invite not found or has already been used.")
    owner = db.query(User).filter(User.id == member.owner_id).first()
    return {
        "inviter_name":  owner.name  if owner else "Someone",
        "inviter_email": owner.email if owner else "",
        "role":          member.role,
        "invited_email": member.invited_email,
        "status":        member.status,
        "has_account":   member.user_id is not None,
    }


@router.get("")
def list_team(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    members = (
        db.query(TeamMember)
        .filter(TeamMember.owner_id == current_user.id)
        .order_by(TeamMember.created_at.asc())
        .all()
    )
    result = []
    for m in members:
        linked = db.query(User).filter(User.id == m.user_id).first() if m.user_id else None
        result.append(member_dict(m, linked))
    return result


@router.post("/invite", status_code=201)
def invite_member(
    body: InviteIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if body.role not in VALID_ROLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Role must be one of: {', '.join(VALID_ROLES)}")
    if body.email == current_user.email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot invite yourself.")

    existing = db.query(TeamMember).filter(
        TeamMember.owner_id == current_user.id,
        TeamMember.invited_email == body.email,
    ).first()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "This person has already been invited.")

    linked_user = db.query(User).filter(User.email == body.email).first()
    member = TeamMember(
        owner_id=current_user.id,
        invited_email=body.email,
        role=body.role,
        name=body.name,
        user_id=linked_user.id if linked_user else None,
        status="active" if linked_user else "pending",
        invite_token=secrets.token_urlsafe(32),
    )
    db.add(member)
    db.commit()
    db.refresh(member)

    invitee_name = (linked_user.name if linked_user else body.name) or body.email.split("@")[0].title()
    send_team_invite(
        to_email=body.email,
        inviter_name=current_user.name or current_user.email,
        invitee_name=invitee_name,
        role=body.role,
        has_account=linked_user is not None,
        invite_token=member.invite_token,
    )
    return member_dict(member, linked_user)


@router.patch("/{member_id}")
def update_role(
    member_id: str,
    body: UpdateRoleIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if body.role not in VALID_ROLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Role must be one of: {', '.join(VALID_ROLES)}")

    member = db.query(TeamMember).filter(
        TeamMember.id == member_id, TeamMember.owner_id == current_user.id
    ).first()
    if not member:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found.")

    member.role = body.role
    db.commit()
    return {"id": member.id, "role": member.role}


@router.delete("/{member_id}", status_code=204)
def remove_member(
    member_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    member = db.query(TeamMember).filter(
        TeamMember.id == member_id, TeamMember.owner_id == current_user.id
    ).first()
    if not member:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found.")
    db.delete(member)
    db.commit()
