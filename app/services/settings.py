from app.config import DEFAULT_RECENT_HOURS
from app.services.llm import provider_status
from app.storage.db import get_recent_hours, set_setting


def get_settings() -> dict:
    return {"recent_hours": get_recent_hours(), "default_recent_hours": DEFAULT_RECENT_HOURS,
            "llm": provider_status()}


def update_recent_hours(hours: int) -> dict:
    if not 1 <= hours <= 8760:
        raise ValueError("时间窗口必须在 1 到 8760 小时之间")
    set_setting("recent_hours", str(hours))
    return get_settings()
