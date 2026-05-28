import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from middleware.auth import get_current_user
from models.user import User

router = APIRouter(prefix="/memory", tags=["memory"])

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


class SearchIn(BaseModel):
    query: str
    thread_id: Optional[str] = None
    n_results: int = 8


def _resolve_workspace(user_id: str) -> Optional[str]:
    try:
        from db.base import SessionLocal
        from models.user import Workspace
        with SessionLocal() as db:
            ws = db.query(Workspace).filter(Workspace.owner_id == user_id).first()
            return ws.id if ws else None
    except Exception:
        return None


@router.post("/search")
def search(
    body: SearchIn,
    current_user: User = Depends(get_current_user),
):
    q = (body.query or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="Query is required.")

    from services.ai.memory_search import search_memory

    workspace_id = _resolve_workspace(current_user.id)

    chunks = search_memory(
        query=q,
        user_id=current_user.id,
        workspace_id=workspace_id,
        thread_id=body.thread_id,
        n_results=min(body.n_results, 12),
    )

    if not chunks:
        return {"answer": None, "sources": [], "query": q}

    # ── Synthesise answer with Claude Haiku ───────────────────────────────────
    answer = None
    if ANTHROPIC_API_KEY:
        try:
            context = "\n\n---\n".join(
                f"[{c['meeting_title']} · {c['meeting_date']}]\n{c['text']}"
                for c in chunks[:6]
            )
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=500,
                messages=[{
                    "role": "user",
                    "content": (
                        "You are a meeting intelligence assistant with access to past meeting records.\n"
                        "Answer the question below using ONLY the excerpts provided. "
                        "If the excerpts don't contain enough information, say so briefly. "
                        "Keep the answer to 2–4 sentences. "
                        "When citing a specific meeting, mention its title naturally.\n\n"
                        f"MEETING EXCERPTS:\n{context}\n\n"
                        f"QUESTION: {q}"
                    ),
                }],
            )
            answer = msg.content[0].text.strip()
        except Exception:
            pass

    # Deduplicate sources by job_id, keep highest-score entry per meeting
    seen: dict[str, dict] = {}
    for c in chunks:
        jid = c["job_id"]
        if jid not in seen or c["score"] > seen[jid]["score"]:
            seen[jid] = c

    sources = [
        {
            "job_id":        s["job_id"],
            "meeting_title": s["meeting_title"],
            "meeting_date":  s["meeting_date"],
            "excerpt":       s["text"][:220] + ("…" if len(s["text"]) > 220 else ""),
            "score":         s["score"],
        }
        for s in sorted(seen.values(), key=lambda x: -x["score"])
    ]

    return {"answer": answer, "sources": sources, "query": q}


@router.get("/stats")
def stats(current_user: User = Depends(get_current_user)):
    """Return total indexed chunks for the current workspace."""
    try:
        from services.ai.memory_search import _get_collection
        col = _get_collection()
        total = col.count() if col else 0
    except Exception:
        total = 0
    return {"total_chunks": total}
