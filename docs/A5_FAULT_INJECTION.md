# A5 故障注入与部署恢复手册

本手册对应 Issue #2 / A5。自动化测试使用临时 SQLite、伪造子进程状态和受控 PID 身份，不连接真实平台、不需要真实服务器或账号。

## 故障边界与预期行为

| 注入点 | 注入方式 | 预期结果 |
|---|---|---|
| Worker 租约在子进程启动前失效 | 续租返回失败 | 不调用启动器，不领取任务 |
| Worker 租约在子进程启动后失效 | 启动后第三次续租失败 | 批次暂停；任务标记 interrupted；PID、job_id、进程身份保留，禁止遗失子进程记录 |
| 服务重启 / 进程内 job registry 丢失 | 查询持久化 job_id 时返回不存在 | 当前任务标记 interrupted，批次 paused；不自动启动第二个采集进程 |
| PID 已不存在 | 进程身份探测无结果 | 允许用户恢复当前任务 |
| PID 被系统复用 | 当前 PID 的启动时间 + 命令行指纹与原记录不同 | 允许恢复；不得向该 PID 发送 terminate/kill |
| 原 PID 仍对应原采集进程 | 当前身份指纹与持久化指纹一致 | 拒绝恢复，要求先停止原进程 |
| 历史任务没有进程身份但 PID 仍存在 | 无法区分原进程与复用 PID | fail closed，拒绝自动恢复 |
| 子进程非零退出 | wait() 返回非零退出码 | job 状态 failed，保留 exit_code、错误分类与结束时间，不得显示 succeeded |

进程身份指纹由系统 ps 输出中的进程启动时间和命令行计算 SHA-256。指纹只用于比较，不记录 Cookie、Token 或环境变量。PID 仅作为定位线索，不作为进程身份。

## 部署建议

### Linux（systemd）

- 每个部署实例只运行一个 API 服务进程；不要用多 worker 模式启动 Uvicorn。发现任务的全局 SQLite 租约用于多实例保护，但 SQLite 文件仍应位于同一台主机的本地持久化磁盘。
- 建议启动命令：uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1。
- systemd 服务设置 Restart=on-failure、KillMode=control-group、TimeoutStopSec=30。停止服务时应先给采集子进程退出时间；不要使用会遗留孤儿进程的 KillMode=process。
- DATA_ROOT 必须位于持久化目录；备份 SQLite 数据库前暂停任务并执行一致性备份，不要只备份数据库而遗漏 logs/ 与 exports/。

### macOS（launchd）与 Windows

- launchd / Windows 服务同样建议单实例运行，并确保停止服务时能回收其启动的子进程。
- PID 身份探测依赖系统可用的 ps 命令；若探测不可用，当前实现返回“身份不可确认”，恢复逻辑按安全策略拒绝存在 PID 的任务，而不是猜测 PID 已退出。
- 不要用固定 PID 白名单，也不要仅凭 PID 存在/不存在以外的单一状态判断进程归属。

## 故障恢复操作

1. 先停止调度服务，避免 worker 在排查期间继续领取任务。
2. 查看任务中心中的 PID、job_id、错误和日志路径；必要时用 ps -p <PID> -o lstart= -o command= 检查进程启动时间与命令行。
3. 仅当原采集进程已经退出，或当前 PID 的身份指纹已确认与旧进程不同，才调用批次恢复接口。
4. 若原 PID 仍匹配，不要直接恢复；先通过任务中心停止任务或由管理员正常终止该进程，再重新检查。
5. 恢复后确认新的 attempt 编号、导出目录和日志路径；旧 attempt 的目录与证据保留，不覆盖。

## 本地验证

pip install -e ".[dev]"
pytest -q tests/test_fault_injection.py
ruff check app/services/crawler_engine.py app/services/discovery.py tests/test_fault_injection.py

CI 仅证明模拟故障边界通过，不代表真实平台登录、采集或服务器重启实测。真实环境验证需另行授权，本 Issue 调度明确跳过。
