from dotenv import load_dotenv
load_dotenv()

import asyncio
import os
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.routes.auth import router as auth_router
from api.routes.users import users_router, team_router, workspaces_router, tasks_router, notifications_router, integrations_router, meeting_bots_router
from api.routes.content import meetings_router, threads_router, consent_router
from api.routes.billing import router as billing_router
from api.routes.media import upload_router, status_router, results_router, download_router, pdf_audio_router, translate_router
from api.routes.live import ws_router, manager, ws_live_transcript_router, ws_control_panel_router, ws_live_translate_router, ai_live_router
from api.routes.admin import admin_router, config_router, support_router, anomalies_router
from api.routes.ai import steph_router, memory_router, chief_of_staff_router
from middleware.anomaly import AnomalyMiddleware

from models.job import store
from db.init_db import create_tables, run_migrations, recover_stuck_jobs, recover_stuck_bot_sessions, seed_default_workspaces
from models.bot_account import ensure_bot_accounts_for_all_workspaces
from models.meeting_bot import recover_processing_sessions
from services.meeting_bots.runner import start_pending_meeting_bot_sessions, start_bot_session_poll_loop
from services.meeting_bots.calendar_sync import start_calendar_sync_loop
from services.meeting_bots.health_check import start_health_check_loop
from services.core.user_calendar_sync import start_user_calendar_sync_loop
from services.core.trials import start_trial_reminder_loop
from services.notifications.digest import start_weekly_digest_loop
from services.notifications.pre_meeting_brief import start_pre_meeting_brief_loop
from services.ai.chief_of_staff import start_daily_briefing_loop

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
app.add_middleware(AnomalyMiddleware)

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
app.include_router(ws_live_transcript_router)
app.include_router(ws_control_panel_router)
app.include_router(ai_live_router)
app.include_router(consent_router)
app.include_router(pdf_audio_router)
app.include_router(threads_router)
app.include_router(config_router)
app.include_router(support_router)
app.include_router(anomalies_router)
app.include_router(memory_router)
app.include_router(chief_of_staff_router)


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
    start_bot_session_poll_loop()
    start_calendar_sync_loop()
    start_user_calendar_sync_loop()
    start_health_check_loop()
    start_trial_reminder_loop()
    start_weekly_digest_loop()
    start_pre_meeting_brief_loop()
    start_daily_briefing_loop()


Path("storage/avatars").mkdir(parents=True, exist_ok=True)
app.mount("/static/avatars", StaticFiles(directory="storage/avatars"), name="avatars")


@app.get("/health")
def health():
    return {"status": "ok"}
