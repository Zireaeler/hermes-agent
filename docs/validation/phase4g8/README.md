# Phase 4G8 真实验证证据

本目录保存 Phase 4G8 正式真实任务的不可变报告副本。报告同时记录 Runtime correctness 与
任务解决能力，不以 worker 自报测试通过替代 independent evaluator 结果。

优先阅读：[Small / Medium 真实任务执行流程](small-medium-execution-flow.md)。该文档按执行阶段
整理任务目标、worker 行为、evaluator 结果、恢复路径和 Runtime 问题；本页及各 run 的
`capability-trace`、`run-report` 保留为审计证据。

环境与 worker 安全边界修复证据：
[Worker / Evaluator 环境等价性修复](environment-parity-fix.md)。其中同时记录隔离
`CODEX_HOME` 的 `on-request + auto_review + exec-policy` 旁路危险操作审查；该审查不替代外层硬隔离，
也不应被描述为全量 shell command 审计。

已完成的 remediation 机制与真实 smoke：
[Evaluator Remediation Loop](evaluator-remediation-loop.md)。该机制在 independent evaluator 失败后
恢复原 implementation node 和同一 Codex backend session，不再默认创建失去上下文的新 recovery node；
文档同时记录确定性回归和受控真实 `codex exec resume` 结果。随后完成的 v2 clean Medium
重跑见本页 Medium 章节。

## Small

实例：`pydantic__pydantic_v2.6.0b1_v2.6.0`

Run：`phase4g8-small-6dafeda34c`

结论：

- Runtime Validation：通过；
- End-to-End Capability Validation：未通过；
- 分类：`runtime-correct/task-failed`；
- official evaluator：连续 3 次 `FAIL_TO_PASS 0/1`、`PASS_TO_PASS 51/51`；
- consistency violation/warning：`0/0`；
- duplicate terminal/ledger fact：`0/0`；
- real compaction fallback：`0`。

报告：

- [中文能力过程记录](pydantic__pydantic_v2.6.0b1_v2.6.0/phase4g8-small-6dafeda34c/capability-trace.md)
- [结构化能力过程记录](pydantic__pydantic_v2.6.0b1_v2.6.0/phase4g8-small-6dafeda34c/capability-trace.json)
- [Runtime 原始运行报告](pydantic__pydantic_v2.6.0b1_v2.6.0/phase4g8-small-6dafeda34c/run-report.json)

该 run 的任务失败不能解释为 Runtime 失败。Runtime 正确拒绝了 worker 的本地 `4662 passed`
自报，使用固定 revision independent evaluator 发现 discriminated-union JSON schema 缺口，并连续
创建两个 evidence-backed recovery responsibility。两轮 recovery 均未修复 evaluator 所要求的
discriminator emission，最终由 task-quality 预算停止继续扩图，完整保留 open gap 与失败证据。

## Medium

实例：`dask__dask_2022.9.2_2022.10.0`

当前 resolved run：`phase4g8-medium-c1e87ae82e`

结论：

- Runtime Validation：通过；
- End-to-End Capability Validation：通过；
- 分类：`resolved`；
- 7 次 official evaluator FAIL_TO_PASS：`34 -> 38 -> 40 -> 40 -> 40 -> 40 -> 44 / 44`；
- 7 次 PASS_TO_PASS：始终 `2861/2861`；
- 前 6 次 unresolved feedback 均为 `current_failure_complete`，failed ID 截断 `0`；
- 6 个 feedback bundle 均先由同一 worker session 消费，再创建下一 evaluator；
- consistency violation/warning：`0/0`；
- duplicate terminal/ledger fact：`0/0`；
- real compaction fallback：`0`；
- context continuity：1 个有效 implementation node、同一 Codex thread、`resume_count=7`；
- WebSocket：隔离配置为 `true/20/8000`，本 run 记录 `upgrade=18`、`101=18`、`failure=0`、
  HTTP request/fallback `0`；
- candidate evidence：28 个 changed files、47333 bytes，patch SHA-256 已归档；
- worker hard interruption、daemon restart、real compaction 和 fixed revision evaluator 均有真实证据；
- 不运行 Large。

报告：

- [可读执行总结](dask__dask_2022.9.2_2022.10.0/phase4g8-medium-c1e87ae82e/execution-summary.md)
- [中文能力过程记录](dask__dask_2022.9.2_2022.10.0/phase4g8-medium-c1e87ae82e/capability-trace.md)
- [结构化能力过程记录](dask__dask_2022.9.2_2022.10.0/phase4g8-medium-c1e87ae82e/capability-trace.json)
- [Runtime 原始运行报告](dask__dask_2022.9.2_2022.10.0/phase4g8-medium-c1e87ae82e/run-report.json)
- [Candidate evidence](dask__dask_2022.9.2_2022.10.0/phase4g8-medium-c1e87ae82e/candidate-evidence.json)
- [Run retention 审计](dask__dask_2022.9.2_2022.10.0/phase4g8-medium-c1e87ae82e/retention.json)

本 run 在同一 implementation responsibility 中逐轮收敛。第一次 evaluator 已有 F2P `34/44`；第二、
三轮提升到 `38/44` 和 `40/44`。之后 failure signature 连续三轮不变，但同一 worker 继续消费诊断，最终
定位到 falsey coalescing 将显式 `shuffle=False` 转为默认 `"tasks"`，第七轮达到 `44/44`。

因此 `no_progress_streak` 只能用于 anti-stuck/observability，不能单独替代固定 attempt count 成为硬
终止条件；硬 operational guard 仍是总 wall/token/cost budget。该结果也证明单个强 worker 并非无法完成
Medium，而是需要完整、持续、可消费的 evaluator feedback loop。

运行期间修复了 `partial + candidate_ready` evaluator selection 和 `candidate_ready -> ready` remediation
状态迁移，并对旧代码留下的半迁移 session 做过一次严格断言后的 DB 修复。因此本 run 是 resolved 的
诊断性 resumed evidence，不冒充 Runtime 代码全程冻结的 release-grade clean run。

历史 run `phase4g8-medium-85eef83bdd` 保留为旧 fixed-attempt/budget-limited 基线：它最终 F2P
`37/44`、P2P `2861/2861`，但最新 feedback 未被 worker 消费，不能作为 capability ceiling。

在 clean run 前还完成了诊断 run `phase4g8-medium-afab266a85`。它真实发现并修复 pytest 分隔线误解析和
WebSocket idle TTL，但由于 Runtime 代码在 run 中变化，只能作为诊断/恢复证据。该 run 最终 F2P
`37/44`、P2P `2861/2861`，Runtime Validation 通过。报告保存在同名目录。

- [诊断 run 中文 trace](dask__dask_2022.9.2_2022.10.0/phase4g8-medium-afab266a85/capability-trace.md)
- [诊断 run 结构化 trace](dask__dask_2022.9.2_2022.10.0/phase4g8-medium-afab266a85/capability-trace.json)
- [真实 WebSocket transport audit](model-transport-audit-smoke.md)

历史 run `phase4g8-medium-559848012d`、`phase4g8-medium-6b2be98f01` 和
`phase4g8-medium-26741ac7ab` 继续保留；第一项发现 diagnostic case ordering 缺陷，第二项是 v2 前的
same-session 基线，第三项存在 worker/evaluator dependency drift，均不替代当前 clean run。

## Large

实例：`iterative__dvc_1.0.0a1_1.0.0a2`

Run：`phase4g8-large-a101c61de3`

结论：

- Runtime Validation：通过；
- End-to-End Capability Validation：未通过；
- 分类：`runtime-correct/task-failed`；
- primary 同一 Codex thread 的有效 F2P 进展：
  `0 -> 43 -> 52 -> 56 -> 57 -> 57 -> 58 -> 58 -> 58 / 68`；
- evidence-backed graph expansion 后，新 strategy thread 的 F2P：`55 -> 56 -> 55 / 68`；
- 最终 PASS_TO_PASS：`241/242`，存在 1 个回归；
- consistency violation/warning：`0/0`；
- duplicate terminal/ledger fact：`0/0`；
- 3 个 real compaction checkpoint accepted，fallback `0`；
- primary session resume `12` 次，expanded strategy resume `3` 次；
- candidate：66 个 changed paths、94,524 bytes；
- worker SIGKILL、daemon hard crash、expired lease takeover、receipt-before-ingest recovery、
  fixed-revision evaluator、structure request 和 fresh durable worker 均有真实证据；
- 运行后已清理 Docker image、worker toolchain、workspace、Codex homes 和 DB，仅保留报告。

报告：

- [可读执行总结](iterative__dvc_1.0.0a1_1.0.0a2/phase4g8-large-a101c61de3/execution-summary.md)
- [Orchestration usefulness 结论](iterative__dvc_1.0.0a1_1.0.0a2/phase4g8-large-a101c61de3/orchestration-usefulness.md)
- [中文能力过程记录](iterative__dvc_1.0.0a1_1.0.0a2/phase4g8-large-a101c61de3/capability-trace.md)
- [结构化能力过程记录](iterative__dvc_1.0.0a1_1.0.0a2/phase4g8-large-a101c61de3/capability-trace.json)
- [Runtime 原始运行报告](iterative__dvc_1.0.0a1_1.0.0a2/phase4g8-large-a101c61de3/run-report.json)
- [Candidate evidence](iterative__dvc_1.0.0a1_1.0.0a2/phase4g8-large-a101c61de3/candidate-evidence.json)
- [Candidate patch](iterative__dvc_1.0.0a1_1.0.0a2/phase4g8-large-a101c61de3/candidate.patch)

本 run 证明 Runtime 的长周期 context continuity、recovery、checkpoint、独立验证和 completion
honesty 有实际价值。Primary coherent worker 在 12 次恢复和多轮 evaluator feedback 下将 F2P 从
`0/68` 提升到 `58/68`。但 durable graph expansion 没有提高 oracle 峰值：fresh strategy worker
扩大了 SRS 覆盖和本地测试面，却回退了 evaluator 已确认的行为，最终为 `55/68`。

因此下一步不应继续增加 worker 数量，而应增加 best-revision preservation、delta evaluation、
regression-aware candidate promotion 和 rollback。完整分析见 orchestration usefulness 报告。
