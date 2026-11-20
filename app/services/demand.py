import re
from collections import Counter
from app.storage.db import list_comments

PATTERNS = {
    "工具需求": r"有没有.{0,12}(工具|软件|插件|程序)|求推荐.{0,8}(工具|软件|插件)|有.{0,8}(工具|软件|插件)吗",
    "功能需求": r"能不能.{0,12}(支持|增加|实现|自动|批量)|希望.{0,12}(支持|增加|实现)",
    "问题求解": r".{1,20}(怎么办|怎么解决|如何解决|怎么弄|怎么做)",
    "替代需求": r"(替代品|平替|代替|免费的.{0,8}(软件|工具)|有没有免费)",
    "效率需求": r"(自动化|批量处理|自动整理|自动生成|省时间)",
    "购买意向": r"(多少钱|哪里买|怎么买|求链接|付费版)",
}
NEGATIVE = re.compile(r"^(哈哈哈+|顶|来了|支持|路过|赞|👍|😂|666+)$", re.I)


def classify(text: str) -> str | None:
    value = text.strip()
    if len(value) < 5 or NEGATIVE.match(value):
        return None
    for label, pattern in PATTERNS.items():
        if re.search(pattern, value, re.I):
            return label
    return None


def demand_candidates(since_hours: int = 72, limit: int = 100) -> list[dict]:
    comments = list_comments(since_hours=since_hours)
    output = []
    for row in comments:
        category = classify(row["comment_text"])
        if category:
            output.append({
                "platform": row["platform"],
                "category": category,
                "text": row["comment_text"],
                "comment_created_at": row["comment_created_at"],
                "content_url": row["content_url"],
                "content_title": row["content_title"],
                "comment_id": row["comment_id"],
                "evidence_count": 1,
                "confidence": "规则初筛，需人工复核",
            })
        if len(output) >= limit:
            break
    return output
