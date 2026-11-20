"""Stable error taxonomy for collection jobs.

This module classifies failures only. It deliberately does not retry or bypass
platform controls; retry orchestration is handled by the job scheduler.
"""
from __future__ import annotations

import re
from typing import Any

ERRORS: dict[str, dict[str, Any]] = {
    "rate_limited": {
        "code": "COLLECTION_RATE_LIMITED",
        "retryable": True,
        "manual_intervention": False,
        "hint": "平台触发限流；应按退避策略等待后再尝试。",
    },
    "captcha_required": {
        "code": "COLLECTION_CAPTCHA_REQUIRED",
        "retryable": False,
        "manual_intervention": True,
        "hint": "检测到验证码；请在平台允许的正常登录流程中人工处理。",
    },
    "authentication_required": {
        "code": "COLLECTION_AUTH_REQUIRED",
        "retryable": False,
        "manual_intervention": True,
        "hint": "登录状态失效或未登录；请重新登录并确认授权状态。",
    },
    "access_denied": {
        "code": "COLLECTION_ACCESS_DENIED",
        "retryable": False,
        "manual_intervention": True,
        "hint": "平台拒绝访问或权限不足；请检查账号权限及平台规则。",
    },
    "timeout": {
        "code": "COLLECTION_TIMEOUT",
        "retryable": True,
        "manual_intervention": False,
        "hint": "请求或连接超时；可在退避后有限重试。",
    },
    "network_error": {
        "code": "COLLECTION_NETWORK_ERROR",
        "retryable": True,
        "manual_intervention": False,
        "hint": "网络连接异常；检查网络后可有限重试。",
    },
    "upstream_unavailable": {
        "code": "COLLECTION_UPSTREAM_UNAVAILABLE",
        "retryable": True,
        "manual_intervention": False,
        "hint": "平台服务暂时不可用；可在退避后有限重试。",
    },
    "invalid_request": {
        "code": "COLLECTION_INVALID_REQUEST",
        "retryable": False,
        "manual_intervention": False,
        "hint": "采集参数或目标格式无效；修正参数后重新提交。",
    },
    "unknown": {
        "code": "COLLECTION_UNKNOWN",
        "retryable": False,
        "manual_intervention": True,
        "hint": "无法识别错误类型；请查看任务日志并人工确认。",
    },
}

_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("rate_limited", (r"\b429\b", r"too many requests", r"rate.?limit", r"频率过高", r"访问频繁", r"请求过于频繁")),
    ("captcha_required", (r"captcha", r"验证码", r"滑块验证", r"安全验证")),
    ("authentication_required", (r"\b401\b", r"unauthorized", r"未登录", r"登录失效", r"请先登录", r"cookie.*expired", r"登录状态")),
    ("access_denied", (r"\b403\b", r"forbidden", r"access denied", r"permission denied", r"无权访问", r"权限不足", r"拒绝访问")),
    ("timeout", (r"timed?\s*out", r"timeout", r"超时", r"readtimeout", r"connecttimeout")),
    ("network_error", (r"connectionerror", r"connection reset", r"connection refused", r"name or service not known", r"dns", r"网络不可达", r"连接中断", r"连接失败")),
    ("upstream_unavailable", (r"\b5\d\d\b", r"bad gateway", r"service unavailable", r"gateway timeout", r"服务器暂时不可用")),
    ("invalid_request", (r"invalid argument", r"invalid url", r"unsupported platform", r"参数无效", r"不支持的平台")),
)


def classify_collection_error(error: BaseException | str | None) -> dict[str, Any]:
    """Return a stable, UI-safe classification for an exception or log excerpt."""
    message = str(error or "").strip()
    lowered = message.lower()
    category = "unknown"
    for candidate, patterns in _PATTERNS:
        if any(re.search(pattern, lowered, re.IGNORECASE) for pattern in patterns):
            category = candidate
            break
    definition = ERRORS[category]
    return {
        "category": category,
        "code": definition["code"],
        "message": message[:2000],
        "retryable": definition["retryable"],
        "manual_intervention": definition["manual_intervention"],
        "hint": definition["hint"],
    }
