import json
import re
from datetime import UTC, datetime

from app.services.demand import classify
from app.services.llm import LLMUnavailable, expand_keyword_batch, provider_status
from app.storage.db import connect, get_setting, list_comments, set_setting

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
            """SELECT a.platform,a.comment_id,a.category,a.need_summary,c.content_id,c.content_url,c.comment_text
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
                "text": row["comment_text"], "category": category})
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
                 "original_expression": source["text"]}]
    return []


def generate_candidates(project_id: str = "default", limit: int = 300) -> dict:
    sources = _sources(project_id, limit)
    if not sources:
        return {"project_id": project_id, "sources_seen": 0, "created": 0, "duplicates": 0,
                "generation_method": "none", "items": []}
    method, groups = ("llm" if provider_status()["configured"] else "rule"), []
    if method == "llm":
        try:
            for offset in range(0, len(sources), 20):
                groups.extend(expand_keyword_batch(sources[offset:offset + 20]))
        except LLMUnavailable:
            method = "rule_fallback"
    if method != "llm":
        groups = [{"source_ref": s["source_ref"], "candidates": _rule_candidates(s)} for s in sources]
    source_map = {s["source_ref"]: s for s in sources}
    try:
        blacklist = {normalize_keyword(x) for x in json.loads(get_setting("keyword_blacklist", "[]") or "[]")}
    except (ValueError, TypeError):
        blacklist = set()
    created = duplicates = 0
    now = datetime.now(UTC).isoformat()
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
                cur = conn.execute(
                    """INSERT OR IGNORE INTO keyword_candidates
                    (project_id,keyword,normalized_keyword,category,source_type,source_platform,
                     source_comment_id,source_content_id,source_content_url,source_text,
                     generation_reason,generation_method,status,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (project_id, keyword, normalized, CATEGORIES.get(item.get("category"),
                     item.get("category", "场景表达")), "comment", source["platform"],
                     source["comment_id"], source.get("content_id"), source.get("content_url"),
                     str(item.get("original_expression") or source["text"])[:2000],
                     str(item.get("reason") or "从来源表达中提取")[:500], method, "pending", now, now))
                created += max(cur.rowcount, 0)
                duplicates += int(cur.rowcount == 0)
    return {"project_id": project_id, "sources_seen": len(sources), "created": created,
            "duplicates": duplicates, "generation_method": method}


def list_candidates(project_id: str = "default", status: str | None = None,
                    limit: int = 100, offset: int = 0) -> dict:
    clauses, params = ["project_id=?"], [project_id]
    if status:
        clauses.append("status=?")
        params.append(status)
    where = " AND ".join(clauses)
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM keyword_candidates WHERE {where}", params).fetchone()[0]
        rows = conn.execute(f"SELECT * FROM keyword_candidates WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                            [*params, max(1, min(limit, 200)), max(0, offset)]).fetchall()
    return {"total": total, "items": [dict(r) for r in rows], "limit": limit, "offset": offset}


def review_candidate(candidate_id: int, action: str, note: str = "",
                    reviewer: str = "local-user", target_candidate_id: int | None = None) -> dict | None:
    allowed = {"approve": "approved", "reject": "rejected", "defer": "deferred",
               "disable": "disabled", "reactivate": "pending"}
    if action not in {*allowed, "merge"}:
        raise ValueError("不支持的审核动作")
    now = datetime.now(UTC).isoformat()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM keyword_candidates WHERE id=?", (candidate_id,)).fetchone()
        if not row:
            return None
        if action == "merge":
            target = conn.execute("SELECT * FROM keyword_candidates WHERE id=?",
                                  (target_candidate_id,)).fetchone()
            if not target or target["id"] == candidate_id or target["project_id"] != row["project_id"]:
                raise ValueError("合并目标不存在或不属于同一项目")
            if target["status"] in {"rejected", "disabled", "merged"}:
                raise ValueError("合并目标状态不可用")
            status = "merged"
        else:
            status = allowed[action]
        conn.execute("UPDATE keyword_candidates SET status=?,merged_into_id=?,updated_at=? WHERE id=?",
                     (status, target_candidate_id if action == "merge" else None, now, candidate_id))
        conn.execute("""INSERT INTO keyword_reviews
            (candidate_id,action,reviewer,note,target_candidate_id,created_at) VALUES(?,?,?,?,?,?)""",
            (candidate_id, action, reviewer[:100], note[:1000], target_candidate_id, now))
        if action == "reject":
            try:
                blacklist = json.loads(get_setting("keyword_blacklist", "[]") or "[]")
            except (TypeError, ValueError):
                blacklist = []
            blacklist = sorted(set(blacklist) | {row["normalized_keyword"]})
            set_setting("keyword_blacklist", json.dumps(blacklist, ensure_ascii=False))
        result = conn.execute("SELECT * FROM keyword_candidates WHERE id=?", (candidate_id,)).fetchone()
    return dict(result)


def include_candidate(candidate_id: int, strategy_id: str, reviewer: str = "local-user") -> dict:
    now = datetime.now(UTC).isoformat()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        candidate = conn.execute("SELECT * FROM keyword_candidates WHERE id=?", (candidate_id,)).fetchone()
        strategy = conn.execute("SELECT * FROM search_strategies WHERE id=?", (strategy_id,)).fetchone()
        if not candidate or not strategy:
            raise ValueError("候选词或搜索策略不存在")
        if candidate["status"] != "approved":
            raise ValueError("只有审核通过的候选词才能纳入正式策略")
        if candidate["project_id"] != strategy["project_id"]:
            raise ValueError("候选词与搜索策略不属于同一项目")
        terms = json.loads(strategy["terms_json"])
        if candidate["keyword"] not in terms:
            terms.append(candidate["keyword"])
            conn.execute("UPDATE search_strategies SET terms_json=?,updated_at=? WHERE id=?",
                         (json.dumps(terms, ensure_ascii=False), now, strategy_id))
        conn.execute("UPDATE keyword_candidates SET status='included',updated_at=? WHERE id=?",
                     (now, candidate_id))
        conn.execute("""INSERT INTO keyword_reviews
            (candidate_id,action,reviewer,note,created_at) VALUES(?,?,?,?,?)""",
            (candidate_id, "include", reviewer[:100], f"纳入策略 {strategy_id}", now))
    return {"candidate_id": candidate_id, "strategy_id": strategy_id, "keyword": candidate["keyword"],
            "included": True, "terms": terms}


def get_candidate(candidate_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM keyword_candidates WHERE id=?", (candidate_id,)).fetchone()
        if not row:
            return None
        reviews = conn.execute("SELECT * FROM keyword_reviews WHERE candidate_id=? ORDER BY created_at DESC",
                               (candidate_id,)).fetchall()
    return {**dict(row), "reviews": [dict(item) for item in reviews]}


def start_verification_batch(project_id: str, platform: str, candidate_ids: list[int],
                             requested_limit: int = 20) -> dict:
    from app.services.crawler_engine import SUPPORTED
    from app.services.discovery import create_discovery_run, create_strategy, execute_discovery_batch

    if platform not in SUPPORTED:
        raise ValueError("该平台暂不支持真实关键词试搜")
    ids = list(dict.fromkeys(candidate_ids))[:100]
    if not ids:
        raise ValueError("至少选择一个候选词")
    with connect() as conn:
        marks = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT * FROM keyword_candidates WHERE project_id=? AND id IN ({marks})",
            [project_id, *ids]).fetchall()
    if len(rows) != len(ids):
        raise ValueError("候选词不存在或不属于当前项目")
    terms = [row["keyword"] for row in rows]
    strategy = create_strategy(project_id, {"name": f"候选词验证-{datetime.now(UTC).strftime('%H%M%S')}",
        "topic": "AI 搜索词质量验证", "terms": terms, "platforms": [platform], "enabled": True})
    if not strategy:
        raise ValueError("研究项目不存在；请使用采集库中的项目 ID")
    run = create_discovery_run(strategy["id"])
    now = datetime.now(UTC).isoformat()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO keyword_verification_batches(project_id,platform,status,requested_limit,created_at) VALUES(?,?, 'queued', ?, ?)",
            (project_id, platform, max(1, min(requested_limit, 100)), now))
        batch_id = cur.lastrowid
        conn.execute("CREATE TABLE IF NOT EXISTS keyword_verification_run_links (batch_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL)")
        conn.execute("INSERT INTO keyword_verification_run_links(batch_id,run_id) VALUES(?,?)",
                     (batch_id, run["id"]))
        conn.execute("CREATE TABLE IF NOT EXISTS keyword_verification_batch_candidates (batch_id INTEGER NOT NULL, candidate_id INTEGER NOT NULL, PRIMARY KEY(batch_id,candidate_id))")
        conn.executemany("INSERT INTO keyword_verification_batch_candidates(batch_id,candidate_id) VALUES(?,?)",
                         [(batch_id, row["id"]) for row in rows])
    try:
        execute_discovery_batch(run["id"], [platform], max_tasks=100, max_posts=requested_limit)
    except (ValueError, RuntimeError) as exc:
        with connect() as conn:
            conn.execute("UPDATE keyword_verification_batches SET status='failed',finished_at=?,error=? WHERE id=?",
                         (datetime.now(UTC).isoformat(), str(exc)[:1000], batch_id))
        raise
    return {"batch_id": batch_id, "run_id": run["id"], "project_id": project_id,
            "platform": platform, "status": "queued", "candidate_count": len(rows),
            "requested_limit": requested_limit}


def refresh_verification_batch(batch_id: int) -> dict | None:
    from app.services.discovery import refresh_discovery_run

    with connect() as conn:
        batch = conn.execute("SELECT * FROM keyword_verification_batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            return None
        link = conn.execute("SELECT run_id FROM keyword_verification_run_links WHERE batch_id=?",
                            (batch_id,)).fetchone()
    if not link:
        return dict(batch)
    run = refresh_discovery_run(link["run_id"])
    status = run.get("status", "failed")
    if status in {"succeeded", "partial", "failed", "paused"}:
        now = datetime.now(UTC).isoformat()
        if status in {"succeeded", "partial"}:
            with connect() as conn:
                candidates = conn.execute(
                    """SELECT c.* FROM keyword_candidates c
                       JOIN keyword_verification_batch_candidates b ON b.candidate_id=c.id
                       WHERE b.batch_id=?""", (batch_id,)).fetchall()
            with connect() as conn:
                discovered = conn.execute("SELECT * FROM discovered_contents WHERE run_id=?",
                                           (link["run_id"],)).fetchall()
                for candidate in candidates:
                    hits = [dict(item) for item in discovered
                            if normalize_keyword(item["query_term"]) == candidate["normalized_keyword"]]
                    valid = [item for item in hits if classify((item.get("title", "") + " " + item.get("body", "")).strip())]
                    title_keys = [normalize_keyword(item.get("title", "")) for item in hits]
                    duplicate_rate = (len(title_keys) - len(set(title_keys))) / len(title_keys) if title_keys else 0.0
                    irrelevant_rate = (len(hits) - len(valid)) / len(hits) if hits else 1.0
                    conn.execute(
                        """INSERT OR REPLACE INTO keyword_verification_results
                        (batch_id,candidate_id,hit_count,valid_demand_count,duplicate_rate,
                         irrelevant_rate,failure_reason,evidence_json,created_at)
                        VALUES(?,?,?,?,?,?,?,?,?)""",
                        (batch_id, candidate["id"], len(hits), len(valid), round(duplicate_rate, 4),
                         round(irrelevant_rate, 4), "未命中" if not hits else None,
                         json.dumps([{"title": x["title"], "url": x["content_url"],
                                      "body": x["body"][:500]} for x in hits[:20]], ensure_ascii=False), now))
            final_status = "succeeded"
        else:
            final_status = "failed"
        with connect() as conn:
            conn.execute("UPDATE keyword_verification_batches SET status=?,finished_at=?,error=? WHERE id=?",
                         (final_status, now, run.get("notes") if final_status == "failed" else None, batch_id))
    with connect() as conn:
        results = conn.execute(
            """SELECT r.*,c.keyword,c.category FROM keyword_verification_results r
               JOIN keyword_candidates c ON c.id=r.candidate_id WHERE r.batch_id=? ORDER BY c.keyword""",
            (batch_id,)).fetchall()
        latest = conn.execute("SELECT * FROM keyword_verification_batches WHERE id=?", (batch_id,)).fetchone()
    return {**dict(latest), "run": run, "results": [dict(r) for r in results]}


def get_verification_batch(batch_id: int) -> dict | None:
    result = refresh_verification_batch(batch_id)
    if not result:
        return None
    thresholds = get_quality_thresholds(result["project_id"])
    for item in result.get("results", []):
        item["quality_pass"] = (
            item["hit_count"] >= thresholds["min_hits"]
            and item["valid_demand_count"] >= thresholds["min_valid_demands"]
            and (item["duplicate_rate"] or 0) <= thresholds["max_duplicate_rate"]
            and (item["irrelevant_rate"] if item["irrelevant_rate"] is not None else 1)
                <= thresholds["max_irrelevant_rate"]
        )
    result["thresholds"] = thresholds
    return result


def update_quality_thresholds(project_id: str, values: dict) -> dict:
    now = datetime.now(UTC).isoformat()
    defaults = {"min_hits": 3, "min_valid_demands": 1,
                "max_duplicate_rate": 0.7, "max_irrelevant_rate": 0.6}
    defaults.update(values)
    if not 0 <= defaults["max_duplicate_rate"] <= 1 or not 0 <= defaults["max_irrelevant_rate"] <= 1:
        raise ValueError("重复率和无关率阈值必须在 0 到 1 之间")
    with connect() as conn:
        conn.execute("""INSERT INTO keyword_quality_thresholds
            (project_id,min_hits,min_valid_demands,max_duplicate_rate,max_irrelevant_rate,updated_at)
            VALUES(?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET
            min_hits=excluded.min_hits,min_valid_demands=excluded.min_valid_demands,
            max_duplicate_rate=excluded.max_duplicate_rate,max_irrelevant_rate=excluded.max_irrelevant_rate,
            updated_at=excluded.updated_at""",
            (project_id, defaults["min_hits"], defaults["min_valid_demands"],
             defaults["max_duplicate_rate"], defaults["max_irrelevant_rate"], now))
    return {"project_id": project_id, **defaults, "updated_at": now}


def get_quality_thresholds(project_id: str) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM keyword_quality_thresholds WHERE project_id=?",
                           (project_id,)).fetchone()
    return dict(row) if row else {"project_id": project_id, "min_hits": 3,
        "min_valid_demands": 1, "max_duplicate_rate": 0.7, "max_irrelevant_rate": 0.6}
