from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.main import app
from app.storage import db


def _task(task_id: str, platform: str, status: str, result_count: int, error_code=None):
    started = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    db.start_collection_task(task_id, platform, "search", started)
    if status != "running":
        db.finish_collection_task(
            task_id,
            status=status,
            result_count=result_count,
            error_code=error_code,
        )


def test_collection_quality_breakdown_groups_platforms_and_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "quality-detail.sqlite3")
    _task("xhs-ok", "xhs", "succeeded", 12)
    _task("xhs-zero", "xhs", "succeeded", 0)
    _task("dy-blocked", "douyin", "blocked", 0, "CAPTCHA_REQUIRED")
    _task("wb-failed", "weibo", "failed", 0, "NETWORK_TIMEOUT")
    _task("tieba-running", "tieba", "running", 0)

    client = TestClient(app)
    response = client.get("/api/v1/quality/collection?since_hours=24")

    assert response.status_code == 200
    data = response.json()
    assert data["summary"]["task_total"] == 5
    assert data["summary"]["task_succeeded"] == 2
    assert data["summary"]["task_running"] == 1
    assert data["summary"]["success_rate"] == 0.5
    by_platform = {row["platform"]: row for row in data["platforms"]}
    assert by_platform["xhs"]["result_count"] == 12
    assert by_platform["xhs"]["zero_result_task_count"] == 1
    assert by_platform["tieba"]["success_rate"] is None
    assert {row["code"]: row["count"] for row in data["error_codes"]} == {
        "CAPTCHA_REQUIRED": 1,
        "NETWORK_TIMEOUT": 1,
    }


def test_demand_quality_uses_latest_feedback_per_target(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "demand-quality.sqlite3")
    db.create_feedback(
        target_type="comment",
        target_id="xhs::comment-1",
        label="valid_demand",
        corrected_category="功能需求",
        reason="明确提出功能诉求",
    )
    db.create_feedback(
        target_type="comment",
        target_id="xhs::comment-1",
        label="non_demand",
        reason="复核后认定为闲聊",
    )
    db.create_feedback(
        target_type="demand_cluster",
        target_id="cluster-1",
        label="category_correction",
        corrected_category="工具需求",
    )
    db.create_feedback(
        target_type="demand_cluster",
        target_id="cluster-2",
        label="merge",
    )

    client = TestClient(app)
    response = client.get("/api/v1/quality/demands?since_hours=24")

    assert response.status_code == 200
    data = response.json()
    assert data["event_count"] == 4
    assert data["current_target_count"] == 3
    assert data["event_labels"]["valid_demand"] == 1
    assert data["current_labels"]["non_demand"] == 1
    assert data["current_labels"]["category_correction"] == 1
    assert data["current_labels"]["merge"] == 1
    assert data["adjudication"]["labeled_count"] == 2
    assert data["adjudication"]["valid_demand_count"] == 1
    assert data["adjudication"]["non_demand_count"] == 1
    assert data["adjudication"]["valid_demand_rate"] == 0.5
    assert {row["category"] for row in data["corrected_categories"]} == {
        "工具需求",
    }


def test_source_quality_discloses_platform_level_granularity(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "source-quality.sqlite3")
    _task("xhs-ok", "xhs", "succeeded", 3)

    client = TestClient(app)
    response = client.get("/api/v1/quality/sources?since_hours=24")

    assert response.status_code == 200
    data = response.json()
    assert data["source_basis"] == "platform_adapter"
    assert data["source_entity_detail_available"] is False
    assert any(row["platform"] == "xhs" for row in data["items"])
    assert sum(data["state_counts"].values()) == len(data["items"])


def test_quality_detail_endpoints_validate_period(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "quality-period.sqlite3")
    client = TestClient(app)

    assert client.get("/api/v1/quality/collection?since_hours=0").status_code == 422
    assert client.get("/api/v1/quality/demands?since_hours=9000").status_code == 422
    assert client.get("/api/v1/quality/sources?since_hours=0").status_code == 422
