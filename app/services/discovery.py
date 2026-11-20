import json
import uuid
from datetime import UTC, datetime

from app.storage.db import connect


def _now():
    return datetime.now(UTC).isoformat()


def _ensure():
    with connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS search_strategies (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES collection_projects(id) ON DELETE CASCADE,
            name TEXT NOT NULL, topic TEXT NOT NULL, terms_json TEXT NOT NULL,
            platforms_json TEXT NOT NULL, language TEXT NOT NULL DEFAULT 'zh',
            enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_search_strategies_project ON search_strategies(project_id);
        CREATE TABLE IF NOT EXISTS discovery_runs (
            id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL REFERENCES search_strategies(id) ON DELETE CASCADE,
            status TEXT NOT NULL, query_count INTEGER NOT NULL DEFAULT 0,
            result_count INTEGER NOT NULL DEFAULT 0, started_at TEXT NOT NULL,
            finished_at TEXT, notes TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS discovered_contents (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES discovery_runs(id) ON DELETE CASCADE,
            platform TEXT NOT NULL, content_url TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '', author_name TEXT NOT NULL DEFAULT '',
            author_url TEXT NOT NULL DEFAULT '', published_at TEXT, query_term TEXT NOT NULL DEFAULT '',
            fingerprint TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'new',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_discovered_contents_run ON discovered_contents(run_id);
        """)


def list_strategies(project_id=None):
    _ensure()
    sql = "SELECT * FROM search_strategies"
    params = []
    if project_id:
        sql += " WHERE project_id=?"
        params.append(project_id)
    sql += " ORDER BY updated_at DESC"
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [{**dict(r), "terms": json.loads(r["terms_json"]),
             "platforms": json.loads(r["platforms_json"])} for r in rows]


def create_strategy(project_id, payload):
    _ensure()
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM collection_projects WHERE id=?", (project_id,)).fetchone():
            return None
        now, strategy_id = _now(), uuid.uuid4().hex[:12]
        terms = list(dict.fromkeys(t.strip() for t in payload["terms"] if t.strip()))
        if not terms:
            raise ValueError("至少需要一个搜索词")
        conn.execute("""INSERT INTO search_strategies
            (id,project_id,name,topic,terms_json,platforms_json,language,enabled,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (strategy_id, project_id, payload["name"].strip(), payload["topic"].strip(),
             json.dumps(terms, ensure_ascii=False),
             json.dumps(payload.get("platforms", []), ensure_ascii=False),
             payload.get("language", "zh"), int(payload.get("enabled", True)), now, now))
    return next(x for x in list_strategies(project_id) if x["id"] == strategy_id)


def create_discovery_run(strategy_id):
    _ensure()
    strategies = [s for s in list_strategies() if s["id"] == strategy_id]
    if not strategies:
        return None
    strategy = strategies[0]
    run_id, now = uuid.uuid4().hex[:12], _now()
    with connect() as conn:
        conn.execute("""INSERT INTO discovery_runs
            (id,strategy_id,status,query_count,started_at,notes) VALUES(?,?,?,?,?,?)""",
            (run_id, strategy_id, "prepared", len(strategy["terms"]), now,
             "已生成搜索任务清单；平台搜索适配器尚未接入，不会伪报为已完成采集。"))
    return {"id": run_id, "strategy_id": strategy_id, "status": "prepared",
            "queries": strategy["terms"], "platforms": strategy["platforms"],
            "query_count": len(strategy["terms"]), "result_count": 0,
            "notes": "搜索任务已准备，等待对应平台搜索适配器执行或人工导入发现结果。"}


def list_discovered(run_id=None, status=None, limit=100, offset=0):
    _ensure()
    clauses, params = [], []
    if run_id:
        clauses.append("run_id=?")
        params.append(run_id)
    if status:
        clauses.append("status=?")
        params.append(status)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM discovered_contents" + where, params).fetchone()[0]
        rows = conn.execute("SELECT * FROM discovered_contents" + where +
                            " ORDER BY created_at DESC LIMIT ? OFFSET ?",
                            [*params, limit, offset]).fetchall()
    return {"total": total, "items": [dict(r) for r in rows],
            "limit": limit, "offset": offset}


def ingest_discovered(run_id, items):
    _ensure()
    now, inserted = _now(), 0
    with connect() as conn:
        run = conn.execute("SELECT id FROM discovery_runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            return None
        for item in items:
            url = item["content_url"].strip()
            fingerprint = item.get("fingerprint") or f'{item["platform"]}:{url}'
            cur = conn.execute("""INSERT OR IGNORE INTO discovered_contents
                (id,run_id,platform,content_url,title,body,author_name,author_url,
                 published_at,query_term,fingerprint,status,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex[:12], run_id, item["platform"], url,
                 item.get("title", ""), item.get("body", ""), item.get("author_name", ""),
                 item.get("author_url", ""), item.get("published_at"), item.get("query_term", ""),
                 fingerprint, "new", now))
            inserted += cur.rowcount
        conn.execute("UPDATE discovery_runs SET result_count=(SELECT COUNT(*) FROM discovered_contents WHERE run_id=?) WHERE id=?",
                     (run_id, run_id))
        count = conn.execute("SELECT result_count FROM discovery_runs WHERE id=?", (run_id,)).fetchone()[0]
    return {"inserted": inserted, "run_result_count": count}


def approve_discovered_source(content_id):
    _ensure()
    with connect() as conn:
        row = conn.execute("SELECT * FROM discovered_contents WHERE id=?", (content_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        if item["status"] == "approved":
            return {"approved": True, "already_approved": True, "content_id": content_id}
        if not item["author_url"]:
            raise ValueError("该发现结果没有作者主页链接，无法沉淀为可追踪来源")
        source_id, now = uuid.uuid4().hex[:12], _now()
        project_id = conn.execute("SELECT project_id FROM search_strategies WHERE id=(SELECT strategy_id FROM discovery_runs WHERE id=?)",
                                  (item["run_id"],)).fetchone()[0]
        conn.execute("""INSERT INTO collection_sources
            (id,project_id,name,platform,mode,target,config_json,enabled,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (source_id, project_id, item["author_name"] or item["author_url"],
             item["platform"], "creator", item["author_url"], "{}", 1, now, now))
        conn.execute("UPDATE discovered_contents SET status='approved' WHERE id=?", (content_id,))
        conn.execute("UPDATE collection_projects SET updated_at=? WHERE id=?", (now, project_id))
    return {"approved": True, "source_id": source_id, "content_id": content_id}
