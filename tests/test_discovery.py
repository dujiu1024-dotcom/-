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
    monkeypatch.setattr(discovery, "start_job", lambda payload: job)
    client = TestClient(app)
    project = client.post("/api/v1/collection/projects", json={"name": "执行测试"}).json()
    strategy = client.post(f"/api/v1/discovery/projects/{project['id']}/strategies", json={
        "name": "B站搜索", "topic": "测试", "terms": ["AI 工具"], "platforms": ["bilibili"]
    }).json()
    run = client.post(f"/api/v1/discovery/strategies/{strategy['id']}/runs").json()

    started = client.post(f"/api/v1/discovery/runs/{run['id']}/execute?platform=bilibili")
    assert started.status_code == 200
    assert started.json()["status"] == "running"
    assert job["id"] == started.json()["job_id"]

    folder = tmp_path / "exports" / "bilibili"
    folder.mkdir(parents=True)
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
    assert started.json()["current_task"]["platform"] == "bilibili"

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
    assert started["status"] == "running"
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
    assert resumed["status"] == "running"
    assert resumed["job_id"] == "persisted-2"
