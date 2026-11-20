from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.discovery import (
    execute_discovery_batch, execute_discovery_run, refresh_discovery_run, resume_discovery_batch,
    approve_discovered_source, create_discovery_run, create_strategy,
    ingest_discovered, list_discovered, list_strategies,
)

router = APIRouter(prefix="/api/v1/discovery", tags=["discovery"])


class StrategyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    topic: str = Field(min_length=1, max_length=300)
    terms: list[str] = Field(min_length=1, max_length=50)
    platforms: list[str] = Field(default_factory=list, max_length=30)
    language: str = Field(default="zh", max_length=12)
    enabled: bool = True


class BatchExecuteRequest(BaseModel):
    platforms: list[str] = Field(default_factory=list, max_length=7)


class DiscoveredItem(BaseModel):
    platform: str = Field(min_length=1, max_length=40)
    content_url: str = Field(min_length=1, max_length=2000)
    title: str = Field(default="", max_length=500)
    body: str = Field(default="", max_length=10000)
    author_name: str = Field(default="", max_length=200)
    author_url: str = Field(default="", max_length=2000)
    published_at: str | None = None
    query_term: str = Field(default="", max_length=300)
    fingerprint: str | None = None


class DiscoveryImportRequest(BaseModel):
    items: list[DiscoveredItem] = Field(min_length=1, max_length=500)


@router.get("/strategies")
def strategies(project_id: str | None = None):
    return {"items": list_strategies(project_id)}


@router.post("/projects/{project_id}/strategies")
def strategy_create(project_id: str, payload: StrategyRequest):
    try:
        result = create_strategy(project_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not result:
        raise HTTPException(404, "研究项目不存在")
    return result


@router.post("/strategies/{strategy_id}/runs")
def discovery_run_create(strategy_id: str):
    result = create_discovery_run(strategy_id)
    if not result:
        raise HTTPException(404, "搜索策略不存在")
    return result


@router.post("/runs/{run_id}/execute")
def discovery_run_execute(run_id: str, platform: str, query_index: int = Query(0, ge=0, le=49)):
    try:
        result = execute_discovery_run(run_id, platform, query_index)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not result:
        raise HTTPException(404, "发现批次不存在")
    return result


@router.post("/runs/{run_id}/execute-batch")
def discovery_run_execute_batch(run_id: str, payload: BatchExecuteRequest):
    try:
        result = execute_discovery_batch(run_id, payload.platforms or None)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not result:
        raise HTTPException(404, "发现批次不存在")
    return result


@router.post("/runs/{run_id}/resume")
def discovery_run_resume(run_id: str):
    try:
        result = resume_discovery_batch(run_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not result:
        raise HTTPException(404, "发现批次不存在")
    return result


@router.get("/runs/{run_id}")
def discovery_run_status(run_id: str):
    result = refresh_discovery_run(run_id)
    if not result:
        raise HTTPException(404, "发现批次不存在")
    return result


@router.get("/results")
def discovery_results(run_id: str | None = None, status: str | None = None,
                      limit: int = Query(100, ge=1, le=500),
                      offset: int = Query(0, ge=0)):
    return list_discovered(run_id, status, limit, offset)


@router.post("/runs/{run_id}/results")
def discovery_results_import(run_id: str, payload: DiscoveryImportRequest):
    result = ingest_discovered(run_id, [item.model_dump() for item in payload.items])
    if not result:
        raise HTTPException(404, "发现批次不存在")
    return result


@router.post("/results/{content_id}/approve")
def discovery_result_approve(content_id: str):
    try:
        result = approve_discovered_source(content_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not result:
        raise HTTPException(404, "发现结果不存在")
    return result
