# 平台采集器能力矩阵

> 状态区分：已接入启动适配器 ≠ 真实平台已验证。未验证适配器必须保留“待实测”标记。

| 平台 | 引擎 / 开源参考 | 可登记能力 | 说明 |
|---|---|---|---|
| 小红书、抖音、快手、B站、微博、贴吧、知乎 | MediaCrawler | 搜索、详情、创作者、评论（按平台能力） | 已有本地任务运行器，待逐平台实测 |
| 今日头条 | Social Media Research CLI | 搜索、文章、评论 | 外部 CLI 适配器，待安装和实测 |
| 微信公众号 | NewsCrawler、Social Media Research CLI | 文章、账号、搜索 | 不把文章抓取误报为评论采集；公众号评论需独立验证 |
| 微信视频号 | Social Media Research CLI | 分享内容读取 / 导出 | 不是公众号文章评论采集 |
| YouTube | Social Media Research CLI / YouTube Data API | 搜索、视频、评论 | 官方 API 需要项目与配额 |
| Reddit | Social Media Research CLI / Reddit API | 搜索、帖子、评论树 | API 权限与限流按官方规则 |
| TikTok | Social Media Research CLI / OpenMediaCrawl | 搜索、视频、评论 | 依赖公开数据可访问性与会话状态 |
| X / Twitter | Social Media Research CLI / OpenMediaCrawl | 搜索、帖子、作者 | 回复数据能力需实测 |
| Instagram | Social Media Research CLI / Instaloader | 公开主页、帖子 | 评论采集不标为已验证 |
| Threads | Social Media Research CLI | 公开主页、帖子 | 评论能力待验证 |
| LinkedIn | Social Media Research CLI | 公开帖子、公司资料 | 登录与访问范围限制明显 |
| Telegram | Social Media Research CLI | 公开频道、帖子、搜索 | 私有频道不在范围内 |
| Quora | NewsCrawler / Social Media Research CLI | 问题、文章、搜索 | 评论能力待验证 |
| Facebook / Pinterest 广告库 | Social Media Research CLI | 公共广告搜索、详情 | 作为需求/竞品补充，不是评论连接器 |

## 参考项目

- [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)：国内七平台评论采集基础；仓库许可为非商业学习许可，按其 LICENSE 使用。
- [Social Media Research CLI](https://github.com/ifccod/social-media-research-cli)：MIT，本地 CLI，覆盖 27 个平台上下文；仅调用只读命令，禁止把发帖、点赞、关注等写操作纳入采集工作流。
- [OpenMediaCrawl](https://github.com/Tommyapi/OpenMediaCrawl)：MIT，Node.js CLI，覆盖小红书、抖音、快手、B站、微博、TikTok、X，支持评论与二级评论；作为可选替代引擎，不与 MediaCrawler 同时自动运行。
- [NewsCrawler](https://github.com/NanmiCoder/NewsCrawler)：GPL-3.0，微信公众号、今日头条等文章正文提取；只作为文章内容解析器，不声称提供公众号评论接口。

## 合规和登录边界

只使用公开可访问内容、官方 API 或用户本人授权的本地登录会话。遇到验证码、访问拒绝、登录失效或平台限流时停止并提示人工处理，不自动破解、绕过或重试规避。
