from fastapi.testclient import TestClient

from app.main import app


def test_discovery_strategy_run_import_and_approval(tmp_path, monkeypatch):
    from app.storage import db

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "discovery.sqlite3")
    client = TestClient(app)
    project = client.post("/api/v1/collection/projects", json={
        "name": "AI 办公需求", "description": "发现用户问题"
    }).json()

    strategy = client.post(f"/api/v1/discovery/projects/{project['id']}/strategies", json={
        "name": "AI 办公搜索", "topic": "AI 办公工具",
        "terms": ["AI PPT", "PPT 自动排版", "AI PPT"],
        "platforms": ["bilibili", "zhihu"]
    })
    assert strategy.status_code == 200
    assert strategy.json()["terms"] == ["AI PPT", "PPT 自动排版"]

    run = client.post(f"/api/v1/discovery/strategies/{strategy.json()['id']}/runs")
    assert run.status_code == 200
    assert run.json()["status"] == "prepared"
    assert run.json()["query_count"] == 2

    imported = client.post(f"/api/v1/discovery/runs/{run.json()['id']}/results", json={
        "items": [{
            "platform": "bilibili", "content_url": "https://example.com/video/1",
            "title": "AI PPT 怎么做", "author_name": "效率研究所",
            "author_url": "https://space.bilibili.com/123", "query_term": "AI PPT"
        }]
    })
    assert imported.status_code == 200
    assert imported.json()["inserted"] == 1

    results = client.get("/api/v1/discovery/results").json()
    assert results["total"] == 1
    content_id = results["items"][0]["id"]
    approved = client.post(f"/api/v1/discovery/results/{content_id}/approve")
    assert approved.status_code == 200
    assert approved.json()["approved"] is True

    sources = client.get("/api/v1/collection/projects").json()["items"][0]["sources"]
    assert sources[0]["mode"] == "creator"
    assert sources[0]["target"] == "https://space.bilibili.com/123"


def test_discovery_rejects_approval_without_author_url(tmp_path, monkeypatch):
    from app.storage import db

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "discovery-no-author.sqlite3")
    client = TestClient(app)
    project = client.post("/api/v1/collection/projects", json={"name": "测试"}).json()
    strategy = client.post(f"/api/v1/discovery/projects/{project['id']}/strategies", json={
        "name": "测试策略", "topic": "测试", "terms": ["测试词"]
    }).json()
    run = client.post(f"/api/v1/discovery/strategies/{strategy['id']}/runs").json()
    client.post(f"/api/v1/discovery/runs/{run['id']}/results", json={"items": [{
        "platform": "zhihu", "content_url": "https://example.com/q/1"
    }]})
    content = client.get("/api/v1/discovery/results").json()["items"][0]
    response = client.post(f"/api/v1/discovery/results/{content['id']}/approve")
    assert response.status_code == 422
