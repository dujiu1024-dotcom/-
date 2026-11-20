# 社交平台需求雷达

面向个人研究的本地多平台评论采集与需求发现工作台。使用独立浏览器会话，统一采集、存储、时间过滤和需求识别。

## 当前能力

- 本地需求情报工作台：侧边栏分区、情报总览、采集库、评论数据、需求分析、任务中心和系统设置；评论数据支持分页与平台 / 时间筛选。
- 采集库：按研究主题建立项目，在项目下保存关键词搜索、博主主页、指定内容等采集对象；支持启停、重复运行和删除采集配置，删除配置不删除历史评论。
- MediaCrawler 运行适配器：首次安装上游代码与依赖，按平台启动可见浏览器。
- 国内平台：小红书、抖音、快手、B站、微博、贴吧、知乎；扩展平台：今日头条、微信公众号文章、微信视频号、YouTube、Reddit、TikTok、X、Instagram、Threads、LinkedIn、Telegram、Quora，以及 Facebook / Pinterest 公共广告库。未实测连接器均明确标记状态。
- 任务方式：关键词搜索、指定内容链接 / ID、博主主页。\n- 需求发现：独立搜索策略（研究主题、关键词集合、平台范围）、发现批次、发现内容入库、人工审核后沉淀为长期追踪来源。发现批次会明确区分“已准备”与“平台实际执行”，未接入的搜索适配器不会伪报成功。
- 一级评论和可选二级回复，JSONL 导出，自动导入统一 SQLite。
- 评论时间与原帖时间分离，默认 72 小时窗口可在工作台配置（1–8760 小时）。
- 可选 Social Media Research CLI（MIT）扩展引擎，支持只读采集命令；采集结果尽可能归一化入库。
- 需求分析：OpenAI-compatible LLM 语义识别、需求分类、相似需求聚类；可选 Embeddings 向量聚类，无模型配置时本地规则降级。
- 需求强度 0–100 排序、跨平台证据汇总、小时 / 天趋势分析。
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

浏览器打开 http://127.0.0.1:8000 。也可以在工作台点击“安装 / 检查 MediaCrawler”。安装过程会从上游仓库克隆源码、安装其 requirements，并安装 Playwright Chromium 浏览器运行时。扩展平台可在工作台单独安装 Social Media Research CLI（需要额外 Python 依赖）。

## 第一次登录

1. 在工作台选平台、采集方式和关键词，启动任务。
2. MediaCrawler 打开可见浏览器；首次登录按平台要求扫码或人工验证。
3. 安装器会关闭 MediaCrawler 的 CDP 共享浏览器模式，并确保 `SAVE_LOGIN_STATE=True`。上游使用 `USER_DATA_DIR` 按平台代码持久化浏览器资料；例如小红书与抖音使用不同的 profile 目录。
4. 后续该平台任务复用自己的会话。失效时在可见浏览器中重新授权。
5. 采集结束后点击“导入采集结果并刷新”，查看评论和需求候选。

**重要：** 会话目录存在不代表账号仍处于登录状态。是否有效由平台页面实际验证；本项目不会绕过登录、验证码或访问控制，也不读取日常 Chrome 的默认个人资料。

## 数据口径

默认按可配置时间窗口（初始 72 小时）筛选评论自己的发布时间，不使用原帖发布时间替代。时间缺失、无法解析或时区不明确的评论不会进入窗口结果。评论按平台和评论 ID 去重。

## API

- `GET /`：本地工作台
- `GET /health`
- `GET /api/v1/collection/projects` / `POST /api/v1/collection/projects`
- `POST /api/v1/collection/projects/{id}/sources` / `PATCH /api/v1/collection/sources/{id}` / `DELETE /api/v1/collection/sources/{id}`
- `POST /api/v1/collection/sources/{id}/run`
- `GET /api/v1/engine/status`
- `POST /api/v1/engine/install`
- `GET /api/v1/sessions`
- `POST /api/v1/crawler/jobs`
- `GET /api/v1/crawler/jobs`
- `GET /api/v1/crawler/jobs/{id}/log`
- `DELETE /api/v1/crawler/jobs/{id}`
- `POST /api/v1/imports/exports`
- `GET /api/v1/settings` / `PUT /api/v1/settings/time-window`
- `GET /api/v1/comments?since_hours=72`（省略时使用配置窗口）
- `POST /api/v1/analysis/run`
- `GET /api/v1/demands`
- `GET /api/v1/analytics/trends?since_hours=720&bucket=day`
- `GET /api/v1/discovery/strategies?project_id=...` / `POST /api/v1/discovery/projects/{project_id}/strategies`\n- `POST /api/v1/discovery/strategies/{strategy_id}/runs`：生成发现批次及查询词清单（当前为 prepared，不代表平台已执行）\n- `GET /api/v1/discovery/results` / `POST /api/v1/discovery/runs/{run_id}/results`\n- `POST /api/v1/discovery/results/{content_id}/approve`：审核作者并沉淀为采集库来源\n- `GET /api/v1/collectors/reverse/status` / `POST /api/v1/collectors/reverse/install`
- `POST /api/v1/collectors/reverse/collect`
- `GET /docs`

## 开源项目参考与许可

- [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)：作为采集引擎独立安装。其仓库声明采用 NON-COMMERCIAL LEARNING LICENSE 1.1；个人非商业研究前请阅读并遵守完整许可。
- [media-crawler-easy](https://github.com/isheng-eqi/media-crawler-easy)：参考环境引导、扫码登录和结果校验，不复制其代码。
- [CreatorHub](https://github.com/3441293738/creatorhub)：仅参考工作台交互；未确认其许可，不复制代码。
- [Social Media Research CLI](https://github.com/ifccod/social-media-research-cli)：MIT，可选扩展引擎；只开放只读命令，不调用发帖、点赞、关注等操作。
- [OpenMediaCrawl](https://github.com/Tommyapi/OpenMediaCrawl)：MIT，备选 Node.js 采集引擎，当前列入平台能力评估，尚未接入主任务调度。
- [NewsCrawler](https://github.com/NanmiCoder/NewsCrawler)：GPL-3.0，适合微信公众号 / 今日头条等文章正文提取；不将正文提取误报为评论采集。

## 发现引擎当前边界\n\n当前实现完成搜索策略管理、查询批次、发现结果入库和来源审核闭环；批次生成只准备搜索清单，不等于已对各平台执行搜索。下一阶段需逐个平台接入搜索适配器，记录实际支持的搜索方式、分页、时间字段、授权状态和错误原因。搜索引擎索引、平台站内搜索和评论获取是不同能力，不能将其中任一项描述为“覆盖全网全部评论”。\n\n## 明确未完成 / 实测边界

- GitHub CI 不能验证真实平台账号登录、扫码、验证码、风控或评论分页。
- 国内七个平台仍需本机真实登录与评论分页验证；扩展平台 CLI 适配器需要逐个平台验证命令参数、字段映射和时间戳。
- 微信公众号当前登记文章、账号和搜索能力；不能保证可抓取文章评论，需尊重公众号评论接口可访问性。
- LLM 语义分析需在本机 `.env` 配置 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`；向量聚类另需 `LLM_EMBEDDING_MODEL`。不配置时为本地规则 / n-gram 降级，不等同于真正的 LLM 语义分析。
- 趋势基于已分析的评论证据；需要定期运行分析才能累积时间序列。
- 本版本一次只允许一个采集任务，避免无控制并发。
- 请遵守平台条款，低频采集，仅采集本人有权访问的公开内容。
