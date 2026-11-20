import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime

from app.services.demand import classify
from app.services.llm import analyze_batch, embed_texts, provider_status
from app.storage.db import connect, get_recent_hours, list_comments

TOKEN_RE = re.compile(r"[a-zA-Z0-9_+#.-]{2,}|[\u4e00-\u9fff]{2,}")


def _tokens(text: str) -> set[str]:
    words = set(TOKEN_RE.findall(text.lower()))
    compact = re.sub(r"\s+", "", text.lower())
    words.update(compact[i:i + 2] for i in range(max(0, len(compact) - 1))
                 if "\u4e00" <= compact[i] <= "\u9fff" and "\u4e00" <= compact[i + 1] <= "\u9fff")
    return words


def _cosine(a: set[str], b: set[str]) -> float:
    return len(a & b) / math.sqrt(len(a) * len(b)) if a and b else 0.0


def _clusters(items: list[dict], vectors: list[list[float]] | None) -> list[list[int]]:
    parent = list(range(len(items)))
    def root(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = root(a), root(b)
        if ra != rb:
            parent[rb] = ra
    token_sets = [_tokens(item["semantic_text"]) for item in items]
    norms = [math.sqrt(sum(v * v for v in vec)) for vec in vectors] if vectors else []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if items[i]["category"] != items[j]["category"]:
                continue
            if vectors:
                dot = sum(a * b for a, b in zip(vectors[i], vectors[j]))
                similarity = dot / (norms[i] * norms[j]) if norms[i] and norms[j] else 0
                threshold = 0.78
            else:
                similarity = _cosine(token_sets[i], token_sets[j])
                threshold = 0.34
            if similarity >= threshold:
                union(i, j)
    groups = defaultdict(list)
    for i in range(len(items)):
        groups[root(i)].append(i)
    return list(groups.values())


def _strength(rows: list[dict], confidence: float) -> float:
    count = len(rows)
    authors = len({r.get("author_key") for r in rows if r.get("author_key")})
    platforms = len({r["platform"] for r in rows})
    likes = sum(max(0, int(r.get("like_count") or 0)) for r in rows)
    latest = max((datetime.fromisoformat(r["comment_created_at"]) for r in rows
                  if r.get("comment_created_at")), default=datetime.now(UTC))
    age_hours = max(0.0, (datetime.now(UTC) - latest.astimezone(UTC)).total_seconds() / 3600)
    volume = min(35.0, 12 * math.log1p(count))
    diversity = min(20.0, platforms * 5 + authors * 0.5)
    engagement = min(15.0, math.log1p(likes) * 2.5)
    recency = max(0.0, 1 - age_hours / 168) * 15
    return round(min(100.0, volume + diversity + engagement + recency + confidence * 15), 2)


def run_analysis(since_hours: int | None = None, limit: int = 500) -> dict:
    hours = since_hours or get_recent_hours()
    comments = list_comments(since_hours=hours)[:max(1, min(limit, 3000))]
    run_at = datetime.now(UTC).isoformat()
    provider = provider_status()
    with connect() as conn:
        cur = conn.execute("INSERT INTO analysis_runs(started_at,provider,status) VALUES(?,?,?)",
                           (run_at, provider["semantic_mode"], "running"))
        run_id = cur.lastrowid
        conn.commit()
    analyses = []
    try:
        llm_results = []
        if provider["configured"]:
            for start in range(0, len(comments), 25):
                llm_results.extend(analyze_batch(comments[start:start + 25]))
        else:
            for row in comments:
                category = classify(row["comment_text"])
                llm_results.append({"id": f"{row['platform']}::{row['comment_id']}",
                    "is_demand": bool(category), "category": category or "其他",
                    "need_summary": row["comment_text"][:120], "intent": "规则初筛" if category else "非明确需求",
                    "confidence": 0.55 if category else 0.0, "semantic_text": row["comment_text"]})
        by_id = {item["id"]: item for item in llm_results}
        for row in comments:
            item = by_id.get(f"{row['platform']}::{row['comment_id']}")
            if item and item.get("is_demand"):
                analyses.append({**row, **item})
        vectors = embed_texts([item["semantic_text"] for item in analyses]) if provider["embedding_model"] else None
        groups = _clusters(analyses, vectors)
        clusters, saved = [], []
        for group in groups:
            members = [analyses[i] for i in group]
            label = Counter(m["need_summary"] for m in members).most_common(1)[0][0]
            category = Counter(m["category"] for m in members).most_common(1)[0][0]
            confidence = sum(float(m.get("confidence", 0.5)) for m in members) / len(members)
            score = _strength(members, confidence)
            signature = hashlib.sha1((category + "|" + "|".join(sorted(_tokens(label)))).encode()).hexdigest()[:12]
            cluster_id = f"d_{signature}"
            dates = [m["comment_created_at"] for m in members if m.get("comment_created_at")]
            evidence = [{"platform": m["platform"], "comment_id": m["comment_id"],
                "text": m["comment_text"], "url": m.get("content_url"),
                "comment_created_at": m.get("comment_created_at")} for m in members[:30]]
            clusters.append({"cluster_id": cluster_id, "label": label, "category": category,
                "member_count": len(members), "platform_count": len({m["platform"] for m in members}),
                "unique_authors": len({m.get("author_key") for m in members if m.get("author_key")}),
                "strength_score": score, "first_seen": min(dates) if dates else None,
                "last_seen": max(dates) if dates else None, "evidence": evidence})
            for m in members:
                saved.append((m["platform"], m["comment_id"], m["category"], m["need_summary"],
                    m.get("intent", ""), float(m.get("confidence", 0.5)), m["semantic_text"],
                    cluster_id, score, run_at, json.dumps(m, ensure_ascii=False, default=str)))
        with connect() as conn:
            conn.executemany("""INSERT INTO demand_analyses VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(platform,comment_id) DO UPDATE SET
                category=excluded.category,need_summary=excluded.need_summary,intent=excluded.intent,
                confidence=excluded.confidence,semantic_text=excluded.semantic_text,
                cluster_id=excluded.cluster_id,strength_score=excluded.strength_score,
                analyzed_at=excluded.analyzed_at,raw_analysis=excluded.raw_analysis""", saved)
            conn.execute("DELETE FROM demand_clusters")
            for c in clusters:
                conn.execute("INSERT INTO demand_clusters VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (c["cluster_id"], c["label"], c["category"], c["member_count"],
                     c["platform_count"], c["unique_authors"], c["strength_score"],
                     c["first_seen"], c["last_seen"], run_at, json.dumps(c["evidence"], ensure_ascii=False)))
            conn.execute("""UPDATE analysis_runs SET finished_at=?,comments_seen=?,demands_found=?,
                clusters_found=?,status='succeeded' WHERE id=?""",
                (datetime.now(UTC).isoformat(), len(comments), len(analyses), len(clusters), run_id))
        return {"run_id": run_id, "provider": provider, "comments_seen": len(comments),
                "demands_found": len(analyses), "clusters_found": len(clusters),
                "clusters": sorted(clusters, key=lambda c: c["strength_score"], reverse=True)}
    except Exception as exc:
        with connect() as conn:
            conn.execute("UPDATE analysis_runs SET finished_at=?,status='failed',error=? WHERE id=?",
                         (datetime.now(UTC).isoformat(), str(exc)[:1000], run_id))
        raise


def get_clusters(limit: int = 100) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM demand_clusters ORDER BY strength_score DESC LIMIT ?",
                            (limit,)).fetchall()
    return [{**dict(row), "evidence": json.loads(row["evidence_json"])} for row in rows]


def get_trends(since_hours: int = 720, bucket: str = "day") -> dict:
    clusters = get_clusters(500)
    start = datetime.now(UTC).timestamp() - since_hours * 3600
    with connect() as conn:
        records = conn.execute("""SELECT a.cluster_id,c.comment_created_at FROM demand_analyses a
            JOIN comments c ON c.platform=a.platform AND c.comment_id=a.comment_id""").fetchall()
    trend_map = defaultdict(Counter)
    for row in records:
        if not row["comment_created_at"]:
            continue
        try:
            dt = datetime.fromisoformat(row["comment_created_at"]).astimezone(UTC)
        except ValueError:
            continue
        if dt.timestamp() < start:
            continue
        key = dt.strftime("%Y-%m-%dT%H:00Z") if bucket == "hour" else dt.strftime("%Y-%m-%d")
        trend_map[row["cluster_id"]][key] += 1
    return {"since_hours": since_hours, "bucket": bucket,
            "series": [{"cluster_id": c["cluster_id"], "label": c["label"],
                "points": [{"time": t, "count": n} for t, n in sorted(trend_map[c["cluster_id"]].items())]}
                for c in clusters]}
