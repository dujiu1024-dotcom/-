from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from app.services import scheduler
from app.storage import db


def _use_database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "scheduler-lease.sqlite3")


def test_only_one_concurrent_process_can_acquire_scheduler_lease(tmp_path, monkeypatch):
    _use_database(tmp_path, monkeypatch)
    with ThreadPoolExecutor(max_workers=8) as pool:
        owners = list(pool.map(lambda index: scheduler._acquire_scheduler_lease(
            owner_token=f"worker-{index}"
        ), range(8)))
    acquired = [owner for owner in owners if owner is not None]
    assert len(acquired) == 1


def test_expired_lease_can_be_taken_over_but_old_owner_cannot_release_it(
    tmp_path, monkeypatch
):
    _use_database(tmp_path, monkeypatch)
    now = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
    assert scheduler._acquire_scheduler_lease("worker-old", now, 30) == "worker-old"
    assert scheduler._acquire_scheduler_lease(
        "worker-new", now + timedelta(seconds=31), 30
    ) == "worker-new"

    assert scheduler._release_scheduler_lease("worker-old") is False
    with db.connect() as conn:
        row = conn.execute(
            "SELECT owner_token FROM scheduler_leases WHERE lease_name=?",
            (scheduler._LEASE_NAME,),
        ).fetchone()
    assert row["owner_token"] == "worker-new"
    assert scheduler._release_scheduler_lease("worker-new") is True


def test_held_lease_defers_tick_without_touching_due_schedule(tmp_path, monkeypatch):
    from app.services import source_tracking

    _use_database(tmp_path, monkeypatch)
    with db.connect() as conn:
        source_tracking._schema(conn)
        conn.execute("""
            INSERT INTO tracked_sources
            (id,platform,source_type,source_key,enabled,priority,interval_minutes,
             window_hours,tracking_scope,created_at,updated_at,tracking_target,tracking_verified)
            VALUES('source-1','xhs','author','author-1',1,5,1440,72,'creator',
                   '2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00','author-1',1)
        """)
        conn.execute("""
            INSERT INTO source_schedules(source_id,enabled,next_run_at,updated_at)
            VALUES('source-1',1,'2000-01-01T00:00:00+00:00','2000-01-01T00:00:00+00:00')
        """)
    assert scheduler._acquire_scheduler_lease("other-worker") == "other-worker"
    monkeypatch.setattr(scheduler, "list_jobs", lambda: [])
    monkeypatch.setattr(
        scheduler, "start_job",
        lambda payload: (_ for _ in ()).throw(AssertionError("must not start crawler")),
    )

    result = scheduler.tick_schedules()

    assert result == {
        "started": [], "reconciled": 0, "deferred": "scheduler_lease_held"
    }
    with db.connect() as conn:
        row = conn.execute(
            "SELECT next_run_at FROM source_schedules WHERE source_id='source-1'"
        ).fetchone()
    assert row["next_run_at"] == "2000-01-01T00:00:00+00:00"


def test_tick_releases_lease_when_scheduler_raises(tmp_path, monkeypatch):
    _use_database(tmp_path, monkeypatch)
    monkeypatch.setattr(
        scheduler, "_tick_schedules",
        lambda limit=1: (_ for _ in ()).throw(RuntimeError("simulated failure")),
    )
    try:
        scheduler.tick_schedules()
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected simulated failure")

    with db.connect() as conn:
        row = conn.execute(
            "SELECT owner_token FROM scheduler_leases WHERE lease_name=?",
            (scheduler._LEASE_NAME,),
        ).fetchone()
    assert row is None
