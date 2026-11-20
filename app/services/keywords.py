import json
import re
from datetime import UTC, datetime

from app.services.demand import classify
from app.services.llm import LLMUnavailable, expand_keyword_batch, provider_status
from app.storage.db import connect, get_setting, list_comments

PUNCT = re.compile(r"[，。！？、；：,.!?;:（）()【】{}\"'“”‘’]+")
SPACE = re.compile(r"\s+")
SYNONYMS = {"批处理": "批量处理", "自动归档": "自动整理", "免费平替": "免费替代方案"}
CATEGORIES = {"问题表达": "问题求解", "场景词": "场景表达", "痛点词": "痛点表达",
              "求助词": "求助表达", "替代方案词": "替代需求", "付费意愿": "购买意向"}


def normalize_keyword(value: str) -> str:
    text = SPACE.sub(" ", PUNCT.sub(" ", str(value or "").strip().lower())).strip()
    for prefix in ("有没有", "求推荐", "请问", "想找", "求一个"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    for suffix in ("怎么办", "怎么解决", "如何解决", "吗", "呢", "啊"):
        if text.endswith(suffix) and len(text) > len(suffix) + 1:
            text = text[:-len(suffix)].strip()
    return SYNONYMS.get(text, text)


def _sources(project_id: str, limit: int) -> list[dict]:
    comments = list_comments(since_hours=None)[:max(1, min(limit, 1000))]
    with connect() as conn:
        analyses = {f"{r['platform']}::{r['comment_id']}": dict(r) for r in conn.execute(
            """SELECT a.platform,a.comment_id,a.category,a.need_summary,a.cluster_id,a.confidence,c.content_id,c.content_url,c.content_title,c.comment_text
               FROM demand_analyses a JOIN comments c
               ON c.platform=a.platform AND c.comment_id=a.comment_id""").fetchall()}
    output = []
    for row in comments:
        ref = f"{row['platform']}::{row['comment_id']}"
        analysis = analyses.get(ref)
        category = analysis["category"] if analysis else classify(row["comment_text"])
        if category:
            output.append({"source_ref": ref, "platform": row["platform"], "comment_id": row["comment_id"],
                "content_id": row.get("content_id"), "content_url": row.get("content_url"),
                "text": row["comment_text"], "title": row.get("content_title"), "author_key": row.get("author_key"),\n                "published_at": row.get("comment_created_at"), "demand_cluster_id": analysis.get("cluster_id") if analysis else None,\n                "confidence": analysis.get("confidence") if analysis else 0.45, "category": category})
    return output


def _rule_candidates(source: dict) -> list[dict]:
    value = SPACE.sub(" ", PUNCT.sub(" ", source["text"])).strip()
    for prefix in ("有没有", "求推荐", "请问", "想找", "求一个"):
        if value.startswith(prefix):
            value = value[len(prefix):].strip()
    for suffix in ("怎么办", "怎么解决", "如何解决", "怎么做", "怎么弄", "吗", "呢", "啊"):
        if value.endswith(suffix):
            value = value[:-len(suffix)].strip()
    if 2 <= len(value) <= 32 and value not in {"哈哈哈", "支持", "路过", "顶"}:
        return [{"keyword": value, "category": "问题表达",
                 "reason": "从已采集需求表达中去除疑问套语，保留核心短语",
                 "original_expression": source["text"], "confidence": 0.45,\n                 "suggested_platforms": [], "negative_terms": []}]
    return []


def generate_candidates(project_id: str = "default", limit: int = 300) -> dict:
    sources = _sources(project_id, limit)
    if not sources:
        return {"project_id": project_id, "sources_seen": 0, "created": 0, "duplicates": 0,
                "generation_method": "none", "items": []}
    provider = provider_status()
    method, groups = ("llm" if provider["configured"] else "rule"), []
    if method == "llm":
        try:
            for offset in range(0, len(sources), 20):
                groups.extend(expand_keyword_batch(sources[offset:offset + 20]))
        except LLMUnavailable:
            method = "rule_fallback"
    if method != "llm":
        groups = [{"source_ref": source["source_ref"], "candidates": _rule_candidates(source)}
                  for source in sources]
    source_map = {source["source_ref"]: source for source in sources}
    try:
        blacklist = {normalize_keyword(x) for x in json.loads(get_setting("keyword_blacklist", "[]") or "[]")}
    except (ValueError, TypeError):
        blacklist = set()
    created = duplicates = evidence_added = 0
    now, prompt_version = datetime.now(UTC).isoformat(), "keyword-expansion-v2"
    with connect() as conn:
        for group in groups:
            source = source_map.get(group.get("source_ref"))
            if not source:
                continue
            for item in group.get("candidates", [])[:5]:
                keyword = str(item.get("keyword", "")).strip()[:80]
                normalized = normalize_keyword(keyword)
                if len(normalized) < 2 or normalized in blacklist:
                    continue
                confidence = item.get("confidence", source.get("confidence", 0.5))
                try:
                    confidence = max(0.0, min(1.0, float(confidence)))
                except (TypeError, ValueError):
                    confidence = 0.5
                suggested = item.get("suggested_platforms", [])
                negative = item.get("negative_terms", [])
                if not isinstance(suggested, list):
                    suggested = []
                if not isinstance(negative, list):
                    negative = []
                cur = conn.execute(
                    """INSERT OR IGNORE INTO keyword_candidates
                    (project_id,keyword,normalized_keyword,category,source_type,source_platform,
                     source_comment_id,source_content_id,source_content_url,source_text,
                     generation_reason,generation_method,confidence,suggested_platforms_json,
                     negative_terms_json,generation_model,prompt_version,status,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (project_id, keyword, normalized, CATEGORIES.get(item.get("category"),
                     item.get("category", "场景表达")), "comment", source["platform"],
                     source["comment_id"], source.get("content_id"), source.get("content_url"),
                     str(item.get("original_expression") or source["text"])[:2000],
                     str(item.get("reason") or "从来源表达中提取")[:500], method, confidence,
                     json.dumps(suggested[:20], ensure_ascii=False),
                     json.dumps(negative[:20], ensure_ascii=False),
                     provider.get("model") if method == "llm" else "", prompt_version,
                     "pending", now, now))
                created += max(cur.rowcount, 0)
                duplicates += int(cur.rowcount == 0)
                candidate = conn.execute(
                    "SELECT id FROM keyword_candidates WHERE project_id=? AND normalized_keyword=?",
                    (project_id, normalized)).fetchone()
                if candidate:
                    evidence = conn.execute(
                        """INSERT OR IGNORE INTO keyword_candidate_evidence
                        (candidate_id,source_ref,source_type,source_platform,source_comment_id,
                         source_content_id,source_content_url,evidence_quote,demand_cluster_id,created_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (candidate["id"], source["source_ref"], "comment", source["platform"],
                         source["comment_id"], source.get("content_id"), source.get("content_url"),
                         str(item.get("original_expression") or source["text"])[:2000],
                         source.get("demand_cluster_id"), now))
                    evidence_added += max(evidence.rowcount, 0)
    return {"project_id": project_id, "sources_seen": len(sources), "created": created,
            "duplicates": duplicates, "evidence_added": evidence_added, "generation_method": method}
