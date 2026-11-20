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
