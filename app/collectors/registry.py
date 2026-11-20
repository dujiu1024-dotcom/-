PLATFORMS = {
    "xhs": {"name": "小红书", "engine": "MediaCrawler", "mode": "crawler", "status": "可启动，待本机实测"},
    "douyin": {"name": "抖音", "engine": "MediaCrawler", "mode": "crawler", "status": "可启动，待本机实测"},
    "kuaishou": {"name": "快手", "engine": "MediaCrawler", "mode": "crawler", "status": "可启动，待本机实测"},
    "bilibili": {"name": "哔哩哔哩", "engine": "MediaCrawler", "mode": "crawler", "status": "可启动，待本机实测"},
    "weibo": {"name": "微博", "engine": "MediaCrawler", "mode": "crawler", "status": "可启动，待本机实测"},
    "tieba": {"name": "贴吧", "engine": "MediaCrawler", "mode": "crawler", "status": "可启动，待本机实测"},
    "zhihu": {"name": "知乎", "engine": "MediaCrawler", "mode": "crawler", "status": "可启动，待本机实测"},
    "toutiao": {"name": "今日头条", "engine": None, "mode": "planned", "status": "待独立适配器"},
    "wechat": {"name": "微信公众号", "engine": "wechat_spider（专项评估）", "mode": "planned", "status": "待独立适配器"},
}


def list_platforms() -> list[dict]:
    return [{"code": code, **config} for code, config in PLATFORMS.items()]
