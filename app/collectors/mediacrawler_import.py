import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from dateutil import parser as date_parser
from app.models import CommentRecord


ALIASES = {
    "comment_id": ["comment_id", "评论id", "评论ID", "cid", "id"],
    "content_id": ["content_id", "视频id", "笔记id", "作品id", "aweme_id", "note_id"],
    "content_url": ["content_url", "视频链接", "笔记链接", "作品链接", "url", "链接"],
    "content_title": ["content_title", "标题", "desc", "内容标题"],
    "comment_text": ["comment_text", "content", "评论内容", "评论", "text"],
    "comment_created_at": ["comment_created_at", "create_time", "评论时间", "发布时间", "created_at", "时间"],
    "content_created_at": ["content_created_at", "作品发布时间", "笔记发布时间"],
    "parent_comment_id": ["parent_comment_id", "回复评论id", "root_comment_id"],
    "author_key": ["author_key", "用户id", "user_id", "sec_uid"],
    "like_count": ["like_count", "点赞数", "点赞"],
}


def _pick(row: dict, names: list[str]):
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for name in names:
        if name.lower() in lowered and lowered[name.lower()] not in (None, ""):
            return lowered[name.lower()]
    return None


def _parse_datetime(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and re.fullmatch(r"\d{10,13}", value.strip())):
        number = float(value)
        if number > 10**12:
            number /= 1000
        try:
            return datetime.fromtimestamp(number, timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    try:
        parsed = date_parser.parse(str(value))
        if parsed.tzinfo is None:
            return None  # no timezone: do not guess and misclassify a 72-hour record
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def normalize_row(row: dict, platform: str, row_number: int) -> CommentRecord | None:
    text = _pick(row, ALIASES["comment_text"])
    if text is None or not str(text).strip():
        return None
    comment_id = _pick(row, ALIASES["comment_id"])
    if comment_id is None:
        # Stable fallback for exports without a comment ID; keeps source context in key.
        seed = f"{platform}|{_pick(row, ALIASES['content_id'])}|{text}|{row_number}"
        import hashlib
        comment_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
    like = _pick(row, ALIASES["like_count"])
    try:
        like_count = int(like) if like is not None else None
    except (ValueError, TypeError):
        like_count = None
    return CommentRecord(
        platform=platform,
        comment_id=str(comment_id),
        content_id=str(_pick(row, ALIASES["content_id"]) or "") or None,
        content_url=str(_pick(row, ALIASES["content_url"]) or "") or None,
        content_title=str(_pick(row, ALIASES["content_title"]) or "") or None,
        comment_text=str(text).strip(),
        comment_created_at=_parse_datetime(_pick(row, ALIASES["comment_created_at"])),
        content_created_at=_parse_datetime(_pick(row, ALIASES["content_created_at"])),
        parent_comment_id=str(_pick(row, ALIASES["parent_comment_id"]) or "") or None,
        author_key=str(_pick(row, ALIASES["author_key"]) or "") or None,
        like_count=like_count,
        raw=row,
    )


def read_export(path: Path):
    suffix = path.suffix.lower()
    if suffix in {".jsonl", ".ndjson"}:
        with path.open("r", encoding="utf-8-sig") as stream:
            for line in stream:
                if line.strip():
                    yield json.loads(line)
    elif suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, list):
            yield from data
        elif isinstance(data, dict):
            for key in ("comments", "data", "items", "records"):
                if isinstance(data.get(key), list):
                    yield from data[key]
                    return
            yield data
    elif suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            yield from csv.DictReader(stream)
    else:
        raise ValueError("仅支持 JSONL、JSON、CSV 文件")
