from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from app.collectors.registry import PLATFORMS
from app.storage.db import connect


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _health_state(
    supported: bool, latest: dict | None, consecutive_failures: int,
    zero_result_streak: int, now: datetime,
) -> tuple[str, str]:
    if not supported:
        return "unsupported", "当前平台适配器不提供可用的评论采集能力"
    if latest is None:
        return "paused", "统计窗口内没有采集任务记录"
    if latest["status"] == "blocked":
        return "blocked", latest.get("error_code") or "最近一次采集被平台阻断"
    if consecutive_failures >= 2:
        return "failing", f"最近连续失败 {consecutive_failures} 次"
    if latest["status"] in {"failed", "interrupted"}:
        return "failing", latest.get("error_code") or f"最近任务状态为 {latest['status']}"
    if zero_result_streak >= 2:
        return "low_yield", f"最近连续 {zero_result_streak} 次成功任务零结果"
    if latest["status"] == "succeeded" and latest["result_count"] == 0:
        return "low_yield", "最近一次成功任务未采集到结果"
    try:
        last_started = datetime.fromisoformat(latest["started_at"])
        if last_started.tzinfo is None:
            last_started = last_started.replace(tzinfo=UTC)
        if now - last_started > timedelta(days=7):
            return "paused", "最近一次采集已超过 7 天"
    except (TypeError, ValueError):
        pass
    return "healthy", "最近一次采集成功且有结果"


def quality_overview(since_hours: int = 168) -> dict:
    """Compute collection execution, field completeness and source health from persisted facts."""
    if since_hours < 1 or since_hours > 8760:
        raise ValueError("since_hours must be between 1 and 8760")
    now = datetime.now(UTC)
    cutoff = (now - timedelta(hours=since_hours)).isoformat()
    with connect() as conn:
        task_rows = [dict(row) for row in conn.execute(
            """SELECT * FROM collection_task_runs
               WHERE started_at >= ? ORDER BY started_at DESC, task_id ASC""",
            (cutoff,),
        ).fetchall()]
        comment_rows = conn.execute(
            """SELECT COUNT(*) AS total,
                SUM(CASE WHEN trim(comment_id) <> '' THEN 1 ELSE 0 END) AS comment_id,
                SUM(CASE WHEN content_id IS NOT NULL AND trim(content_id) <> '' THEN 1 ELSE 0 END) AS content_id,
                SUM(CASE WHEN content_url IS NOT NULL AND trim(content_url) <> '' THEN 1 ELSE 0 END) AS content_url,
                SUM(CASE WHEN author_key IS NOT NULL AND trim(author_key) <> '' THEN 1 ELSE 0 END) AS author_id,
                SUM(CASE WHEN comment_created_at IS NOT NULL AND trim(comment_created_at) <> ''
                    AND julianday(comment_created_at) IS NOT NULL THEN 1 ELSE 0 END) AS comment_created_at,
                SUM(CASE WHEN comment_created_at IS NOT NULL AND
                    (upper(substr(trim(comment_created_at), -1)) = 'Z'
                     OR instr(substr(comment_created_at, 20), '+') > 0
                     OR instr(substr(comment_created_at, 20), '-') > 0)
                    AND julianday(comment_created_at) IS NOT NULL THEN 1 ELSE 0 END) AS timezone_explicit,
                SUM(CASE WHEN trim(comment_text) <> '' THEN 1 ELSE 0 END) AS comment_text,
                SUM(CASE WHEN like_count IS NOT NULL THEN 1 ELSE 0 END) AS like_count,
                SUM(CASE WHEN content_title IS NOT NULL AND trim(content_title) <> '' THEN 1 ELSE 0 END) AS content_title
               FROM comments WHERE imported_at >= ?""",
            (cutoff,),
        ).fetchone()

    comment_total = int(comment_rows["total"] or 0)
    supported_fields = {
        "comment_id": "评论 ID", "content_id": "内容 ID", "content_url": "内容链接",
        "author_id": "作者 ID", "comment_created_at": "评论时间",
        "timezone_explicit": "时区明确", "comment_text": "评论正文",
        "like_count": "点赞数", "content_title": "内容标题",
    }
    fields = {}
    for key, label in supported_fields.items():
        complete = int(comment_rows[key] or 0)
        fields[key] = {
            "label": label, "status": "supported", "total": comment_total,
            "complete": complete, "missing": max(0, comment_total - complete),
            "completeness_rate": _rate(complete, comment_total),
        }
    fields["author_url"] = {
        "label": "作者主页链接", "status": "not_applicable",
        "total": comment_total, "complete": None, "missing": None,
        "completeness_rate": None,
        "note": "当前 comments 标准模型未单独存储该字段，不计入采集缺失率",
    }

    total = len(task_rows)
    by_status = defaultdict(int)
    for task in task_rows:
        by_status[task["status"]] += 1
    completed = sum(by_status[state] for state in ("succeeded", "failed", "blocked", "interrupted"))
    succeeded = by_status["succeeded"]
    retried = sum(1 for task in task_rows if task["retry_count"] > 0)
    zero_result = sum(
        1 for task in task_rows
        if task["status"] == "succeeded" and task["result_count"] == 0
    )
    durations = sorted(task["duration_ms"] for task in task_rows if task["duration_ms"] is not None)
    median_duration = None
    p95_duration = None
    if durations:
        middle = len(durations) // 2
        median_duration = durations[middle] if len(durations) % 2 else round(
            (durations[middle - 1] + durations[middle]) / 2
        )
        p95_duration = durations[max(0, int((len(durations) - 1) * 0.95))]

    grouped: dict[str, list[dict]] = defaultdict(list)
    for task in task_rows:
        grouped[task["platform"]].append(task)
    sources = []
    for platform, metadata in PLATFORMS.items():
        runs = grouped.get(platform, [])
        latest = runs[0] if runs else None
        consecutive_failures = 0
        for task in runs:
            if task["status"] in {"failed", "blocked", "interrupted"}:
                consecutive_failures += 1
            else:
                break
        zero_streak = 0
        for task in runs:
            if task["status"] == "succeeded" and task["result_count"] == 0:
                zero_streak += 1
            else:
                break
        supported = bool({"comments", "replies"} & set(metadata.get("capabilities", [])))
        state, reason = _health_state(
            supported, latest, consecutive_failures, zero_streak, now
        )
        successes = [task for task in runs if task["status"] == "succeeded"]
        sources.append({
            "platform": platform, "name": metadata["name"], "state": state,
            "reason": reason, "last_attempt_at": latest["started_at"] if latest else None,
            "last_success_at": next(
                (task["finished_at"] for task in runs if task["status"] == "succeeded"), None
            ),
            "task_count": len(runs),
            "success_rate": _rate(
                len(successes), sum(1 for task in runs if task["status"] != "running")
            ),
            "avg_result_count": round(
                sum(task["result_count"] for task in successes) / len(successes), 2
            ) if successes else None,
            "zero_result_streak": zero_streak,
            "consecutive_failures": consecutive_failures,
        })
    sources.sort(key=lambda item: (
        {"blocked": 0, "failing": 1, "low_yield": 2, "paused": 3,
         "healthy": 4, "unsupported": 5}.get(item["state"], 6),
        item["name"],
    ))

    denominator = comment_total * len(supported_fields)
    return {
        "generated_at": now.isoformat(), "period_hours": since_hours,
        "collection": {
            "task_total": total, "task_running": by_status["running"],
            "task_succeeded": succeeded, "task_failed": by_status["failed"],
            "task_blocked": by_status["blocked"], "task_interrupted": by_status["interrupted"],
            "task_success_rate": _rate(succeeded, completed),
            "retry_task_count": retried, "retry_rate": _rate(retried, total),
            "zero_result_task_count": zero_result,
            "zero_result_rate": _rate(zero_result, succeeded),
            "median_duration_ms": median_duration, "p95_duration_ms": p95_duration,
            "result_count": sum(task["result_count"] for task in task_rows),
            "retry_rate_note": "当前采集器未实现自动重试；按持久化任务 retry_count 统计，暂无重试记录",
        },
        "field_quality": {
            "comment_total": comment_total,
            "overall_completeness_rate": _rate(
                sum(int(comment_rows[key] or 0) for key in supported_fields), denominator
            ),
            "fields": fields,
        },
        "sources": sources,
        "definitions": {
            "task_success_rate": "成功任务数 / 已结束任务数；运行中任务不进入分母",
            "retry_rate": "至少发生过一次重试的任务数 / 任务总数",
            "zero_result_rate": "成功但结果数为 0 的任务数 / 成功任务数",
            "field_completeness": "非空且通过基础格式校验的字段数 / 窗口内导入评论应填字段数",
            "source_health": "按统计窗口内任务状态、连续失败和连续零结果判定",
        },
    }


def _period_cutoff(since_hours: int) -> tuple[datetime, str]:
    if since_hours < 1 or since_hours > 8760:
        raise ValueError("since_hours must be between 1 and 8760")
    now = datetime.now(UTC)
    return now, (now - timedelta(hours=since_hours)).isoformat()


def _duration_summary(values: list[int]) -> dict:
    ordered = sorted(values)
    if not ordered:
        return {"median_ms": None, "p95_ms": None}
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else round(
        (ordered[middle - 1] + ordered[middle]) / 2
    )
    return {
        "median_ms": median,
        "p95_ms": ordered[max(0, int((len(ordered) - 1) * 0.95))],
    }


def quality_collection(since_hours: int = 168) -> dict:
    """Return platform-level task outcomes and actionable error-code counts."""
    now, cutoff = _period_cutoff(since_hours)
    with connect() as conn:
        rows = [dict(row) for row in conn.execute(
            """SELECT platform, mode, status, retry_count, result_count,
                      error_code, started_at, finished_at, duration_ms
               FROM collection_task_runs
               WHERE started_at >= ?
               ORDER BY started_at DESC, task_id ASC""",
            (cutoff,),
        ).fetchall()]

    grouped: dict[str, list[dict]] = defaultdict(list)
    errors: dict[str, int] = defaultdict(int)
    for row in rows:
        grouped[row["platform"]].append(row)
        if row["status"] in {"failed", "blocked", "interrupted"}:
            errors[row["error_code"] or "UNKNOWN_ERROR"] += 1

    platforms = []
    for platform, runs in sorted(grouped.items()):
        statuses: dict[str, int] = defaultdict(int)
        for run in runs:
            statuses[run["status"]] += 1
        ended = len(runs) - statuses["running"]
        succeeded = statuses["succeeded"]
        durations = [run["duration_ms"] for run in runs if run["duration_ms"] is not None]
        platforms.append({
            "platform": platform,
            "task_total": len(runs),
            "task_running": statuses["running"],
            "task_succeeded": succeeded,
            "task_failed": statuses["failed"],
            "task_blocked": statuses["blocked"],
            "task_interrupted": statuses["interrupted"],
            "success_rate": _rate(succeeded, ended),
            "retry_task_count": sum(run["retry_count"] > 0 for run in runs),
            "result_count": sum(run["result_count"] for run in runs),
            "zero_result_task_count": sum(
                run["status"] == "succeeded" and run["result_count"] == 0 for run in runs
            ),
            "duration": _duration_summary(durations),
            "modes": dict(sorted({
                mode: sum(run["mode"] == mode for run in runs)
                for mode in {run["mode"] for run in runs}
            }.items())),
        })
    total = len(rows)
    succeeded_total = sum(row["status"] == "succeeded" for row in rows)
    ended_total = sum(row["status"] != "running" for row in rows)
    return {
        "generated_at": now.isoformat(),
        "period_hours": since_hours,
        "summary": {
            "task_total": total,
            "task_succeeded": succeeded_total,
            "task_failed": sum(row["status"] == "failed" for row in rows),
            "task_blocked": sum(row["status"] == "blocked" for row in rows),
            "task_interrupted": sum(row["status"] == "interrupted" for row in rows),
            "task_running": sum(row["status"] == "running" for row in rows),
            "success_rate": _rate(succeeded_total, ended_total),
            "retry_task_count": sum(row["retry_count"] > 0 for row in rows),
            "result_count": sum(row["result_count"] for row in rows),
        },
        "platforms": platforms,
        "error_codes": [
            {"code": code, "count": count}
            for code, count in sorted(errors.items(), key=lambda item: (-item[1], item[0]))
        ],
    }


def quality_demands(since_hours: int = 168) -> dict:
    """Summarize append-only human feedback using each target's latest judgement."""
    now, cutoff = _period_cutoff(since_hours)
    with connect() as conn:
        event_rows = [dict(row) for row in conn.execute(
            """SELECT label, COUNT(*) AS count
               FROM demand_feedback
               WHERE created_at >= ?
               GROUP BY label ORDER BY label""",
            (cutoff,),
        ).fetchall()]
        current_rows = [dict(row) for row in conn.execute(
            """WITH ranked AS (
                   SELECT target_type, target_id, label, corrected_category,
                          reason, created_at,
                          ROW_NUMBER() OVER (
                              PARTITION BY target_type, target_id
                              ORDER BY created_at DESC, id DESC
                          ) AS rank
                   FROM demand_feedback
               )
               SELECT target_type, target_id, label, corrected_category, reason, created_at
               FROM ranked WHERE rank=1 AND created_at >= ?
               ORDER BY created_at DESC, target_type, target_id""",
            (cutoff,),
        ).fetchall()]

    event_counts = {row["label"]: int(row["count"]) for row in event_rows}
    current_counts: dict[str, int] = defaultdict(int)
    target_counts: dict[str, int] = defaultdict(int)
    category_counts: dict[str, int] = defaultdict(int)
    reason_counts: dict[str, int] = defaultdict(int)
    for row in current_rows:
        current_counts[row["label"]] += 1
        target_counts[row["target_type"]] += 1
        if row["corrected_category"]:
            category_counts[row["corrected_category"]] += 1
        if row["reason"]:
            reason_counts[row["reason"].strip()] += 1

    positive = current_counts["valid_demand"] + current_counts["category_correction"]
    negative = current_counts["non_demand"]
    adjudicated = positive + negative
    return {
        "generated_at": now.isoformat(),
        "period_hours": since_hours,
        "event_count": sum(event_counts.values()),
        "current_target_count": len(current_rows),
        "event_labels": dict(sorted(event_counts.items())),
        "current_labels": dict(sorted(current_counts.items())),
        "targets": dict(sorted(target_counts.items())),
        "adjudication": {
            "labeled_count": adjudicated,
            "valid_demand_count": positive,
            "non_demand_count": negative,
            "valid_demand_rate": _rate(positive, adjudicated),
            "note": "valid_demand 与 category_correction 计为有效需求；merge/split 属于结构性操作，不进入二分类分母",
        },
        "corrected_categories": [
            {"category": category, "count": count}
            for category, count in sorted(category_counts.items(), key=lambda item: (-item[1], item[0]))
        ],
        "common_reasons": [
            {"reason": reason, "count": count}
            for reason, count in sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))[:10]
        ],
        "scope_note": "准确率/召回率以版本化 AI 预测与人工标签匹配计算，请使用 POST /api/v1/analysis/evaluate；本接口仅汇总人工反馈分布。",
    }


def quality_sources(since_hours: int = 168) -> dict:
    """Expose current source-health grouping while stating its platform-adapter granularity."""
    overview = quality_overview(since_hours)
    sources = overview["sources"]
    counts: dict[str, int] = defaultdict(int)
    for source in sources:
        counts[source["state"]] += 1
    return {
        "generated_at": overview["generated_at"],
        "period_hours": since_hours,
        "source_basis": "platform_adapter",
        "source_entity_detail_available": False,
        "state_counts": dict(sorted(counts.items())),
        "items": sources,
        "scope_note": "当前采集任务只持久化平台维度，尚无账号/关键词级来源实体；此处按平台适配器统计，不将平台伪装成具体来源。",
    }
