from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import DATA_ROOT, ENGINE_ROOT, ENGINE_URL

SUPPORTED = {"xhs": "xhs", "douyin": "dy", "kuaishou": "ks", "bilibili": "bili", "weibo": "wb", "tieba": "tieba", "zhihu": "zhihu"}
MODES = {"search", "detail", "creator"}
_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}
_processes: dict[str, subprocess.Popen] = {}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def engine_status() -> dict[str, Any]:
    config_file = ENGINE_ROOT / "config" / "base_config.py"
    config_text = config_file.read_text(encoding="utf-8") if config_file.is_file() else ""
    return {"installed": (ENGINE_ROOT / "main.py").is_file(), "isolated_profiles": "ENABLE_CDP_MODE = False" in config_text and "SAVE_LOGIN_STATE = True" in config_text, "root": str(ENGINE_ROOT), "expected_url": ENGINE_URL, "python": sys.executable}


def _enable_isolated_profiles() -> None:
    config_file = ENGINE_ROOT / "config" / "base_config.py"
    if not config_file.is_file():
        raise RuntimeError("MediaCrawler 配置文件结构变化，无法确认浏览器隔离模式；请勿直接启动采集")
    source = config_file.read_text(encoding="utf-8")
    patched, count = re.subn(r"(?m)^ENABLE_CDP_MODE\s*=\s*True\s*$", "ENABLE_CDP_MODE = False", source)
    if count != 1:
        if not re.search(r"(?m)^ENABLE_CDP_MODE\s*=\s*False\s*$", source):
            raise RuntimeError("未能安全关闭 MediaCrawler 的 CDP 共享浏览器模式，请检查上游版本")
        patched = source
    patched, save_count = re.subn(r"(?m)^SAVE_LOGIN_STATE\s*=\s*False\s*$", "SAVE_LOGIN_STATE = True", patched)
    if save_count or count:
        config_file.write_text(patched, encoding="utf-8")


def install_engine() -> dict[str, Any]:
    ENGINE_ROOT.parent.mkdir(parents=True, exist_ok=True)
    if (ENGINE_ROOT / "main.py").is_file():
        _enable_isolated_profiles()
        return {"installed": True, "already_installed": True, "isolated_profiles": True, "root": str(ENGINE_ROOT)}
    if ENGINE_ROOT.exists() and any(ENGINE_ROOT.iterdir()):
        raise RuntimeError(f"引擎目录已存在但不完整：{ENGINE_ROOT}，请检查后手动处理")
    result = subprocess.run(["git", "clone", "--depth", "1", ENGINE_URL, str(ENGINE_ROOT)], capture_output=True, text=True, timeout=300, check=False)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "git clone 失败")[-4000:])
    requirements = ENGINE_ROOT / "requirements.txt"
    _enable_isolated_profiles()
    if requirements.is_file():
        installed = subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(requirements)], capture_output=True, text=True, timeout=900, check=False)
        if installed.returncode:
            raise RuntimeError((installed.stderr or installed.stdout or "依赖安装失败")[-6000:])
    browser_install = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=True, text=True, timeout=900, check=False,
    )
    if browser_install.returncode:
        raise RuntimeError((browser_install.stderr or browser_install.stdout or "Playwright 浏览器安装失败")[-6000:])
    return {"installed": True, "already_installed": False, "isolated_profiles": True, "root": str(ENGINE_ROOT)}


def session_status() -> list[dict[str, Any]]:
    browser_root = ENGINE_ROOT / "browser_data"
    result = []
    for platform, code in SUPPORTED.items():
        paths = [browser_root / f"{code}_user_data_dir", browser_root / f"cdp_{code}_user_data_dir"]
        existing = [str(path) for path in paths if path.is_dir()]
        result.append({"platform": platform, "profile_exists": bool(existing), "profile_paths": existing, "login_state": "unknown" if existing else "not_initialized", "note": "目录存在不代表登录仍有效；启动可见浏览器后由平台页面确认。"})
    return result


def _build_command(payload: dict[str, Any]) -> tuple[str, list[str]]:
    platform = payload.get("platform")
    if platform not in SUPPORTED:
        raise ValueError(f"不支持的平台：{platform}")
    mode = payload.get("mode", "search")
    if mode not in MODES:
        raise ValueError("mode 仅支持 search、detail、creator")
    args = [sys.executable, "main.py", "--platform", SUPPORTED[platform], "--lt", "qrcode", "--type", mode,
            "--get_comment", "true", "--get_sub_comment", str(bool(payload.get("include_replies", True))).lower(),
            "--get_media", "false", "--headless", "false", "--save_data_option", "jsonl",
            "--crawler_max_notes_count", str(max(1, min(int(payload.get("max_posts", 20)), 100))),
            "--max_comments_count_singlenotes", str(max(1, min(int(payload.get("max_comments_per_post", 100)), 1000))),
            "--max_concurrency_num", "1", "--save_data_path", str((DATA_ROOT / "exports" / platform).resolve())]
    field = {"search": "keywords", "detail": "target_ids", "creator": "creator_ids"}[mode]
    value = str(payload.get(field, "")).strip()
    if not value:
        raise ValueError({"search": "请填写关键词", "detail": "请填写内容链接或 ID", "creator": "请填写博主主页链接或 ID"}[mode])
    args.extend([{"search": "--keywords", "detail": "--specified_id", "creator": "--creator_id"}[mode], value])
    return platform, args


def start_job(payload: dict[str, Any]) -> dict[str, Any]:
    if not (ENGINE_ROOT / "main.py").is_file():
        raise RuntimeError("MediaCrawler 尚未安装，请先安装采集引擎")
    platform, command = _build_command(payload)
    with _lock:
        running = [job for job in _jobs.values() if job["status"] == "running"]
        if running:
            raise RuntimeError(f"已有采集任务运行中：{running[0]['id']}；当前版本一次运行一个任务")
        job_id = uuid.uuid4().hex[:12]
        log_dir = DATA_ROOT / "logs"
        export_dir = DATA_ROOT / "exports" / platform
        log_dir.mkdir(parents=True, exist_ok=True)
        export_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{job_id}.log"
        with log_path.open("w", encoding="utf-8") as stream:
            process = subprocess.Popen(command, cwd=ENGINE_ROOT, stdout=stream, stderr=subprocess.STDOUT, text=True, env={**os.environ, "PYTHONUNBUFFERED": "1"})
        job = {"id": job_id, "platform": platform, "mode": payload.get("mode", "search"), "status": "running", "started_at": _now(), "finished_at": None, "pid": process.pid, "log_file": str(log_path), "export_dir": str(export_dir), "command": command[1:]}
        _jobs[job_id] = job
        _processes[job_id] = process
        threading.Thread(target=_watch_job, args=(job_id,), daemon=True).start()
        return dict(job)


def _watch_job(job_id: str) -> None:
    process = _processes[job_id]
    code = process.wait()
    with _lock:
        job = _jobs[job_id]
        job["status"] = "succeeded" if code == 0 else "failed"
        job["exit_code"] = code
        job["finished_at"] = _now()
        _processes.pop(job_id, None)


def list_jobs() -> list[dict[str, Any]]:
    with _lock:
        for job_id, process in list(_processes.items()):
            code = process.poll()
            if code is not None:
                job = _jobs[job_id]
                job["status"] = "succeeded" if code == 0 else "failed"
                job["exit_code"] = code
                job["finished_at"] = _now()
                _processes.pop(job_id, None)
        return [dict(job) for job in sorted(_jobs.values(), key=lambda item: item["started_at"], reverse=True)]


def get_job(job_id: str) -> dict[str, Any] | None:
    return next((job for job in list_jobs() if job["id"] == job_id), None)


def read_job_log(job_id: str, limit: int = 12000) -> str:
    job = get_job(job_id)
    if not job:
        raise KeyError(job_id)
    path = Path(job["log_file"])
    return path.read_text(encoding="utf-8", errors="replace")[-limit:] if path.is_file() else ""


def stop_job(job_id: str) -> dict[str, Any] | None:
    with _lock:
        process = _processes.get(job_id)
        if process:
            process.terminate()
    return get_job(job_id)


def import_latest_exports() -> dict[str, int]:
    from app.collectors.mediacrawler_import import normalize_row, read_export
    from app.storage.db import upsert_comment
    root = DATA_ROOT / "exports"
    counts = {}
    for platform in SUPPORTED:
        imported = 0
        folder = root / platform
        if folder.exists():
            for path in sorted(folder.rglob("*.jsonl")):
                for index, row in enumerate(read_export(path), start=1):
                    record = normalize_row(row, platform, index)
                    if record:
                        upsert_comment(record)
                        imported += 1
        counts[platform] = imported
    return counts
