from __future__ import annotations

import argparse
import gc
import json
import platform
import statistics
import tempfile
import time
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

from app.storage import db

DEFAULT_SIZES = (10_000, 100_000, 500_000)
CI_SIZES = (10_000,)
DEFAULT_REPETITIONS = 5
THRESHOLDS = {
    "first_page_ms": 2_000.0,
    "count_ms": 5_000.0,
    "filtered_page_ms": 3_000.0,
    "filtered_count_ms": 5_000.0,
    "first_page_peak_bytes": 8 * 1024 * 1024,
    "stream_5000_peak_bytes": 32 * 1024 * 1024,
    "page_memory_growth_bytes": 4 * 1024 * 1024,
}


def populate_comments(size: int, database_path: Path, *, batch_size: int = 5_000) -> None:
    """Create a deterministic, reproducible synthetic comments dataset."""
    if size < 1:
        raise ValueError("size must be at least 1")
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    db.DATABASE_PATH = database_path
    now = datetime.now(UTC)
    platforms = ("xhs", "douyin", "weibo")
    insert_sql = """
        INSERT INTO comments(
            platform,comment_id,content_id,content_url,content_title,comment_text,
            comment_created_at,content_created_at,parent_comment_id,author_key,
            like_count,raw_json,imported_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    with db.connect() as conn:
        batch = []
        for index in range(size):
            created_at = now - timedelta(hours=index % (24 * 30))
            imported_at = now - timedelta(seconds=index)
            platform_name = platforms[index % len(platforms)]
            batch.append((
                platform_name,
                f"synthetic-{index:09d}",
                f"content-{index // 20:08d}",
                f"https://example.invalid/content/{index // 20}",
                f"Synthetic content {index // 20}",
                f"Synthetic comment body {index} requests an export feature",
                created_at.isoformat(),
                (created_at - timedelta(days=1)).isoformat(),
                None,
                f"author-{index % 10000:05d}",
                index % 100,
                "{}",
                imported_at.isoformat(),
            ))
            if len(batch) >= batch_size:
                conn.executemany(insert_sql, batch)
                batch.clear()
        if batch:
            conn.executemany(insert_sql, batch)


def _median_ms(operation: Callable[[], object], repetitions: int) -> float:
    operation()
    durations = []
    for _ in range(repetitions):
        started = time.perf_counter_ns()
        operation()
        durations.append((time.perf_counter_ns() - started) / 1_000_000)
    return round(statistics.median(durations), 3)


def _peak_bytes(operation: Callable[[], object]) -> int:
    gc.collect()
    tracemalloc.start()
    try:
        operation()
        _, peak = tracemalloc.get_traced_memory()
        return int(peak)
    finally:
        tracemalloc.stop()


def benchmark_dataset(size: int, database_path: Path, repetitions: int = DEFAULT_REPETITIONS) -> dict:
    original_database_path = db.DATABASE_PATH
    try:
        populate_comments(size, database_path)
        db.DATABASE_PATH = database_path
        filters = db.CommentFilters(since_hours=72, platform="xhs")

        def page_query():
            return db.page_comments(limit=50, offset=0)

        def count_query():
            return db.count_comments()

        def filtered_page_query():
            return db.page_comments(filters, limit=50, offset=0)

        def filtered_count_query():
            return db.count_comments(filters)

        page_ms = _median_ms(page_query, repetitions)
        count_ms = _median_ms(count_query, repetitions)
        filtered_page_ms = _median_ms(filtered_page_query, repetitions)
        filtered_count_ms = _median_ms(filtered_count_query, repetitions)
        page_peak = _peak_bytes(page_query)
        stream_limit = min(size, 5_000)
        stream_peak = _peak_bytes(
            lambda: sum(1 for _ in db.iter_comments(batch_size=250, limit=stream_limit))
        )
        with db.connect() as conn:
            actual_count = int(conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0])
        return {
            "dataset_size": size,
            "verified_rows": actual_count,
            "first_page_ms": page_ms,
            "count_ms": count_ms,
            "filtered_page_ms": filtered_page_ms,
            "filtered_count_ms": filtered_count_ms,
            "first_page_peak_bytes": page_peak,
            "stream_5000_rows": stream_limit,
            "stream_5000_peak_bytes": stream_peak,
            "database_size_bytes": database_path.stat().st_size,
        }
    finally:
        db.DATABASE_PATH = original_database_path


def evaluate_regression(results: list[dict]) -> dict:
    if not results:
        raise ValueError("at least one benchmark result is required")
    largest_page_peak = max(row["first_page_peak_bytes"] for row in results)
    smallest_page_peak = min(row["first_page_peak_bytes"] for row in results)
    checks = []
    for row in results:
        for metric in (
            "first_page_ms",
            "count_ms",
            "filtered_page_ms",
            "filtered_count_ms",
            "first_page_peak_bytes",
            "stream_5000_peak_bytes",
        ):
            checks.append({
                "dataset_size": row["dataset_size"],
                "metric": metric,
                "actual": row[metric],
                "maximum": THRESHOLDS[metric],
                "passed": row[metric] <= THRESHOLDS[metric],
            })
    checks.append({
        "dataset_size": "all",
        "metric": "page_memory_growth_bytes",
        "actual": largest_page_peak - smallest_page_peak,
        "maximum": THRESHOLDS["page_memory_growth_bytes"],
        "passed": largest_page_peak - smallest_page_peak
        <= THRESHOLDS["page_memory_growth_bytes"],
    })
    return {
        "passed": all(check["passed"] for check in checks),
        "thresholds": THRESHOLDS,
        "checks": checks,
    }


def run_benchmarks(
    sizes: tuple[int, ...] | list[int],
    repetitions: int,
    output_dir: Path,
) -> dict:
    if repetitions < 1:
        raise ValueError("repetitions must be at least 1")
    results = []
    for size in sizes:
        database_path = output_dir / f"comments-{size}.sqlite3"
        results.append(benchmark_dataset(size, database_path, repetitions))
    gate = evaluate_regression(results)
    return {
        "benchmark": "comment-query-performance-v1",
        "environment": {
            "python": platform.python_version(),
            "sqlite": db.sqlite3.sqlite_version,
            "platform": platform.platform(),
            "repetitions": repetitions,
        },
        "results": results,
        "regression_gate": gate,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Synthetic comment query performance benchmark")
    parser.add_argument("--sizes", nargs="+", type=int, default=list(DEFAULT_SIZES))
    parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
    parser.add_argument("--ci", action="store_true", help="Run the lightweight 10k CI gate")
    parser.add_argument("--output", type=Path, help="Write JSON report to this path")
    args = parser.parse_args()
    sizes = list(CI_SIZES) if args.ci else args.sizes
    with tempfile.TemporaryDirectory(prefix="demand-radar-benchmark-") as directory:
        report = run_benchmarks(sizes, args.repetitions, Path(directory))
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["regression_gate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
