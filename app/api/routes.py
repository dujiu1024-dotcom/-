from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.collectors.mediacrawler_import import normalize_row, read_export
from app.collectors.registry import list_platforms
from app.collectors.reverse_cli import collect as collect_external, install_reverse_cli, reverse_status
from app.config import IMPORT_ROOT
from app.models import ImportRequest
from app.services.analysis import get_clusters, get_trends, run_analysis
from app.services.collection_library import (create_project, create_source, delete_project, delete_source, get_project, get_source, list_projects, update_project, update_source)
from app.services.crawler_engine import (
    engine_status, get_job, import_latest_exports, install_engine, read_job_log,
    session_status, start_job, stop_job, list_jobs,
)
from app.services.demand import demand_candidates
from app.services.settings import get_settings, update_recent_hours
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


class CollectionProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)


class CollectionSourceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    platform: str = Field(min_length=1, max_length=40)
    mode: str = Field(default="search", pattern="^(search|detail|creator|community)$")
    target: str = Field(min_length=1, max_length=1000)
    config: dict = Field(default_factory=dict)
    enabled: bool = True


@router.get("/collection/projects")
def collection_projects():
    return {"items": list_projects()}


@router.post("/collection/projects")
def collection_project_create(payload: CollectionProjectRequest):
    return create_project(payload.name, payload.description)


@router.patch("/collection/projects/{project_id}")
def collection_project_update(project_id: str, payload: dict):
    if "name" in payload and (not str(payload["name"]).strip() or len(str(payload["name"])) > 100):
        raise HTTPException(422, "项目名称不能为空且不超过 100 字")
    return update_project(project_id, payload.get("name"), payload.get("description")) or HTTPException(404, "采集项目不存在")


@router.delete("/collection/projects/{project_id}")
def collection_project_delete(project_id: str):
    if not delete_project(project_id):
        raise HTTPException(404, "采集项目不存在")
    return {"deleted": True, "id": project_id}


@router.post("/collection/projects/{project_id}/sources")
def collection_source_create(project_id: str, payload: CollectionSourceRequest):
    source = create_source(project_id, payload.model_dump())
    if not source:
        raise HTTPException(404, "采集项目不存在")
    return source


@router.patch("/collection/sources/{source_id}")
def collection_source_update(source_id: str, payload: dict):
    source = update_source(source_id, payload)
    if not source:
        raise HTTPException(404, "采集对象不存在")
    return source


@router.delete("/collection/sources/{source_id}")
def collection_source_delete(source_id: str):
    if not delete_source(source_id):
        raise HTTPException(404, "采集对象不存在")
    return {"deleted": True, "id": source_id}


@router.post("/collection/sources/{source_id}/run")
def collection_source_run(source_id: str):
    source = get_source(source_id)
    if not source:
        raise HTTPException(404, "采集对象不存在")
    if not source["enabled"]:
        raise HTTPException(409, "该采集对象已停用")
    config = source["config"]
    if source["platform"] in {"xhs", "douyin", "kuaishou", "bilibili", "weibo", "tieba", "zhihu"}:
        payload = {"platform": source["platform"], "mode": source["mode"],
                   "include_replies": config.get("include_replies", True),
                   "max_posts": config.get("max_posts", 20),
                   "max_comments_per_post": config.get("max_comments_per_post", 100)}
        payload[{"search": "keywords", "detail": "target_ids", "creator": "creator_ids"}.get(source["mode"], "keywords")] = source["target"]
        try:
            return start_job(payload)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
    try:
        return collect_external(source["platform"], "comments" if config.get("collect_comments", True) else source["mode"],
                                source["target"], int(config.get("limit", 50)))
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc


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
