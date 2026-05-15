from dotenv import load_dotenv
load_dotenv()

import asyncio
import os
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.routes.auth import router as auth_router
from api.routes.users import router as users_router
from api.routes.meetings import router as meetings_router
from api.routes.team import router as team_router
from api.routes.billing import router as billing_router
from api.routes.upload import router as upload_router
from api.routes.status import router as status_router
from api.routes.results import router as results_router
from api.routes.download import router as download_router
from api.routes.ws import router as ws_router, manager
from api.routes.workspaces import router as workspaces_router
from api.routes.tasks import router as tasks_router
from api.routes.notifications import router as notifications_router
from api.routes.admin import router as admin_router
from api.routes.meeting_bots import router as meeting_bots_router
from api.routes.translate import router as translate_router
from api.routes.integrations import router as integrations_router
from api.routes.ws_live_translate import router as ws_live_translate_router
from api.routes.steph import router as steph_router

from models.job import store
from db.init_db import create_tables, run_migrations, recover_stuck_jobs, recover_stuck_bot_sessions, seed_default_workspaces
from models.bot_account import ensure_bot_accounts_for_all_workspaces
from models.meeting_bot import recover_processing_sessions
from services.meeting_bots.runner import start_pending_meeting_bot_sessions
from services.meeting_bots.calendar_sync import start_calendar_sync_loop
from services.user_calendar_sync import start_user_calendar_sync_loop

app = FastAPI(title="MinuteAI API", version="1.0.0")

DEFAULT_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://localhost:5174",
    "http://localhost:5175",
    "http://localhost:4175",
    "https://minuteai.vercel.app",
    "https://minuteai-frontend.onrender.com",
    "https://minuteai-admin.onrender.com",
]


def cors_origins():
    configured = [
        origin.strip().rstrip("/")
        for origin in os.environ.get("CORS_ORIGINS", "").split(",")
        if origin.strip()
    ]
    app_url = os.environ.get("APP_URL", "").strip().rstrip("/")
    if app_url:
        configured.append(app_url)
    return list(dict.fromkeys(DEFAULT_CORS_ORIGINS + configured))


app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(users_router)
app.include_router(meetings_router)
app.include_router(team_router)
app.include_router(workspaces_router)
app.include_router(tasks_router)
app.include_router(notifications_router)
app.include_router(admin_router)
app.include_router(meeting_bots_router)
app.include_router(translate_router)
app.include_router(integrations_router)
app.include_router(ws_live_translate_router)
app.include_router(steph_router)
app.include_router(billing_router)
app.include_router(upload_router)
app.include_router(status_router)
app.include_router(results_router)
app.include_router(download_router)
app.include_router(ws_router)


@app.on_event("startup")
async def startup():
    for d in ["storage/uploads", "storage/outputs", "storage/chunks", "storage/avatars",
              "storage/meeting_bot_debug", "storage/meeting_bot_captures"]:
        Path(d).mkdir(parents=True, exist_ok=True)
    create_tables()
    run_migrations()
    seed_default_workspaces()
    ensure_bot_accounts_for_all_workspaces()
    recover_stuck_jobs()
    recover_stuck_bot_sessions()
    recover_processing_sessions()
    manager.set_loop(asyncio.get_event_loop())
    store.set_notify(manager.notify)
    start_pending_meeting_bot_sessions()
    start_calendar_sync_loop()
    start_user_calendar_sync_loop()


Path("storage/avatars").mkdir(parents=True, exist_ok=True)
app.mount("/static/avatars", StaticFiles(directory="storage/avatars"), name="avatars")


@app.get("/health")
def health():
    return {"status": "ok"}
