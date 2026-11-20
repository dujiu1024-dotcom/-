from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.main import app
from app.models import CommentRecord
from app.storage import db
from app.services.evaluation import evaluate_analysis


def _comment(comment_id):
    return CommentRecord(
        platform="xhs", comment_id=comment_id, content_id=f"post-{comment_id}",
        content_url="https://example.test/post", content_title="测试",
        comment_text="需要批量导出", comment_created_at=datetime.now(UTC),
        content_created_at=None, parent_comment_id=None, author_key="u1",
        like_count=0, raw={"id": comment_id},
    )


def _prediction(run_id, comment_id, is_demand, category, model="model-a", prompt="prompt-v1"):
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO analysis_predictions(
                analysis_run_id,platform,comment_id,is_demand,category,
                model_version,prompt_version,analyzed_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (run_id, "xhs", comment_id, int(is_demand), category, model, prompt,
             datetime.now(UTC).isoformat()),
        )


def test_evaluation_precision_confusion_and_latest_feedback(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "evaluation.sqlite3")
    for cid in ("a", "b", "c", "d"):
        db.upsert_comment(_comment(cid))
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO analysis_runs(
                started_at,provider,status,model_version,prompt_version
            ) VALUES(?,?,?,?,?)""",
            (datetime.now(UTC).isoformat(), "local_fallback", "succeeded",
             "model-a", "prompt-v1"),
        )
    for cid, pred, category in [
        ("a", True, "功能需求"), ("b", True, "工具需求"),
        ("c", False, "其他"), ("d", False, "其他"),
    ]:
        _prediction(1, cid, pred, category)
    db.create_feedback(target_type="comment", target_id="xhs::a",
                       label="valid_demand", corrected_category="功能需求")
    db.create_feedback(target_type="comment", target_id="xhs::b",
                       label="non_demand")
    db.create_feedback(target_type="comment", target_id="xhs::c",
                       label="valid_demand", corrected_category="功能需求")
    db.create_feedback(target_type="comment", target_id="xhs::d",
                       label="non_demand")
    db.create_feedback(target_type="comment", target_id="xhs::d",
                       label="valid_demand", corrected_category="功能需求")

    version = evaluate_analysis()["versions"][0]
    assert version["sample_count"] == 4
    assert version["true_positive"] == 1
    assert version["false_positive"] == 1
    assert version["false_negative"] == 2
    assert version["true_negative"] == 0
    assert version["precision"] == 0.5
    assert version["category_confusion_matrix"]["matrix"]["功能需求"]["功能需求"] == 1


def test_evaluation_api_filters_versions_and_empty_dataset(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "evaluation-api.sqlite3")
    client = TestClient(app)
    empty = client.post("/api/v1/analysis/evaluate", json={})
    assert empty.status_code == 200
    assert empty.json()["dataset"]["sample_count"] == 0
    assert empty.json()["versions"] == []
    assert client.post("/api/v1/analysis/evaluate", json={"since_hours": 0}).status_code == 422
    assert client.post("/api/v1/analysis/evaluate", json={"limit": 20001}).status_code == 422


def test_analysis_run_records_positive_and_negative_predictions(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "analysis-predictions.sqlite3")
    db.upsert_comment(_comment("positive"))
    db.upsert_comment(_comment("negative"))
    from app.services import analysis
    monkeypatch.setattr(analysis, "provider_status", lambda: {
        "configured": False, "model": None, "embedding_model": None,
        "semantic_mode": "local_fallback",
    })
    monkeypatch.setattr(analysis, "classify", lambda text: "功能需求" if text == "positive" else "")
    with db.connect() as conn:
        conn.execute("UPDATE comments SET comment_text='positive' WHERE comment_id='positive'")
        conn.execute("UPDATE comments SET comment_text='negative' WHERE comment_id='negative'")
    result = analysis.run_analysis(since_hours=8760, limit=10)
    assert result["model_version"] == "local-rule-v1"
    assert result["prompt_version"] == "demand-analysis-v1"
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT is_demand FROM analysis_predictions ORDER BY comment_id"
        ).fetchall()
    assert [row["is_demand"] for row in rows] == [0, 1]



def test_evaluation_compares_model_and_prompt_versions(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "evaluation-versions.sqlite3")
    db.upsert_comment(_comment("same-comment"))
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO analysis_runs(
                started_at,provider,status,model_version,prompt_version
            ) VALUES(?,?,?,?,?)""",
            (datetime.now(UTC).isoformat(), "local_fallback", "succeeded",
             "model-a", "prompt-v1"),
        )
        conn.execute(
            """INSERT INTO analysis_runs(
                started_at,provider,status,model_version,prompt_version
            ) VALUES(?,?,?,?,?)""",
            (datetime.now(UTC).isoformat(), "local_fallback", "succeeded",
             "model-b", "prompt-v2"),
        )
    _prediction(1, "same-comment", True, "功能需求", "model-a", "prompt-v1")
    _prediction(2, "same-comment", False, "其他", "model-b", "prompt-v2")
    db.create_feedback(
        target_type="comment", target_id="xhs::same-comment",
        label="valid_demand", corrected_category="功能需求",
    )
    result = evaluate_analysis()
    assert result["comparison"]["version_count"] == 2
    by_version = {(v["model_version"], v["prompt_version"]): v for v in result["versions"]}
    assert by_version[("model-a", "prompt-v1")]["precision"] == 1.0
    assert by_version[("model-b", "prompt-v2")]["precision"] is None
    assert result["comparison"]["best_precision"]["model_version"] == "model-a"
