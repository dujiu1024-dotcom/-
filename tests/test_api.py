from fastapi.testclient import TestClient
from app.main import app


def test_health_and_platforms():
    client = TestClient(app)
    assert client.get("/health").json()["status"] == "ok"
    platforms = client.get("/api/v1/platforms").json()
    codes = {item["code"] for item in platforms}
    assert {"xhs", "douyin", "kuaishou", "weibo", "bilibili", "toutiao", "wechat"} <= codes
