import json
import uuid
from datetime import UTC, datetime

from app.storage.db import connect


def _now():
    return datetime.now(UTC).isoformat()


def list_projects():
    with connect() as conn:
        rows = conn.execute("""
            SELECT p.*, COUNT(s.id) AS source_count,
                   SUM(CASE WHEN s.enabled=1 THEN 1 ELSE 0 END) AS enabled_count
            FROM collection_projects p
            LEFT JOIN collection_sources s ON s.project_id=p.id
            GROUP BY p.id ORDER BY p.updated_at DESC
        """).fetchall()
        projects = [dict(row) for row in rows]
        for project in projects:
            sources = conn.execute(
                "SELECT * FROM collection_sources WHERE project_id=? ORDER BY created_at DESC",
                (project["id"],),
            ).fetchall()
            project["sources"] = [
                {**dict(source), "config": json.loads(source["config_json"] or "{}")}
                for source in sources
            ]
    return projects


def create_project(name, description=""):
    now, project_id = _now(), uuid.uuid4().hex[:12]
    with connect() as conn:
        conn.execute(
            "INSERT INTO collection_projects(id,name,description,created_at,updated_at) VALUES(?,?,?,?,?)",
            (project_id, name.strip(), description.strip(), now, now),
        )
    return get_project(project_id)


def get_project(project_id):
    return next((item for item in list_projects() if item["id"] == project_id), None)


def update_project(project_id, name=None, description=None):
    project = get_project(project_id)
    if not project:
        return None
    with connect() as conn:
        conn.execute(
            "UPDATE collection_projects SET name=?,description=?,updated_at=? WHERE id=?",
            (name.strip() if name is not None else project["name"],
             description.strip() if description is not None else project["description"],
             _now(), project_id),
        )
    return get_project(project_id)


def delete_project(project_id):
    with connect() as conn:
        cur = conn.execute("DELETE FROM collection_projects WHERE id=?", (project_id,))
        return cur.rowcount > 0


def create_source(project_id, payload):
    if not get_project(project_id):
        return None
    source_id, now = uuid.uuid4().hex[:12], _now()
    config = payload.get("config") or {}
    with connect() as conn:
        conn.execute("""
            INSERT INTO collection_sources
            (id,project_id,name,platform,mode,target,config_json,enabled,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
        """, (source_id, project_id, payload["name"].strip(), payload["platform"],
              payload.get("mode", "search"), payload["target"].strip(),
              json.dumps(config, ensure_ascii=False), int(payload.get("enabled", True)), now, now))
        conn.execute("UPDATE collection_projects SET updated_at=? WHERE id=?", (now, project_id))
    return get_source(source_id)


def get_source(source_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM collection_sources WHERE id=?", (source_id,)).fetchone()
    if not row:
        return None
    item = dict(row)
    item["config"] = json.loads(item.pop("config_json") or "{}")
    return item


def update_source(source_id, payload):
    source = get_source(source_id)
    if not source:
        return None
    values = {
        "name": payload.get("name", source["name"]),
        "platform": payload.get("platform", source["platform"]),
        "mode": payload.get("mode", source["mode"]),
        "target": payload.get("target", source["target"]),
        "config_json": json.dumps(payload.get("config", source["config"]), ensure_ascii=False),
        "enabled": int(payload.get("enabled", bool(source["enabled"]))),
        "updated_at": _now(),
    }
    with connect() as conn:
        conn.execute("""
            UPDATE collection_sources SET name=?,platform=?,mode=?,target=?,config_json=?,
            enabled=?,updated_at=? WHERE id=?
        """, (values["name"], values["platform"], values["mode"], values["target"],
              values["config_json"], values["enabled"], values["updated_at"], source_id))
        conn.execute("UPDATE collection_projects SET updated_at=? WHERE id=?",
                     (values["updated_at"], source["project_id"]))
    return get_source(source_id)


def delete_source(source_id):
    with connect() as conn:
        cur = conn.execute("DELETE FROM collection_sources WHERE id=?", (source_id,))
        return cur.rowcount > 0
