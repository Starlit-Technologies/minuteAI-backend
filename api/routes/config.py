from fastapi import APIRouter

from models.bot_config import load_bot_config

router = APIRouter(tags=["config"])


@router.get("/app-config")
def get_app_config():
    cfg = load_bot_config()
    return {
        "intro_video_url": cfg.get("intro_video_url", ""),
    }
