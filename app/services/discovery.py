import json
import random
import re
import threading
import uuid
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

from app.collectors.mediacrawler_import import read_export
from app.config import DATA_ROOT
from app.services.crawler_engine import SUPPORTED, get_job, get_process_identity, start_job
from app.services.collection_errors import classify_collection_error
from app.storage.db import connect

_worker_lock = threading.Lock()
_worker_thread = None
_worker_stop = threading.Event()
_refresh_lock = threading.RLock()
_worker_id = uuid.uuid4().hex
_WORKER_LEASE_SECONDS = 60
_DEFAULT_MAX_ATTEMPTS = 4
_BACKOFF_BASE_SECONDS = 15
_BACKOFF_MAX_SECONDS = 300
_RETRY_AFTER_MAX_SECONDS = 21600


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
        CREATE TABLE IF NOT EXISTS discovery_run_tasks (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES discovery_runs(id) ON DELETE CASCADE,
            task_index INTEGER NOT NULL, platform TEXT NOT NULL, query_term TEXT NOT NULL,
            query_index INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
            job_id TEXT, pid INTEGER, started_at TEXT, finished_at TEXT,
            result_count INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
            UNIQUE(run_id, task_index)
        );
        CREATE INDEX IF NOT EXISTS idx_discovery_run_tasks_status ON discovery_run_tasks(status, run_id, task_index);
        CREATE TABLE IF NOT EXISTS discovery_worker_leases (
            lease_name TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
            lease_until REAL NOT NULL, updated_at TEXT NOT NULL
        );
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(discovery_runs)")}
        if "execution_json" not in columns:
            conn.execute("ALTER TABLE discovery_runs ADD COLUMN execution_json TEXT NOT NULL DEFAULT '{}'")
        task_columns = {row[1] for row in conn.execute("PRAGMA table_info(discovery_run_tasks)")}
        migrations = {
            "attempt_count": "INTEGER NOT NULL DEFAULT 0",
            "max_attempts": "INTEGER NOT NULL DEFAULT 4",
            "next_retry_at": "TEXT",
            "last_error_at": "TEXT",
            "error_code": "TEXT NOT NULL DEFAULT ''",
            "retryable": "INTEGER NOT NULL DEFAULT 0",
            "retry_after_seconds": "INTEGER",
            "log_path": "TEXT NOT NULL DEFAULT ''",
            "process_identity": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in migrations.items():
            if name not in task_columns:
                conn.execute(f"ALTER TABLE discovery_run_tasks ADD COLUMN {name} {definition}")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS discovery_task_attempts (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES discovery_run_tasks(id) ON DELETE CASCADE,
            attempt_number INTEGER NOT NULL, status TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT, job_id TEXT, pid INTEGER,
            exit_code INTEGER, error_code TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '', retryable INTEGER NOT NULL DEFAULT 0,
            retry_after_seconds INTEGER, UNIQUE(task_id, attempt_number)
        );
        CREATE INDEX IF NOT EXISTS idx_discovery_task_attempts_task
            ON discovery_task_attempts(task_id, attempt_number);
        CREATE TABLE IF NOT EXISTS discovery_platform_cooldowns (
            platform TEXT PRIMARY KEY, cooldown_until REAL NOT NULL,
            reason TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS discovery_import_batches (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES discovery_runs(id) ON DELETE CASCADE,
            task_id TEXT NOT NULL REFERENCES discovery_run_tasks(id) ON DELETE CASCADE,
            attempt_number INTEGER NOT NULL, platform TEXT NOT NULL,
            file_path TEXT NOT NULL, file_fingerprint TEXT NOT NULL,
            file_size INTEGER NOT NULL, modified_at TEXT NOT NULL,
            row_count INTEGER NOT NULL DEFAULT 0, imported_count INTEGER NOT NULL DEFAULT 0,
            skipped_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'processing',
            error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, completed_at TEXT,
            UNIQUE(platform,file_fingerprint)
        );
        CREATE INDEX IF NOT EXISTS idx_discovery_import_batches_task
            ON discovery_import_batches(task_id,attempt_number);
        """)
        task_columns = {row[1] for row in conn.execute("PRAGMA table_info(discovery_run_tasks)")}
        for name, definition in {
            "export_dir": "TEXT NOT NULL DEFAULT ''",
            "imported_file_count": "INTEGER NOT NULL DEFAULT 0",
            "imported_row_count": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in task_columns:
                conn.execute(f"ALTER TABLE discovery_run_tasks ADD COLUMN {name} {definition}")
        attempt_columns = {row[1] for row in conn.execute("PRAGMA table_info(discovery_task_attempts)")}
        if "export_dir" not in attempt_columns:
            conn.execute("ALTER TABLE discovery_task_attempts ADD COLUMN export_dir TEXT NOT NULL DEFAULT ''")
        if "process_identity" not in attempt_columns:
            conn.execute("ALTER TABLE discovery_task_attempts ADD COLUMN process_identity TEXT NOT NULL DEFAULT ''")


def _claim_worker_lease(owner_id, lease_seconds=_WORKER_LEASE_SECONDS):
    now = datetime.now(UTC).timestamp()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR IGNORE INTO discovery_worker_leases(lease_name,owner_id,lease_until,updated_at) VALUES('global','',0,?)", (_now(),))
        cur = conn.execute("UPDATE discovery_worker_leases SET owner_id=?, lease_until=?, updated_at=? WHERE lease_name='global' AND (owner_id=? OR lease_until<=?)", (owner_id, now + lease_seconds, _now(), owner_id, now))
        return cur.rowcount == 1


def _renew_worker_lease(owner_id, lease_seconds=_WORKER_LEASE_SECONDS):
    now = datetime.now(UTC).timestamp()
    with connect() as conn:
        cur = conn.execute("UPDATE discovery_worker_leases SET lease_until=?, updated_at=? WHERE lease_name='global' AND owner_id=? AND lease_until>?", (now + lease_seconds, _now(), owner_id, now))
        return cur.rowcount == 1


def _release_worker_lease(owner_id):
    with connect() as conn:
        conn.execute("UPDATE discovery_worker_leases SET owner_id='', lease_until=0, updated_at=? WHERE lease_name='global' AND owner_id=?", (_now(), owner_id))



def _parse_retry_after(value, now=None):
    """Parse Retry-After delta-seconds or HTTP-date; return a bounded delay."""
    if value is None or str(value).strip() == "":
        return None
    now = now or datetime.now(UTC)
    raw = str(value).strip()
    try:
        seconds = float(raw)
    except ValueError:
        try:
            target = parsedate_to_datetime(raw)
            if target.tzinfo is None:
                target = target.replace(tzinfo=UTC)
            seconds = (target.astimezone(UTC) - now).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0, min(_RETRY_AFTER_MAX_SECONDS, int(seconds + 0.999)))


def _retry_after_from_job(job):
    explicit = job.get("retry_after") or job.get("retry_after_seconds")
    if explicit is not None:
        return _parse_retry_after(explicit)
    message = str(job.get("error_message") or "")
    match = re.search(r"retry[-_ ]after\\s*[:=]\\s*([^\\s,;]+)", message, re.IGNORECASE)
    return _parse_retry_after(match.group(1)) if match else None


def _retry_delay(attempt_count, retry_after=None, jitter=None):
    exponent = max(0, int(attempt_count) - 1)
    base = min(_BACKOFF_MAX_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** exponent))
    spread = random.uniform(0, base * 0.2) if jitter is None else max(0, min(1, jitter)) * base * 0.2
    delay = int(base + spread + 0.999)
    if retry_after is not None:
        delay = max(delay, _parse_retry_after(retry_after) or 0)
    return min(_RETRY_AFTER_MAX_SECONDS, delay)


def _platform_cooldown_until(platform):
    _ensure()
    with connect() as conn:
        row = conn.execute(
            "SELECT cooldown_until FROM discovery_platform_cooldowns WHERE platform=?",
            (platform,),
        ).fetchone()
    return float(row["cooldown_until"]) if row else 0.0


def _set_platform_cooldown(platform, seconds, reason):
    seconds = max(0, min(_RETRY_AFTER_MAX_SECONDS, int(seconds)))
    if seconds <= 0:
        return
    until = datetime.now(UTC).timestamp() + seconds
    with connect() as conn:
        conn.execute("""
            INSERT INTO discovery_platform_cooldowns(platform,cooldown_until,reason,updated_at)
            VALUES(?,?,?,?)
            ON CONFLICT(platform) DO UPDATE SET
                cooldown_until=MAX(discovery_platform_cooldowns.cooldown_until,excluded.cooldown_until),
                reason=excluded.reason, updated_at=excluded.updated_at
        """, (platform, until, str(reason)[:300], _now()))


def _cooldown_remaining(platform, now=None):
    return max(0, int(_platform_cooldown_until(platform) - (now or datetime.now(UTC).timestamp()) + 0.999))


def _schedule_retry(run_id, execution, task_row, job, classification):
    attempt_count = int(task_row["attempt_count"] or 0)
    retry_after = _retry_after_from_job(job)
    delay = _retry_delay(attempt_count, retry_after)
    if classification["category"] == "rate_limited":
        _set_platform_cooldown(task_row["platform"], delay, classification["code"])
    retry_at = datetime.now(UTC) + timedelta(seconds=delay)
    message = classification["message"][:2000]
    with connect() as conn:
        conn.execute("""
            UPDATE discovery_run_tasks SET status='retry_wait', error=?, error_code=?,
                retryable=1, next_retry_at=?, last_error_at=?, retry_after_seconds=?
            WHERE id=? AND status='running'
        """, (message, classification["code"], retry_at.isoformat(), _now(),
              retry_after, task_row["id"]))
        conn.execute("""
            INSERT INTO discovery_task_attempts
                (id,task_id,attempt_number,status,started_at,finished_at,job_id,pid,
                 exit_code,error_code,error_message,retryable,retry_after_seconds)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(task_id,attempt_number) DO UPDATE SET
                status=excluded.status, finished_at=excluded.finished_at,
                job_id=excluded.job_id, pid=excluded.pid, exit_code=excluded.exit_code,
                error_code=excluded.error_code, error_message=excluded.error_message,
                retryable=excluded.retryable, retry_after_seconds=excluded.retry_after_seconds
        """, (uuid.uuid4().hex, task_row["id"], attempt_count, "retry_wait",
              task_row["started_at"] or _now(), _now(), job.get("id"), job.get("pid"),
              job.get("exit_code"), classification["code"], message, 1, retry_after))
        execution.pop("job_id", None)
        conn.execute("UPDATE discovery_runs SET status='retry_wait', execution_json=?, notes=? WHERE id=?",
                     (json.dumps(execution, ensure_ascii=False),
                      f"{classification['code']}：第 {attempt_count}/{task_row['max_attempts']} 次执行失败，{delay} 秒后重试。",
                      run_id))
    return {"id": run_id, "status": "retry_wait", "retry_at": retry_at.isoformat(),
            "retry_in_seconds": delay, "error_code": classification["code"]}


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


def _file_sha256(path):
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _import_discovery_exports(run_id, task_id, attempt_number, platform, query_term, export_dir):
    folder = Path(export_dir).resolve()
    export_root = (DATA_ROOT / "exports").resolve()
    if export_root not in folder.parents:
        raise ValueError("导入目录不在 exports 隔离目录内")
    if not folder.exists():
        return {"items": [], "file_count": 0, "imported_file_count": 0, "imported_row_count": 0}
    found, imported_files, imported_rows, file_count = [], 0, 0, 0
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".jsonl", ".ndjson", ".json", ".csv"}:
            continue
        file_count += 1
        stat = path.stat()
        fingerprint = _file_sha256(path)
        relative_path = str(path.relative_to(DATA_ROOT))
        modified_at = datetime.fromtimestamp(stat.st_mtime, UTC).isoformat()
        with connect() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO discovery_import_batches
                    (id,run_id,task_id,attempt_number,platform,file_path,file_fingerprint,
                     file_size,modified_at,status,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,'processing',?)
            """, (uuid.uuid4().hex, run_id, task_id, attempt_number, platform,
                  relative_path, fingerprint, stat.st_size, modified_at, _now()))
            batch = conn.execute(
                "SELECT * FROM discovery_import_batches WHERE platform=? AND file_fingerprint=?",
                (platform, fingerprint),
            ).fetchone()
            if batch and batch["status"] == "imported":
                continue
            batch_id = batch["id"]
            conn.execute("UPDATE discovery_import_batches SET status='processing',error='' WHERE id=?",
                         (batch_id,))
        file_items, row_count = [], 0
        try:
            for row in read_export(path):
                row_count += 1
                item = _content_from_row(row, platform, query_term)
                if item:
                    file_items.append(item)
            imported = ingest_discovered(run_id, file_items) if file_items else {"inserted": 0}
            with connect() as conn:
                conn.execute("""
                    UPDATE discovery_import_batches SET row_count=?,imported_count=?,
                        skipped_count=?,status='imported',error='',completed_at=?
                    WHERE id=?
                """, (row_count, imported["inserted"], max(0, row_count-len(file_items)),
                      _now(), batch_id))
            found.extend(file_items)
            imported_files += 1
            imported_rows += imported["inserted"]
        except (OSError, ValueError, TypeError, KeyError) as exc:
            with connect() as conn:
                conn.execute("""
                    UPDATE discovery_import_batches SET row_count=?,status='failed',
                        error=?,completed_at=? WHERE id=?
                """, (row_count, str(exc)[:1000], _now(), batch_id))
            raise
    return {"items": found, "file_count": file_count,
            "imported_file_count": imported_files, "imported_row_count": imported_rows}


def execute_discovery_run(run_id, platform, query_index=0):
    _ensure()
    if platform not in SUPPORTED:
        raise ValueError(f"{platform} 暂未接入原生搜索执行；首批支持：{', '.join(SUPPORTED)}")
    with connect() as conn:
        row = conn.execute("""SELECT r.*, s.terms_json, s.platforms_json
            FROM discovery_runs r JOIN search_strategies s ON s.id=r.strategy_id
            WHERE r.id=?""", (run_id,)).fetchone()
        if not row:
            return None
        run = dict(row)
        if run["status"] != "prepared":
            raise ValueError(f"该发现批次当前状态为 {run['status']}，不能重复提交")
        terms = json.loads(run["terms_json"])
        allowed_platforms = json.loads(run["platforms_json"])
        if allowed_platforms and platform not in allowed_platforms:
            raise ValueError(f"该搜索策略未启用平台：{platform}")
        if query_index < 0 or query_index >= len(terms):
            raise ValueError("搜索词序号超出范围")
        task = {"platform": platform, "query_term": terms[query_index],
                "query_index": query_index}
        execution = {"batch": True, "tasks": [task], "cursor": 0,
                     "completed": 0, "failed": 0, "created_at": _now()}
    with connect() as conn:
        cur = conn.execute(
            "UPDATE discovery_runs SET status='queued', execution_json=?, notes=? WHERE id=? AND status='prepared'",
            (json.dumps(execution, ensure_ascii=False),
             f"单平台任务已加入后台队列：{platform} · {task['query_term']}", run_id),
        )
        if cur.rowcount != 1:
            raise ValueError("该发现批次已被其他请求提交，请刷新批次状态")
        conn.execute("DELETE FROM discovery_run_tasks WHERE run_id=?", (run_id,))
        conn.execute("""INSERT INTO discovery_run_tasks
            (id,run_id,task_index,platform,query_term,query_index,status)
            VALUES(?,?,?,?,?,?,?)""",
            (uuid.uuid4().hex[:12], run_id, 0, platform,
             task["query_term"], query_index, "queued"))
    return {"run_id": run_id, "status": "queued", "task_count": 1,
            "current_task": task,
            "notes": "任务已持久化，由持有全局租约的后台 worker 串行领取。"}


def _start_batch_task(run_id, execution, lease_owner=None):
    if lease_owner and not _renew_worker_lease(lease_owner):
        raise RuntimeError("全局 Worker 租约失效，拒绝启动子进程")
    index = execution["cursor"]
    if index >= len(execution["tasks"]):
        return None
    task = execution["tasks"][index]
    with connect() as conn:
        task_row = conn.execute(
            "SELECT id,attempt_count FROM discovery_run_tasks WHERE run_id=? AND task_index=?",
            (run_id, index),
        ).fetchone()
    if not task_row:
        raise RuntimeError("子任务记录不存在，拒绝启动采集")
    attempt_number = int(task_row["attempt_count"] or 0) + 1
    export_dir = (DATA_ROOT / "exports" / "runs" / run_id / task_row["id"] /
                  f"attempt-{attempt_number:04d}").resolve()
    export_root = (DATA_ROOT / "exports").resolve()
    if export_root not in export_dir.parents:
        raise ValueError("计算得到的任务导出目录越界")
    export_dir.mkdir(parents=True, exist_ok=False)
    started_at = datetime.now(UTC)
    execution.pop("job_id", None)
    execution["starting_at"] = started_at.isoformat()
    execution["starting_owner"] = lease_owner or "manual-test"
    with connect() as conn:
        cur = conn.execute(
            "UPDATE discovery_runs SET status='starting', execution_json=?, notes=? WHERE id=? AND status IN ('queued','running')",
            (json.dumps(execution, ensure_ascii=False),
             f"正在领取任务 {index + 1}/{len(execution['tasks'])}：{task['platform']} · {task['query_term']}",
             run_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("批次状态已变化，未能领取当前任务")
        task_cur = conn.execute(
            """UPDATE discovery_run_tasks SET status='starting', job_id=NULL, pid=NULL,
               started_at=?, finished_at=NULL, error='', next_retry_at=NULL,
               attempt_count=attempt_count+1
               WHERE run_id=? AND task_index=? AND status IN ('queued','interrupted')""",
            (started_at.isoformat(), run_id, index),
        )
        if task_cur.rowcount != 1:
            conn.execute("UPDATE discovery_runs SET status='paused', notes=? WHERE id=?",
                         ("子任务状态异常，批次已暂停，请检查任务明细。", run_id))
            raise RuntimeError("子任务未处于可领取状态")
        relative_export_dir = str(export_dir.relative_to(DATA_ROOT))
        conn.execute("UPDATE discovery_run_tasks SET export_dir=? WHERE id=?",
                     (relative_export_dir, task_row["id"]))
        conn.execute("""
            INSERT INTO discovery_task_attempts
                (id,task_id,attempt_number,status,started_at,export_dir)
            VALUES(?,?,?,?,?,?)
        """, (uuid.uuid4().hex, task_row["id"], attempt_number, "starting",
              started_at.isoformat(), relative_export_dir))
    if lease_owner and not _renew_worker_lease(lease_owner):
        with connect() as conn:
            conn.execute("UPDATE discovery_run_tasks SET status='interrupted', finished_at=?, error=? WHERE run_id=? AND task_index=? AND status='starting'",
                         (_now(), "租约在启动前失效，未启动子进程。", run_id, index))
            conn.execute("UPDATE discovery_runs SET status='paused', notes=? WHERE id=?",
                         ("Worker 租约失效，未启动子进程。", run_id))
        raise RuntimeError("全局 Worker 租约失效，启动已取消")
    try:
        job = start_job({"platform": task["platform"], "mode": "search",
                         "keywords": task["query_term"], "max_posts": 30,
                         "max_comments_per_post": 0, "include_replies": False,
                         "export_dir": str(export_dir)})
    except (RuntimeError, OSError, ValueError) as exc:
        with connect() as conn:
            conn.execute("UPDATE discovery_run_tasks SET status='interrupted', finished_at=?, error=? WHERE run_id=? AND task_index=?",
                         (_now(), str(exc), run_id, index))
            conn.execute("UPDATE discovery_runs SET status='paused', notes=? WHERE id=?",
                         (f"启动失败，批次已暂停：{exc}", run_id))
        raise
    execution.update({
        "job_id": job["id"], "platform": task["platform"],
        "query_term": task["query_term"], "query_index": task["query_index"],
        "started_at": started_at.isoformat(), "started_epoch": started_at.timestamp(),
        "task_id": task_row["id"], "attempt_number": attempt_number,
        "export_dir": str(export_dir),
    })
    execution.pop("starting_at", None)
    execution.pop("starting_owner", None)
    if lease_owner and not _renew_worker_lease(lease_owner):
        notes = "启动期间全局租约失效；任务进程信息已保留，批次暂停，需确认进程状态后恢复。"
        with connect() as conn:
            conn.execute("UPDATE discovery_run_tasks SET status='interrupted', job_id=?, pid=?, process_identity=?, finished_at=?, error=? WHERE run_id=? AND task_index=?",
                         (job["id"], job.get("pid"), job.get("process_identity") or "", _now(), notes, run_id, index))
            conn.execute("UPDATE discovery_task_attempts SET job_id=?,pid=?,process_identity=? WHERE task_id=? AND attempt_number=?",
                         (job["id"], job.get("pid"), job.get("process_identity") or "", task_row["id"], attempt_number))
            conn.execute("UPDATE discovery_runs SET status='paused', execution_json=?, notes=? WHERE id=?",
                         (json.dumps(execution, ensure_ascii=False), notes, run_id))
        raise RuntimeError(notes)
    with connect() as conn:
        conn.execute("UPDATE discovery_runs SET status='running', execution_json=?, notes=? WHERE id=? AND status='starting'",
                     (json.dumps(execution, ensure_ascii=False),
                      f"批量执行 {index + 1}/{len(execution['tasks'])}：{task['platform']} · {task['query_term']}",
                      run_id))
        conn.execute("UPDATE discovery_run_tasks SET status='running', job_id=?, pid=?, process_identity=?, started_at=?, error='' WHERE run_id=? AND task_index=? AND status='starting'",
                     (job["id"], job.get("pid"), job.get("process_identity") or "", started_at.isoformat(), run_id, index))
        conn.execute("UPDATE discovery_task_attempts SET job_id=?,pid=?,process_identity=? WHERE task_id=? AND attempt_number=?",
                     (job["id"], job.get("pid"), job.get("process_identity") or "", task_row["id"], attempt_number))
    return job


def execute_discovery_batch(run_id, platforms=None, max_tasks=100):
    with connect() as conn:
        row = conn.execute("""SELECT r.*, s.terms_json, s.platforms_json
            FROM discovery_runs r JOIN search_strategies s ON s.id=r.strategy_id
            WHERE r.id=?""", (run_id,)).fetchone()
        if not row:
            return None
        run = dict(row)
        if run["status"] != "prepared":
            raise ValueError(f"该发现批次当前状态为 {run['status']}，不能重复提交；中断任务请使用恢复接口")
        terms = json.loads(run["terms_json"])
        allowed = json.loads(run["platforms_json"])
        selected = list(dict.fromkeys(platforms or allowed))
        if not selected:
            raise ValueError("请在搜索策略中配置平台，或在批量执行时指定平台")
        invalid = [p for p in selected if p not in SUPPORTED]
        if invalid:
            raise ValueError(f"暂不支持主动搜索的平台：{', '.join(invalid)}")
        if allowed and any(p not in allowed for p in selected):
            raise ValueError("批量执行平台超出搜索策略配置范围")
        tasks = [{"platform": p, "query_term": term, "query_index": i}
                 for p in selected for i, term in enumerate(terms)]
        if len(tasks) > max_tasks:
            raise ValueError(f"本批次最多 {max_tasks} 个任务，当前生成 {len(tasks)} 个；请拆分策略")
        execution = {"batch": True, "tasks": tasks, "cursor": 0, "completed": 0, "failed": 0,
                 "created_at": _now()}
    with connect() as conn:
        cur = conn.execute("UPDATE discovery_runs SET status='queued', execution_json=?, notes=? WHERE id=? AND status='prepared'",
                           (json.dumps(execution, ensure_ascii=False), "批量任务已持久化，等待全局租约持有者调度。", run_id))
        if cur.rowcount != 1:
            raise ValueError("该发现批次已被其他请求提交，请刷新批次状态")
        conn.execute("DELETE FROM discovery_run_tasks WHERE run_id=?", (run_id,))
        for index, task in enumerate(tasks):
            conn.execute("""INSERT INTO discovery_run_tasks
                (id,run_id,task_index,platform,query_term,query_index,status)
                VALUES(?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex[:12], run_id, index, task["platform"],
                 task["query_term"], task["query_index"], "queued"))
    return {"run_id": run_id, "status": "queued", "task_count": len(tasks),
            "platform_count": len(selected), "current_task": tasks[0],
            "notes": "任务已持久化，由持有全局租约的后台 worker 串行领取。"}


def _refresh_discovery_run_locked(run_id, lease_owner=None):
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
        notes = "任务进程记录在服务重启后不可恢复，批次已暂停；确认旧进程已退出后可手动恢复当前子任务。"
        if execution.get("batch"):
            with connect() as conn:
                conn.execute("UPDATE discovery_run_tasks SET status='interrupted', error=?, finished_at=? WHERE run_id=? AND task_index=? AND status='running'",
                             (notes, _now(), run_id, execution.get("cursor", 0)))
                conn.execute("UPDATE discovery_runs SET status='paused', notes=? WHERE id=?", (notes, run_id))
            return {"id": run_id, "status": "paused", "completed": execution.get("completed", 0),
                    "failed": execution.get("failed", 0), "task_count": len(execution.get("tasks", [])), "notes": notes}
        with connect() as conn:
            conn.execute("UPDATE discovery_runs SET status='failed', finished_at=?, notes=? WHERE id=?",
                         (_now(), notes, run_id))
        return {"id": run_id, "status": "failed", "notes": notes}
    if job["status"] == "running":
        return {"id": run_id, "status": "running", "job": job,
                "notes": f"平台搜索执行中：{execution.get('platform')} · {execution.get('query_term')}"}
    if job["status"] != "succeeded" and execution.get("batch"):
        with connect() as conn:
            task_row = conn.execute(
                "SELECT * FROM discovery_run_tasks WHERE run_id=? AND task_index=?",
                (run_id, execution.get("cursor", 0)),
            ).fetchone()
        if task_row:
            fallback = classify_collection_error(
                job.get("error_message") or f"采集任务退出码：{job.get('exit_code', 'unknown')}"
            )
            classification = {
                **fallback,
                "code": job.get("error_code") or fallback["code"],
                "category": job.get("error_category") or fallback["category"],
                "message": job.get("error_message") or fallback["message"],
                "retryable": bool(job.get("retryable", fallback["retryable"])),
                "manual_intervention": bool(job.get(
                    "manual_intervention", fallback["manual_intervention"]
                )),
            }
            if classification["retryable"] and int(task_row["attempt_count"] or 0) < int(task_row["max_attempts"] or _DEFAULT_MAX_ATTEMPTS):
                return _schedule_retry(run_id, execution, task_row, job, classification)
            final_status = "blocked" if classification["manual_intervention"] else "failed"
            with connect() as conn:
                conn.execute("""
                    UPDATE discovery_run_tasks SET status=?, finished_at=?, error=?, error_code=?,
                        retryable=?, last_error_at=?, next_retry_at=NULL
                    WHERE run_id=? AND task_index=? AND status='running'
                """, (final_status, _now(), classification["message"][:2000],
                      classification["code"], int(classification["retryable"]), _now(),
                      run_id, execution.get("cursor", 0)))
                conn.execute("""
                    UPDATE discovery_task_attempts SET status=?, finished_at=?, job_id=?, pid=?,
                        exit_code=?, error_code=?, error_message=?, retryable=?
                    WHERE task_id=? AND attempt_number=?
                """, (final_status, _now(), job.get("id"), job.get("pid"), job.get("exit_code"),
                      classification["code"], classification["message"][:2000],
                      int(classification["retryable"]), task_row["id"], task_row["attempt_count"]))
    imported_count = 0
    if job["status"] == "succeeded":
        import_result = _import_discovery_exports(
            run_id, execution["task_id"], execution["attempt_number"],
            execution["platform"], execution["query_term"], execution["export_dir"],
        )
        imported_count = import_result["imported_row_count"]
        with connect() as conn:
            conn.execute("""
                UPDATE discovery_run_tasks SET imported_file_count=?,imported_row_count=?
                WHERE run_id=? AND task_index=?
            """, (import_result["imported_file_count"], imported_count,
                  run_id, execution.get("cursor", 0)))
    if execution.get("batch"):
        with connect() as conn:
            conn.execute("""UPDATE discovery_run_tasks SET status=?, finished_at=?, result_count=?, error=?
                WHERE run_id=? AND task_index=? AND status='running'""",
                ("succeeded" if job["status"] == "succeeded" else "failed", _now(),
                 imported_count, "" if job["status"] == "succeeded" else f"采集任务退出码：{job.get('exit_code', 'unknown')}",
                 run_id, execution.get("cursor", 0)))
        if job["status"] == "succeeded":
            with connect() as conn:
                task_row = conn.execute(
                    "SELECT id,attempt_count FROM discovery_run_tasks WHERE run_id=? AND task_index=?",
                    (run_id, execution.get("cursor", 0)),
                ).fetchone()
                if task_row:
                    conn.execute("""
                        UPDATE discovery_task_attempts SET status='succeeded', finished_at=?,
                            job_id=?, pid=?, exit_code=?
                        WHERE task_id=? AND attempt_number=?
                    """, (_now(), job.get("id"), job.get("pid"), job.get("exit_code"),
                          task_row["id"], task_row["attempt_count"]))
            execution["completed"] += 1
        else:
            execution["failed"] += 1
        execution["cursor"] += 1
        execution.pop("job_id", None)
        if execution["cursor"] < len(execution["tasks"]):
            try:
                next_job = _start_batch_task(run_id, execution, lease_owner=lease_owner)
            except RuntimeError as exc:
                notes = f"批量任务暂停：{exc}"
                with connect() as conn:
                    conn.execute("UPDATE discovery_runs SET status='paused', execution_json=?, notes=? WHERE id=?",
                                 (json.dumps(execution, ensure_ascii=False), notes, run_id))
                return {"id": run_id, "status": "paused", "completed": execution["completed"],
                        "failed": execution["failed"], "task_count": len(execution["tasks"]), "notes": notes}
            return {"id": run_id, "status": "running", "completed": execution["completed"],
                    "failed": execution["failed"], "task_count": len(execution["tasks"]),
                    "current_task": execution["tasks"][execution["cursor"]],
                    "next_job_id": next_job["id"],
                    "notes": f"已完成 {execution['cursor']}/{len(execution['tasks'])} 个任务，发现内容新增 {imported_count} 条。"}
        final_status = "succeeded" if execution["failed"] == 0 else "partial"
        notes = (f"批量执行结束：成功 {execution['completed']}，失败 {execution['failed']}；"
                 f"累计发现 {list_discovered(run_id)['total']} 条。")
        with connect() as conn:
            conn.execute("UPDATE discovery_runs SET status=?, finished_at=?, execution_json=?, notes=? WHERE id=?",
                         (final_status, _now(), json.dumps(execution, ensure_ascii=False), notes, run_id))
        return {"id": run_id, "status": final_status, "completed": execution["completed"],
                "failed": execution["failed"], "task_count": len(execution["tasks"]),
                "result_count": list_discovered(run_id)["total"], "notes": notes}
    final_status = "succeeded" if job["status"] == "succeeded" else "failed"
    notes = (f"平台任务完成；发现内容入库 {imported_count} 条。" if final_status == "succeeded"
             else f"平台任务失败，退出码：{job.get('exit_code', 'unknown')}；请查看采集任务日志。")
    with connect() as conn:
        conn.execute("UPDATE discovery_runs SET status=?, finished_at=?, notes=? WHERE id=?",
                     (final_status, _now(), notes, run_id))
    return {"id": run_id, "status": final_status, "result_count": list_discovered(run_id)["total"],
            "notes": notes, "job": job}



def refresh_discovery_run(run_id, lease_owner=None):
    temporary_owner = None
    if lease_owner is None:
        temporary_owner = f"status-{uuid.uuid4().hex}"
        if not _claim_worker_lease(temporary_owner):
            with connect() as conn:
                row = conn.execute("SELECT id,status,result_count,notes FROM discovery_runs WHERE id=?", (run_id,)).fetchone()
            return dict(row) if row else None
        lease_owner = temporary_owner
    try:
        if not _renew_worker_lease(lease_owner):
            raise RuntimeError("Worker 租约已丢失，拒绝推进任务状态")
        with _refresh_lock:
            if not _renew_worker_lease(lease_owner):
                raise RuntimeError("Worker 租约已丢失，拒绝推进任务状态")
            return _refresh_discovery_run_locked(run_id, lease_owner=lease_owner)
    finally:
        if temporary_owner:
            _release_worker_lease(temporary_owner)


def resume_discovery_batch(run_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM discovery_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        if row["status"] != "paused":
            raise ValueError("只有已暂停的批次可以恢复")
        execution = json.loads(row["execution_json"] or "{}")
        if not execution.get("batch"):
            raise ValueError("该批次不是批量任务")
        cursor = execution.get("cursor", 0)
        if cursor >= len(execution.get("tasks", [])):
            raise ValueError("批次没有待恢复的子任务")
        # Clear the previous process-local job reference so the queued worker can start
        # a fresh job only after the user confirms the old process has exited.
        execution.pop("job_id", None)
        execution.pop("started_at", None)
        execution.pop("started_epoch", None)
        task = conn.execute("SELECT * FROM discovery_run_tasks WHERE run_id=? AND task_index=?",
                            (run_id, cursor)).fetchone()
        if task and task["status"] == "interrupted" and task["pid"]:
            current_identity = get_process_identity(task["pid"])
            expected_identity = task["process_identity"] or ""
            if current_identity == "unavailable":
                raise ValueError("无法安全恢复：系统无法读取 PID 启动身份；请检查 ps 权限和进程状态后重试")
            if current_identity is not None and not expected_identity:
                raise ValueError("无法安全恢复：历史任务缺少 PID 启动身份，当前 PID 仍存在；请先人工确认并停止旧进程")
            if current_identity is not None and current_identity == expected_identity:
                raise ValueError("无法恢复：原采集子进程仍在运行；请先停止该进程并重新确认")
            # Missing PID or a different identity proves the original process is gone.
            # Never signal a PID whose identity differs: it may belong to another process.
        if task and task["status"] == "interrupted":
            conn.execute("UPDATE discovery_run_tasks SET status='queued', job_id=NULL, pid=NULL, process_identity='', error='' WHERE id=?",
                         (task["id"],))
        conn.execute("UPDATE discovery_runs SET status='queued', execution_json=?, notes=? WHERE id=? AND status='paused'",
                     (json.dumps(execution, ensure_ascii=False),
                      "用户确认旧任务已停止，等待全局租约持有者恢复当前子任务。", run_id))
    return {"id": run_id, "status": "queued",
            "current_task": execution["tasks"][cursor],
            "notes": "已确认旧任务退出；当前子任务重新排队，由全局 worker 租约持有者执行。"}


def start_discovery_worker():
    global _worker_thread
    with _worker_lock:
        if _worker_thread and _worker_thread.is_alive():
            return False
        _worker_stop.clear()

        def loop():
            _ensure()
            recovered = False
            while not _worker_stop.is_set():
                try:
                    if not _claim_worker_lease(_worker_id):
                        _worker_stop.wait(2)
                        continue
                    if not recovered:
                        # A new process cannot inspect the previous process's in-memory job registry.
                        # Pause uncertain jobs rather than relaunching them automatically.
                        with connect() as conn:
                            rows = conn.execute("SELECT id,status,execution_json FROM discovery_runs WHERE status IN ('running','queued','starting')").fetchall()
                            for row in rows:
                                execution = json.loads(row["execution_json"] or "{}")
                                if execution.get("job_id") or row["status"] == "starting":
                                    conn.execute("UPDATE discovery_run_tasks SET status='interrupted', error=?, finished_at=? WHERE run_id=? AND status IN ('running','starting')",
                                                 ("服务进程切换，原采集任务状态不可确认；需人工确认后恢复。", _now(), row["id"]))
                                    conn.execute("UPDATE discovery_runs SET status='paused', notes=? WHERE id=?",
                                                 ("服务进程切换后批次暂停；确认旧采集进程已退出后可恢复。", row["id"]))
                        recovered = True
                    if not _renew_worker_lease(_worker_id):
                        _worker_stop.wait(1)
                        continue
                    now_iso = _now()
                    now_epoch = datetime.now(UTC).timestamp()
                    with connect() as conn:
                        due = conn.execute("""
                            SELECT r.id, t.platform FROM discovery_runs r
                            JOIN discovery_run_tasks t ON t.run_id=r.id
                              AND t.task_index=json_extract(r.execution_json,'$.cursor')
                            WHERE r.status='retry_wait' AND t.status='retry_wait'
                              AND t.next_retry_at IS NOT NULL AND t.next_retry_at<=?
                        """, (now_iso,)).fetchall()
                        for item in due:
                            cooldown = conn.execute(
                                "SELECT cooldown_until FROM discovery_platform_cooldowns WHERE platform=?",
                                (item["platform"],),
                            ).fetchone()
                            if not cooldown or float(cooldown["cooldown_until"])<=now_epoch:
                                conn.execute("UPDATE discovery_run_tasks SET status='queued' WHERE run_id=? AND status='retry_wait'",
                                             (item["id"],))
                                conn.execute("UPDATE discovery_runs SET status='queued', notes=? WHERE id=? AND status='retry_wait'",
                                             ("退避时间与平台冷却均已结束，重新进入队列。", item["id"]))
                        queued = conn.execute("SELECT id,execution_json FROM discovery_runs WHERE status='queued' ORDER BY started_at").fetchall()
                        rows = conn.execute("SELECT id FROM discovery_runs WHERE status='running' ORDER BY started_at").fetchall()
                    for queued_run in queued:
                        if _worker_stop.is_set() or not _renew_worker_lease(_worker_id):
                            break
                        try:
                            execution = json.loads(queued_run["execution_json"] or "{}")
                            if execution.get("batch") and not execution.get("job_id"):
                                current = execution.get("tasks", [])[execution.get("cursor", 0)]
                                if _cooldown_remaining(current["platform"]) > 0:
                                    continue
                                _start_batch_task(queued_run["id"], execution, lease_owner=_worker_id)
                        except (RuntimeError, OSError, ValueError) as exc:
                            with connect() as conn:
                                conn.execute("UPDATE discovery_runs SET status='paused', notes=? WHERE id=? AND status='queued'",
                                             (f"后台启动失败，批次已暂停：{exc}", queued_run["id"]))
                    for row in rows:
                        if _worker_stop.is_set() or not _renew_worker_lease(_worker_id):
                            break
                        try:
                            refresh_discovery_run(row["id"], lease_owner=_worker_id)
                        except (RuntimeError, OSError, ValueError):
                            continue
                except (RuntimeError, OSError, ValueError):
                    pass
                _worker_stop.wait(2)
            _release_worker_lease(_worker_id)

        _worker_thread = threading.Thread(target=loop, name="discovery-batch-worker", daemon=True)
        _worker_thread.start()
        return True


def stop_discovery_worker():
    _worker_stop.set()
    thread = _worker_thread
    if thread and thread.is_alive():
        thread.join(timeout=3)
