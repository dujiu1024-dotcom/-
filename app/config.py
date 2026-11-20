from pathlib import Path
import os

APP_NAME = os.getenv("APP_NAME", "社交平台需求雷达")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "./data/demand_radar.sqlite3"))
IMPORT_ROOT = Path(os.getenv("IMPORT_ROOT", "./data/imports")).resolve()
DEFAULT_RECENT_HOURS = int(os.getenv("DEFAULT_RECENT_HOURS", "72"))
