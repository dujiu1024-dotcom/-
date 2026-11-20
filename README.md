# 社交平台需求雷达

面向个人研究的本地多平台评论采集与需求发现工作台。使用独立浏览器会话，统一采集、存储、时间过滤和需求识别。

## 当前能力

- 本地 Web 工作台：账号会话目录、引擎状态、采集任务、运行日志、结果导入。
- MediaCrawler 运行适配器：首次安装上游代码与依赖，按平台启动可见浏览器。
- 平台：小红书、抖音、快手、B站、微博、贴吧、知乎（均待本机真实环境验证）。
- 任务方式：关键词搜索、指定内容链接 / ID、博主主页。
- 一级评论和可选二级回复，JSONL 导出，自动导入统一 SQLite。
- 评论时间与原帖时间分离，严格按评论自身时间筛选最近 72 小时。
- 账号数据、浏览器资料、导出文件、日志全部留在本机，不提交 Git。

## 一次性安装

需要 macOS / Windows / Linux、Python 3.11+、Git、Chrome 或 Edge。建议至少 8GB 内存。

```bash
git clone https://github.com/dujiu1024-dotcom/-.git
cd ./-
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
python -m app.cli install-engine
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

浏览器打开 http://127.0.0.1:8000 。也可以在工作台点击“安装 / 检查 MediaCrawler”。安装过程会从上游仓库克隆源码、安装其 requirements，并安装 Playwright Chromium 浏览器运行时。

## 第一次登录

1. 在工作台选平台、采集方式和关键词，启动任务。
2. MediaCrawler 打开可见浏览器；首次登录按平台要求扫码或人工验证。
3. 安装器会关闭 MediaCrawler 的 CDP 共享浏览器模式，并确保 `SAVE_LOGIN_STATE=True`。上游使用 `USER_DATA_DIR` 按平台代码持久化浏览器资料；例如小红书与抖音使用不同的 profile 目录。
4. 后续该平台任务复用自己的会话。失效时在可见浏览器中重新授权。
5. 采集结束后点击“导入采集结果并刷新”，查看评论和需求候选。

**重要：** 会话目录存在不代表账号仍处于登录状态。是否有效由平台页面实际验证；本项目不会绕过登录、验证码或访问控制，也不读取日常 Chrome 的默认个人资料。

## 数据口径

最近 72 小时按评论自己的发布时间计算，不使用原帖发布时间替代。时间缺失、无法解析或时区不明确的评论不会进入 72 小时结果。评论按平台和评论 ID 去重。

## API

- `GET /`：本地工作台
- `GET /health`
- `GET /api/v1/engine/status`
- `POST /api/v1/engine/install`
- `GET /api/v1/sessions`
- `POST /api/v1/crawler/jobs`
- `GET /api/v1/crawler/jobs`
- `GET /api/v1/crawler/jobs/{id}/log`
- `DELETE /api/v1/crawler/jobs/{id}`
- `POST /api/v1/imports/exports`
- `GET /api/v1/comments?since_hours=72`
- `GET /api/v1/demands?since_hours=72`
- `GET /docs`

## 开源项目参考与许可

- [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)：作为采集引擎独立安装。其仓库声明采用 NON-COMMERCIAL LEARNING LICENSE 1.1；个人非商业研究前请阅读并遵守完整许可。
- [media-crawler-easy](https://github.com/isheng-eqi/media-crawler-easy)：参考环境引导、扫码登录和结果校验，不复制其代码。
- [CreatorHub](https://github.com/3441293738/creatorhub)：仅参考工作台交互；未确认其许可，不复制代码。

## 明确未完成 / 实测边界

- GitHub CI 不能验证真实平台账号登录、扫码、验证码、风控或评论分页。
- 今日头条、微信公众号尚无可用采集连接器。
- 当前需求识别仍为规则基线；LLM 语义识别、聚类、需求强度评分和趋势看板尚未完成。
- 本版本一次只允许一个采集任务，避免无控制并发。
- 请遵守平台条款，低频采集，仅采集本人有权访问的公开内容。
