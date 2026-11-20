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
from app.services.source_tracking import (
    discover_candidates, list_candidates, list_sources, merge_sources,
    review_candidate, update_source, configure_schedule, list_schedules,
    list_source_runs, save_checkpoint, get_checkpoint,
)
from app.services.scheduler import tick_schedules
from app.storage.db import get_recent_hours, list_comments, upsert_comment

router = APIRouter(prefix="/api/v1")


class TimeWindowRequest(BaseModel):
    hours: int = Field(ge=1, le=8760)


class AnalysisRequest(BaseModel):
    since_hours: int | None = Field(default=None, ge=1, le=8760)
    limit: int = Field(default=500, ge=1, le=3000)


class ExternalCollectRequest(BaseModel):
    platform: str
    mode: str = "search"
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=50, ge=1, le=200)


class SourceReviewRequest(BaseModel):
    action: str
    group_name: str = Field(default="", max_length=100)
    priority: int = Field(default=5, ge=1, le=10)
    interval_minutes: int = Field(default=1440, ge=15, le=10080)
    window_hours: int = Field(default=72, ge=1, le=8760)
    tracking_scope: str = "creator"


class SourceUpdateRequest(BaseModel):
    group_name: str | None = Field(default=None, max_length=100)
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=1, le=10)
    interval_minutes: int | None = Field(default=None, ge=15, le=10080)
    window_hours: int | None = Field(default=None, ge=1, le=8760)
    tracking_scope: str | None = None
    tracking_target: str | None = Field(default=None, max_length=500)
    tracking_verified: bool | None = None


class SourceCheckpointRequest(BaseModel):
    cursor: dict = Field(default_factory=dict)
    last_content_id: str | None = None
    window_start: str | None = None
    window_end: str | None = None


class SourceScheduleRequest(BaseModel):
    enabled: bool


class SourceMergeRequest(BaseModel):
    source_ids: list[str] = Field(min_length=2, max_length=100)
    target_id: str


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


@router.post("/sources/discover")
def source_discover(limit: int = Query(500, ge=1, le=5000)):
    return discover_candidates(limit)


@router.get("/sources/candidates")
def source_candidates(status: str | None = None, limit: int = Query(200, ge=1, le=1000)):
    return {"count": len(items := list_candidates(status, limit)), "items": items}


@router.post("/sources/candidates/{candidate_id}/review")
def source_candidate_review(candidate_id: str, payload: SourceReviewRequest):
    try:
        return review_candidate(candidate_id, payload.action, payload.group_name,
                                payload.priority, payload.interval_minutes,
                                payload.window_hours, payload.tracking_scope)
    except KeyError as exc:
        raise HTTPException(404, "来源候选不存在") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/sources")
def sources(enabled: bool | None = None, limit: int = Query(500, ge=1, le=2000)):
    items = list_sources(enabled, limit)
    return {"count": len(items), "items": items}


@router.patch("/sources/{source_id}")
def source_update(source_id: str, payload: SourceUpdateRequest):
    try:
        return update_source(source_id, **payload.model_dump(exclude_none=True))
    except KeyError as exc:
        raise HTTPException(404, "追踪来源不存在") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/sources/schedules")
def source_schedules():
    return {"count": len(items := list_schedules()), "items": items}


@router.post("/sources/{source_id}/schedule")
def source_schedule_update(source_id: str, payload: SourceScheduleRequest):
    try:
        return configure_schedule(source_id, payload.enabled)
    except KeyError as exc:
        raise HTTPException(404, "追踪来源不存在") from exc


@router.get("/sources/runs")
def source_runs(source_id: str | None = None, limit: int = Query(100, ge=1, le=500)):
    items = list_source_runs(source_id, limit)
    return {"count": len(items), "items": items}


@router.post("/scheduler/tick")
def scheduler_tick():
    return tick_schedules()


@router.get("/sources/{source_id}/checkpoint")
def source_checkpoint(source_id: str):
    try:
        return get_checkpoint(source_id)
    except KeyError as exc:
        raise HTTPException(404, "追踪来源不存在") from exc


@router.put("/sources/{source_id}/checkpoint")
def source_checkpoint_update(source_id: str, payload: SourceCheckpointRequest):
    try:
        return save_checkpoint(source_id, payload.cursor, payload.last_content_id,
                              payload.window_start, payload.window_end)
    except KeyError as exc:
        raise HTTPException(404, "追踪来源不存在") from exc


@router.post("/sources/merge")
def source_merge(payload: SourceMergeRequest):
    try:
        return merge_sources(payload.source_ids, payload.target_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
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
