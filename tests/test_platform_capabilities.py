from fastapi.testclient import TestClient

from app.main import app


def test_capability_matrix_lists_seven_platforms_and_five_operations(tmp_path, monkeypatch):
    from app.storage import db

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "platform-capabilities.sqlite3")
    client = TestClient(app)
    response = client.get("/api/v1/platform-capabilities")
    assert response.status_code == 200
    payload = response.json()
    assert {item["code"] for item in payload["items"]} == {
        "xhs", "douyin", "kuaishou", "bilibili", "weibo", "tieba", "zhihu"
    }
    assert {item["code"] for item in payload["operations"]} == {
        "search_content", "fetch_content_detail", "fetch_comments",
        "fetch_replies", "creator_posts"
    }
    assert payload["live_test_performed"] is False
    for platform in payload["items"]:
        assert set(platform["operations"]) == {
            "search_content", "fetch_content_detail", "fetch_comments",
            "fetch_replies", "creator_posts"
        }
        assert all(value["verification_status"] == "not_tested"
                   for value in platform["operations"].values())


def test_platform_capability_adapter_support_is_not_claimed_as_verified(tmp_path, monkeypatch):
    from app.storage import db

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "platform-adapters.sqlite3")
    client = TestClient(app)
    detail = client.get("/api/v1/platform-capabilities/xhs")
    assert detail.status_code == 200
    operations = detail.json()["operations"]
    assert operations["search_content"]["adapter_available"] is True
    assert operations["search_content"]["adapter_status"] == "available"
    assert operations["search_content"]["verification_status"] == "not_tested"
    tieba = client.get("/api/v1/platform-capabilities/tieba").json()["operations"]
    assert tieba["creator_posts"]["adapter_available"] is False


def test_platform_verification_record_is_persisted_and_returned(tmp_path, monkeypatch):
    from app.storage import db

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "platform-records.sqlite3")
    client = TestClient(app)
    payload = {
        "operation": "search_content",
        "status": "passed",
        "evidence": "Automated contract fixture: mocked adapter response; not a live platform test.",
        "engine_version": "test-fixture",
        "environment": "CI",
        "notes": "Test-only record verifies persistence and rendering contract.",
    }
    created = client.post("/api/v1/platform-capabilities/xhs/verification", json=payload)
    assert created.status_code == 201
    assert created.json()["status"] == "passed"
    refreshed = client.get("/api/v1/platform-capabilities/xhs").json()
    assert refreshed["operations"]["search_content"]["verification_status"] == "passed"
    assert "mocked adapter" in refreshed["operations"]["search_content"]["evidence"]


def test_platform_verification_rejects_unknown_platform_and_operation(tmp_path, monkeypatch):
    from app.storage import db

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "platform-invalid.sqlite3")
    client = TestClient(app)
    payload = {"operation": "search_content", "status": "skipped", "evidence": "Not run"}
    assert client.post("/api/v1/platform-capabilities/youtube/verification", json=payload).status_code == 404
    payload["operation"] = "unknown_operation"
    assert client.post("/api/v1/platform-capabilities/xhs/verification", json=payload).status_code == 422


def test_platform_capability_page_is_served():
    client = TestClient(app)
    response = client.get("/platform-capabilities")
    assert response.status_code == 200
    assert "七平台能力矩阵" in response.text
    assert "记录验证" in response.text
