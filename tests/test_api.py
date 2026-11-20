from fastapi.testclient import TestClient

from app.main import app


def test_health_and_platforms():
    client = TestClient(app)
    assert client.get("/health").json()["status"] == "ok"
    platforms = client.get("/api/v1/platforms").json()
    codes = {item["code"] for item in platforms}
    assert {"xhs", "douyin", "kuaishou", "weibo", "bilibili", "toutiao", "wechat_mp"} <= codes

def test_dashboard_and_engine_status():
    client = TestClient(app)
    page = client.get("/")
    assert page.status_code == 200
    assert "需求情报工作台" in page.text
    assert "采集库" in page.text
    status = client.get("/api/v1/engine/status").json()
    assert "installed" in status
    sessions = client.get("/api/v1/sessions").json()
    assert len(sessions) == 7

def test_configurable_window_and_extended_platforms(tmp_path, monkeypatch):
    from app.storage import db
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "api-test.sqlite3")
    client = TestClient(app)
    settings = client.get("/api/v1/settings")
    assert settings.status_code == 200
    assert settings.json()["recent_hours"] == 72
    updated = client.put("/api/v1/settings/time-window", json={"hours": 48})
    assert updated.status_code == 200
    assert updated.json()["recent_hours"] == 48
    platforms = {item["code"] for item in client.get("/api/v1/platforms").json()}
    assert {"toutiao", "wechat_mp", "youtube", "reddit", "tiktok", "twitter", "instagram"} <= platforms
    assert client.get("/api/v1/analytics/trends?since_hours=168&bucket=day").status_code == 200
