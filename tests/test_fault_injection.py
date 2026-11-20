import json

import pytest
from fastapi.testclient import TestClient

from app.main import app


def _running_interrupted_batch(tmp_path, monkeypatch):
    from app.storage import db
    from app.services import discovery

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "fault-injection.sqlite3")
    monkeypatch.setattr(discovery, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(discovery, "start_job", lambda payload: {
        "id": "fault-job", "pid": 43210, "process_identity": "original-process",
    })
    client = TestClient(app)
    project = client.post("/api/v1/collection/projects", json={"name": "故障注入"}).json()
    strategy = client.post(
        f"/api/v1/discovery/projects/{project['id']}/strategies",
        json={"name": "故障策略", "topic": "进程恢复", "terms": ["故障测试"],
              "platforms": ["bilibili"]},
    ).json()
    run = client.post(
        f"/api/v1/discovery/strategies/{strategy['id']}/runs"
    ).json()
    queued = client.post(
        f"/api/v1/discovery/runs/{run['id']}/execute-batch", json={}
    )
    assert queued.status_code == 200
    with db.connect() as conn:
        execution = json.loads(conn.execute(
            "SELECT execution_json FROM discovery_runs WHERE id=?", (run["id"],)
        ).fetchone()["execution_json"])
    discovery._start_batch_task(run["id"], execution)
    with db.connect() as conn:
        conn.execute(
            "UPDATE discovery_run_tasks SET status='interrupted' WHERE run_id=? AND task_index=0",
            (run["id"],),
        )
        conn.execute(
            "UPDATE discovery_runs SET status='paused' WHERE id=?", (run["id"],)
        )
        task = dict(conn.execute(
            "SELECT * FROM discovery_run_tasks WHERE run_id=?", (run["id"],)
        ).fetchone())
    return client, run["id"], task


def test_worker_lease_expiring_after_spawn_preserves_process_identity(tmp_path, monkeypatch):
    from app.storage import db
    from app.services import discovery

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "lease-expiry.sqlite3")
    monkeypatch.setattr(discovery, "DATA_ROOT", tmp_path)
    started = []
    monkeypatch.setattr(discovery, "start_job", lambda payload: (
        started.append(payload) or {
            "id": "lease-child", "pid": 65432, "process_identity": "child-identity"
        }
    ))
    client = TestClient(app)
    project = client.post("/api/v1/collection/projects", json={"name": "租约失效"}).json()
    strategy = client.post(
        f"/api/v1/discovery/projects/{project['id']}/strategies",
        json={"name": "租约策略", "topic": "租约", "terms": ["lease"],
              "platforms": ["bilibili"]},
    ).json()
    run = client.post(
        f"/api/v1/discovery/strategies/{strategy['id']}/runs"
    ).json()
    client.post(f"/api/v1/discovery/runs/{run['id']}/execute-batch", json={})
    with db.connect() as conn:
        execution = json.loads(conn.execute(
            "SELECT execution_json FROM discovery_runs WHERE id=?", (run["id"],)
        ).fetchone()["execution_json"])

    renewals = iter([True, True, False])
    monkeypatch.setattr(discovery, "_renew_worker_lease", lambda owner: next(renewals))
    with pytest.raises(RuntimeError, match="租约失效"):
        discovery._start_batch_task(run["id"], execution, lease_owner="worker-old")

    assert len(started) == 1
    with db.connect() as conn:
        task = conn.execute(
            "SELECT status,job_id,pid,process_identity FROM discovery_run_tasks WHERE run_id=?",
            (run["id"],),
        ).fetchone()
        run_row = conn.execute(
            "SELECT status,execution_json FROM discovery_runs WHERE id=?", (run["id"],)
        ).fetchone()
        attempt = conn.execute(
            "SELECT job_id,pid,process_identity FROM discovery_task_attempts "
            "WHERE task_id=(SELECT id FROM discovery_run_tasks WHERE run_id=?)",
            (run["id"],),
        ).fetchone()
    assert task["status"] == "interrupted"
    assert task["job_id"] == "lease-child"
    assert task["pid"] == 65432
    assert task["process_identity"] == "child-identity"
    assert attempt["process_identity"] == "child-identity"
    assert run_row["status"] == "paused"
    assert json.loads(run_row["execution_json"])["job_id"] == "lease-child"


def test_service_restart_pauses_unknown_job_and_missing_pid_can_resume(tmp_path, monkeypatch):
    from app.storage import db
    from app.services import discovery

    client, run_id, task = _running_interrupted_batch(tmp_path, monkeypatch)
    starts = []
    monkeypatch.setattr(discovery, "get_job", lambda job_id: None)
    monkeypatch.setattr(discovery, "start_job", lambda payload: starts.append(payload))
    monkeypatch.setattr(discovery, "get_process_identity", lambda pid: None)

    with db.connect() as conn:
        conn.execute("UPDATE discovery_runs SET status='running' WHERE id=?", (run_id,))
        conn.execute(
            "UPDATE discovery_run_tasks SET status='running' WHERE run_id=?", (run_id,)
        )
    paused = client.get(f"/api/v1/discovery/runs/{run_id}").json()
    assert paused["status"] == "paused"
    assert starts == []

    resumed = discovery.resume_discovery_batch(run_id)
    assert resumed["status"] == "queued"
    with db.connect() as conn:
        row = conn.execute(
            "SELECT status,pid,process_identity FROM discovery_run_tasks WHERE run_id=?",
            (run_id,),
        ).fetchone()
    assert row["status"] == "queued"
    assert row["pid"] is None
    assert row["process_identity"] == ""


def test_resume_rejects_original_pid_but_allows_pid_reuse(tmp_path, monkeypatch):
    from app.services import discovery

    _, run_id, _ = _running_interrupted_batch(tmp_path, monkeypatch)
    monkeypatch.setattr(discovery, "get_process_identity", lambda pid: "original-process")
    with pytest.raises(ValueError, match="仍在运行"):
        discovery.resume_discovery_batch(run_id)

    monkeypatch.setattr(discovery, "get_process_identity", lambda pid: "different-process")
    resumed = discovery.resume_discovery_batch(run_id)
    assert resumed["status"] == "queued"


def test_resume_fails_closed_when_live_pid_has_no_saved_identity(tmp_path, monkeypatch):
    from app.storage import db
    from app.services import discovery

    _, run_id, _ = _running_interrupted_batch(tmp_path, monkeypatch)
    with db.connect() as conn:
        conn.execute(
            "UPDATE discovery_run_tasks SET process_identity='' WHERE run_id=?", (run_id,)
        )
    monkeypatch.setattr(discovery, "get_process_identity", lambda pid: "some-live-process")
    with pytest.raises(ValueError, match="缺少 PID 启动身份"):
        discovery.resume_discovery_batch(run_id)


def test_child_process_abnormal_exit_is_recorded_with_exit_code(tmp_path):
    from app.services import crawler_engine

    log_path = tmp_path / "abnormal-child.log"
    log_path.write_text("worker crashed unexpectedly", encoding="utf-8")

    class CrashedProcess:
        def wait(self):
            return 23

    crawler_engine._jobs["fault-child"] = {
        "id": "fault-child", "status": "running", "pid": 77881,
        "log_file": str(log_path), "finished_at": None,
    }
    crawler_engine._processes["fault-child"] = CrashedProcess()
    crawler_engine._watch_job("fault-child")
    job = crawler_engine.get_job("fault-child")

    assert job["status"] == "failed"
    assert job["exit_code"] == 23
    assert job["error_code"]
    assert job["error_occurred_at"]
    assert job["retryable"] is False
