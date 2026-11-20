from datetime import UTC, datetime

from app.services import analysis
from app.storage import db


def test_run_analysis_uses_sql_limited_batches(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "analysis.sqlite3")
    imported = datetime.now(UTC).isoformat()
    with db.connect() as conn:
        for index in range(9):
            conn.execute(
                """INSERT INTO comments (
                    platform, comment_id, comment_text, comment_created_at,
                    raw_json, imported_at
                ) VALUES ('xhs', ?, ?, ?, '{}', ?)""",
                (str(index), "普通文本内容", imported, imported),
            )

    monkeypatch.setattr(analysis, "provider_status", lambda: {
        "semantic_mode": "rules", "configured": False, "embedding_model": None,
    })
    result = analysis.run_analysis(since_hours=24, limit=4)

    assert result["comments_seen"] == 4
    with db.connect() as conn:
        run = conn.execute(
            "SELECT comments_seen,status FROM analysis_runs WHERE id=?",
            (result["run_id"],),
        ).fetchone()
    assert run["comments_seen"] == 4
    assert run["status"] == "succeeded"
