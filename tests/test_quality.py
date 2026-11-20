from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.main import app
from app.models import CommentRecord
from app.storage import db
from app.services.crawler_engine import _count_export_delta


def _comment(comment_id, *, complete=True):
    return CommentRecord(
        platform="xhs", comment_id=comment_id,
        content_id="post-1" if complete else None,
        content_url="https://example.test/post-1" if complete else None,
        content_title="测试内容" if complete else None,
        comment_text="希望增加导出功能" if complete else "",
        comment_created_at=datetime.now(UTC) if complete else None,
        content_created_at=None, parent_comment_id=None,
        author_key="author-1" if complete else None,
        like_count=3 if complete else None, raw={"id": comment_id},
    )


def _task(task_id, platform, status, result_count, hours_ago=1):
    started = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    db.start_collection_task(task_id, platform, "search", started)
    if status != "running":
        db.finish_collection_task(
            task_id, status=status, result_count=result_count,
            error_code="platform_blocked" if status == "blocked" else None,
        )


def test_quality_overview_aggregates_tasks_fields_and_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "quality.sqlite3")
    db.upsert_comment(_comment("complete"))
    db.upsert_comment(_comment("incomplete", complete=False))
    _task("xhs-ok", "xhs", "succeeded", 12)
    _task("xhs-zero", "xhs", "succeeded", 0)
    _task("dy-fail-1", "douyin", "failed", 0)
    _task("dy-fail-2", "douyin", "failed", 0)
    _task("wb-blocked", "weibo", "blocked", 0)
    with db.connect() as conn:
        conn.execute("UPDATE collection_task_runs SET retry_count=1 WHERE task_id='xhs-ok'")

    client = TestClient(app)
    response = client.get("/api/v1/quality/overview?since_hours=168")
    assert response.status_code == 200
    data = response.json()
    collection = data["collection"]
    assert collection["task_total"] == 5
    assert collection["task_succeeded"] == 2
    assert collection["task_failed"] == 2
    assert collection["task_blocked"] == 1
    assert collection["task_success_rate"] == 0.4
    assert collection["retry_rate"] == 0.2
    assert collection["zero_result_rate"] == 0.5

    quality = data["field_quality"]
    assert quality["comment_total"] == 2
    assert quality["fields"]["content_id"]["complete"] == 1
    assert quality["fields"]["comment_text"]["completeness_rate"] == 0.5
    assert quality["fields"]["author_url"]["status"] == "not_applicable"

    states = {source["platform"]: source["state"] for source in data["sources"]}
    assert states["xhs"] == "low_yield"
    assert states["douyin"] == "failing"
    assert states["weibo"] == "blocked"
    assert states["facebook_ads"] == "unsupported"


def test_quality_overview_empty_data_and_period_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "quality-empty.sqlite3")
    client = TestClient(app)
    response = client.get("/api/v1/quality/overview")
    assert response.status_code == 200
    data = response.json()
    assert data["collection"]["task_total"] == 0
    assert data["collection"]["task_success_rate"] is None
    assert data["field_quality"]["overall_completeness_rate"] is None
    assert client.get("/api/v1/quality/overview?since_hours=0").status_code == 422


def test_collection_task_persistence_and_export_delta(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "task-runs.sqlite3")
    db.start_collection_task("task-1", "xhs", "search", datetime.now(UTC).isoformat())
    db.finish_collection_task("task-1", status="succeeded", result_count=4)
    rows = db.list_collection_task_runs()
    assert rows[0]["task_id"] == "task-1"
    assert rows[0]["status"] == "succeeded"
    assert rows[0]["result_count"] == 4
    assert rows[0]["duration_ms"] >= 0

    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    file_path = export_dir / "comments.jsonl"
    file_path.write_text('{"id":1}\n', encoding="utf-8")
    baseline = {str(file_path): file_path.stat().st_size}
    with file_path.open("a", encoding="utf-8") as stream:
        stream.write('{"id":2}\n{"id":3}\n')
    assert _count_export_delta({
        "export_dir": str(export_dir), "export_baseline": baseline,
    }) == 2
