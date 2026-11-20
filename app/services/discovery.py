import json
import uuid
from datetime import UTC, datetime

from app.collectors.mediacrawler_import import read_export
from app.config import DATA_ROOT
from app.services.crawler_engine import SUPPORTED, get_job, start_job
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
            finished_at TEXT, notes TEXT NOT NULL DEFAULT '', execution_json TEXT NOT NULL DEFAULT '{}'
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
        columns = {row[1] for row in conn.execute("PRAGMA table_info(discovery_runs)")}
        if "execution_json" not in columns:
            conn.execute("ALTER TABLE discovery_runs ADD COLUMN execution_json TEXT NOT NULL DEFAULT '{}'")


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


def _content_from_row(row, platform, query_term):
    if any(key in row for key in ("comment_id", "comment_text", "root_comment_id", "评论内容")):
        return None
    title = next((row.get(k) for k in ("title", "note_title", "desc", "description", "视频标题", "标题", "name") if row.get(k)), "")
    body = next((row.get(k) for k in ("content", "text", "note_desc", "description", "desc", "正文") if row.get(k)), "")
    url = next((row.get(k) for k in ("content_url", "url", "share_url", "链接") if row.get(k)), "")
    content_id = next((row.get(k) for k in ("note_id", "aweme_id", "video_id", "bvid", "object_id", "id") if row.get(k)), "")
    if not url and content_id:
        content_paths = {
            "xhs": f"https://www.xiaohongshu.com/explore/{content_id}",
            "douyin": f"https://www.douyin.com/video/{content_id}",
            "kuaishou": f"https://www.kuaishou.com/short-video/{content_id}",
            "bilibili": f"https://www.bilibili.com/video/{content_id}",
            "weibo": f"https://weibo.com/{content_id}",
            "tieba": f"https://tieba.baidu.com/p/{content_id}",
            "zhihu": f"https://www.zhihu.com/question/{content_id}",
        }
        url = content_paths.get(platform, str(content_id))
    author = row.get("nickname") or row.get("author_name") or row.get("user_name") or row.get("name") or ""
    author_url = row.get("author_url") or row.get("profile_url") or row.get("user_url") or ""
    author_id = row.get("user_id") or row.get("sec_uid") or row.get("mid") or row.get("uid")
    if not author_url and author_id:
        author_paths = {
            "xhs": f"https://www.xiaohongshu.com/user/profile/{author_id}",
            "douyin": f"https://www.douyin.com/user/{author_id}",
            "kuaishou": f"https://www.kuaishou.com/profile/{author_id}",
            "bilibili": f"https://space.bilibili.com/{author_id}",
            "weibo": f"https://weibo.com/u/{author_id}",
            "tieba": f"https://tieba.baidu.com/home/main?id={author_id}",
            "zhihu": f"https://www.zhihu.com/people/{author_id}",
        }
        author_url = author_paths.get(platform, str(author_id))
    if not (title or body) or not url:
        return None
    return {
        "platform": platform, "content_url": str(url), "title": str(title or "")[:500],
        "body": str(body or "")[:10000], "author_name": str(author or "")[:200],
        "author_url": str(author_url or ""), "query_term": query_term,
        "fingerprint": f"{platform}:{content_id or url}",
        "published_at": row.get("create_time") or row.get("publish_time") or row.get("发布时间"),
    }


def _import_discovery_exports(platform, query_term, started_epoch):
    folder = DATA_ROOT / "exports" / platform
    if not folder.exists():
        return []
    found = []
    for path in folder.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".jsonl", ".ndjson", ".json"}:
            continue
        try:
            if path.stat().st_mtime < started_epoch:
                continue
            for row in read_export(path):
                item = _content_from_row(row, platform, query_term)
                if item:
                    found.append(item)
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return found


def execute_discovery_run(run_id, platform, query_index=0):
    if platform not in SUPPORTED:
        raise ValueError(f"{platform} 暂未接入原生搜索执行；首批支持：{', '.join(SUPPORTED)}")
    with connect() as conn:
        row = conn.execute("""SELECT r.*, s.terms_json, s.platforms_json
            FROM discovery_runs r JOIN search_strategies s ON s.id=r.strategy_id
            WHERE r.id=?""", (run_id,)).fetchone()
        if not row:
            return None
        run = dict(row)
        if run["status"] == "running":
            raise ValueError("该发现批次已有平台任务运行中，请先刷新任务状态")
        terms = json.loads(run["terms_json"])
        allowed_platforms = json.loads(run["platforms_json"])
        if allowed_platforms and platform not in allowed_platforms:
            raise ValueError(f"该搜索策略未启用平台：{platform}")
        if query_index < 0 or query_index >= len(terms):
            raise ValueError("搜索词序号超出范围")
        term = terms[query_index]
        started_at = datetime.now(UTC)
        job = start_job({"platform": platform, "mode": "search", "keywords": term,
                         "max_posts": 30, "max_comments_per_post": 100,
                         "include_replies": False})
        execution = {"platform": platform, "query_term": term, "query_index": query_index,
                     "job_id": job["id"], "started_at": started_at.isoformat(),
                     "started_epoch": started_at.timestamp()}
        conn.execute("""UPDATE discovery_runs SET status='running', execution_json=?,
            notes=? WHERE id=?""",
            (json.dumps(execution, ensure_ascii=False),
             f"正在执行 {platform} 搜索：{term}", run_id))
    return {"run_id": run_id, "status": "running", **execution}


def refresh_discovery_run(run_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM discovery_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        run = dict(row)
        execution = json.loads(run.get("execution_json") or "{}")
    if run["status"] != "running" or not execution.get("job_id"):
        return {"id": run_id, "status": run["status"], "result_count": run["result_count"],
                "notes": run["notes"]}
    job = get_job(execution["job_id"])
    if not job:
        notes = "本机任务记录不存在，可能服务重启导致任务状态丢失；请重新创建发现批次。"
        with connect() as conn:
            conn.execute("UPDATE discovery_runs SET status='failed', finished_at=?, notes=? WHERE id=?",
                         (_now(), notes, run_id))
        return {"id": run_id, "status": "failed", "notes": notes}
    if job["status"] == "running":
        return {"id": run_id, "status": "running", "job": job, "notes": "平台搜索执行中"}
    if job["status"] == "succeeded":
        items = _import_discovery_exports(execution["platform"], execution["query_term"],
                                          execution["started_epoch"])
        imported = ingest_discovered(run_id, items) if items else {"inserted": 0}
        final_status = "succeeded"
        notes = f"平台任务完成；发现内容入库 {imported['inserted']} 条。"
    else:
        final_status = "failed"
        notes = f"平台任务失败，退出码：{job.get('exit_code', 'unknown')}；请查看采集任务日志。"
    with connect() as conn:
        conn.execute("UPDATE discovery_runs SET status=?, finished_at=?, notes=? WHERE id=?",
                     (final_status, _now(), notes, run_id))
    return {"id": run_id, "status": final_status, "result_count": list_discovered(run_id)["total"],
            "notes": notes, "job": job}
