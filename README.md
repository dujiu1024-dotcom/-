# 社交平台需求雷达

面向个人研究的多平台公开内容与评论采集、统一存储和需求发现工具。

## 当前策略

- **采集底座**：通过适配器接入 MediaCrawler 的本地导出数据；不复制上游代码，避免版本漂移和许可混淆。
- **平台覆盖**：MediaCrawler 可覆盖的小红书、抖音、快手、B站、微博、贴吧、知乎，先统一导入；头条、公众号作为后续独立适配器。
- **管理与分析**：本项目自建任务、统一数据模型、72 小时窗口过滤、需求识别接口和证据链。
- **CreatorHub**：作为独立工作台参考，不把其整套应用复制进来。后续若验证出独有能力，再通过连接器集成。
- **不绕过登录、验证码、访问控制或平台限制**。使用本人授权账号、低频采集，并遵守平台规则。

## 快速启动

需要 Python 3.11+。

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\\Scripts\\activate
pip install -e ".[dev]"
cp .env.example .env
uvicorn app.main:app --reload
```

打开 http://127.0.0.1:8000/docs 查看 API。

## 导入 MediaCrawler 数据

1. 按 [上游项目文档](https://github.com/NanmiCoder/MediaCrawler) 在本机单独部署并登录。
2. 在上游明确开启评论采集，选择平台和采集范围。建议低频、小批量。
3. 将上游生成的 JSONL 或 CSV 文件放入本项目的 `data/imports/`。
4. 调用：

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/imports/mediacrawler" \
  -H "Content-Type: application/json" \
  -d '{"path":"data/imports/comments.jsonl","platform":"xhs"}'
```

导入器会尽可能识别常见字段名；不能确认评论发布时间的记录会保留为时间未知，不会被误判为 72 小时内。

## 核心 API

- `GET /health`：健康检查
- `GET /api/v1/platforms`：连接器及能力状态
- `POST /api/v1/imports/mediacrawler`：导入上游 JSONL/CSV
- `GET /api/v1/comments?since_hours=72`：查询近期评论
- `GET /api/v1/demands`：需求候选（当前为规则基线，LLM 识别待接入）
- `GET /docs`：交互式 API 文档

## 时间口径

“最近 72 小时”按**评论自身发布时间**计算，原帖发布时间单独保存。时间缺失、无法解析或时区不明确的记录标记为 unknown，不会被误判为 72 小时内。

## 目录

```
app/
  api/             FastAPI 路由
  collectors/      平台连接器与导入适配器
  services/        需求识别与时间过滤
  storage/         SQLite 数据访问
tests/             自动化测试
data/              本地数据（不提交）
docs/              融合与实测计划
```

## 说明

本仓库第一阶段先完成统一数据底座和可验证导入链路。不同平台页面、字段和登录流程经常变化；只有在目标机器、授权账号和真实样本上跑通后，才将某个平台标记为“实测可用”。GitHub CI 只验证本项目代码和测试，不代表已完成各平台在线采集验证。
