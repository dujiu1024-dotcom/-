import json
import shutil
import subprocess
import sys
from typing import Any

from app.config import REVERSE_ROOT, REVERSE_URL

COMMANDS = {
    "toutiao": {"search": ("toutiao", "search"), "comments": ("toutiao", "comments"), "detail": ("toutiao", "article")},
    "youtube": {"search": ("youtube", "search"), "comments": ("youtube", "comments"), "detail": ("youtube", "video")},
    "reddit": {"search": ("reddit", "search"), "comments": ("reddit", "more-comments"), "detail": ("reddit", "post")},
    "tiktok": {"search": ("tiktok", "search-videos"), "comments": ("tiktok", "comments"), "detail": ("tiktok", "video")},
    "twitter": {"search": ("twitter", "search-posts"), "detail": ("twitter", "tweet")},
    "instagram": {"search": ("instagram", "posts"), "detail": ("instagram", "post")},
    "threads": {"detail": ("threads", "post")},
    "linkedin": {"search": ("linkedin", "post"), "detail": ("linkedin", "post")},
    "telegram": {"search": ("telegram", "search"), "detail": ("telegram", "posts")},
    "wechat_mp": {"search": ("wechat_search", "articles"), "detail": ("wechat_mp", "article")},
    "wechat_channels": {"detail": ("wechat_channels", "feed")},
    "quora": {"search": ("quora", "search"), "detail": ("quora", "article")},
}


def reverse_status() -> dict[str, Any]:
    binary = shutil.which("reverse")
    return {"installed": bool(binary), "binary": binary, "root": str(REVERSE_ROOT)}


def install_reverse_cli() -> dict[str, Any]:
    REVERSE_ROOT.parent.mkdir(parents=True, exist_ok=True)
    if not REVERSE_ROOT.exists():
        clone = subprocess.run(["git", "clone", "--depth", "1", REVERSE_URL, str(REVERSE_ROOT)],
                               capture_output=True, text=True, timeout=300, check=False)
        if clone.returncode:
            raise RuntimeError((clone.stderr or clone.stdout)[-4000:])
    result = subprocess.run([sys.executable, "-m", "pip", "install", "-e", str(REVERSE_ROOT)],
                            capture_output=True, text=True, timeout=900, check=False)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout)[-6000:])
    return reverse_status()


def collect(platform: str, mode: str, query: str, limit: int = 50) -> dict[str, Any]:
    if platform not in COMMANDS or mode not in COMMANDS[platform]:
        raise ValueError(f"{platform} 暂不支持 {mode} 模式")
    binary = shutil.which("reverse")
    if not binary:
        raise RuntimeError("可选采集引擎 reverse CLI 未安装")
    group, command = COMMANDS[platform][mode]
    args = [binary, group, command, query, "--limit", str(max(1, min(limit, 200)))]
    result = subprocess.run(args, cwd=REVERSE_ROOT if REVERSE_ROOT.exists() else None,
                            capture_output=True, text=True, timeout=180, check=False)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "reverse CLI 执行失败")[-3000:])
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {"raw_output": result.stdout[:20000]}
    imported = 0
    if mode == "comments":
        from app.collectors.mediacrawler_import import normalize_row
        from app.storage.db import upsert_comment
        candidates = payload if isinstance(payload, list) else None
        if candidates is None and isinstance(payload, dict):
            for key in ("comments", "items", "data", "results", "replies"):
                if isinstance(payload.get(key), list):
                    candidates = payload[key]
                    break
        for index, row in enumerate(candidates or [], start=1):
            if not isinstance(row, dict):
                continue
            record = normalize_row(row, platform, index)
            if record:
                upsert_comment(record)
                imported += 1
    return {"platform": platform, "mode": mode, "items": payload,
            "imported_comments": imported, "engine": "reverse CLI"}
