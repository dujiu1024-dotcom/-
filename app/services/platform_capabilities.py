from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from app.collectors.registry import PLATFORMS
from app.services.crawler_engine import MODES, SUPPORTED
from app.storage.db import connect

OPERATIONS = {
    "search_content": "关键词搜索内容",
    "fetch_content_detail": "获取内容详情",
    "fetch_comments": "获取一级评论",
    "fetch_replies": "获取二级回复",
    "creator_posts": "按作者主页获取内容",
}
SEVEN_PLATFORMS = ("xhs", "douyin", "kuaishou", "bilibili", "weibo", "tieba", "zhihu")


def _adapter_support(platform: str, operation: str) -> tuple[bool, str]:
    config = PLATFORMS.get(platform)
    if not config:
        return False, "平台未登记"
    capabilities = set(config.get("capabilities", []))
    if platform not in SUPPORTED:
        return False, "当前七平台 MediaCrawler 执行器未接入该平台"
    mode_by_operation = {
        "search_content": ("search", "search"),
        "fetch_content_detail": ("detail", "detail"),
        "fetch_comments": (None, "comments"),
        "fetch_replies": (None, "replies"),
        "creator_posts": ("creator", "creator"),
    }
    mode, capability = mode_by_operation[operation]
    if capability not in capabilities:
        return False, "平台注册表未声明该能力"
    if mode and mode not in MODES:
        return False, "采集引擎未实现对应执行模式"
    return True, "代码入口已登记；实际可用性仍需本机授权环境验证"


def _latest_records() -> dict[tuple[str, str], dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT v.* FROM platform_verification_runs v
               JOIN (
                 SELECT platform, operation, MAX(tested_at) AS latest_at
                 FROM platform_verification_runs GROUP BY platform, operation
               ) latest
               ON latest.platform=v.platform AND latest.operation=v.operation
                  AND latest.latest_at=v.tested_at
               ORDER BY v.tested_at DESC"""
        ).fetchall()
    return {(row["platform"], row["operation"]): dict(row) for row in rows}


def list_capability_matrix() -> dict[str, Any]:
    latest = _latest_records()
    platforms = []
    for platform in SEVEN_PLATFORMS:
        config = PLATFORMS[platform]
        operations = {}
        for operation, label in OPERATIONS.items():
            supported, reason = _adapter_support(platform, operation)
            record = latest.get((platform, operation))
            operations[operation] = {
                "label": label,
                "adapter_available": supported,
                "adapter_status": "available" if supported else "unsupported",
                "adapter_note": reason,
                "verification_status": record["status"] if record else "not_tested",
                "last_verified_at": record["tested_at"] if record else None,
                "verification_id": record["id"] if record else None,
                "evidence": record["evidence"] if record else None,
                "notes": record["notes"] if record else None,
            }
        platforms.append({
            "code": platform,
            "name": config["name"],
            "engine": config["engine"],
            "operations": operations,
        })
    return {
        "operations": [{"code": code, "name": label} for code, label in OPERATIONS.items()],
        "items": platforms,
        "verification_policy": "适配器登记不等于实测通过；只有存在验证记录时才显示实测状态。",
        "live_test_performed": False,
        "live_test_note": "本次调度明确跳过真机验证；所有未记录项保持待实测。",
    }


def get_platform_capability(platform: str) -> dict[str, Any] | None:
    matrix = list_capability_matrix()
    return next((item for item in matrix["items"] if item["code"] == platform), None)


def record_platform_verification(
    platform: str,
    operation: str,
    status: str,
    evidence: str,
    notes: str = "",
    engine_version: str = "",
    environment: str = "",
) -> dict[str, Any]:
    if platform not in SEVEN_PLATFORMS:
        raise ValueError("仅允许记录首批七个平台")
    if operation not in OPERATIONS:
        raise ValueError("不支持的能力项")
    if status not in {"passed", "partial", "failed", "unavailable", "skipped"}:
        raise ValueError("status 必须为 passed/partial/failed/unavailable/skipped")
    if not evidence.strip():
        raise ValueError("必须提供可追溯的验证证据或跳过原因")
    record = {
        "id": uuid.uuid4().hex,
        "platform": platform,
        "operation": operation,
        "status": status,
        "tested_at": datetime.now(UTC).isoformat(),
        "engine": PLATFORMS[platform]["engine"],
        "engine_version": engine_version.strip(),
        "environment": environment.strip(),
        "evidence": evidence.strip(),
        "notes": notes.strip(),
    }
    with connect() as conn:
        conn.execute(
            """INSERT INTO platform_verification_runs
               (id,platform,operation,status,tested_at,engine,engine_version,environment,evidence,notes)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            tuple(record[key] for key in (
                "id", "platform", "operation", "status", "tested_at", "engine",
                "engine_version", "environment", "evidence", "notes"
            )),
        )
    return record
