import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path(os.getenv("DATA_ROOT", str(PROJECT_ROOT / "data"))).resolve()
APP_NAME = os.getenv("APP_NAME", "社交平台需求雷达")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", str(DATA_ROOT / "demand_radar.sqlite3")))
IMPORT_ROOT = Path(os.getenv("IMPORT_ROOT", str(DATA_ROOT / "imports"))).resolve()
ENGINE_ROOT = Path(os.getenv("MEDIACRAWLER_ROOT", str(PROJECT_ROOT / "vendor" / "MediaCrawler"))).resolve()
ENGINE_URL = os.getenv("MEDIACRAWLER_URL", "https://github.com/NanmiCoder/MediaCrawler.git")
DEFAULT_RECENT_HOURS = int(os.getenv("DEFAULT_RECENT_HOURS", "72"))
