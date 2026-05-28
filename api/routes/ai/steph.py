from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from middleware.auth import get_current_user, get_db
from models.user import User
from services.ai.steph_chat import get_reply

router = APIRouter(prefix="/steph", tags=["steph"])


class ChatRequest(BaseModel):
    message: str
    conversation_history: list[dict] = []
    workspace_id: str | None = None


@router.post("/chat")
def chat(
    body: ChatRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    reply = get_reply(
        message=body.message,
        user_id=current_user.id,
        workspace_id=body.workspace_id,
        history=body.conversation_history,
        user_name=current_user.name,
        db=db,
    )
    return {"reply": reply}


@router.get("/active-session")
def active_session(current_user: User = Depends(get_current_user)):
    from services.live.control_panel_bus import get_active_session_id
    return {"session_id": get_active_session_id(current_user.id)}
