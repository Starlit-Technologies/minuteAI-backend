from typing import Optional

from models.user import TeamMember, User


def member_dict(m: TeamMember, linked_user: Optional[User] = None) -> dict:
    name = linked_user.name if linked_user else (m.name or m.invited_email.split("@")[0].title())
    initials = "".join(w[0] for w in name.split()[:2]).upper()
    return {
        "id":        m.id,
        "name":      name,
        "email":     m.invited_email,
        "role":      m.role,
        "status":    m.status,
        "initials":  initials,
        "joined_at": m.created_at.isoformat() if m.created_at else None,
    }
