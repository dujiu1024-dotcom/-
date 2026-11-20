from pathlib import Path

from app.storage import db
from benchmarks.benchmark_comments import (
    THRESHOLDS,
    benchmark_dataset,
    evaluate_regression,
    populate_comments,
)


def test_synthetic_dataset_is_reproducible_and_queryable(tmp_path, monkeypatch):
    database_path = tmp_path / "synthetic.sqlite3"
    monkeypatch.setattr(db, "DATABASE_PATH", database_path)
    populate_comments(1_200, database_path, batch_size=137)

    assert db.count_comments() == 1_200
    assert len(db.page_comments(limit=50)) == 50
    assert db.count_comments(db.CommentFilters(platform="xhs")) == 400
    assert len(db.page_comments(db.CommentFilters(platform="xhs"), limit=25)) == 25


def test_benchmark_reports_latency_memory_and_database_size(tmp_path):
    result = benchmark_dataset(
        1_000,
        Path(tmp_path) / "benchmark.sqlite3",
        repetitions=1,
    )

    assert result["verified_rows"] == 1_000
    assert result["first_page_ms"] >= 0
    assert result["count_ms"] >= 0
    assert result["filtered_page_ms"] >= 0
    assert result["filtered_count_ms"] >= 0
    assert result["first_page_peak_bytes"] > 0
    assert result["stream_5000_peak_bytes"] > 0
    assert result["database_size_bytes"] > 0


def test_performance_regression_gate_rejects_threshold_breach():
    metrics = {
        "dataset_size": 10_000,
        "first_page_ms": THRESHOLDS["first_page_ms"] + 1,
        "count_ms": 10,
        "filtered_page_ms": 10,
        "filtered_count_ms": 10,
        "first_page_peak_bytes": 1024,
        "stream_5000_peak_bytes": 2048,
    }

    report = evaluate_regression([metrics])

    assert report["passed"] is False
    assert any(
        check["metric"] == "first_page_ms" and not check["passed"]
        for check in report["checks"]
    )
