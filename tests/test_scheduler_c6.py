from datetime import UTC, datetime, timedelta

from app.models import CommentRecord
from app.services.source_tracking import (
    configure_schedule,
    discover_candidates,
    list_candidates,
    list_source_runs,
    review_candidate,
    update_source,
)
from app.services.scheduler import _iso, tick_schedules
from app.storage.db import connect, upsert_comment


def _prepare_due_source(tmp_path, monkeypatch):
    from app.storage import db

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "c6-scheduler.sqlite3")
    upsert_comment(CommentRecord(
        platform="xhs",
        comment_id="c6-comment",
        author_key="c6-creator",
        comment_text="求推荐工具",
        content_id="c6-post",
        content_url="https://example.invalid/c6-post",
        content_title="工具讨论",
        raw={},
    ))
    discover_candidates()
    candidate = list_candidates("pending")[0]
    source_id = review_candidate(candidate["id"], "approve")["source_id"]
    update_source(source_id, tracking_target="c6-creator", tracking_verified=True)
    configure_schedule(source_id, True)
    with connect() as conn:
        conn.execute(
            "UPDATE source_schedules SET next_run_at=? WHERE source_id=?",
            ("2000-01-01T00:00:00+00:00", source_id),
        )
    return source_id


def test_scheduler_iso_normalizes_offsets_to_utc():
    local = datetime.fromisoformat("2026-11-20T17:00:00+09:00")
    assert _iso(local) == "2026-11-20T08:00:00+00:00"


def test_restart_reconciles_orphaned_running_run_without_replaying_history(tmp_path, monkeypatch):
    from app.services import scheduler

    source_id = _prepare_due_source(tmp_path, monkeypatch)
    with connect() as conn:
        conn.execute(
            """INSERT INTO source_runs
               (id,source_id,job_id,status,scheduled_at,started_at)
               VALUES('orphan-run',?,'lost-job','running','2026-11-19T00:00:00+00:00',
                      '2026-11-19T00:00:00+00:00')""",
            (source_id,),
        )
        conn.execute(
            "UPDATE source_schedules SET next_run_at='2000-01-01T00:00:00+00:00' WHERE source_id=?",
            (source_id,),
        )
    monkeypatch.setattr(scheduler, "list_jobs", list)
    result = tick_schedules()
    assert result["reconciled"] == 1
    run = list_source_runs(source_id)[0]
    assert run["id"] == "orphan-run"
    assert run["status"] == "interrupted"
    assert run["error"]
    with connect() as conn:
        schedule = conn.execute(
            "SELECT next_run_at FROM source_schedules WHERE source_id=?", (source_id,)
        ).fetchone()
    assert datetime.fromisoformat(schedule["next_run_at"]) > datetime.now(UTC) - timedelta(minutes=1)


def test_misfire_is_coalesced_to_one_dispatch_and_next_run_moves_forward(tmp_path, monkeypatch):
    from app.services import scheduler

    source_id = _prepare_due_source(tmp_path, monkeypatch)
    monkeypatch.setattr(scheduler, "list_jobs", list)
    monkeypatch.setattr(scheduler, "list_platforms", lambda: [{"code": "xhs", "capabilities": ["creator"]}])
    monkeypatch.setattr(scheduler, "SUPPORTED", {"xhs"})
    calls = []

    def start_job(payload):
        calls.append(payload)
        return {"id": "job-c6", "status": "running", "started_at": "2026-11-20T08:00:00+00:00"}

    monkeypatch.setattr(scheduler, "start_job", start_job)
    first = tick_schedules()
    second = tick_schedules()
    assert len(first["started"]) == 1
    assert second["started"] == []
    assert len(calls) == 1
    assert len(list_source_runs(source_id)) == 1
    with connect() as conn:
        next_run = conn.execute(
            "SELECT next_run_at FROM source_schedules WHERE source_id=?", (source_id,)
        ).fetchone()["next_run_at"]
    assert datetime.fromisoformat(next_run) > datetime.now(UTC) - timedelta(minutes=1)


def test_schedule_pause_and_resume_controls_due_dispatch(tmp_path, monkeypatch):
    from app.services import scheduler

    source_id = _prepare_due_source(tmp_path, monkeypatch)
    monkeypatch.setattr(scheduler, "list_jobs", list)
    monkeypatch.setattr(scheduler, "list_platforms", lambda: [{"code": "xhs", "capabilities": ["creator"]}])
    monkeypatch.setattr(scheduler, "SUPPORTED", {"xhs"})
    calls = []
    monkeypatch.setattr(scheduler, "start_job", lambda payload: calls.append(payload) or {
        "id": "job-resumed", "status": "running", "started_at": "2026-11-20T08:00:00+00:00"
    })

    paused = configure_schedule(source_id, False)
    assert paused["enabled"] == 0
    assert tick_schedules()["started"] == []
    assert calls == []

    resumed = configure_schedule(source_id, True)
    assert resumed["enabled"] == 1
    # Resume schedules the next interval from now instead of replaying the paused backlog.
    assert datetime.fromisoformat(resumed["next_run_at"]) > datetime.now(UTC) - timedelta(minutes=1)
    assert tick_schedules()["started"] == []
    assert calls == []


def test_scheduler_lock_rejects_overlapping_worker_tick(tmp_path, monkeypatch):
    from app.services import scheduler

    _prepare_due_source(tmp_path, monkeypatch)
    assert scheduler._tick_lock.acquire(blocking=False)
    try:
        result = tick_schedules()
    finally:
        scheduler._tick_lock.release()
    assert result == {"started": [], "reconciled": 0, "deferred": "scheduler_busy"}
