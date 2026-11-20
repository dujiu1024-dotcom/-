PLATFORMS = {
    "xhs": {"name": "小红书", "engine": "MediaCrawler", "status": "可启动，待本机实测", "capabilities": ["search", "detail", "creator", "comments", "replies"], "region": "国内"},
    "douyin": {"name": "抖音", "engine": "MediaCrawler", "status": "可启动，待本机实测", "capabilities": ["search", "detail", "creator", "comments", "replies"], "region": "国内"},
    "kuaishou": {"name": "快手", "engine": "MediaCrawler", "status": "可启动，待本机实测", "capabilities": ["search", "detail", "creator", "comments", "replies"], "region": "国内"},
    "bilibili": {"name": "哔哩哔哩", "engine": "MediaCrawler", "status": "可启动，待本机实测", "capabilities": ["search", "detail", "creator", "comments", "replies"], "region": "国内"},
    "weibo": {"name": "微博", "engine": "MediaCrawler", "status": "可启动，待本机实测", "capabilities": ["search", "detail", "creator", "comments"], "region": "国内"},
    "tieba": {"name": "贴吧", "engine": "MediaCrawler", "status": "可启动，待本机实测", "capabilities": ["search", "detail", "comments"], "region": "国内"},
    "zhihu": {"name": "知乎", "engine": "MediaCrawler", "status": "可启动，待本机实测", "capabilities": ["search", "detail", "creator", "comments", "replies"], "region": "国内"},
    "toutiao": {"name": "今日头条", "engine": "reverse CLI", "status": "适配器已登记，需安装可选引擎验证", "capabilities": ["search", "detail", "comments"], "region": "国内"},
    "wechat_mp": {"name": "微信公众号", "engine": "NewsCrawler / reverse CLI", "status": "文章采集可用方向；评论采集能力受平台接口限制", "capabilities": ["article", "account", "search"], "region": "国内"},
    "wechat_channels": {"name": "微信视频号", "engine": "reverse CLI", "status": "适配器已登记，需安装可选引擎验证", "capabilities": ["feed", "export"], "region": "国内"},
    "youtube": {"name": "YouTube", "engine": "reverse CLI / YouTube Data API", "status": "适配器已登记，需安装可选引擎验证", "capabilities": ["search", "video", "comments", "replies"], "region": "海外"},
    "reddit": {"name": "Reddit", "engine": "reverse CLI / Reddit API", "status": "适配器已登记，需安装可选引擎验证", "capabilities": ["search", "post", "comments", "replies", "creator"], "region": "海外"},
    "tiktok": {"name": "TikTok", "engine": "reverse CLI / OpenMediaCrawl", "status": "适配器已登记，需安装可选引擎验证", "capabilities": ["search", "video", "comments", "replies", "creator"], "region": "海外"},
    "twitter": {"name": "X / Twitter", "engine": "reverse CLI / OpenMediaCrawl", "status": "适配器已登记，需安装可选引擎验证", "capabilities": ["search", "post", "replies", "creator"], "region": "海外"},
    "instagram": {"name": "Instagram", "engine": "reverse CLI / Instaloader", "status": "适配器已登记；公开内容优先，评论能力需验证", "capabilities": ["profile", "posts", "post"], "region": "海外"},
    "threads": {"name": "Threads", "engine": "reverse CLI", "status": "适配器已登记，需安装可选引擎验证", "capabilities": ["profile", "post"], "region": "海外"},
    "linkedin": {"name": "LinkedIn", "engine": "reverse CLI", "status": "适配器已登记，需安装可选引擎验证", "capabilities": ["profile", "post", "company"], "region": "海外"},
    "telegram": {"name": "Telegram", "engine": "reverse CLI", "status": "适配器已登记，需安装可选引擎验证", "capabilities": ["channel", "posts", "search"], "region": "海外"},
    "quora": {"name": "Quora", "engine": "NewsCrawler / reverse CLI", "status": "文章与问题内容方向已登记，评论能力需验证", "capabilities": ["search", "article", "profile"], "region": "海外"},
    "facebook_ads": {"name": "Facebook 公共广告库", "engine": "reverse CLI", "status": "适配器已登记，非评论采集器", "capabilities": ["ads_search", "ad_detail"], "region": "海外"},
    "pinterest_ads": {"name": "Pinterest 公共广告库", "engine": "reverse CLI", "status": "适配器已登记，非评论采集器", "capabilities": ["ads_search", "ad_detail"], "region": "海外"},
}

def list_platforms() -> list[dict]:
    return [{"code": code, **config} for code, config in PLATFORMS.items()]
