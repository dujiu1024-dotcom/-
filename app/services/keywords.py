import json
import re
from datetime import UTC, datetime

from app.services.demand import classify
from app.services.llm import LLMUnavailable, expand_keyword_batch, provider_status
from app.storage.db import connect, get_setting, list_comments

_SPACE = re.compile(r"\\s+")
_PUNCT = re.compile(r"[，。！？、；：,.!?;:（）()【】\\[\\]{}\"'“”‘’]+")
_STOP = {"有没有", "怎么", "如何", "求推荐", "推荐一下", "怎么办", "为什么", "请问", "这个", "那个"}
_CATEGORY_MAP = {
    "问题表达": "问题求解", "场景词": "场景表达", "痛点词": "痛点表达",
    "求助词": "求助表达", "替代方案词": "替代需求", "付费意愿": "购买意向",
}
_SYNONYMS = {
    "批量处理": "批量处理", "批处理": "批量处理",
    "自动整理": "自动整理", "自动归档": "自动整理",
    "免费替代": "免费替代方案", "免费平替": "免费替代方案",
}


def normalize_keyword(value: str) -> str:
    text = _PUNCT.sub(" ", str(value or "").strip().lower())
    text = _SPACE.sub(" ", text).strip()
    for prefix in ("有没有", "求推荐", "请问", "想找", "求一个"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    for suffix in ("怎么办", "怎么解决", "如何解决", "求推荐", "吗", "呢", "啊"):
        if text.endswith(suffix) and len(text) > len(suffix) + 1:
            text = text[:-len(suffix)].strip()
    return _SYNONYMS.get(text, text)


def _blacklist() -> set[str]:
    try:
        values = json.loads(get_setting("keyword_blacklist", "[]") or "[]")
        return {normalize_keyword(item) for item in values if isinstance(item, str)}
    except (TypeError, ValueError):
        return set()


def _source_rows(project_id: str, limit: int) -> list[dict]:
    rows = list_comments(since_hours=None)[:max(1, min(limit, 1000))]
    with connect() as conn:
        analyses = {
            f"{row['platform']}::{row['comment_id']}": dict(row)
            for row in conn.execute(
                """SELECT a.platform,a.comment_id,a.category,a.need_summary,a.intent,a.confidence,
                          a.semantic_text,c.content_id,c.content_url,c.content_title,c.comment_text
                   FROM demand_analyses a JOIN comments c
                     ON c.platform=a.platform AND c.comment_id=a.comment_id"""
            ).fetchall()
        }
    output = []
    for row in rows:
        ref = f"{row['platform']}::{row['comment_id']}"
        analysis = analyses.get(ref)
        category = analysis["category"] if analysis else classify(row["comment_text"])
        if not category:
            continue
        output.append({
            "source_ref": ref, "project_id": project_id, "platform": row["platform"],
            "comment_id": row["comment_id"], "content_id": row.get("content_id"),
            "content_url": row.get("content_url"), "content_title": row.get("content_title"),
            "text": row["comment_text"],
            "need_summary": analysis.get("need_summary") if analysis else row["comment_text"],
            "category": category,
        })
    return output


def _rule_candidates(source: dict) -> list[dict]:
    text = source["text"].strip()
    value = _PUNCT.sub(" ", text)
    value = _SPACE.sub(" ", value).strip()
    for prefix in ("有没有", "求推荐", "请问", "想找", "求一个", "有没有人知道"):
        if value.startswith(prefix):
            value = value[len(prefix):].strip()
    for suffix in ("怎么办", "怎么解决", "如何解决", "怎么做", "怎么弄", "吗", "呢", "啊"):
        if value.endswith(suffix):
            value = value[:-len(suffix)].strip()
    candidates = []
    if 2 <= len(value) <= 32 and value not in _STOP:
        candidates.append({"keyword": value, "category": "问题表达",
                           "reason": "从已采集需求表达中去除疑问套语，保留核心问题短语",
                           "original_expression": text})
    for match in re.findall(r"[\\u4e00-\\u9fffA-Za-z0-9+#.]{2,16}(?:工具|软件|插件|自动化|批量处理|替代方案)", text):
        candidates.append({"keyword": match, "category": "场景词",
                           "reason": "识别到具体工具、软件或处理场景表达",
                           "original_expression": text})
    return candidates[:5]


def generate_candidates(project_id: str = "default", limit: int = 300) -> dict:
    sources = _source_rows(project_id, limit)
    if not sources:
        return {"project_id": project_id, "sources_seen": 0, "created": 0, "duplicates": 0,
                "generation_method": "none", "items": []}
    provider = provider_status()
    method = "llm" if provider["configured"] else "rule"
    generated = []
    if method == "llm":
        try:
            for start in range(0, len(sources), 20):
                generated.extend(expand_keyword_batch(sources[start:start + 20]))
        except LLMUnavailable:
            method = "rule_fallback"
    if method != "llm":
        generated = [{"source_ref": row["source_ref"], "candidates": _rule_candidates(row)}
                     for row in sources]
    source_map = {row["source_ref"]: row for row in sources}
    blocked = _blacklist()
    now = datetime.now(UTC).isoformat()
    created, duplicates, items = 0, 0, []
    with connect() as conn:
        for group in generated:
            source = source_map.get(group.get("source_ref"))
            if not source:
                continue
            for candidate in group.get("candidates", [])[:5]:
                keyword = str(candidate.get("keyword", "")).strip()[:80]
                normalized = normalize_keyword(keyword)
                if len(normalized) < 2 or normalized in blocked:
                    continue
                category = _CATEGORY_MAP.get(candidate.get("category"), candidate.get("category", "场景表达"))
                reason = str(candidate.get("reason") or "从来源表达中提取")[:500]
                original = str(candidate.get("original_expression") or source["text"])[:2000]
                cur = conn.execute(
                    """INSERT OR IGNORE INTO keyword_candidates
                       (project_id,keyword,normalized_keyword,category,source_type,source_platform,
                        source_comment_id,source_content_id,source_content_url,source_text,
                        generation_reason,generation_method,status,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (project_id, keyword, normalized, category, "comment", source["platform"],
                     source["comment_id"], source.get("content_id"), source.get("content_url"),
                     original, reason, method, "pending", now, now),
                )
                created += max(cur.rowcount, 0)
                duplicates += 1 if cur.rowcount == 0 else 0
                row = conn.execute(
                    "SELECT id,keyword,category,status FROM keyword_candidates WHERE project_id=? AND normalized_keyword=?",
                    (project_id, normalized),
                ).fetchone()
                if row:
                    items.append(dict(row))
    return {"project_id": project_id, "sources_seen": len(sources), "created": created,
            "duplicates": duplicates, "generation_method": method, "items": items[:100]}


def list_candidates(project_id: str = "default", status: str | None = None,
                    limit: int = 100, offset: int = 0) -> dict:
    clauses, params = ["project_id=?"], [project_id]
    if status:
        clauses.append("status=?")
        params.append(status)
    where = " AND ".join(clauses)
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM keyword_candidates WHERE {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM keyword_candidates WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            [*params, max(1, min(limit, 200)), max(0, offset)],
        ).fetchall()
    return {"total": total, "items": [dict(row) for row in rows], "limit": limit, "offset": offset}
