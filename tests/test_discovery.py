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


def test_discovery_executes_platform_search_and_imports_posts(tmp_path, monkeypatch):
    import json
    import time
    from app.storage import db
    from app.services import discovery

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "discovery-exec.sqlite3")
    monkeypatch.setattr(discovery, "DATA_ROOT", tmp_path)
    job = {"id": "job-123", "status": "running", "platform": "bilibili"}
    started_payload = {}
    monkeypatch.setattr(discovery, "start_job", lambda payload: (started_payload.update(payload) or job))
    client = TestClient(app)
    project = client.post("/api/v1/collection/projects", json={"name": "执行测试"}).json()
    strategy = client.post(f"/api/v1/discovery/projects/{project['id']}/strategies", json={
        "name": "B站搜索", "topic": "测试", "terms": ["AI 工具"], "platforms": ["bilibili"]
    }).json()
    run = client.post(f"/api/v1/discovery/strategies/{strategy['id']}/runs").json()

    started = client.post(f"/api/v1/discovery/runs/{run['id']}/execute?platform=bilibili")
    assert started.status_code == 200
    assert started.json()["status"] == "queued"
    assert started.json()["current_task"]["platform"] == "bilibili"
    with db.connect() as conn:
        execution = json.loads(conn.execute(
            "SELECT execution_json FROM discovery_runs WHERE id=?", (run["id"],)
        ).fetchone()[0])
    started_job = discovery._start_batch_task(run["id"], execution)
    assert job["id"] == started_job["id"]

    folder = __import__("pathlib").Path(started_payload["export_dir"])
    assert folder.is_dir()
    assert "/runs/" in str(folder)
    export = folder / "contents.jsonl"
    export.write_text(json.dumps({
        "bvid": "BV123", "title": "AI 工具真实体验", "desc": "寻找自动化工具",
        "mid": "9988", "name": "效率观察员"
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    now = time.time()
    import os
    os.utime(export, (now, now))
    job["status"] = "succeeded"
    monkeypatch.setattr(discovery, "get_job", lambda job_id: {**job, "status": "succeeded"})
    refreshed = client.get(f"/api/v1/discovery/runs/{run['id']}")
    assert refreshed.status_code == 200
    assert refreshed.json()["status"] == "succeeded"
    assert refreshed.json()["result_count"] == 1
    results = client.get("/api/v1/discovery/results").json()
    assert results["items"][0]["title"] == "AI 工具真实体验"
    imports = client.get(f"/api/v1/discovery/runs/{run['id']}/imports").json()
    assert imports["total"] == 1
    assert imports["items"][0]["file_fingerprint"]
    assert imports["items"][0]["file_size"] == export.stat().st_size
    assert imports["items"][0]["row_count"] == 1
    assert imports["items"][0]["imported_count"] == 1
    assert imports["items"][0]["status"] == "imported"
    task_detail = client.get(f"/api/v1/discovery/runs/{run['id']}/tasks").json()["items"][0]
    assert task_detail["export_dir"].endswith("attempt-0001")
    assert task_detail["imported_file_count"] == 1
    assert task_detail["imported_row_count"] == 1


def test_discovery_execution_rejects_unavailable_platform(tmp_path, monkeypatch):
    from app.storage import db

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "discovery-platform.sqlite3")
    client = TestClient(app)
    project = client.post("/api/v1/collection/projects", json={"name": "平台边界"}).json()
    strategy = client.post(f"/api/v1/discovery/projects/{project['id']}/strategies", json={
        "name": "海外平台", "topic": "测试", "terms": ["test"]
    }).json()
    run = client.post(f"/api/v1/discovery/strategies/{strategy['id']}/runs").json()
    response = client.post(f"/api/v1/discovery/runs/{run['id']}/execute?platform=youtube")
    assert response.status_code == 409
    assert "暂未接入原生搜索执行" in response.json()["detail"]



def test_discovery_batch_runs_platforms_sequentially(tmp_path, monkeypatch):
    from app.storage import db
    from app.services import discovery

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "discovery-batch.sqlite3")
    monkeypatch.setattr(discovery, "DATA_ROOT", tmp_path)
    jobs = {}
    counter = {"value": 0}

    def start_job(payload):
        counter["value"] += 1
        job = {"id": f"job-{counter['value']}", "status": "running",
               "platform": payload["platform"]}
        jobs[job["id"]] = job
        return job

    monkeypatch.setattr(discovery, "start_job", start_job)
    monkeypatch.setattr(discovery, "get_job", lambda job_id: jobs.get(job_id))
    client = TestClient(app)
    project = client.post("/api/v1/collection/projects", json={"name": "批量发现"}).json()
    strategy = client.post(f"/api/v1/discovery/projects/{project['id']}/strategies", json={
        "name": "跨平台策略", "topic": "AI 办公",
        "terms": ["AI PPT"], "platforms": ["bilibili", "zhihu"]
    }).json()
    run = client.post(f"/api/v1/discovery/strategies/{strategy['id']}/runs").json()

    started = client.post(f"/api/v1/discovery/runs/{run['id']}/execute-batch", json={})
    assert started.status_code == 200
    assert started.json()["task_count"] == 2
    assert started.json()["status"] == "queued"
    assert started.json()["current_task"]["platform"] == "bilibili"
    discovery._start_batch_task(run["id"], {"batch": True, "tasks": [
        {"platform": "bilibili", "query_term": "AI PPT", "query_index": 0},
        {"platform": "zhihu", "query_term": "AI PPT", "query_index": 0}
    ], "cursor": 0, "completed": 0, "failed": 0})
    duplicate = client.post(f"/api/v1/discovery/runs/{run['id']}/execute-batch", json={})
    assert duplicate.status_code == 409
    assert client.get(f"/api/v1/discovery/runs/{run['id']}/tasks").json()["total"] == 2
    task_snapshot = client.get(f"/api/v1/discovery/runs/{run['id']}/tasks").json()
    assert task_snapshot["total"] == 2
    assert [item["status"] for item in task_snapshot["items"]] == ["running", "queued"]

    jobs["job-1"]["status"] = "succeeded"
    advanced = client.get(f"/api/v1/discovery/runs/{run['id']}").json()
    assert advanced["status"] == "running"
    assert advanced["completed"] == 1
    assert advanced["current_task"]["platform"] == "zhihu"
    assert counter["value"] == 2

    jobs["job-2"]["status"] = "succeeded"
    finished = client.get(f"/api/v1/discovery/runs/{run['id']}").json()
    assert finished["status"] == "succeeded"
    assert finished["completed"] == 2
    assert finished["failed"] == 0


def test_discovery_batch_limits_task_count_and_platform_scope(tmp_path, monkeypatch):
    from app.storage import db

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "discovery-batch-limits.sqlite3")
    client = TestClient(app)
    project = client.post("/api/v1/collection/projects", json={"name": "范围校验"}).json()
    strategy = client.post(f"/api/v1/discovery/projects/{project['id']}/strategies", json={
        "name": "范围策略", "topic": "测试", "terms": ["a", "b"],
        "platforms": ["bilibili"]
    }).json()
    run = client.post(f"/api/v1/discovery/strategies/{strategy['id']}/runs").json()
    response = client.post(f"/api/v1/discovery/runs/{run['id']}/execute-batch",
                           json={"platforms": ["zhihu"]})
    assert response.status_code == 409


def test_discovery_batch_persists_tasks_and_can_resume_after_restart(tmp_path, monkeypatch):
    from app.storage import db
    from app.services import discovery

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "discovery-resume.sqlite3")
    monkeypatch.setattr(discovery, "DATA_ROOT", tmp_path)
    jobs = {}
    count = {"n": 0}

    def start_job(payload):
        count["n"] += 1
        job = {"id": f"persisted-{count['n']}", "status": "running",
               "platform": payload["platform"], "pid": 4321}
        jobs[job["id"]] = job
        return job

    monkeypatch.setattr(discovery, "start_job", start_job)
    monkeypatch.setattr(discovery, "get_job", lambda job_id: jobs.get(job_id))
    client = TestClient(app)
    project = client.post("/api/v1/collection/projects", json={"name": "恢复测试"}).json()
    strategy = client.post(f"/api/v1/discovery/projects/{project['id']}/strategies", json={
        "name": "持久化策略", "topic": "测试", "terms": ["词一", "词二"],
        "platforms": ["bilibili"]
    }).json()
    run = client.post(f"/api/v1/discovery/strategies/{strategy['id']}/runs").json()
    run_id = run["id"]

    started = client.post(f"/api/v1/discovery/runs/{run_id}/execute-batch", json={}).json()
    assert started["status"] == "queued"
    with db.connect() as conn:
        execution = __import__("json").loads(conn.execute(
            "SELECT execution_json FROM discovery_runs WHERE id=?", (run_id,)
        ).fetchone()[0])
    discovery._start_batch_task(run_id, execution)
    with db.connect() as conn:
        tasks = conn.execute("SELECT status,job_id FROM discovery_run_tasks WHERE run_id=? ORDER BY task_index",
                             (run_id,)).fetchall()
    assert [row["status"] for row in tasks] == ["running", "queued"]
    assert tasks[0]["job_id"] == "persisted-1"

    # Simulate a service restart: the in-memory process registry no longer knows the job.
    del jobs["persisted-1"]
    paused = client.get(f"/api/v1/discovery/runs/{run_id}").json()
    assert paused["status"] == "paused"
    with db.connect() as conn:
        task = conn.execute("SELECT status FROM discovery_run_tasks WHERE run_id=? AND task_index=0",
                            (run_id,)).fetchone()
    assert task["status"] == "interrupted"

    resumed = client.post(f"/api/v1/discovery/runs/{run_id}/resume").json()
    assert resumed["status"] == "queued"
    assert resumed["current_task"]["query_term"] == "词一"
    with db.connect() as conn:
        execution = __import__("json").loads(conn.execute(
            "SELECT execution_json FROM discovery_runs WHERE id=?", (run_id,)
        ).fetchone()[0])
    discovery._start_batch_task(run_id, execution)
    with db.connect() as conn:
        current = conn.execute(
            "SELECT job_id,status FROM discovery_run_tasks WHERE run_id=? AND task_index=0",
            (run_id,)
        ).fetchone()
    assert current["status"] == "running"
    assert current["job_id"] == "persisted-2"



def test_discovery_worker_lease_is_exclusive_and_expires(tmp_path, monkeypatch):
    from app.storage import db
    from app.services import discovery

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "discovery-lease.sqlite3")
    discovery._ensure()
    assert discovery._claim_worker_lease("worker-a", lease_seconds=60) is True
    assert discovery._claim_worker_lease("worker-b", lease_seconds=60) is False
    assert discovery._renew_worker_lease("worker-b", lease_seconds=60) is False
    assert discovery._renew_worker_lease("worker-a", lease_seconds=60) is True

    with db.connect() as conn:
        conn.execute(
            "UPDATE discovery_worker_leases SET lease_until=0 WHERE lease_name='global'"
        )
    assert discovery._claim_worker_lease("worker-b", lease_seconds=60) is True
    assert discovery._renew_worker_lease("worker-a", lease_seconds=60) is False



def test_discovery_worker_lease_loss_keeps_started_job_recoverable(tmp_path, monkeypatch):
    import json
    import pytest
    from app.storage import db
    from app.services import discovery

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "discovery-lease-loss.sqlite3")
    jobs = {"id": "lease-lost-job", "pid": 9876}
    monkeypatch.setattr(discovery, "start_job", lambda payload: jobs)
    client = TestClient(app)
    project = client.post("/api/v1/collection/projects", json={"name": "租约丢失"}).json()
    strategy = client.post(f"/api/v1/discovery/projects/{project['id']}/strategies", json={
        "name": "租约丢失策略", "topic": "测试", "terms": ["关键词"],
        "platforms": ["bilibili"]
    }).json()
    run = client.post(f"/api/v1/discovery/strategies/{strategy['id']}/runs").json()
    queued = client.post(f"/api/v1/discovery/runs/{run['id']}/execute-batch", json={}).json()
    assert queued["status"] == "queued"
    with db.connect() as conn:
        execution = json.loads(conn.execute(
            "SELECT execution_json FROM discovery_runs WHERE id=?", (run["id"],)
        ).fetchone()[0])
    renew_results = iter([True, True, False])
    monkeypatch.setattr(discovery, "_renew_worker_lease", lambda owner_id: next(renew_results))

    with pytest.raises(RuntimeError, match="租约失效"):
        discovery._start_batch_task(run["id"], execution, lease_owner="stale-worker")

    with db.connect() as conn:
        run_row = conn.execute(
            "SELECT status,execution_json FROM discovery_runs WHERE id=?", (run["id"],)
        ).fetchone()
        task = conn.execute(
            "SELECT status,job_id,pid FROM discovery_run_tasks WHERE run_id=? AND task_index=0",
            (run["id"],)
        ).fetchone()
    assert run_row["status"] == "paused"
    assert json.loads(run_row["execution_json"])["job_id"] == "lease-lost-job"
    assert task["status"] == "interrupted"
    assert task["job_id"] == "lease-lost-job"
    assert task["pid"] == 9876



def test_retry_after_and_exponential_backoff_are_bounded():
    from app.services import discovery

    assert discovery._parse_retry_after("120") == 120
    assert discovery._retry_delay(1, retry_after=120, jitter=0) == 120
    assert discovery._retry_delay(2, jitter=0) == 30
    assert discovery._retry_delay(20, jitter=0) == discovery._BACKOFF_MAX_SECONDS
    assert discovery._parse_retry_after("999999") == discovery._RETRY_AFTER_MAX_SECONDS


def test_rate_limit_retry_persists_attempt_and_platform_cooldown(tmp_path, monkeypatch):
    import json
    from app.storage import db
    from app.services import discovery

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "retry-rate-limit.sqlite3")
    monkeypatch.setattr(discovery, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(discovery, "_worker_id", "test-worker")
    monkeypatch.setattr(discovery, "_claim_worker_lease", lambda owner_id, **kwargs: owner_id != "test-worker")
    monkeypatch.setattr(discovery, "_renew_worker_lease", lambda owner_id, **kwargs: owner_id != "test-worker")
    job = {"id": "rate-job", "pid": 321, "status": "running"}
    monkeypatch.setattr(discovery, "start_job", lambda payload: job)
    client = TestClient(app)
    project = client.post("/api/v1/collection/projects", json={"name": "限流重试"}).json()
    strategy = client.post(f"/api/v1/discovery/projects/{project['id']}/strategies", json={
        "name": "限流策略", "topic": "测试", "terms": ["关键词"], "platforms": ["bilibili"]
    }).json()
    run = client.post(f"/api/v1/discovery/strategies/{strategy['id']}/runs").json()
    client.post(f"/api/v1/discovery/runs/{run['id']}/execute-batch", json={})
    with db.connect() as conn:
        execution = json.loads(conn.execute(
            "SELECT execution_json FROM discovery_runs WHERE id=?", (run["id"],)
        ).fetchone()[0])
    discovery._start_batch_task(run["id"], execution)
    failed_job = {
        "id": "rate-job", "pid": 321, "status": "failed", "exit_code": 1,
        "error_category": "rate_limited", "error_code": "COLLECTION_RATE_LIMITED",
        "error_message": "HTTP 429 Too Many Requests", "retryable": True,
        "manual_intervention": False, "retry_after": "120",
    }
    monkeypatch.setattr(discovery, "get_job", lambda job_id: failed_job)
    result = discovery.refresh_discovery_run(run["id"])
    assert result["status"] == "retry_wait"
    assert result["retry_in_seconds"] >= 120
    assert discovery._cooldown_remaining("bilibili") >= 120
    with db.connect() as conn:
        task = conn.execute(
            "SELECT status,attempt_count,max_attempts,error_code,next_retry_at FROM discovery_run_tasks WHERE run_id=?",
            (run["id"],),
        ).fetchone()
        attempts = conn.execute(
            "SELECT COUNT(*) AS count FROM discovery_task_attempts WHERE task_id=("
            "SELECT id FROM discovery_run_tasks WHERE run_id=?)", (run["id"],)
        ).fetchone()["count"]
    assert task["status"] == "retry_wait"
    assert task["attempt_count"] == 1
    assert task["max_attempts"] == 4
    assert task["error_code"] == "COLLECTION_RATE_LIMITED"
    assert task["next_retry_at"]
    assert attempts >= 1


def test_non_retryable_auth_failure_is_blocked_without_retry(tmp_path, monkeypatch):
    import json
    from app.storage import db
    from app.services import discovery

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "retry-auth.sqlite3")
    monkeypatch.setattr(discovery, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(discovery, "start_job", lambda payload: {"id": "auth-job", "pid": 88})
    client = TestClient(app)
    project = client.post("/api/v1/collection/projects", json={"name": "登录失效"}).json()
    strategy = client.post(f"/api/v1/discovery/projects/{project['id']}/strategies", json={
        "name": "登录策略", "topic": "测试", "terms": ["关键词"], "platforms": ["bilibili"]
    }).json()
    run = client.post(f"/api/v1/discovery/strategies/{strategy['id']}/runs").json()
    client.post(f"/api/v1/discovery/runs/{run['id']}/execute-batch", json={})
    with db.connect() as conn:
        execution = json.loads(conn.execute(
            "SELECT execution_json FROM discovery_runs WHERE id=?", (run["id"],)
        ).fetchone()[0])
    discovery._start_batch_task(run["id"], execution)
    monkeypatch.setattr(discovery, "get_job", lambda job_id: {
        "id": "auth-job", "status": "failed", "exit_code": 1,
        "error_category": "authentication_required", "error_code": "COLLECTION_AUTH_REQUIRED",
        "error_message": "登录失效", "retryable": False, "manual_intervention": True,
    })
    result = discovery.refresh_discovery_run(run["id"])
    assert result["status"] == "partial"
    with db.connect() as conn:
        task = conn.execute(
            "SELECT status,attempt_count,next_retry_at FROM discovery_run_tasks WHERE run_id=?",
            (run["id"],),
        ).fetchone()
    assert task["status"] == "blocked"
    assert task["attempt_count"] == 1
    assert task["next_retry_at"] is None


def test_worker_refuses_to_start_after_lease_loss(tmp_path, monkeypatch):
    import json
    import pytest
    from app.storage import db
    from app.services import discovery

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "retry-lease-loss.sqlite3")
    called = {"start": 0}
    monkeypatch.setattr(discovery, "start_job", lambda payload: called.__setitem__("start", 1))
    client = TestClient(app)
    project = client.post("/api/v1/collection/projects", json={"name": "租约前置检查"}).json()
    strategy = client.post(f"/api/v1/discovery/projects/{project['id']}/strategies", json={
        "name": "租约策略", "topic": "测试", "terms": ["关键词"], "platforms": ["bilibili"]
    }).json()
    run = client.post(f"/api/v1/discovery/strategies/{strategy['id']}/runs").json()
    client.post(f"/api/v1/discovery/runs/{run['id']}/execute-batch", json={})
    with db.connect() as conn:
        execution = json.loads(conn.execute(
            "SELECT execution_json FROM discovery_runs WHERE id=?", (run["id"],)
        ).fetchone()[0])
    monkeypatch.setattr(discovery, "_renew_worker_lease", lambda owner_id: False)
    with pytest.raises(RuntimeError, match="租约失效"):
        discovery._start_batch_task(run["id"], execution, lease_owner="stale-worker")
    assert called["start"] == 0
    with db.connect() as conn:
        task = conn.execute(
            "SELECT status FROM discovery_run_tasks WHERE run_id=? AND task_index=0",
            (run["id"],),
        ).fetchone()
    assert task["status"] == "queued"



def test_discovery_attempt_export_directories_are_isolated(tmp_path, monkeypatch):
    import json
    from app.storage import db
    from app.services import discovery

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "discovery-isolation.sqlite3")
    monkeypatch.setattr(discovery, "DATA_ROOT", tmp_path)
    payloads = []
    monkeypatch.setattr(discovery, "start_job", lambda payload: (
        payloads.append(dict(payload)) or {"id": f"job-{len(payloads)}", "pid": 100+len(payloads)}
    ))
    client = TestClient(app)
    project = client.post("/api/v1/collection/projects", json={"name": "目录隔离"}).json()
    strategy = client.post(f"/api/v1/discovery/projects/{project['id']}/strategies", json={
        "name": "隔离策略", "topic": "测试", "terms": ["词一"], "platforms": ["bilibili", "zhihu"]
    }).json()
    run = client.post(f"/api/v1/discovery/strategies/{strategy['id']}/runs").json()
    client.post(f"/api/v1/discovery/runs/{run['id']}/execute-batch", json={})
    with db.connect() as conn:
        execution = json.loads(conn.execute(
            "SELECT execution_json FROM discovery_runs WHERE id=?", (run["id"],)
        ).fetchone()[0])
    discovery._start_batch_task(run["id"], execution)
    first_dir = payloads[0]["export_dir"]
    with db.connect() as conn:
        conn.execute("UPDATE discovery_run_tasks SET status='succeeded' WHERE run_id=? AND task_index=0",
                     (run["id"],))
        conn.execute("UPDATE discovery_runs SET status='running' WHERE id=?", (run["id"],))
    execution["cursor"] = 1
    discovery._start_batch_task(run["id"], execution)
    second_dir = payloads[1]["export_dir"]
    assert first_dir != second_dir
    assert f"/{run['id']}/" in first_dir
    assert f"/{run['id']}/" in second_dir
    assert first_dir.endswith("attempt-0001")
    assert second_dir.endswith("attempt-0001")
    assert __import__("pathlib").Path(first_dir).is_dir()
    assert __import__("pathlib").Path(second_dir).is_dir()


def test_discovery_import_file_fingerprint_is_idempotent(tmp_path, monkeypatch):
    import hashlib
    import json
    from app.storage import db
    from app.services import discovery

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "discovery-fingerprint.sqlite3")
    monkeypatch.setattr(discovery, "DATA_ROOT", tmp_path)
    client = TestClient(app)
    project = client.post("/api/v1/collection/projects", json={"name": "指纹幂等"}).json()
    strategy = client.post(f"/api/v1/discovery/projects/{project['id']}/strategies", json={
        "name": "指纹策略", "topic": "测试", "terms": ["关键词"], "platforms": ["bilibili"]
    }).json()
    run = client.post(f"/api/v1/discovery/strategies/{strategy['id']}/runs").json()
    client.post(f"/api/v1/discovery/runs/{run['id']}/execute-batch", json={})
    with db.connect() as conn:
        task = conn.execute(
            "SELECT id FROM discovery_run_tasks WHERE run_id=? AND task_index=0",
            (run["id"],),
        ).fetchone()
    export_dir = tmp_path / "exports" / "runs" / run["id"] / task["id"] / "attempt-0001"
    export_dir.mkdir(parents=True)
    export = export_dir / "content.jsonl"
    payload = {"bvid": "BV-FINGERPRINT", "title": "指纹测试", "url": "https://example.com/BV-FINGERPRINT"}
    content = json.dumps(payload, ensure_ascii=False) + "\n"
    export.write_text(content, encoding="utf-8")
    first = discovery._import_discovery_exports(
        run["id"], task["id"], 1, "bilibili", "关键词", export_dir
    )
    second = discovery._import_discovery_exports(
        run["id"], task["id"], 1, "bilibili", "关键词", export_dir
    )
    expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert first["imported_file_count"] == 1
    assert first["imported_row_count"] == 1
    assert second["imported_file_count"] == 0
    assert second["imported_row_count"] == 0
    with db.connect() as conn:
        batches = conn.execute(
            "SELECT file_fingerprint,file_size,row_count,imported_count,status "
            "FROM discovery_import_batches WHERE run_id=?", (run["id"],)
        ).fetchall()
        contents = conn.execute(
            "SELECT COUNT(*) AS count FROM discovered_contents WHERE run_id=?", (run["id"],)
        ).fetchone()["count"]
    assert len(batches) == 1
    assert batches[0]["file_fingerprint"] == expected
    assert batches[0]["file_size"] == len(content.encode("utf-8"))
    assert batches[0]["row_count"] == 1
    assert batches[0]["imported_count"] == 1
    assert batches[0]["status"] == "imported"
    assert contents == 1


def test_crawler_command_rejects_export_directory_outside_data_root(tmp_path, monkeypatch):
    import pytest
    from app.services import crawler_engine

    monkeypatch.setattr(crawler_engine, "DATA_ROOT", tmp_path)
    with pytest.raises(ValueError, match="exports"):
        crawler_engine._build_command({
            "platform": "bilibili", "mode": "search", "keywords": "测试",
            "export_dir": tmp_path.parent / "outside",
        })
