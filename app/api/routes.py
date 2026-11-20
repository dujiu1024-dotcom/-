from pathlib import Path
from fastapi import APIRouter, HTTPException, Query
from app.collectors.registry import list_platforms
from app.collectors.mediacrawler_import import read_export, normalize_row
from app.config import IMPORT_ROOT, DEFAULT_RECENT_HOURS
from app.models import ImportRequest
from app.storage.db import upsert_comment, list_comments
from app.services.demand import demand_candidates

router = APIRouter(prefix="/api/v1")


@router.get("/platforms")
def platforms():
    return list_platforms()


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


@router.get("/comments")
def comments(since_hours: int = Query(DEFAULT_RECENT_HOURS, ge=1, le=8760),
             platform: str | None = None, include_unknown_time: bool = False):
    rows = list_comments(since_hours=since_hours, platform=platform,
                         include_unknown_time=include_unknown_time)
    return {"count": len(rows), "since_hours": since_hours, "items": rows}


@router.get("/demands")
def demands(since_hours: int = Query(DEFAULT_RECENT_HOURS, ge=1, le=8760),
            limit: int = Query(100, ge=1, le=1000)):
    items = demand_candidates(since_hours=since_hours, limit=limit)
    return {"count": len(items), "items": items}
