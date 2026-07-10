# Hermes Kanban Runtime Kernel Phase 4G7

# Packaged Runtime Supervisor Daemon

## 1. 背景

Phase 4G6 已经证明 Runtime Kernel 可以在 production reducer、decision session、
compaction、worker recovery、capability policy 和 DB-backed supervisor lease 边界下完成
50+ active tick 的 synthetic long-run，并完成多轮真实 compaction provider 验证。

当前生产路径已经具备：

- `supervisor_runtime_tick()`；
- `supervise_runtime_jobs_once()`；
- 每个 runtime job 的 `advance_lock` 和 TTL takeover；
- materialization idempotency；
- worker crash/retry/recovery；
- liveness、consistency 和 compaction health observability。

但这些能力仍主要通过一次性 CLI poll 驱动。系统还缺少一个可直接由 systemd 或等价
进程管理器托管的常驻 supervisor。缺少这个运行形态时，operator 必须自行循环调用 CLI，
也无法统一获得进程健康、优雅停机、PID/state 文件和 restart soak 证据。

Phase 4G7 把已有 production supervisor tick 包装成可部署 daemon。它不增加新的 runtime
事实模型，也不让 daemon 内存成为 correctness 的一部分。

---

## 2. 目标

Phase 4G7 实现：

- 常驻 `runtime supervisor daemon`；
- bounded polling 和有界错误退避；
- graceful `SIGINT` / `SIGTERM` shutdown；
- 每进程唯一 supervisor owner；
- PID 文件和只包含运维状态的 state 文件；
- loopback-only liveness/readiness HTTP endpoint；
- systemd user service 模板；
- crash lease takeover；
- daemon restart 后不重复 materialization、decision 或 terminal fact；
- isolated daemon restart soak。

主运行路径：

```text
process start
      |
      v
claim PID file + create unique owner
      |
      v
start local health server
      |
      v
open fresh DB connection
      |
      v
discover bounded nonterminal jobs
      |
      v
supervisor_runtime_tick() under per-job DB lease
      |
      v
persist operational state snapshot
      |
      v
wait / bounded backoff / signal wakeup
```

---

## 3. 非目标

Phase 4G7 不实现：

- Dashboard Runtime UI；
- daemon 内部 job state cache；
- daemon 内部 completion、readiness 或 recovery reducer；
- 新的全局 leader-election 数据模型；
- 通过 PID 文件代替 DB lease；
- 远程可写管理 API；
- 自动加载 API key 到 state/log/health response；
- 路径级 worker sandbox；
- 多 provider 长时间真实 worker soak；
- worker internal subagent observability。

Dashboard 继续放在 Phase 4H。Phase 4G7 的 HTTP endpoint 只用于进程健康，不是 dashboard
API，也不得暴露 runtime private tables。

---

## 4. 不可违反的边界

### 4.1 DB 仍是唯一事实源

Daemon process memory 只能保存：

- process owner；
- poll 次数和耗时；
- 最近一次 poll 成功或错误；
- health/readiness 投影；
- shutdown signal。

以下状态必须继续从 DB 推导：

- runtime job state；
- graph revision；
- goal completion；
- readiness；
- worker recovery；
- materialization attempt；
- decision/patch history；
- checkpoint chain；
- capability authorization；
- supervisor job lease。

删除 state 文件、重启 daemon 或更换 host process 后，runtime 必须仍能从 DB 正确继续。

### 4.2 复用 production tick

Daemon 不复制 `advance_runtime_job()`、reconcile、ingest、ledger、readiness、decision、
compaction 或 materialization 顺序。每个 job 的推进必须调用现有
`supervisor_runtime_tick()`，并继续受 `advance_lock` 保护。

### 4.3 Provider 边界不变

Daemon 可以显式配置 `none`、`fake` 或 `real` decision provider，但：

- 默认必须是 `none`；
- `real` 必须显式提供模型源，或显式使用隔离后的 `--codex-config`；
- provider 仍然 no-tools、single-shot、proposal-only；
- provider 不能写 DB、创建 task、完成 job 或绕过 validator；
- API key 不得进入 PID/state 文件、health response 或日志。

### 4.4 PID 文件不是 runtime lock

PID 文件只防止同一 service instance 被误启动两次。多个有意部署的 daemon 可以使用不同
PID/state 文件共同处理同一 DB，最终互斥仍由每个 job 的 DB lease 保证。

### 4.5 每次启动使用唯一 owner

Supervisor owner 不能跨 daemon restart 复用。若新进程复用旧 owner，
`acquire_runtime_advance_lock()` 会把它视为同一 owner，并可能在旧进程仍运行时绕过 lease
互斥。

默认 owner 应包含 hostname、PID 和随机启动 nonce。State 文件可以记录 owner 供审计，
但新进程不得从 state 文件恢复 owner。

---

## 5. Daemon 配置

新增 CLI：

```bash
hermes kanban runtime daemon \
  --interval 5 \
  --limit 10 \
  --lock-ttl 60 \
  --health-host 127.0.0.1 \
  --health-port 8791
```

核心参数：

- `--interval`：正常 poll 间隔；
- `--limit`：每轮最多发现的 nonterminal job 数；
- `--lock-ttl`：每个 job advance lease TTL；
- `--max-consecutive-errors`：连续 poll 错误达到阈值后退出非零，由 service manager restart；
- `--error-backoff-max`：错误退避上限；
- `--pidfile`：PID 文件路径；
- `--state-file`：运维状态快照路径；
- `--health-host` / `--health-port`：本地健康端点；
- `--max-polls`：测试和 bounded soak 使用，`0` 表示常驻；
- `--provider` / `--model-provider` / `--model`：沿用现有 decision provider 参数；
- `--no-create-tasks`：诊断模式，不物化 Kanban task。

默认 PID/state 路径应由当前 Kanban DB 路径派生，避免隔离测试写入主 `HERMES_HOME`。

---

## 6. Polling 与错误策略

每轮 poll 必须：

1. 新建 DB connection；
2. 调用 `supervise_runtime_jobs_once()`；
3. 最多处理 `limit` 个 nonterminal jobs；
4. 关闭 connection；
5. 原子更新 state 文件；
6. 使用可被 shutdown event 唤醒的 wait。

新建 connection 的原因是避免一个永久 SQLite connection 成为 daemon 隐藏状态，也使 DB
替换、WAL recovery 和测试隔离边界更清晰。

Poll 错误不得直接修改 runtime job。Daemon 应：

- 记录结构化错误摘要，不记录 credential 或完整 provider response；
- 增加 `consecutive_errors`；
- 使用 bounded backoff；
- readiness 转为 false；
- 达到配置阈值后非零退出，让 systemd 等进程管理器接管 restart。

一次成功 poll，包括“当前没有 job”，都应清零连续错误计数。

---

## 7. Signal 与优雅停机

Daemon 主线程处理 `SIGINT` 和 `SIGTERM`：

- 设置 stop event；
- 唤醒 poll interval wait；
- 不启动新的 poll；
- 等待当前同步 tick 返回；
- 关闭 health server；
- 最后一次写入 `stopped` state；
- 只删除自己持有的 PID 文件。

Phase 4G7 不实现强行中断正在进行的 provider request。Provider timeout 必须由已有 provider
transport 参数约束，service manager 的 stop timeout 是最后外部边界。若进程被 `SIGKILL`
或 crash，当前 job lease 留在 DB，后续进程在 TTL 过期后 takeover。

---

## 8. PID 与 State 文件

### 8.1 PID 文件

PID 文件必须使用 exclusive create：

- 文件不存在：写入当前 PID；
- 文件存在且 PID 活跃：拒绝启动；
- 文件存在但 PID 已失效：替换 stale PID 文件；
- shutdown 时只有文件仍指向当前 PID 才删除。

PID 存活检查只是 service instance 防重，不参与 runtime correctness。

### 8.2 State 文件

State 文件使用临时文件 + atomic replace，至少包含：

```json
{
  "schema_version": 1,
  "status": "starting|running|degraded|stopping|stopped|failed",
  "owner": "runtime-daemon:host:pid:nonce",
  "pid": 1234,
  "started_at": 0,
  "last_poll_started_at": null,
  "last_poll_completed_at": null,
  "last_success_at": null,
  "poll_count": 0,
  "consecutive_errors": 0,
  "last_error": null,
  "last_result": null
}
```

`last_result` 只能保存 bounded aggregate，例如 job count、advanced count 和 skip reason counts，
不能把完整 provider response、worker log、decision request 或 API key 写入 state 文件。

---

## 9. Health 与 Readiness

HTTP server 默认只允许 loopback bind，并提供：

- `GET /health/live`：daemon process 正在运行且未完成 shutdown；
- `GET /health/ready`：至少成功完成一次 poll，最近成功时间未超出 freshness window，且未处于
  stopping/failed；
- `GET /health`：返回 bounded liveness/readiness 摘要。

Endpoint 不提供 mutation，不接受 job ID，不读取 runtime private table，不返回 provider request、
memory hint、checkpoint 正文或 secret。

Readiness 是运维状态，不是 runtime job 的 legal waiting reason。没有 active job 的成功 poll
仍然是 ready。

---

## 10. Lease、Crash 与 Restart 语义

### 10.1 正常退出

`supervisor_runtime_tick()` 在 `finally` 中释放当前 job lease。正常 signal 只阻止下一轮 poll，
不改变 reducer 顺序。

### 10.2 硬 crash

若进程在 tick 中崩溃：

- PID/state 文件可能残留；
- DB `advance_lock` 保留至 TTL；
- 新进程使用新 owner；
- lease 未过期时返回 `locked`；
- lease 过期后 takeover；
- materialization idempotency 和 patch revision validator 防止重复事实。

### 10.3 并行 daemon

两个 daemon 可以发现同一 job，但只有一个能获得该 job lease。另一个必须记录 bounded
`locked` skip，不得 busy-loop，也不得把 lock contention 视为 runtime failure。

### 10.4 长 tick 限制

Phase 4G7 MVP 依赖配置保证 `lock_ttl` 覆盖单次 production tick 的预期上限。使用 real
provider 时必须配置 provider timeout，并要求 `lock_ttl` 大于 provider timeout 加本地 reducer
余量。动态 lease heartbeat/renewal 不在 MVP 内；在加入该能力前，不得把无 timeout 的真实
provider 配置作为 production 推荐值。

---

## 11. Systemd Packaging

新增 user unit 模板，原则：

- `Type=simple`；
- `ExecStart=hermes kanban runtime daemon ...`；
- `Restart=on-failure`；
- stdout/stderr 进入 journal；
- PID/state 放在 `%t` 或显式 writable runtime directory；
- `KillSignal=SIGTERM`；
- 不在 unit 文件中内嵌 API key；
- provider credential 通过已有隔离配置或 service environment file 提供。

Unit 模板只负责进程生命周期。它不宣称 systemd PID 等于 runtime lease owner，也不允许
两个不同 service 复用同一 PID 文件。

---

## 12. 实现规划

### Step 1：Daemon core

- 新增独立 supervisor daemon module；
- 定义配置、operational state 和 poll result；
- 复用 `supervise_runtime_jobs_once()`；
- 每 poll 使用 fresh DB connection；
- 实现 bounded backoff 和 max consecutive error。

### Step 2：Process lifecycle

- 唯一 owner；
- PID claim/stale recovery；
- atomic state file；
- signal handler；
- graceful shutdown。

### Step 3：Health server

- loopback-only bind；
- `/health/live`、`/health/ready`、`/health`；
- bounded JSON；
- shutdown 时关闭 server thread。

### Step 4：CLI 与 packaging

- `hermes kanban runtime daemon`；
- provider/config 参数复用；
- systemd user unit；
- CLI help 和错误码测试。

### Step 5：Restart soak

- 独立 `HERMES_HOME` 和 Kanban DB；
- 启动 daemon 完成一次 poll；
- 模拟 stale PID/expired lease；
- 新进程以新 owner restart；
- 验证不重复 materialization/decision；
- 验证 health/state 无 secret；
- consistency 0 violations。

---

## 13. 测试要求

必须覆盖：

- PID exclusive claim 和 stale PID replacement；
- state 文件 atomic update；
- first successful poll 后 readiness=true；
- DB/poll error 后 readiness=false 和 bounded backoff；
- `SIGTERM`/stop event 不启动下一轮 poll；
- `max-polls` bounded exit；
- 两个不同 owner 不能同时 advance 同一 job；
- expired lease 可以被新 owner takeover；
- restart 不重复 materialization；
- restart 不重复 applied patch；
- terminal job 不被再次推进；
- health response 不包含 credential/provider payload；
- non-loopback health bind 被拒绝；
- CLI parser 和 systemd unit 命令有效；
- Runtime Kernel、CLI 和 observability API 回归通过。

---

## 14. 完成标准

Phase 4G7 MVP 完成必须满足：

- 中文阶段文档、roadmap 和 `AGENTS.md` 一致；
- daemon 只包装 production supervisor，不复制 reducer；
- correctness 不依赖 daemon memory、PID 或 state 文件；
- 默认 provider 为 `none`；
- graceful stop、PID/state 和 health/readiness 可用；
- systemd user service 模板可用；
- crash takeover 和 restart idempotency 有 deterministic test；
- 完成至少一次隔离 subprocess restart soak；
- soak 后 consistency 为 0 violations；
- credential scan 为 0 命中；
- 默认离线测试不需要真实 API key；
- 实现、测试、文档和验证事实作为一个阶段提交并推送。

---

## 15. 后续关系

Phase 4G7 完成后，下一阶段进入 Phase 4H Dashboard Runtime UI。Dashboard 应消费已有只读
observability API 和稳定的 supervisor/compaction/recovery 状态，而不是通过前端推断 runtime
truth。

更长时间真实 worker soak、多 provider soak、动态 lease renewal 和路径级 sandbox 仍是
production final 的后续 hardening，不应被 Phase 4H UI 掩盖。

---

## 16. 当前实现与验证结果

2026-07-11 已完成 Phase 4G7 MVP。

实现入口：

```text
hermes_cli/kanban_runtime_supervisor.py
hermes_cli/kanban.py
plugins/kanban/systemd/hermes-kanban-runtime-supervisor.service
tests/hermes_cli/test_kanban_runtime_supervisor.py
```

已实现：

- `hermes kanban runtime daemon`；
- fresh-connection bounded polling；
- bounded error backoff 和连续错误退出；
- 每进程唯一 owner；
- exclusive PID claim、stale PID replacement 和 owner-only cleanup；
- 0600 atomic operational state 文件；
- `SIGINT` / `SIGTERM` graceful shutdown；
- loopback-only `/health/live`、`/health/ready` 和 `/health`；
- provider payload 不进入 state/health，错误仅保存异常类型与 detail hash；
- `real` provider 强制 timeout，并校验 lease TTL 覆盖全部 retry window；
- systemd user service 模板及 package-data 收录；
- active lease contention、expired lease takeover、terminal skip 和 restart idempotency 测试。

隔离 subprocess restart soak 使用独立：

```text
HOME
HERMES_HOME
HERMES_KANBAN_DB
PID/state/report files
```

Soak 连续启动三个独立 daemon process：

1. 第一进程通过 fake provider 应用初始 graph patch；
2. 写入模拟 crash 的 expired lease 和 stale PID；
3. 第二进程使用新 owner takeover 并物化 node；
4. 第三进程再次 poll，验证没有重复 graph patch、decision 或 materialization。

结果：

```text
job_id: rjob_975edb91630e
process owners: 3 unique
applied patches: 1
kernel decisions: 1
materializations: 1
pidfile after exit: absent
consistency violations: 0
consistency warnings: 0
credential scan hits: 0
```

验证：

```text
Runtime/CLI focused suite: 263 passed
Runtime observability API: 1 passed
Phase 4G7 supervisor focused suite: 16 passed
systemd-analyze verify: passed
setuptools build_py package-data check: passed
py_compile: passed
git diff --check: passed
```

当前限制：

- 尚未实现运行中 lease heartbeat/renewal；
- real provider daemon 必须使用有界 timeout，并配置足够大的 lease TTL；
- 尚未完成数小时真实 worker + real decision provider daemon soak；
- health endpoint 是进程运维面，不替代 Phase 4H dashboard observability API。
