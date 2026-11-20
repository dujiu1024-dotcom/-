from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.collectors.mediacrawler_import import normalize_row, read_export
from app.collectors.registry import list_platforms
from app.collectors.reverse_cli import collect as collect_external, install_reverse_cli, reverse_status
from app.config import IMPORT_ROOT
from app.models import ImportRequest
from app.services.analysis import get_clusters, get_trends, run_analysis
from app.services.crawler_engine import (
    engine_status, get_job, import_latest_exports, install_engine, read_job_log,
    session_status, start_job, stop_job, list_jobs,
)
from app.services.demand import demand_candidates
from app.services.settings import get_settings, update_recent_hours
from app.storage.db import (connect, create_feedback, get_recent_hours, list_comments, list_feedback, upsert_comment)

router = APIRouter(prefix="/api/v1")


class TimeWindowRequest(BaseModel):
    hours: int = Field(ge=1, le=8760)


class AnalysisRequest(BaseModel):
    since_hours: int | None = Field(default=None, ge=1, le=8760)
    limit: int = Field(default=500, ge=1, le=3000)



class FeedbackRequest(BaseModel):
    target_type: str = Field(pattern="^(comment|content|demand_cluster)$")
    target_id: str = Field(min_length=1, max_length=300)
    label: str = Field(pattern="^(valid_demand|non_demand|category_correction|merge|split)$")
    corrected_category: str | None = Field(default=None, max_length=100)
    corrected_summary: str | None = Field(default=None, max_length=1000)
    reason: str | None = Field(default=None, max_length=2000)
    evidence_note: str | None = Field(default=None, max_length=4000)
    actor: str = Field(default="local_user", min_length=1, max_length=100)

class ExternalCollectRequest(BaseModel):
    platform: str
    mode: str = "search"
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=50, ge=1, le=200)


@router.get("/platforms")
def platforms():
    return list_platforms()


@router.get("/engine/status")
def get_engine_status():
    return engine_status()


@router.post("/engine/install")
def post_engine_install():
    try:
        return install_engine()
    except (RuntimeError, OSError) as exc:
        raise HTTPException(500, str(exc)) from exc


@router.get("/collectors/reverse/status")
def external_engine_status():
    return reverse_status()


@router.post("/collectors/reverse/install")
def external_engine_install():
    try:
        return install_reverse_cli()
    except (RuntimeError, OSError) as exc:
        raise HTTPException(500, str(exc)) from exc


@router.post("/collectors/reverse/collect")
def external_collect(payload: ExternalCollectRequest):
    try:
        return collect_external(payload.platform, payload.mode, payload.query, payload.limit)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/sessions")
def sessions():
    return session_status()


@router.post("/crawler/jobs")
def create_crawler_job(payload: dict):
    try:
        return start_job(payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/crawler/jobs")
def crawler_jobs():
    return {"items": list_jobs()}


@router.get("/crawler/jobs/{job_id}")
def crawler_job(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return job


@router.get("/crawler/jobs/{job_id}/log")
def crawler_job_log(job_id: str):
    try:
        return {"job_id": job_id, "log": read_job_log(job_id)}
    except KeyError as exc:
        raise HTTPException(404, "任务不存在") from exc


@router.delete("/crawler/jobs/{job_id}")
def delete_crawler_job(job_id: str):
    job = stop_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return job


@router.post("/imports/exports")
def import_exports():
    try:
        counts = import_latest_exports()
    except (ValueError, OSError, UnicodeError) as exc:
        raise HTTPException(400, f"导入失败：{exc}") from exc
    return {"imported_by_platform": counts, "total_imported": sum(counts.values())}


@router.post("/imports/mediacrawler")
def import_mediacrawler(request: ImportRequest):
    candidate = (IMPORT_ROOT / request.path).resolve()
    if IMPORT_ROOT not in candidate.parents and candidate != IMPORT_ROOT:
        raise HTTPException(400, "导入文件必须位于 IMPORT_ROOT 目录内")
    if not candidate.is_file():
        raise HTTPException(404, "导入文件不存在")
    imported = skipped = 0
    try:
        for index, row in enumerate(read_export(candidate), start=1):
            record = normalize_row(row, request.platform, index)
            if record is None:
                skipped += 1
                continue
            upsert_comment(record)
            imported += 1
    except (ValueError, OSError, UnicodeError) as exc:
        raise HTTPException(400, f"导入失败：{exc}") from exc
    return {"platform": request.platform, "imported": imported, "skipped": skipped,
            "source": candidate.name}


@router.get("/settings")
def settings():
    return get_settings()


@router.put("/settings/time-window")
def set_time_window(payload: TimeWindowRequest):
    try:
        return update_recent_hours(payload.hours)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/comments")
def comments(since_hours: int | None = Query(None, ge=1, le=8760),
             platform: str | None = None, include_unknown_time: bool = False):
    hours = since_hours or get_recent_hours()
    rows = list_comments(since_hours=hours, platform=platform,
                         include_unknown_time=include_unknown_time)
    return {"count": len(rows), "since_hours": hours, "items": rows}



@router.post("/feedback", status_code=201)
def post_feedback(payload: FeedbackRequest):
    target_id = payload.target_id.strip()
    if not target_id:
        raise HTTPException(422, "target_id 不能为空")
    try:
        if payload.target_type == "comment":
            if "::" not in target_id:
                raise HTTPException(422, "评论 target_id 必须使用 platform::comment_id")
            platform, comment_id = target_id.split("::", 1)
            with connect() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM comments WHERE platform=? AND comment_id=?",
                    (platform, comment_id),
                ).fetchone()
            if not exists:
                raise HTTPException(404, "评论不存在")
        elif payload.target_type == "content":
            with __import__("app.storage.db", fromlist=["connect"]).connect() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM comments WHERE content_id=? LIMIT 1", (target_id,)
                ).fetchone()
            if not exists:
                raise HTTPException(404, "内容不存在")
        else:
            with __import__("app.storage.db", fromlist=["connect"]).connect() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM demand_clusters WHERE cluster_id=?", (target_id,)
                ).fetchone()
            if not exists:
                raise HTTPException(404, "需求簇不存在")
        return create_feedback(
            target_type=payload.target_type, target_id=target_id, label=payload.label,
            corrected_category=payload.corrected_category,
            corrected_summary=payload.corrected_summary, reason=payload.reason,
            evidence_note=payload.evidence_note, actor=payload.actor,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/feedback")
def get_feedback(
    target_type: str | None = Query(None, pattern="^(comment|content|demand_cluster)$"),
    target_id: str | None = Query(None, min_length=1, max_length=300),
    label: str | None = Query(None, pattern="^(valid_demand|non_demand|category_correction|merge|split)$"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    try:
        return list_feedback(
            target_type=target_type, target_id=target_id, label=label,
            limit=limit, offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/analysis/run")
def analysis_run(payload: AnalysisRequest):
    try:
        return run_analysis(payload.since_hours, payload.limit)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/demands")
def demands(limit: int = Query(100, ge=1, le=1000)):
    clusters = get_clusters(limit)
    if clusters:
        return {"count": len(clusters), "items": clusters, "sorted_by": "strength_score"}
    items = demand_candidates(since_hours=get_recent_hours(), limit=limit)
    return {"count": len(items), "items": items, "sorted_by": "rule_candidates"}


@router.get("/analytics/trends")
def analytics_trends(since_hours: int = Query(720, ge=1, le=8760),
                     bucket: str = Query("day", pattern="^(hour|day)$")):
    return get_trends(since_hours, bucket)
