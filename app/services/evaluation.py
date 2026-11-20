from collections import defaultdict
from datetime import UTC, datetime, timedelta

from app.storage.db import connect

EVALUABLE_LABELS = ("valid_demand", "non_demand", "category_correction")


def _metrics(rows: list[dict]) -> dict:
    tp = fp = fn = tn = 0
    category_matrix: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    category_total = 0
    samples = []
    for row in rows:
        actual_positive = row["label"] in {"valid_demand", "category_correction"}
        predicted_positive = bool(row["is_demand"])
        if actual_positive and predicted_positive:
            tp += 1
        elif not actual_positive and predicted_positive:
            fp += 1
        elif actual_positive and not predicted_positive:
            fn += 1
        else:
            tn += 1
        actual_category = row.get("corrected_category")
        if actual_positive and actual_category:
            category_matrix[str(actual_category)][str(row["predicted_category"])] += 1
            category_total += 1
        samples.append({
            "target_id": f'{row["platform"]}::{row["comment_id"]}',
            "feedback_id": row["feedback_id"],
            "label": row["label"],
            "predicted_is_demand": predicted_positive,
            "predicted_category": row["predicted_category"],
            "corrected_category": actual_category,
            "is_correct": actual_positive == predicted_positive,
        })
    precision_denominator = tp + fp
    recall_denominator = tp + fn
    total = tp + fp + fn + tn
    return {
        "sample_count": total,
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "true_negative": tn,
        "precision": round(tp / precision_denominator, 4) if precision_denominator else None,
        "recall": round(tp / recall_denominator, 4) if recall_denominator else None,
        "accuracy": round((tp + tn) / total, 4) if total else None,
        "category_sample_count": category_total,
        "category_confusion_matrix": {
            "actual_categories": sorted(category_matrix),
            "predicted_categories": sorted({
                predicted for values in category_matrix.values() for predicted in values
            }),
            "matrix": {
                actual: dict(sorted(predictions.items()))
                for actual, predictions in sorted(category_matrix.items())
            },
        },
        "samples": samples,
    }


def evaluate_analysis(
    *,
    model_version: str | None = None,
    prompt_version: str | None = None,
    platform: str | None = None,
    since_hours: int | None = None,
    limit: int = 5000,
) -> dict:
    if limit < 1 or limit > 20000:
        raise ValueError("limit must be between 1 and 20000")
    clauses = [
        "f.target_type='comment'",
        "f.label IN ('valid_demand','non_demand','category_correction')",
    ]
    params: list[object] = []
    if platform:
        clauses.append("substr(f.target_id, 1, instr(f.target_id, '::') - 1)=?")
        params.append(platform)
    if since_hours is not None:
        if since_hours < 1 or since_hours > 8760:
            raise ValueError("since_hours must be between 1 and 8760")
        cutoff = (datetime.now(UTC) - timedelta(hours=since_hours)).isoformat()
        clauses.append("f.created_at >= ?")
        params.append(cutoff)
    if model_version:
        clauses.append("p.model_version = ?")
        params.append(model_version)
    if prompt_version:
        clauses.append("p.prompt_version = ?")
        params.append(prompt_version)
    where = " AND ".join(clauses)
    with connect() as conn:
        rows = conn.execute(
            f"""
            WITH ranked_feedback AS (
                SELECT f.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY f.target_type, f.target_id
                        ORDER BY f.created_at DESC, f.id DESC
                    ) AS feedback_rank
                FROM demand_feedback f
                WHERE f.target_type='comment'
            ),
            latest_feedback AS (
                SELECT * FROM ranked_feedback WHERE feedback_rank=1
            ),
            ranked_predictions AS (
                SELECT p.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY p.model_version,p.prompt_version,p.platform,p.comment_id
                        ORDER BY p.analyzed_at DESC,p.id DESC
                    ) AS prediction_rank
                FROM analysis_predictions p
            ),
            latest_predictions AS (
                SELECT * FROM ranked_predictions WHERE prediction_rank=1
            )
            SELECT f.id AS feedback_id,f.label,f.corrected_category,
                   p.platform,p.comment_id,p.is_demand,p.category AS predicted_category,
                   p.model_version,p.prompt_version,p.analyzed_at
            FROM latest_feedback f
            JOIN latest_predictions p
              ON f.target_id = p.platform || '::' || p.comment_id
            WHERE {where}
            ORDER BY p.model_version,p.prompt_version,p.platform,p.comment_id
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        item = dict(row)
        grouped[(item["model_version"], item["prompt_version"])].append(item)
    versions = []
    for (model, prompt), samples in sorted(grouped.items()):
        versions.append({
            "model_version": model,
            "prompt_version": prompt,
            **_metrics(samples),
        })
    return {
        "dataset": {
            "source": "latest human feedback on comment targets",
            "eligible_labels": list(EVALUABLE_LABELS),
            "feedback_is_append_only": True,
            "sample_count": len({row["feedback_id"] for row in rows}),
            "prediction_observations": len(rows),
            "limit": limit,
        },
        "filters": {
            "model_version": model_version,
            "prompt_version": prompt_version,
            "platform": platform,
            "since_hours": since_hours,
        },
        "versions": versions,
        "comparison": {
            "version_count": len(versions),
            "best_precision": max(
                (v for v in versions if v["precision"] is not None),
                key=lambda v: v["precision"],
                default=None,
            ),
        },
        "notes": [
            "仅使用每个评论对象最新的人工反馈；merge/split 等结构性标签不进入二分类评估。",
            "precision 的真值来自人工标注，不使用模型 confidence。",
            "类别混淆矩阵仅纳入提供 corrected_category 的人工正样本。",
            "只有分析运行后保存的版本化预测可参与对比；历史未留存版本预测不回填推测。",
        ],
    }
