from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.main import app


def _project_and_strategy(client):
    project = client.post("/api/v1/collection/projects", json={"name": "关键词验证项目"}).json()
    strategy = client.post(f"/api/v1/discovery/projects/{project['id']}/strategies", json={
        "name": "正式搜索策略", "topic": "效率工具", "terms": ["Excel 自动化"],
        "platforms": ["bilibili"]
    }).json()
    return project, strategy


def _insert_comment(monkeypatch, tmp_path):
    from app.models import CommentRecord
    from app.storage import db

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "keywords.sqlite3")
    db.upsert_comment(CommentRecord(
        platform="bilibili", comment_id="c-1",
        comment_text="有没有批量处理 Excel 的工具？",
        comment_created_at=datetime.now(UTC), raw={}
    ))


def test_keyword_expansion_is_traceable_and_deduplicated(tmp_path, monkeypatch):
    from app.services import keywords
    _insert_comment(monkeypatch, tmp_path)
    monkeypatch.setattr(keywords, "provider_status", lambda: {"configured": False})
    client = TestClient(app)
    project, _ = _project_and_strategy(client)

    first = client.post(f"/api/v1/keywords/expand?project_id={project['id']}")
    assert first.status_code == 200
    assert first.json()["created"] >= 1
    listed = client.get(f"/api/v1/keywords/candidates?project_id={project['id']}")
    assert listed.json()["total"] >= 1
    candidate = listed.json()["items"][0]
    assert candidate["status"] == "pending"
    assert candidate["source_platform"] == "bilibili"
    assert candidate["source_comment_id"] == "c-1"
    assert candidate["source_text"] == "有没有批量处理 Excel 的工具？"

    second = client.post(f"/api/v1/keywords/expand?project_id={project['id']}")
    assert second.status_code == 200
    assert second.json()["created"] == 0
    assert second.json()["duplicates"] >= 1


def test_keyword_review_and_inclusion_require_approval(tmp_path, monkeypatch):
    from app.services import keywords
    _insert_comment(monkeypatch, tmp_path)
    monkeypatch.setattr(keywords, "provider_status", lambda: {"configured": False})
    client = TestClient(app)
    project, strategy = _project_and_strategy(client)
    client.post(f"/api/v1/keywords/expand?project_id={project['id']}")
    candidate = client.get(f"/api/v1/keywords/candidates?project_id={project['id']}").json()["items"][0]
    cid = candidate["id"]

    blocked = client.post(f"/api/v1/keywords/candidates/{cid}/include",
                          json={"strategy_id": strategy["id"]})
    assert blocked.status_code == 409

    approved = client.post(f"/api/v1/keywords/candidates/{cid}/review",
                           json={"action": "approve", "note": "来源表达清晰"})
    assert approved.status_code == 200
    included = client.post(f"/api/v1/keywords/candidates/{cid}/include",
                           json={"strategy_id": strategy["id"]})
    assert included.status_code == 200
    assert included.json()["included"] is True

    from app.services.discovery import list_strategies
    updated = next(x for x in list_strategies(project["id"]) if x["id"] == strategy["id"])
    assert candidate["keyword"] in updated["terms"]
    detail = client.get(f"/api/v1/keywords/candidates/{cid}").json()
    assert [x["action"] for x in detail["reviews"]] == ["include", "approve"]


def test_keyword_merge_rejects_cross_project_target(tmp_path, monkeypatch):
    from app.services import keywords
    _insert_comment(monkeypatch, tmp_path)
    monkeypatch.setattr(keywords, "provider_status", lambda: {"configured": False})
    client = TestClient(app)
    p1, _ = _project_and_strategy(client)
    p2, _ = _project_and_strategy(client)
    client.post(f"/api/v1/keywords/expand?project_id={p1['id']}")
    candidate = client.get(f"/api/v1/keywords/candidates?project_id={p1['id']}").json()["items"][0]
    other = client.post("/api/v1/keywords/expand?project_id="+p2["id"])
    assert other.status_code == 200
    target = client.get(f"/api/v1/keywords/candidates?project_id={p2['id']}").json()["items"][0]
    response = client.post(f"/api/v1/keywords/candidates/{candidate['id']}/review",
                           json={"action": "merge", "target_candidate_id": target["id"]})
    assert response.status_code == 422


def test_keyword_quality_thresholds_validate_ranges(tmp_path, monkeypatch):
    from app.storage import db
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "thresholds.sqlite3")
    client = TestClient(app)
    saved = client.put("/api/v1/keywords/quality-thresholds?project_id=demo", json={
        "min_hits": 5, "min_valid_demands": 2,
        "max_duplicate_rate": 0.4, "max_irrelevant_rate": 0.5
    })
    assert saved.status_code == 200
    assert saved.json()["min_hits"] == 5
    invalid = client.put("/api/v1/keywords/quality-thresholds?project_id=demo", json={
        "max_duplicate_rate": 1.5
    })
    assert invalid.status_code == 422


def test_verification_batch_records_metrics_and_threshold_decision(tmp_path, monkeypatch):
    from app.storage import db
    from app.services import discovery, keywords

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "verify.sqlite3")
    discovery._ensure()
    from app.services.collection_library import create_project
    project_id = create_project("验证项目")["id"]
    now = datetime.now(UTC).isoformat()
    with db.connect() as conn:
        conn.execute("""INSERT INTO keyword_candidates
            (project_id,keyword,normalized_keyword,category,source_type,source_text,
             generation_reason,status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (project_id, "批量处理工具", "批量处理工具", "工具需求", "comment",
             "有没有批量处理工具", "来自评论", "pending", now, now))
        candidate_id = conn.execute("SELECT id FROM keyword_candidates").fetchone()[0]

    monkeypatch.setattr(discovery, "execute_discovery_batch", lambda *args, **kwargs: {"status": "queued"})
    batch = keywords.start_verification_batch(project_id, "bilibili", [candidate_id])
    assert batch["status"] == "queued"

    with db.connect() as conn:
        for idx, body in enumerate(["有没有批量处理工具", "只是路过"]):
            conn.execute("""INSERT INTO discovered_contents
                (id,run_id,platform,content_url,title,body,author_name,author_url,
                 published_at,query_term,fingerprint,status,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"content-{idx}", batch["run_id"], "bilibili", f"https://example.com/{idx}",
                 "通用标题", body, "", "", None, "批量处理工具",
                 f"fingerprint-{idx}", "new", now))
    monkeypatch.setattr(discovery, "refresh_discovery_run", lambda run_id: {"id": run_id, "status": "succeeded"})
    result = keywords.get_verification_batch(batch["batch_id"])
    assert result["status"] == "succeeded"
    assert result["results"][0]["hit_count"] == 2
    assert result["results"][0]["valid_demand_count"] == 1
    assert result["results"][0]["duplicate_rate"] == 0.5
    assert result["results"][0]["quality_pass"] is False


def test_verification_rejects_unsupported_platform(tmp_path, monkeypatch):
    from app.storage import db
    from app.services.keywords import start_verification_batch
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "verify-platform.sqlite3")
    with db.connect() as conn:
        conn.execute("""INSERT INTO keyword_candidates
            (project_id,keyword,normalized_keyword,category,source_type,source_text,
             generation_reason,status,created_at,updated_at)
            VALUES('p','测试词','测试词','场景表达','comment','测试来源','规则','pending','now','now')""")
        cid = conn.execute("SELECT id FROM keyword_candidates").fetchone()[0]
    try:
        start_verification_batch("p", "youtube", [cid])
    except ValueError as exc:
        assert "暂不支持" in str(exc)
    else:
        raise AssertionError("unsupported platform should be rejected")



def test_rejected_keyword_updates_expansion_blacklist(tmp_path, monkeypatch):
    from app.services import keywords
    _insert_comment(monkeypatch, tmp_path)
    monkeypatch.setattr(keywords, "provider_status", lambda: {"configured": False})
    client = TestClient(app)
    project, _ = _project_and_strategy(client)
    client.post(f"/api/v1/keywords/expand?project_id={project['id']}")
    candidate = client.get(f"/api/v1/keywords/candidates?project_id={project['id']}").json()["items"][0]
    rejected = client.post(f"/api/v1/keywords/candidates/{candidate['id']}/review",
                           json={"action": "reject", "note": "噪声词"})
    assert rejected.status_code == 200
    from app.storage.db import get_setting
    import json
    saved = json.loads(get_setting("keyword_blacklist", "[]"))
    assert candidate["normalized_keyword"] in saved
