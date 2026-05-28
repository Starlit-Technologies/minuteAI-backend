import asyncio
import json
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from models.job import store

router = APIRouter()


class ConnectionManager:
    def __init__(self):
        # job_id -> list of connected WebSockets
        self._connections: dict[str, list[WebSocket]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    async def connect(self, job_id: str, ws: WebSocket):
        await ws.accept()
        self._connections.setdefault(job_id, []).append(ws)

    def disconnect(self, job_id: str, ws: WebSocket):
        conns = self._connections.get(job_id, [])
        if ws in conns:
            conns.remove(ws)

    async def _send(self, job_id: str, payload: dict):
        conns = self._connections.get(job_id, [])
        dead = []
        for ws in conns:
            try:
                await ws.send_text(json.dumps(payload))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(job_id, ws)

    def notify(self, job_id: str, payload: dict):
        """Called from a background thread — schedules async send on the event loop."""
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._send(job_id, payload), self._loop)


# Singleton — imported by main.py and job store
manager = ConnectionManager()


@router.websocket("/ws/{job_id}")
async def ws_job(websocket: WebSocket, job_id: str):
    job = store.get(job_id)
    if not job:
        await websocket.close(code=4004)
        return

    await manager.connect(job_id, websocket)

    # Push current state immediately so the client doesn't wait for next change
    await websocket.send_text(json.dumps({
        "status": job.status,
        "message": job.message,
        "transcript": job.transcript,
    }))

    try:
        # Keep connection open — client sends nothing, we just push updates
        while True:
            await asyncio.sleep(20)
            # Send a keepalive ping so proxies don't close idle connections
            try:
                await websocket.send_text(json.dumps({"type": "ping"}))
            except Exception:
                break
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(job_id, websocket)
