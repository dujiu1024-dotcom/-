# 社交平台需求雷达：本地架构

## 设计依据

- [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)：平台适配、扫码登录、搜索 / 指定内容 / 创作者采集、一级与二级评论和 JSONL 输出。本项目独立检出并通过命令行调用，不复制上游源码。
- [media-crawler-easy](https://github.com/isheng-eqi/media-crawler-easy)：参考环境检查、可见扫码登录、登录提醒、产物校验与报告工作流；不依赖其 Claude Code 专用 Skill。
- [CreatorHub](https://github.com/3441293738/creatorhub)：参考多平台统一工作台。当前未确认其代码许可，不复制其代码。

## 登录与浏览器隔离

MediaCrawler 上游提供 `SAVE_LOGIN_STATE` 与 `USER_DATA_DIR = "%s_user_data_dir"`，并在各平台浏览器实现中按平台代码建立持久化目录。CDP 模式另使用 `cdp_` 前缀目录。本项目安装时会将上游 `ENABLE_CDP_MODE` 关闭，并确保 `SAVE_LOGIN_STATE=True`，避免 CDP 连接到日常运行中的共享浏览器；采集器使用上游的持久化上下文，并按平台代码建立独立 profile。本项目不读取日常 Chrome 默认用户目录。

- 首次运行使用可见浏览器，用户自行扫码和完成人机验证。
- 不提交 Cookie、浏览器配置和用户数据目录。
- 目录存在只代表有历史会话文件，不代表登录仍有效。
- 会话失效后由用户重新授权；不破解验证码、不伪造登录、不绕过访问控制。

## 任务生命周期

`running → succeeded | failed`；当前限制单任务、最大并发 1。任务日志与导出目录独立。导入可重复执行，统一存储按平台和评论 ID 去重。

## 数据流程

`MediaCrawler JSONL → 字段映射 → 评论事实表 → 评论时间过滤（72h） → 需求候选 → 证据回溯`

原帖时间与评论时间分开保存。评论时间缺失或无法确认时不进入最近 72 小时结果。当前需求识别仍是规则基线，LLM 语义聚类与评分尚未完成。

## 本地运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m app.cli install-engine
uvicorn app.main:app --reload
```

打开 `http://127.0.0.1:8000`。首次安装需访问 GitHub 与 PyPI。点击安装后选择平台和关键词启动任务，在可见浏览器中扫码。采集完成后点击“导入采集结果并刷新”。

## 限制

- 登录有效性必须由真实平台页面验证，不能仅凭 Cookie 文件判定。
- 真实采集必须在用户本机、授权账号和当前网络环境验证；CI 不能代替。
- 今日头条与公众号不在当前 MediaCrawler 七个平台范围内，暂不作为可采集平台。
