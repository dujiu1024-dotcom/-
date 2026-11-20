# 评论查询性能基准

## 本地完整基准

在仓库根目录运行：

```bash
python -m benchmarks.benchmark_comments --sizes 10000 100000 500000 --repetitions 5 --output benchmark-results.json
```

每档会生成独立 SQLite 数据库，包含三个模拟平台、稳定评论 ID、时间分布和导入时间。基准记录：

- 首屏查询：`page_comments(limit=50, offset=0)`
- 全量 COUNT：`count_comments()`
- 平台 + 72 小时时间窗口筛选的首屏和 COUNT
- 首屏分页查询的 Python 峰值分配
- 最多 5,000 条流式读取的 Python 峰值分配
- SQLite 文件大小、Python/SQLite 版本及操作系统

所有耗时均在一次预热后取多次运行的中位数。性能门禁使用宽松的绝对阈值，避免把硬件波动误判为回归；内存趋势同时比较不同数据规模下首屏查询峰值分配。

## CI 性能回归门禁

PR CI 会运行 1万/10万/50万三档数据（每项 3 次测量），并对首屏、COUNT、筛选和内存趋势执行回归门禁。若只需快速本地冒烟，可使用：

```bash
python -m benchmarks.benchmark_comments --ci
```

GitHub Actions 的手动工作流以 5 次测量生成完整 JSON artifact，便于重复观察性能趋势。

## 指标解释

- `first_page_peak_bytes` / `stream_5000_peak_bytes` 是 Python `tracemalloc` 追踪到的峰值分配，不等同于进程 RSS 或 SQLite 原生内存。
- 基准使用合成数据和临时数据库，不连接真实平台、外部服务或生产数据库。
- JSON 报告中的阈值和每项 pass/fail 可作为性能回归证据。
