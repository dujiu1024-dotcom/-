import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")
DATA_ROOT = Path(os.getenv("DATA_ROOT", str(PROJECT_ROOT / "data"))).resolve()
APP_NAME = os.getenv("APP_NAME", "社交平台需求雷达")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", str(DATA_ROOT / "demand_radar.sqlite3")))
IMPORT_ROOT = Path(os.getenv("IMPORT_ROOT", str(DATA_ROOT / "imports"))).resolve()
ENGINE_ROOT = Path(os.getenv("MEDIACRAWLER_ROOT", str(PROJECT_ROOT / "vendor" / "MediaCrawler"))).resolve()
ENGINE_URL = os.getenv("MEDIACRAWLER_URL", "https://github.com/NanmiCoder/MediaCrawler.git")
REVERSE_ROOT = Path(os.getenv("REVERSE_ROOT", str(PROJECT_ROOT / "vendor" / "social-media-research-cli"))).resolve()
REVERSE_URL = os.getenv("REVERSE_URL", "https://github.com/ifccod/social-media-research-cli.git")
DEFAULT_RECENT_HOURS = int(os.getenv("DEFAULT_RECENT_HOURS", "72"))
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "")
LLM_EMBEDDING_MODEL = os.getenv("LLM_EMBEDDING_MODEL", "")
