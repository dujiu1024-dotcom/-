from fastapi.testclient import TestClient

from app.main import app


def test_collection_library_crud_and_comment_pagination(tmp_path, monkeypatch):
    from app.storage import db

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "collection-test.sqlite3")
    client = TestClient(app)

    project = client.post("/api/v1/collection/projects", json={
        "name": "AI 工具需求", "description": "跟踪用户真实需求"
    })
    assert project.status_code == 200
    project_id = project.json()["id"]

    source = client.post(f"/api/v1/collection/projects/{project_id}/sources", json={
        "name": "AI 工具测评博主", "platform": "bilibili", "mode": "search",
        "target": "AI工具", "config": {"max_posts": 10}
    })
    assert source.status_code == 200
    assert source.json()["config"]["max_posts"] == 10

    listed = client.get("/api/v1/collection/projects").json()["items"]
    assert listed[0]["source_count"] == 1
    assert listed[0]["sources"][0]["name"] == "AI 工具测评博主"

    disabled = client.patch(f"/api/v1/collection/sources/{source.json()['id']}",
                            json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] == 0

    deleted = client.delete(f"/api/v1/collection/projects/{project_id}")
    assert deleted.status_code == 200
    assert client.get("/api/v1/collection/projects").json()["items"] == []
