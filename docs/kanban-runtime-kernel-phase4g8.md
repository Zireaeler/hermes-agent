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
| small | `iterative__dvc_1.10.2_1.11.0` | 版本演进范围相对较小，但仍包含多个 requirement 和真实回归测试 |
| medium | `dask__dask_2022.9.2_2022.10.0` | 多模块演进、较大测试面，适合 worker resume 和 compaction |
| large | `conan-io__conan_2.0.14_2.0.15` | 大范围版本演进，适合多轮 decision、structure escalation 和长期恢复 |

公开第三方运行中生成的 patch 大小只能作为粗略分档信号，不能作为 official task ground truth，
也不能暴露给 provider。正式任务只有在完成 oracle qualification 后才锁定。

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
  codex-home/
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

模型源可以从主 `.codex` 读取后复制到隔离 `CODEX_HOME`，但必须：

- 只复制运行所需的 `base_url`、model 和 API key；
- 文件权限为 0600；
- 不修改主 `.codex/config.toml` 或 `auth.json`；
- 运行前后记录主配置 hash；
- 不复制主 session history；
- 不把 key 写入 DB、state、report、prompt、event 或日志。

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

- 把 hidden test failure 内容改写成 task-specific prompt；
- 手工告诉 worker gold 修改位置；
- 多次随机采样直到偶然通过却只报告最佳结果；
- 在失败 workspace 上手工补代码后继续；
- 删除 rejected decision/patch/attempt history。

每次正式 run 都必须保留，包括失败 run。

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

### Step 1：Qualification Harness

- 读取 SWE-EVO instance metadata；
- exact base checkout；
- gold/base evaluator qualification；
- 资源估算；
- 生成不含 gold 内容的 locked task manifest。

### Step 2：Real Daemon Provider Wiring

- daemon 接入 real compaction provider；
- no-fallback validation mode；
- provider/compaction timeout 与 lease TTL 校验；
- service environment/config 生成；
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
- 所有失败尝试、recovery、validator rejection 和 fallback audit 均保留；
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
