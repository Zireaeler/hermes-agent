# Hermes Kanban Runtime Kernel Phase 4G8

# SWE-EVO 真实长周期验证

## 1. 背景

Phase 4G1 至 Phase 4G7 已分别验证：

- 真实 decision provider 的 no-tools proposal path；
- 真实 compaction provider 的解析、validator、fallback 和 no-fallback candidate；
- 真实 Codex worker lane；
- worker timeout/crash 后的 Codex session resume；
- 50+ active tick synthetic long-run；
- packaged supervisor daemon、DB lease、PID/state、health/readiness 和 restart idempotency。

这些验证仍然是分段的。Phase 4G7 的 daemon restart soak 使用真实 OS process 和真实 SQLite
DB，但 decision provider 是 fake，worker 也不是长期真实 worker。此前真实 provider、真实 worker、
真实 compaction 和 continuity smoke 则是 bounded 场景，没有把所有能力放进一个真实、长期、由
外部 evaluator 判定结果的任务中。

此前考虑使用三个 SWE-bench Lite 单 issue 作为端到端任务，但这类任务通常可以由一个能力足够
的 Codex / Claude Code worker 在一个连续 session 内完成。它们可以评价 patch quality，却不能
充分证明 Decision Session、compaction、daemon restart、worker recovery、durable graph
expansion 和 independent verification 的价值。

Phase 4G8 改用 SWE-EVO 长期软件演进任务，并把进程边界本身定义为测试契约。

---

## 2. 核心判断

无法从任务描述语义上证明某项工作绝对不可能由一个强 worker 在一次连续 session 中完成。

因此 Phase 4G8 不以“看起来复杂”作为长期任务证明，而通过可观察、可复现的故障注入和独立
验证边界，使任务在运行事实上不能依赖一次连续对话完成。

必须出现的边界包括：

- supervisor daemon process restart；
- worker process interruption；
- Codex backend session resume；
- Decision Session compaction 后由新进程继续；
- implementation 与 verifier 使用不同 session/context；
- official hidden evaluator 独立于 worker 自报结果。

测试目标不是证明模型绝对不能一次写完代码，而是证明即使一次对话、一个进程或一个 worker
attempt 不可依赖，Hermes 仍能保持目标连续性、事实一致性、权限边界、恢复能力和最终完成能力。

---

## 3. 基准选择

### 3.1 主基准：SWE-EVO

Phase 4G8 使用 SWE-EVO 作为主任务来源：

```text
https://github.com/SWE-EVO/SWE-EVO
```

SWE-EVO 的定位是 long-horizon brownfield software evolution，而不是 isolated issue fix。
公开资料给出的基线特征包括：

- 48 个真实开源项目版本演进实例；
- 输入为高层 Software Requirements Specification；
- 平均跨约 21 个文件；
- 每个实例平均约 874 个测试；
- 使用真实项目版本历史构造 base/evolved codebase；
- GPT-5.4 + OpenHands 的公开结果约为 25% fix rate。

这些特征与 Runtime Kernel 的目标匹配：任务需要长期 goal tracking、多轮策略调整、真实恢复、
独立验证和跨进程上下文延续。

### 3.2 备选基准：FeatureBench

FeatureBench 可作为候选实例无法在当前环境完成 oracle qualification 时的替代来源：

```text
https://github.com/LiberCoders/FeatureBench
```

替换实例仍必须满足本阶段的 long-horizon、process-boundary 和 independent evaluator 门槛。
不能因为 FeatureBench 提供 fast/lite split 就退回单次短 worker patch smoke。

### 3.3 不采用 SWE-bench Lite 作为主任务

SWE-bench Lite 仍可用于 evaluator adapter 的快速校验，但不计入 Phase 4G8 的三项正式任务。

原因：

- 单 issue 常由一个 worker session 完成；
- gold patch 通常较小；
- 很难自然触发 compaction、recovery 和 structure escalation；
- 通过 hidden tests 主要证明 worker patch quality，而不是长期 runtime correctness。

---

## 4. 非目标

Phase 4G8 不实现：

- 新 planner/coder/reviewer/tester 固定阶段；
- 为了增加 node 数量而预拆 analysis、implementation、test、debug；
- 模型排行榜或多模型横向评测；
- 全量运行 SWE-EVO 48 个实例；
- 把 gold patch、reference solution 或 hidden test 正文注入 provider/worker；
- 通过直接修改 runtime DB 注入 evaluator 结果；
- 为让任务通过而增加 task-specific prompt hint；
- 用 synthetic receipt 替代真实 worker/evaluator evidence；
- 以 runtime `done` 替代 official evaluator resolved；
- Phase 4H Dashboard UI。

---

## 5. 三项任务选择

Phase 4G8 固定选择 small、medium、large 三档任务。候选实例为：

| 档位 | 候选实例 | 选择原因 |
|---|---|---|
| small | `pydantic__pydantic_v2.6.0b1_v2.6.0` | 较小但非局部修复，官方 oracle 包含 1 个 FAIL_TO_PASS 与 51 个 PASS_TO_PASS 测试 |
| medium | `dask__dask_2022.9.2_2022.10.0` | 多模块演进、较大测试面，适合 worker resume 和 compaction |
| large | `iterative__dvc_1.0.0a1_1.0.0a2` | 大范围版本演进，gold patch 超过 220 KB，官方 oracle 覆盖 68 个 FAIL_TO_PASS 与 242 个 PASS_TO_PASS 测试 |

公开第三方运行中生成的 patch 大小只能作为粗略分档信号，不能作为 official task ground truth，
也不能暴露给 provider。正式任务只有在完成 oracle qualification 后才锁定。

三个正式实例均已完成严格 oracle qualification。原 large 候选
`dask__dask_2023.3.2_2023.4.0` 在固定 gold revision 的多次运行中出现不同 PASS_TO_PASS
偶发失败，无法形成稳定 oracle，因此已淘汰。替代 DVC large 候选已由 qualification harness
完整执行 base/gold 并正式锁定。

最终 qualification 事实：

| 档位 | Base F2P | Base P2P | Gold F2P | Gold P2P | 结果 |
|---|---:|---:|---:|---:|---|
| small | 0/1 | 51/51 | 1/1 | 51/51 | qualified |
| medium | 0/44 | 2861/2861 | 44/44 | 2861/2861 | qualified |
| large | 0/68 | 242/242 | 68/68 | 242/242 | qualified |

### 5.1 分档不是只看 LOC

分档同时考虑：

- SRS 独立 acceptance item 数；
- official gold changed files；
- official test 数量和运行时间；
- 代码库规模；
- 模块耦合；
- 预期 worker wall time；
- 是否存在自然的 capability/workspace/verification boundary。

### 5.2 最低资格

三个正式实例都必须满足：

- 不属于单一显然局部的一行修复；
- 至少包含多个可独立验证的 requirement；
- base code 与 evolved target 均可被官方 evaluator 重建；
- gold patch 能通过 evaluator；
- base code 对 FAIL_TO_PASS 测试确实失败；
- PASS_TO_PASS 测试在 base 和 gold 上保持通过；
- 资源可在隔离环境中顺序运行；
- 不依赖无法获得的付费外部服务或私有 credential。

如果候选不满足，必须从 SWE-EVO 中按相同档位重新选择，不能降低门槛。

---

## 6. Oracle Qualification

真实模型运行前必须先完成 oracle qualification，且该过程不能调用 decision provider 或 worker。

每个实例依次验证：

```text
dataset metadata integrity
        |
        v
checkout exact base commit
        |
        v
build official evaluator environment
        |
        v
base: FAIL_TO_PASS fails
        |
        v
base: PASS_TO_PASS passes
        |
        v
apply gold patch outside worker workspace
        |
        v
gold: FAIL_TO_PASS + PASS_TO_PASS pass
        |
        v
record image size / test time / disk budget
```

Qualification 输出必须包含：

- `instance_id`；
- repository 和 exact base commit；
- evaluator image/digest；
- gold patch hash，不保存到 worker 可见目录；
- FAIL_TO_PASS/PASS_TO_PASS 数量；
- base/gold evaluator 结果；
- changed files/patch size 统计；
- image、checkout、dependency 和 artifact 大小；
- cold/warm evaluator 时间；
- qualification timestamp。

Gold patch 只用于证明 evaluator 有效。正式运行期间不得 mount、复制、检索或显示给 runtime、
provider、worker、memory candidate 或 decision checkpoint。

---

## 7. 什么才算真实完整流程

每项正式任务必须同时使用：

- 真实 RuntimeDecisionProvider；
- 真实 RuntimeCompactionProvider；
- 真实 Codex worker lane；
- packaged runtime supervisor daemon；
- 现有 Kanban dispatcher/worker materialization；
- 真实 worker heartbeat/session/receipt；
- official benchmark evaluator；
- DB-backed decision session、checkpoint、graph、ledger、lease 和 recovery。

以下任一情况出现，该任务不能标记为 real end-to-end passed：

- decision provider 使用 fake/replay；
- compaction 使用 deterministic fallback；
- worker evidence 是 synthetic；
- evaluator 结果由 worker 自报；
- test harness 直接更新 execution node、ledger、goal 或 job state；
- daemon 只运行一次 CLI poll 而没有真实 restart；
- worker 没有跨 process/session boundary；
- hidden evaluator 没有运行；
- runtime done 但 official evaluator failed。

---

## 8. 隔离要求

每个实例使用独立目录：

```text
phase4g8/<instance_id>/
  home/
  hermes-home/
  codex-home-seed/
  codex-homes/
    node-<execution_node_hash>/
  kanban.db
  workspace/
  evaluator/
  artifacts/
  service/
  reports/
```

必须隔离：

- `HOME`；
- `HERMES_HOME`；
- `CODEX_HOME`；
- `HERMES_KANBAN_DB`；
- Git workspace；
- worker logs；
- daemon PID/state；
- health port；
- systemd unit instance；
- evaluator cache/result；
- memory topics/candidates。

模型源可以从主 `.codex` 读取后复制到只包含配置和认证的 `codex-home-seed`，但必须：

- 只复制运行所需的 `base_url`、model 和 API key；
- 文件权限为 0600；
- 不修改主 `.codex/config.toml` 或 `auth.json`；
- 运行前后记录主配置 hash；
- 不复制主 session history；
- 不把 key 写入 DB、state、report、prompt、event 或日志。

`codex-home-seed/config.toml` 还必须显式启用 Phase 4G8 专用危险操作审查：

```toml
approval_policy = "on-request"
approvals_reviewer = "auto_review"

[auto_review]
policy = "<Phase 4G8 strict dangerous-operation policy>"

[features]
guardian_approval = true
```

隔离 seed 还必须包含 `rules/default.rules`。Auto-review 只处理已经形成的 approval request，不会旁路
观察每一条 shell command；因此必须由 Codex exec-policy 将风险命令确定性分类：提权、host service、
namespace/firewall、工具外网和 Git network 操作为 `forbidden`，`rm/chmod/chown/kill`、破坏性 Git
操作和 package environment mutation 为 `prompt`。`prompt` 请求才进入 auto-review；普通 `pytest`、
仓库读取和常规编辑不应产生额外 handoff。

该配置只写入隔离的 seed，并由 execution node 复制到各自的 `CODEX_HOME`；不得写入或修改主
`.codex/config.toml`。审查 policy 默认允许 workspace 内的常规检查、修改、构建和测试，拒绝凭证读取、
protected oracle/gold/evaluator 访问、提权与 namespace/firewall 操作、工具侧外网、workspace 外破坏和
关闭审计边界；目标或副作用不明确时必须拒绝。配置 preflight 和 `worker_started` event 必须记录
approval mode、reviewer、review policy version/hash 和 exec-policy version/hash，不记录 policy 正文或
认证信息。外层 bwrap 必须在可写 node `CODEX_HOME` 上将 `config.toml`、`auth.json` 和 `rules/` 重新
叠加为只读挂载，使 worker 可以保存 session history，但不能修改模型源、认证或审查规则。

Worker 的实际 `CODEX_HOME` 必须按 `execution_node_id` 派生。相同 node 的 retry/resume 复用同一
目录，以保留 backend session；新的 recovery、verification 或其他 execution node 必须使用不同
目录。Worker filesystem namespace 只挂载当前 node 的目录，不挂载 seed 或其他 node 的 session
目录。这样 durable resume 可以保留，但不同 runtime responsibility 之间不能读取隐藏对话历史。

---

## 9. Benchmark Integrity

### 9.1 Worker 不得看到答案

Worker input 只能包含：

- SRS；
- 允许公开的 test requirement 名称/描述；
- 当前 workspace；
- runtime node contract；
- capability footer；
- 当前 task/attempt evidence。

Worker 不得看到：

- gold patch；
- reference solution；
- evolved commit diff；
- hidden test source；
- 其他 agent 的 patch；
- benchmark result repository；
- PR/issue answer URL。

### 9.2 网络边界

依赖、镜像和 repository mirror 在正式计时前准备完成。Worker tool environment 不允许自由访问
GitHub、搜索引擎或 benchmark answer source。

模型 transport 仍需访问配置的 model `base_url`。如果当前 worker backend 不能把 model transport
network 与 worker tool network 分离，则必须使用网络 namespace、allowlist proxy 或等价机制只
允许 model endpoint。

如果只能依赖 prompt 约束而不能物理限制答案检索，该次运行可以记为 runtime integration
evidence，但不能记为 benchmark-comparable evidence，也不能满足 Phase 4G8 最终门槛。

### 9.3 Memory 隔离

正式任务默认不加载包含该 benchmark 实例答案的 workspace/global memory。新生成 candidate
可以记录抽象 runtime 经验，但不得记录 gold diff、hidden tests 或任务答案。

---

## 10. Runtime Graph 策略

Phase 4G8 继续遵守 delegation policy：

- 初始默认一个 coherent primary execution node；
- 不按 SRS 条目、文件、模块、传统阶段预拆多个 worker；
- inspection、local planning、implementation、test 和 debug 默认由 primary worker 连续负责；
- 只有真实 evidence 证明出现结构边界时才扩展 durable graph；
- worker 可以使用 inherited-capability 内的 ephemeral internal subagents；
- worker 不得直接创建 durable runtime node。

合法的 graph expansion 包括：

- primary worker 返回 `structure_request`；
- 独立 verifier 必须使用新 context/session；
- 不同 workspace/capability boundary；
- worker attempt 已达到可靠 runtime/context limit；
- hidden evaluator 暴露新的、尚未覆盖的 goal gap；
- human authority 或 credential boundary。

不合法的 expansion 包括：

- 因任务复杂而先创建 analysis/research/implementation/test/debug nodes；
- 每个 SRS item 一个 worker；
- 每个文件或模块一个 worker；
- 仅因为理论上可以并行；
- 为满足测试中的 node 数量而拆分。

真实任务不得被要求自然触发全部可选 runtime 分支。Phase 4G8 将断言分为两类：

- 强制断言：进程边界、DB continuity、独立 evaluator、compaction/recovery correctness、
  committed fact 幂等性和最终 evaluator 结果；
- 条件断言：`structure_request`、graph expansion 和 evaluator 首次失败一旦发生，就必须按
  合法 evidence path 被持久化、处理和恢复；未发生本身不构成真实任务失败。

需要必然覆盖的可选分支由受控 integration case 确定性触发，不能通过篡改 SWE-EVO 业务结果、
制造无意义 decision round 或诱导 provider 进行 speculative split 来满足测试计数。

---

## 11. 独立 Evaluator

Official evaluator 是 worker 之外的独立责任主体。

本节定义的是 SWE-EVO 提供 protected FAIL_TO_PASS / PASS_TO_PASS oracle 时的显式
external-oracle adapter，不是普通开发任务的默认 completion path。Phase 4G8 harness 必须在
goal contract 中显式设置 `verifier_required=true`，并配置
`verification_policy.mode=required_evaluator`。缺少这种独立 oracle 或明确审计责任的普通任务默认
采用 `worker_owned` verification，由 primary worker 完成实现、测试、debug 和本地验证；Runtime
不得仅为了重跑 worker 自写测试而创建 evaluator。

它必须：

- 使用 worker 未继承的全新 process/context；
- 对固定 candidate revision/commit/diff 运行；
- 使用 official hidden tests；
- 不接受 implementation worker 的隐藏推理或自报 verdict；
- 输出结构化 evaluator receipt；
- 保留完整原始日志到 artifact，runtime 只接收 bounded summary/reference。

Evaluator 结果不能直接写 runtime private tables。正确路径：

```text
official evaluator
        |
        v
dedicated evaluator task/run
        |
        v
structured terminal receipt
        |
        v
runtime evidence ingest
        |
        v
progress ledger / goal gap reducer
```

Evaluator lane 可以是 deterministic local backend，不要求额外 LLM，但必须经过 Kanban
task/run/receipt contract，不能由测试脚本直接把 goal item 改成 satisfied。

### 11.1 独立验证 provenance

当前 implementation worker 的 `verification.passed=true` 只能表示自报验证，不能满足
`verifier_required=true`。Phase 4G8 正式运行前，progress ledger 必须区分：

```text
implementation worker verification
        -> self_reported / implementation_verified

independent evaluator + fixed target + independent process/session
        -> independently_verified
```

`verifier_required=true` 只能由 `independently_verified` 或显式、可审计的 human waiver 满足。
Evaluator receipt 至少携带：

```json
{
  "producer_kind": "official_evaluator",
  "producer_node_id": "node_xxx",
  "producer_task_id": "task_xxx",
  "producer_session_id": "evaluator-session-xxx",
  "target_revision": "git:<sha>",
  "target_materialization_id": "mat_xxx",
  "target_evidence_ref": "receipt:implementation:attempt-2",
  "independent_from_session_id": "codex-thread-xxx"
}
```

本地 reducer 必须校验 producer kind、固定 target、node/task/materialization 关联和 session
独立性。仅把 evaluator 日志放入 artifact、仅创建 `node_type=verification`，或让 implementation
worker 自报 official tests passed，均不能提升为 `independently_verified`。

### 11.2 Evaluator 的确定性创建

独立 evaluator 是已知 completion policy，不依赖 Decision Provider 临场决定是否插入：

```text
implementation terminal receipt
        |
        v
freeze candidate revision
        |
        v
local verification policy checks verifier_required
        |
        v
create or activate evaluator node
        |
        v
normal materialization -> task/run/receipt -> ledger
```

Decision Provider 只处理执行中发现的未知结构边界。已知 verifier constraint 由本地 policy
确定性执行，且重复 reducer/tick 不得重复创建 evaluator node 或 evaluator task。

---

## 12. 强制进程边界

故障注入不能依赖随机 sleep。Harness 必须观察 DB event、task/run/session state 或 daemon state，
在精确触发条件成立后执行 signal/kill。

### 12.1 Small

必须覆盖：

- real decision provider 创建 primary node；
- primary materialization 后启动真实 worker；
- worker running 期间 graceful restart supervisor daemon；
- implementation 完成后由独立 evaluator session 验证；
- runtime 从 evaluator receipt 完成或重新打开 gap。

该任务的整体完成不能只依赖 implementation worker session，因为最终 verifier 必须独立。

### 12.2 Medium

除 small 覆盖外，必须：

- 在 worker 已产生 heartbeat 和 backend session id 后终止 worker process；
- 只终止测试 harness 启动并拥有的 worker process group，不影响用户 Codex session；
- recovery 创建新的 materialization attempt；
- 新 attempt resume 原 Codex session；
- 至少一次真实 Decision Session compaction；
- compaction 后由新 daemon process 继续。

Worker interruption 必须终止测试 harness 所拥有的 worker process group，而不只是 wrapper PID；
必须确认 Codex child 未成为 orphan，且不得影响用户自己的 Codex session。旧 attempt 在新 attempt
开始后提交的迟到 receipt 必须被识别为 stale，不能覆盖新 attempt 或重复提交 terminal fact。

基础设施恢复预算和 receipt 协议恢复预算必须独立计数。先前的 worker crash、timeout 或 lease
recovery 不得消耗后续 `receipt_missing` / `receipt_invalid` 的恢复额度。若 worker 已完成 workspace
修改但 terminal receipt schema 无效，Runtime 应保留 workspace 和 backend session，创建仅用于
receipt protocol recovery 的新 materialization，并明确要求同一 worker 输出合法
`runtime_worker_receipt_v1`；不得因此丢弃 candidate、伪造 receipt 或直接标记业务失败。
Receipt protocol recovery 尚未结束时不得向 Decision Provider 暴露为业务 `no_runnable` gap，也不得创建
新的 strategy/implementation writer。若旧版本已基于该错误状态创建 speculative recovery branch，resume
只能在 candidate revision 未变化且该分支尚无 terminal receipt/evidence 时将其审计为 `superseded`。

为避免长 session 反复复制旧 receipt 形状，Phase 4G8 可以使用一个严格限定的本地 candidate adapter：
仅当 worker 显式返回 `status=completed`、`outcome=implementation_ready`、全部结构化本地 verification
均为 passed、workspace revision 可复核、且 job 明确配置 `required_evaluator` 时，将其归一化为
`candidate_ready`。Adapter 必须写审计 event，并标记 independent evaluator 仍然 required；它不得适配
失败/不确定 verification、不得生成 `independently_verified`，也不得直接满足 goal completion。

### 12.3 Large

除 medium 覆盖外，必须：

- 至少两次 daemon process boundary；
- 一次为 worker-running 期间的真实 hard crash / `SIGKILL`，并在 lease expiry 后由新 owner takeover；
- 一次发生在 task terminal receipt 已存在但 node 尚未 ingest；
- 至少两个 accepted real checkpoints；
- 最终运行 official evaluator，并保留 resolved 或 failed 的完整结果。

如果 large 实例出现 `structure_request` 或 graph expansion，必须验证其 evidence 和 delegation
policy 合法性；未出现时不得为了测试计数强制扩图。如果第一次 evaluator 失败，runtime 必须重新
打开 gap 并继续；如果第一次直接通过，不得人为篡改 evaluator 结果制造第二轮。

Receipt-before-ingest 的精确触发条件是：

- Kanban terminal receipt 已持久化；
- runtime node 尚未 terminal；
- progress ledger 尚无该 receipt evidence；
- `node_completed` / `node_failed` terminal execution event 尚不存在。

恢复后允许重复读取，但 receipt ingest、ledger insert 和 terminal fact 的 committed 结果必须各
恰好一次。

### 12.4 受控 integration cases

正式 SWE-EVO 运行前必须用隔离 fixture 确定性覆盖：

- Control verifier：implementation 自报不能满足 `verifier_required`，独立 evaluator 可以满足，
  stale target evaluator receipt 被拒绝；
- Control structure：合法 `structure_request` 被持久化并驱动 evidence-backed expansion，非法
  speculative split 被 validator 拒绝；
- Control compaction：daemon production poll 使用 real provider，在固定低 threshold 下触发
  checkpoint，fallback 为 0，compaction 后新 daemon 不读取 old transcript。

这些 control cases 验证 runtime 分支，不计入 SWE-EVO task capability 结果。

---

## 13. 真实 Compaction 要求

Phase 4G8 必须把 real compaction provider 正式接入 daemon production poll，而不是在运行间隙
手工调用独立 CLI。

Daemon 配置必须支持：

- decision provider mode/model/profile；
- compaction provider mode/model/profile；
- compaction timeout/retry；
- no-fallback production validation mode；
- compaction health threshold；
- checkpoint chain validation。

实现必须形成显式配置链：

```text
daemon CLI/config
        -> RuntimeSupervisorDaemonConfig
        -> supervise_runtime_jobs_once
        -> supervisor_runtime_tick
        -> advance_runtime_job
        -> compact_decision_session
        -> selected RuntimeCompactionProvider
```

每次 compaction 必须记录 provider identity、model、profile hash、timeout、retry、fallback policy、
request/response reference 和 validator result。测试使用固定、较低的 token threshold 确定性触发
real compaction，不通过无意义 decision rounds 填充上下文。

正式三项任务中：

- deterministic fallback 必须禁用；
- provider error/parse failure/validator rejection 必须可审计；
- accepted checkpoint 必须由 real provider 生成；
- old compacted transcript 不得再次进入 provider input；
- stale/invalid checkpoint 必须被拒绝；
- checkpoint 不能恢复或覆盖 runtime truth。

如果 real compaction 失败，任务可以继续由 DB truth 恢复，但该实例不能满足 Phase 4G8
no-fallback 验收，必须修复通用问题后从干净环境重跑。

### 13.1 模型上下文与自动触发策略

Compaction 的生产触发条件必须基于即将进入下一次 decision provider 请求的
`projected_context_tokens`，不能基于包含 runtime audit 原文的数据库 segment 总大小。
模型上下文窗口是输入、已有上下文、reasoning 和输出共享的总预算，不得把它解释为纯输入额度。

当前正式验证使用的模型源为 `MySub2api/gpt-5.6-sol`，reasoning effort 为 `high`。Codex
runtime 在真实会话中上报的有效 `model_context_window` 为 `353400`；本机 Codex 配置中的手工
override 不能替代该运行事实。Phase 4G8 使用以下 provider/model profile：

```json
{
  "provider": "MySub2api",
  "model": "gpt-5.6-sol",
  "reasoning_effort": "high",
  "context_window_tokens": 353400,
  "compaction_trigger_ratio": 0.65,
  "reserved_output_tokens": 8192,
  "reserved_reasoning_tokens": 32768,
  "estimation_safety_tokens": 32768,
  "max_compaction_input_ratio": 0.55,
  "max_compaction_input_chars": 1000000,
  "max_single_entry_chars": 16000,
  "max_segment_entries": 200
}
```

该 profile 的自动触发点为 `353400 * 0.65 = 229710` tokens，compaction provider 输入硬上限
为 `353400 * 0.55 = 194370` tokens。比例、上下文窗口和 reserve 均必须可配置；不得把该模型
profile 写成所有 provider 的全局常量。生产默认优先使用比例触发，固定
`max_active_segment_tokens` 只允许作为显式兼容兜底或受控测试开关。Phase 4G8 control case 可以
使用固定低阈值确定性触发，但必须在报告中标记 `test_only_forced_threshold=true`。

触发计算至少包含：stable prefix、validated checkpoint、eligible short tail、当前 delta、下一轮
固定协议开销以及 reserve。`compaction_requested`、provider 调用审计和 rejected checkpoint
不能增加 eligible context，也不能单独触发下一次 compaction。

### 13.2 非递归输入与失败抑制

下列 audit entry 永远不能作为后续 compaction provider 的正文输入：

- `compaction_requested`；
- `compaction_provider_input`；
- `compaction_provider_output`；
- `compaction_fallback`；
- `compaction_event`；
- `checkpoint_created`；
- `checkpoint_rejected`。

普通 `provider_input` 也不能以完整 prompt 形式进入 compaction，只能投影为有界 metadata 和
引用。Compaction source selection 应优先使用 `delta_appended`、provider parsed result、validator
result、patch result、worker/evaluator evidence summary 和 human decision 等语义 entry，并对单条、
总 token、总字符和 entry 数量分别执行构造前预算。最终 rendered request 超预算时必须在本地返回
`compaction_input_budget_exceeded`，provider 调用次数必须为 0。

`compaction_provider_input` 只持久化 request/response ref、provider/model/profile、included entry
ids、included/omitted count、token/character estimate 和预算结果。不得把完整 messages、rendered
request 或旧 compaction prompt 写回 active segment。完整原文若因受控诊断确需保存，只能进入
有大小上限、root-only、不会被 provider read path 读取的外部 artifact。

Rejected compaction 必须记录 eligible-content fingerprint。相同 fingerprint 在没有新 decision、
evidence、validator 或 human fact 时不得重复调用 provider；compaction 自身产生的 audit entry 不算
新 fact。可配置 cooldown 只能作为第二层保护，不能替代 fingerprint 门槛。

### 13.3 失败传播与进程收割

Phase 4G8 harness 每轮必须先检查 supervisor process 和 root-only state file，再 dispatch 新 worker。
Supervisor 达到 fail-fast error budget 后，harness 必须立即：

1. 停止继续 materialize 或 dispatch；
2. 只终止带当前 `phase4g8_run_id` ownership marker 的 worker process group；
3. 等待并回收 owned child process；
4. 从 DB 重新读取 evaluator provenance、fixed target、ledger 和 continuity facts；
5. 写入 `infrastructure_invalid` 报告并立即退出。

Wrapper heartbeat 不能掩盖没有任何 `worker_codex_event` 的 backend stall。正式运行必须配置
Codex event startup/stall timeout；超时后按 owned process group 收割，并明确区分 provider transport
stall、worker timeout 和 task-quality failure。异常路径不得继续等待整个 case `max_wall_seconds`。

### 13.4 Task-quality 终止预算

真实任务不能因 official evaluator 持续失败而无限创建 recovery worker。Harness 必须在每次
`dispatch_once()` 之前统计 evaluator progress、structured failure signature、feedback consumption、
no-progress streak 和总资源使用。固定 unresolved evaluator attempt 数不得作为 task-quality 终止条件。

达到总 wall/token/cost 资源预算后必须：

1. 只有最新 feedback bundle 已由同一 implementation session 消费后，才允许停止 dispatch；
2. 停止本 case 的 daemon 与 owned worker；
3. 保留最新 fixed-revision evaluator result、open/reopened gap 和全部失败 evidence；
4. 生成正常 `run-report.json`，记录 attempt count、failure count、预算和 exhausted 状态；
5. 终止必须分类为 `runtime-correct / resource-exhausted`，前提是其他 runtime invariants 均通过；
   `no-progress streak` 只触发 anti-stuck/audit，不单独形成 task-quality 终止事实。

资源耗尽不是 `infrastructure_invalid`，也不能把 runtime job 伪造为 `done`。若最新 evaluator 已
resolved，应按成功路径完成，不能由历史失败覆盖最终有效证据。有 F2P/P2P 改善或 failure signature
变化时必须重置 no-progress streak；相同测试进入更深断言属于 signature 变化。

### 13.5 Evaluator remediation continuity

Implementation receipt 通过本地验证后进入 `candidate_ready`，而不是 `succeeded`。该状态仅表示固定
revision 可交给 required evaluator；它不满足 verifier-required goal。只有独立 evaluator 写入
`independently_verified` evidence 后，才形成最终 completion fact。Evaluator unresolved 后，同一 node
从 `candidate_ready` 回到 `ready` 并恢复原 backend session。

Official evaluator 返回有效 unresolved result 后，默认不得立即让 Decision Provider 创建新的 recovery
node。对于显式启用 `resume_target_session` 的 job，只要原 implementation node 的 workspace、
capability、contract、worker lane 和 Codex backend session 仍满足恢复条件，本地 reducer 必须将同一
node 重新置为 `ready`，并把受预算、已脱敏的 failure bundle 送入同一 backend session。

Failure bundle 必须区分“测试源码”与“测试结果”。Hidden test source、fixture、gold/test patch、protected
path 和完整 harness log 始终隔离；但 official evaluator 已经产生的失败 test id、对称 comparison
relation、安全标量调用条件、expected/actual、regex/input、emitted warning 与 exception summary 可以作为
标准化 outcome 反馈给 worker。Comparison 只表达诸如“左右值必须相等”的关系，不能把任一侧伪装成
期望值；branch condition 只允许脱敏后的标量 keyword，例如 `shuffle=False`，不能包含路径、URL、
credential 或任意代码。Condition 来自失败 test 的安全分支上下文，可能同时包含直接失败分支与应抛
异常的替代分支，不得宣称每项 condition 都是当前异常的直接调用参数。Evaluator 对
pytest 仅通过 `PYTEST_ADDOPTS=-vv` 提高 assertion diff 的显示完整度，不得修改 protected test command
或测试选择。Runtime 必须再次按 failed test id 做关联校验，只转发 allow-list 字段；全部 failed test id
和每项首个 structured diagnostic 必须保留，诊断可以分 batch 组织但不能因 case/总字符预算被丢弃。
Worker 收到的仍是 non-authoritative diagnostics，不能据此读取
protected artifact、扩大 capability 或硬编码单个测试值。

若同一 test 产生多个 pytest section，evaluator 必须为每个 official failed test 选择第一条可提取诊断，
重复 section 不进入 worker bundle。无关 failure、warning 或未计入 official result 的 section 不得进入
bundle。无法为某个 official failed
test 提取 bounded outcome 时，必须显式写入 `missing_test_ids`；不得静默退化成只有 failed-test list、却让
下游误以为诊断完整。

Evaluator 必须同时生成 `feedback_coverage`：`current_failure_complete` 表示本轮全部 official failed
test id 都有 bounded diagnostic；`extraction_incomplete` 表示至少一个 official failure 没有 diagnostic。
不得再使用 `budget_limited` 将固定槽位覆盖误报为当前失败集合完整。只有第一种状态可以进入 worker
remediation。`extraction_incomplete` 是 evaluator infrastructure failure：
不得恢复 worker 盲猜，不得消耗 task-quality retry budget，并必须尽快把 real case 终止为
`infrastructure_invalid`。

### 13.6 Evaluator 与 run artifact retention

Evaluator 在 bounded diagnostics、coverage、result hash、environment fingerprint 和 provenance 已构造
后，只有 coverage 为 `current_failure_complete` 才删除该次 protected evaluator run 的
`combined.patch`、`test_output` 和 harness raw log。若 coverage 为 `extraction_incomplete`，必须保留 raw
目录并记录 `retained_for_incomplete_feedback`，供本地 infrastructure diagnosis；该路径和 raw 内容不能
进入 worker prompt。其他清理异常同样必须保留 raw 并明确记录 cleanup error，不能静默丢失唯一失败证据。

每个 real case 在写最终报告或进入 retention cleanup 前，必须从 worker workspace 归档精确
`reports/candidate.patch` 与 `reports/candidate-evidence.json`。Evidence 至少包含 base commit、固定
workspace revision、patch SHA-256、字节数、changed files 和 `protected_oracle_included=false`。该归档只
包含 candidate workspace diff，不包含 hidden test/gold patch、evaluator raw、provider credential 或其他
protected path。

Fresh real case 启动前，harness 必须扫描同一稳定 `run_root/instance` 下已有 run。只有已经持久化
`reports/run-report.json` 且已归档 candidate evidence 的结束 run 才可压缩：保留整个 `reports/`，删除 workspace、Hermes DB、隔离
home、Codex session cache 和 service state，并写 `reports/retention.json` 记录删除项与字节数。没有最终
报告的 run 视为可能仍需 resume，不得清理。Qualification base/gold、source mirror 和共享只读 worker
toolchain 是可复用资产，不属于 per-run garbage。

每个 remediation candidate 仍由新的独立 evaluator node 验证，并固定到新的 materialization 和
workspace revision。Auto-remediation、restart 幂等、per-materialization evidence ref、预算耗尽后的
provider suppression 和 fallback 条件详见：
[Phase 4G8 Evaluator Remediation Loop](validation/phase4g8/evaluator-remediation-loop.md)。

---

## 14. 完成与判定

每项任务分别产生 runtime validation 和 end-to-end capability 两类结论，不能混为一个布尔值。

### 14.1 Runtime completion

- required goal items 有 sufficient evidence；
- verifier_required items 有独立 evaluator evidence；
- progress ledger verified；
- reducer 将 job 置为 done；
- 无 open required gap；
- consistency checker 通过。

### 14.2 Benchmark completion

- official evaluator patch applied；
- FAIL_TO_PASS 全部通过；
- PASS_TO_PASS 全部通过；
- instance status 为 resolved；
- evaluator artifact 可追溯到固定 candidate revision。

### 14.3 聚合结论

Phase 4G8 必须分别发布：

**Runtime Validation**

- 三项实例的 runtime invariants 均通过；
- task quality failure 可以记录为 `runtime-correct / task-failed`；
- 任一 runtime correctness failure 都会使该结论失败。

**End-to-End Capability Validation**

- 三项实例 official evaluator 均为 resolved；
- 只有 3/3 resolved 才标记 capability validation complete。

3/3 resolved 仍是进入 production capability baseline 的高标准目标，但单纯 task quality failure
不能否定已由同一次运行证明的 lease、recovery、compaction、evaluator provenance 和 consistency
invariants。

当 official evaluator failed 时，Runtime Validation 通过的前提是 runtime 如实保留 open/reopened
gap、可继续结构或显式 exhausted/failed 状态，并且没有 premature `done`、silent idle 或伪造
verified ledger；它不表示该 job 已完成。

以下状态均不算完成：

- runtime done / evaluator failed；
- evaluator passed / runtime ledger 未完成；
- worker 自报 tests passed，但 evaluator 未运行；
- partial fix rate；
- fallback checkpoint；
- consistency warning 被静默忽略。

---

## 15. 失败分类

Phase 4G8 报告必须区分三类失败。

### 15.1 Runtime Correctness Failure

例如：

- 重复 materialization/decision/patch；
- lease takeover 错误；
- worker receipt 丢失；
- terminal fact 被覆盖；
- restart 后 goal/gap/ledger 不一致；
- invalid checkpoint 污染 provider context；
- goal 未完成但 runtime idle/done；
- capability/validator 被绕过。

任何 runtime correctness failure 都是阶段阻断问题，即使 candidate patch 最终通过测试。

### 15.2 Task Quality Failure

Runtime 边界正确，但模型未完成 SRS 或 official tests 未通过。

这说明当前 provider/worker/delegation/context 策略不足。该实例不能计入 End-to-End Capability
Validation resolved，但可以计入通过的 Runtime Validation。不得通过放宽 validator、直接写
ledger 或注入 gold hint 修复。

### 15.3 Infrastructure Invalid

例如：

- official gold 本身无法通过；
- image/dataset 损坏；
- dependency source 不可用；
- disk/host crash；
- evaluator timeout 与 gold baseline 同样发生。

Infrastructure invalid 不算模型或 runtime 失败，但实例必须重新 qualification 或替换。

---

## 16. 重试规则

正式运行使用固定模型、固定 profile、固定 temperature 和固定任务输入。

允许：

- infrastructure invalid 后从干净环境重跑；
- 修复通用 Runtime Kernel bug 后从干净环境重跑全部受影响实例；
- worker infra crash 按 runtime recovery policy 自动 retry/resume；
- evaluator 对明确 transient infrastructure error 重试一次。

禁止：

- 人工读取 hidden test 后把源码、fixture、gold 信息或 task-specific 解题提示改写进 worker prompt；
- 手工告诉 worker gold 修改位置；
- 多次随机采样直到偶然通过却只报告最佳结果；
- 在失败 workspace 上手工补代码后继续；
- 删除 rejected decision/patch/attempt history。

每次正式 run 都必须保留，包括失败 run。

允许通过统一、受预算、可审计的 evaluator failure schema 返回断言 outcome；这不等于暴露 hidden test
source。该 schema 必须适用于所有任务，且不能包含测试正文、protected path 或完整 raw output。

---

## 17. 可观测指标

每项任务至少记录：

### 17.1 Task outcome

- official resolved/fix rate；
- FAIL_TO_PASS/PASS_TO_PASS；
- candidate changed files/patch size；
- wall time；
- evaluator time。

### 17.2 Runtime structure

- decision round count；
- applied/rejected patch count；
- node/materialization/attempt count；
- first patch runnable node count；
- worker handoff count；
- graph expansion reason/evidence；
- independent verifier target revision。

### 17.3 Continuity

- daemon process count/restart points；
- lease owners/takeover；
- worker PID interruption；
- backend session id/resume count；
- receipt-before-ingest recovery；
- duplicate fact count。

### 17.4 Context lifecycle

- decision segment count；
- compaction attempts/accepted/rejected；
- fallback count/streak；
- checkpoint chain status；
- old transcript exclusion；
- provider token/usage/cost。

### 17.5 Context 与编排效率

- `worker_input_tokens`；
- `worker_cached_input_tokens`；
- `worker_output_tokens`；
- `cache_hit_ratio`；
- `tokens_before_and_after_resume`；
- `context_reacquisition_count`；
- `worker_execution_wall_time`；
- `orchestration_wall_time`；
- `time_to_first_evidence`；
- `decision_provider_wait_time`；
- `evaluator_wall_time`；
- `nodes_per_goal_item`；
- `handoffs_per_goal_item`。

### 17.6 Correctness

- consistency violations/warnings；
- liveness violations；
- legal waiting reasons；
- goal reopen/resolve history；
- capability blocks/authorizations；
- credential scan hits。

### 17.7 实际能力过程记录

每个正式 case 除 `run-report.json` 外，必须同时生成：

- `capability-trace.json`：供聚合、对比和后续分析使用的结构化过程记录；
- `capability-trace.md`：供 operator 阅读的中文能力过程报告。

能力过程报告不是普通测试日志，也不能只重复最终 pass/fail。它至少必须回答：

- Decision Provider 最初创建了什么责任，为什么没有或为什么需要拆分；
- worker 实际检查、修改和验证了什么；
- worker 的本地判断与 independent evaluator 是否一致；
- evaluator 提供了什么固定 revision failure evidence；
- Runtime 如何创建 recovery responsibility，后续策略是否相对前一轮发生实质变化；
- 多轮 recovery 是收敛、重复失败还是预算耗尽；
- daemon、session、checkpoint、consistency 和 capability 边界是否保持正确；
- 最终是 runtime correctness failure、task quality failure 还是 resolved。

报告应保留可观察的 agent message、已完成命令、修改文件、标准化 receipt、evaluator result、
checkpoint audit 和 evidence ref，但不得包含 gold patch、受保护 evaluator 源码、隐藏推理、凭据或
其他 node 的私有 session。真实任务失败同样必须生成该报告，因为失败路径通常比成功数字更能说明
当前系统的诊断、恢复和收敛能力。

---

## 18. 测试矩阵

| Case | 强制覆盖 | 不应强制 |
|---|---|---|
| Offline qualification | dataset revision、base/gold oracle、manifest hash、gold isolation、资源预算 | 模型调用 |
| Control verifier | implementation 自报不能满足 `verifier_required`；独立 evaluator 可以满足；stale target 被拒绝 | SWE-EVO resolved |
| Control structure | 合法 `structure_request`、evidence-backed expansion、非法 speculative split 被拒绝 | 等待真实模型偶然触发 |
| Control compaction | real provider、固定低 threshold、fallback=0、old transcript exclusion、restart continuation | 依赖任务自然积累 checkpoint |
| Small SWE-EVO | one primary node、真实 worker、worker-running 时 graceful daemon restart、固定 revision evaluator | graph expansion、evaluator 首次失败 |
| Medium SWE-EVO | hard worker interruption、same backend session resume、新 attempt、至少一次 real checkpoint、新 daemon continuation | 必须拆成多个 worker |
| Large SWE-EVO | hard daemon crash、lease takeover、receipt-before-ingest restart、至少两个 real checkpoint、固定 revision evaluator | 必须出现 `structure_request`；必须首次 evaluator 失败 |
| Aggregate | runtime invariants、task quality、资源、token/cache、全部失败记录 | 只报告最佳 run |

---

## 19. 资源与运行策略

截至 2026-07-11，当前执行 host 清理后约有 26G 可用空间。SWE-EVO 全量 evaluator 公开说明
可能需要 50-80G Docker images，因此 Phase 4G8 必须顺序运行：

```text
qualify/run small
        |
        v
export immutable artifacts + remove image/workspace cache
        |
        v
qualify/run medium
        |
        v
export immutable artifacts + remove image/workspace cache
        |
        v
qualify/run large
```

每项开始前必须：

- 估算 image expanded size；
- 保证运行中至少保留 8G free safety margin；
- 不删除当前 Runtime Kernel worktree；
- 不删除活跃用户 Codex/VS Code/Hermes process 数据；
- 不并行保留三个 evaluator image；
- artifact 使用压缩和 hash；
- 运行后只删除可重建 checkout/image/cache，不删除审计报告。

若单实例无法在 26G 内保持安全余量，应停止并报告资源需求，不得通过删除主 `.codex`、活跃
worktree、部署数据或 runtime audit DB 强行腾挪。

---

## 20. 实现范围

正式 SWE-EVO 运行前有四个阻断项：

1. 独立 evaluator provenance 与 `verifier_required` completion invariant；
2. real compaction provider 的 daemon production wiring；
3. worker tool network 与模型 transport network 的物理隔离；
4. 修订后的强制/条件断言、双结论和 fault trigger state machine。

Phase 4G8 worker 的工具边界由外层 netns、nft allowlist、非特权 UID、`no-new-privs` 和
capability drop 强制实施。Codex 内层 sandbox 使用 `danger-full-access`，避免嵌套 `bwrap`
在外层 netns 中因无法配置 loopback 而阻断所有工具；这不会移除外层网络、身份和 protected
artifact 隔离。

内层 Codex 必须同时使用 `on-request + auto_review + exec-policy`。Exec-policy 先阻断 hard deny，或把
需要判断目标/范围的风险操作升级为 approval request；随后由独立的只读 reviewer session 旁路审查。
Reviewer 只能批准或拒绝当前请求，不能扩大外层 capability envelope。Auto-review 是
defense-in-depth，不是全量命令审计器，也不是授权事实源：未命中规则的命令仍由 Codex 本地启发式处理，
审查允许的操作仍必须通过 bwrap/netns/UID 的物理限制；审查超时、解析失败或不确定时不得回退为
`never` 或无条件执行。不得使用 `--full-auto` 或
`--dangerously-bypass-approvals-and-sandbox`，因为这两种模式会把 approval policy 强制降为 `never`。

正式 worker 必须能运行 base workspace 中的公开测试。Runner 必须先使用 official harness 渲染
evaluator 的测试前环境准备段，并在临时 official image container 中执行该准备段，再提取最终的
toolchain/conda environment。不能直接从 pristine image 提取，因为 official harness 可能在测试前执行
依赖 hot-fix，例如降级某个不兼容依赖。

Toolchain cache identity 必须同时包含 image content digest、环境准备脚本 hash、setup environment
hash 和 setup 后的 resolved environment fingerprint。每次 run 都必须重新执行 setup preflight；解析
结果未变化时复用只读快照，结果变化时创建新的内容寻址缓存。提取后必须删除指向 `/testbed`、
`/workspace` 等 image workspace 的 `.pth` 绝对路径，将目录转为
root-owned read-only，并比较临时 official container 与提取后 toolchain 的 Python 版本和非 workspace
package fingerprint。每次 official evaluator 也必须回报同一 fingerprint；不匹配时该次运行属于
infrastructure-invalid，不能计为 task-quality failure。

环境准备脚本、evaluator test command、test patch、gold patch、`/testbed` checkout 和 evaluator artifact
均不得进入 worker 可见目录。Worker 只能读取准备后的 toolchain 和不含敏感正文的审计 manifest。
公开 toolchain 不是 evaluator evidence，不能替代固定 revision official evaluator。

### Step 1：Qualification Harness

- 读取 SWE-EVO instance metadata；
- exact base checkout；
- gold/base evaluator qualification；
- 资源估算；
- 生成不含 gold 内容的 locked task manifest。

### Step 2：Real Daemon Provider Wiring

- daemon 接入 real compaction provider；
- 为 `MySub2api/gpt-5.6-sol high` 配置 `353400` token model profile、`0.65` trigger ratio 和
  `0.55` provider input hard limit；
- compaction source projection 排除全部 compaction audit 原文，并执行 token/character/entry 硬预算；
- rejected compaction 使用 eligible-content fingerprint 抑制相同输入重试；
- no-fallback validation mode；
- provider/compaction timeout 与 lease TTL 校验；
- service environment/config 生成；
- 从 locked official image 提取不含 `/testbed` 的只读公开测试 toolchain；
- state/log secret redaction。

### Step 3：Evaluator Lane

- dedicated deterministic evaluator task/run；
- fixed candidate revision；
- official evaluator invocation；
- bounded structured receipt；
- raw log artifact reference；
- runtime evidence ingest；
- 本地 completion policy 确定性创建/激活 evaluator；
- `independently_verified` provenance 校验；
- stale target、同 session 和 implementation 自报拒绝测试。

### Step 4：Fault Injector

- DB/event-driven trigger；
- daemon graceful restart；
- daemon hard crash/expired lease takeover；
- worker-owned PID termination；
- backend session resume validation；
- receipt-before-ingest restart；
- daemon fail-fast 后 owned worker 收割和 harness 快速退出；
- wrapper heartbeat 存在但无 `worker_codex_event` 的 stall detection；
- 不影响用户 session/process。

### Step 5：Offline Tests

- qualification parser/manifest；
- gold isolation；
- evaluator receipt path；
- fault trigger state machine；
- daemon real compaction config；
- no direct DB mutation；
- report/credential redaction；
- deterministic fixture 只验证 harness，不计正式结果。

### Step 6：Controlled Integration Cases

- Control verifier 确定性验证 independent provenance completion invariant；
- Control structure 确定性验证 structure request、合法 expansion 和 speculative split rejection；
- Control compaction 使用真实 provider 和 daemon poll，验证 no-fallback checkpoint 与 restart；
- control result 与 SWE-EVO capability result 分开报告。

### Step 7：Worker Receipt Contract

Worker materialization prompt 必须明确 `structure_request`：

- optional；
- 只允许出现在 terminal receipt；
- 与 `verdict` 正交；
- `reason_type` 使用 delegation policy 枚举；
- discovered gap 必须携带 evidence refs；
- 不能直接 mutate graph。

Malformed `structure_request` 不得产生 graph mutation；原始 worker artifact 必须保留，validator
应拒绝该字段或整份 receipt，并提供可审计原因。

### Step 8：Sequential Real Runs

- small qualification + real run；
- medium qualification + real run；
- large qualification + real run；
- 每项完成后审查结果和磁盘；
- 最终聚合 3-task report。

---

## 21. 验收标准

### 21.1 Runtime Validation 完成条件

- 三项正式任务均通过 oracle qualification；
- 三项均使用真实 decision provider；
- 三项均使用真实 Codex worker；
- 三项均使用真实 compaction provider；
- 三项均发生预定 daemon process boundary；
- medium/large 均发生 worker interruption 和 backend session resume；
- large 覆盖 receipt-before-ingest restart；
- implementation 和 evaluator 使用独立 process/session；
- 三项均运行 official evaluator，并保留 fixed revision 的 resolved/failed 原始结果；
- evaluator failed 时 runtime 不得 premature `done` 或 silent idle；
- evaluator 连续失败达到预算时停止新 recovery dispatch，并输出
  `runtime-correct / task-failed`，不得误报 `infrastructure_invalid`；
- implementation 自报 verification 不得满足 `verifier_required`；
- evaluator provenance、fixed target 和 stale target rejection 均通过；
- deterministic compaction fallback 为 0；
- checkpoint chain 全部 valid；
- duplicate decision/patch/materialization/terminal fact 为 0；
- consistency violations 为 0；
- consistency warnings 为 0；
- goal 未满足时没有 silent idle/done；
- credential scan 为 0；
- 主 `.codex` config/auth hash 未变化；
- 三项 worker 均通过隔离 auto-review/exec-policy 配置 preflight，实际 lane 使用 `on-request`，且不存在
  `--full-auto`/bypass；
- hard-deny command 被 exec-policy 本地拒绝，prompt command 确实进入 auto-review；普通公开测试命令
  不被无条件送审；
- `config.toml`、`auth.json` 和 `rules/` 在 worker filesystem namespace 中不可写；
- auto-review 失败或拒绝不会扩大 capability，也不会绕过外层 filesystem/network 隔离；
- 不同 execution node 的 `CODEX_HOME` 相互隔离，同一 node 的 retry/resume 可复用；
- 所有失败尝试、recovery、validator rejection 和 fallback audit 均保留；
- 每项正式任务均生成中文 `capability-trace.md` 和结构化 `capability-trace.json`，并能区分
  worker 自报、本地验证、official evaluator、recovery 收敛性与最终双重结论；
- Control verifier / structure / compaction 均通过；
- 文档、实现、qualification、真实报告、测试和提交作为一个阶段统一交付并推送。

### 21.2 End-to-End Capability Validation 完成条件

- 三项 official evaluator 均为 resolved；
- 三项 FAIL_TO_PASS 和 PASS_TO_PASS 均全部通过；
- 三项 runtime job 均由 reducer/ledger/evidence 正常完成；
- evaluator artifact 均可追溯到各自固定 candidate revision。

三项任务只要有一项未 resolved，就不能写成 End-to-End Capability Validation complete，必须
准确记录 task quality failure。此结果不自动推翻已通过的 Runtime Validation，也不得把
`runtime-correct / task-failed` 冒充 production capability complete。

---

## 22. 后续关系

Phase 4G8 是 Phase 4H Dashboard Runtime UI 之前的 production validation gate。

只有在真实长周期任务证明：

- daemon 可以跨进程继续；
- worker 可以跨 attempt/session 继续；
- compaction 可以跨 segment 继续；
- evaluator 可以通过正常 evidence path 驱动 gap/completion；
- DB truth、goal、ledger、graph 和 checkpoint 始终一致；

之后，Dashboard 才有稳定、真实的长期运行状态可以展示。

Phase 4H 不得通过 UI 隐藏 Phase 4G8 暴露的 task quality、recovery、compaction、capability 或
consistency 问题。

---

## 23. 2026-07-16 当前验证事实

Medium `phase4g8-medium-c1e87ae82e` 已完成并 resolved：

- Runtime Validation：通过；
- End-to-End Capability Validation：通过；
- 7 次 official evaluator FAIL_TO_PASS：`34 -> 38 -> 40 -> 40 -> 40 -> 40 -> 44 / 44`；
- 7 次 PASS_TO_PASS：始终 `2861/2861`；
- 前 6 个 unresolved result 均保留全部 failed IDs，diagnostic coverage 为
  `current_failure_complete`；
- 6 个 feedback bundle 均先由同一 Codex worker session 消费，再形成下一 candidate；
- 1 个有效 implementation node、8 个 materialization attempts、同一 Codex thread，
  `resume_count=7`；
- worker hard interruption、daemon restart、real compaction、fixed revision evaluator 和 same-session
  remediation 均有真实证据；
- consistency violation/warning、duplicate terminal/ledger fact、compaction fallback、credential scan 均为 0；
- final candidate 为 28 个 changed files、47333 bytes，protected oracle included 为 false；
- WebSocket transport 为 upgrade/101 `18/18`、failure `0`、HTTP fallback `0`。

本次运行修复了两个状态机缺口：required evaluator selector 未接受合法的
`partial + candidate_ready` evidence；remediation reopen SQL 未包含 `candidate_ready`。旧代码已在 DB 中
形成一次 session interrupted/node candidate_ready 的半迁移状态，恢复前通过严格断言只回滚该 session
状态，再由修复后代码重新调度。因此该结果是 resolved 的诊断性 resumed evidence，不应描述为 Runtime
代码从启动到结束完全冻结的 release-grade clean run。

Evaluator 在第 3-6 轮连续返回相同 4 个 failure/signature，`no_progress_streak` 一度达到 3，但第 7 轮
仍然 resolved。该事实否定了把 streak 或固定 evaluator attempt count 作为硬 task-quality gate；它们只
用于 anti-stuck 和 observability，Phase 4G8 的硬 operational guard 保持为总 wall/token/cost budget。

历史 Medium `phase4g8-medium-85eef83bdd` 仍保留为旧 `budget_limited`、固定 3 次 attempt 的失败基线；其
最新 feedback 未被 worker 消费，不能作为 single-worker capability ceiling。Small
`phase4g8-small-6dafeda34c` 的 Runtime Validation 通过但 task 未 resolved，继续作为独立历史事实。

Large 按 operator 要求未运行。因此当前可确认 Medium 的 Runtime 和 End-to-End Capability Validation
均通过，但三任务 production capability gate 尚未满足，也不据此自动进入 Phase 4H。

详细证据见：

- `docs/validation/phase4g8/dask__dask_2022.9.2_2022.10.0/phase4g8-medium-c1e87ae82e/`；
- `docs/validation/phase4g8/small-medium-execution-flow.md`；
- `docs/validation/phase4g8/evaluator-remediation-loop.md`；
- `docs/validation/phase4g8/environment-parity-fix.md`。
