"""In-memory event bus for Host Control Panel WebSockets.

Lifecycle:
  register(user_id, session_id)   — called when a live transcript session starts
  unregister(user_id, session_id) — called when it ends
  subscribe(session_id)           — control panel WS calls this to receive events
  publish(session_id, event)      — live transcript session calls this to emit events
"""

import asyncio

# session_id → list of subscriber queues
_queues: dict[str, list[asyncio.Queue]] = {}

# user_id → active session_id
_user_session: dict[str, str] = {}

# session_id → chat history [{role, content}]
_history: dict[str, list[dict]] = {}


def register(user_id: str, session_id: str) -> None:
    _user_session[user_id] = session_id
    _history.setdefault(session_id, [])


def unregister(user_id: str, session_id: str) -> None:
    if _user_session.get(user_id) == session_id:
        _user_session.pop(user_id, None)
    _queues.pop(session_id, None)
    _history.pop(session_id, None)


def get_active_session_id(user_id: str) -> str | None:
    return _user_session.get(user_id)


def subscribe(session_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _queues.setdefault(session_id, []).append(q)
    return q


def unsubscribe(session_id: str, q: asyncio.Queue) -> None:
    bucket = _queues.get(session_id)
    if bucket:
        try:
            bucket.remove(q)
        except ValueError:
            pass


def publish(session_id: str, event: dict) -> None:
    for q in list(_queues.get(session_id, [])):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


def get_history(session_id: str) -> list[dict]:
    return list(_history.get(session_id, []))


def append_history(session_id: str, role: str, content: str) -> None:
    bucket = _history.setdefault(session_id, [])
    bucket.append({"role": role, "content": content})
    if len(bucket) > 40:
        _history[session_id] = bucket[-40:]
