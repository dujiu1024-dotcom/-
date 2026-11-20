import json
import urllib.error
import urllib.request
from typing import Any

from app.config import LLM_API_KEY, LLM_BASE_URL, LLM_EMBEDDING_MODEL, LLM_MODEL


class LLMUnavailable(RuntimeError):
    pass


def provider_status() -> dict[str, Any]:
    return {"configured": bool(LLM_API_KEY and LLM_MODEL), "base_url": LLM_BASE_URL,
            "model": LLM_MODEL or None, "embedding_model": LLM_EMBEDDING_MODEL or None,
            "semantic_mode": "llm" if LLM_API_KEY and LLM_MODEL else "local_fallback"}


def _post(path: str, payload: dict) -> dict:
    if not LLM_API_KEY:
        raise LLMUnavailable("未配置 LLM_API_KEY")
    request = urllib.request.Request(
        f"{LLM_BASE_URL}/{path.lstrip('/')}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise LLMUnavailable(f"LLM 请求失败：{exc}") from exc


def analyze_batch(comments: list[dict]) -> list[dict]:
    if not LLM_MODEL or not LLM_API_KEY:
        raise LLMUnavailable("未配置 LLM_API_KEY / LLM_MODEL")
    compact = [{"id": f"{r['platform']}::{r['comment_id']}", "text": r["comment_text"],
                "title": r.get("content_title"), "platform": r["platform"]} for r in comments]
    prompt = (
        "你是用户需求研究分析器。逐条分析评论，不要把原帖内容当作评论。只把表达真实问题、愿望、"
        "工具需求、购买意向或改进诉求的评论判为需求。排除寒暄、灌水、纯情绪、广告、无关争吵。"
        "不得臆造。返回 JSON 对象 {items:[{id,is_demand,category,need_summary,intent,confidence,semantic_text}]}"
        "。category 取工具需求/功能需求/问题求解/替代需求/效率需求/购买意向/其他。confidence 为 0 到 1。"
        "need_summary 用一句简体中文概括具体待解决任务。semantic_text 用于相似需求聚类，保留场景和目标。")
    data = _post("chat/completions", {"model": LLM_MODEL, "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": prompt},
                     {"role": "user", "content": json.dumps(compact, ensure_ascii=False)}]})
    try:
        items = json.loads(data["choices"][0]["message"]["content"])["items"]
        by_id = {item["id"]: item for item in items}
        return [by_id[f"{r['platform']}::{r['comment_id']}"] for r in comments
                if f"{r['platform']}::{r['comment_id']}" in by_id]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise LLMUnavailable(f"LLM 返回结构不符合约定：{exc}") from exc


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    if not LLM_API_KEY or not LLM_EMBEDDING_MODEL or not texts:
        return None
    data = _post("embeddings", {"model": LLM_EMBEDDING_MODEL, "input": texts})
    try:
        return [row["embedding"] for row in sorted(data["data"], key=lambda x: x["index"])]
    except (KeyError, TypeError) as exc:
        raise LLMUnavailable(f"Embedding 返回结构错误：{exc}") from exc
