PLATFORMS = {
    "xhs": {"name": "小红书", "engine": "MediaCrawler", "mode": "import", "status": "待真实环境验证"},
    "douyin": {"name": "抖音", "engine": "MediaCrawler", "mode": "import", "status": "待真实环境验证"},
    "kuaishou": {"name": "快手", "engine": "MediaCrawler", "mode": "import", "status": "待真实环境验证"},
    "bilibili": {"name": "哔哩哔哩", "engine": "MediaCrawler", "mode": "import", "status": "待真实环境验证"},
    "weibo": {"name": "微博", "engine": "MediaCrawler", "mode": "import", "status": "待真实环境验证"},
    "tieba": {"name": "贴吧", "engine": "MediaCrawler", "mode": "import", "status": "待真实环境验证"},
    "zhihu": {"name": "知乎", "engine": "MediaCrawler", "mode": "import", "status": "待真实环境验证"},
    "toutiao": {"name": "今日头条", "engine": None, "mode": "planned", "status": "待开发"},
    "wechat": {"name": "微信公众号", "engine": "wechat_spider（专项评估）", "mode": "planned", "status": "待开发"},
}


def list_platforms() -> list[dict]:
    return [{"code": code, **config} for code, config in PLATFORMS.items()]
