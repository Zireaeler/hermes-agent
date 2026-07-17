# Phase 4G8 Small / Medium 真实任务执行流程

本文用于快速理解 Phase 4G8 的 Small 与多次 Medium 真实长周期验证。它按执行主线重排信息，不复制完整
worker 输出、命令日志和 receipt。需要审计具体事实时，应继续查看每个 run 的原始报告和
`capability-trace`。

## 1. 如何理解结论

Phase 4G8 同时给出两个互相独立的结论：

- **Runtime Validation**：验证 Hermes 是否正确完成持久化、调度、恢复、compaction、固定版本
  evaluator、幂等 ingest 和一致性检查。
- **End-to-End Capability Validation**：验证 worker 最终提交的代码是否真正通过 SWE-EVO official
  evaluator。

因此，`runtime-correct/task-failed` 的含义是：

> Runtime 正确地运行并拒绝了不充分的 worker 自报，但模型没有解决 benchmark 任务。

worker 的本地测试不能直接满足 `verifier_required` goal。只有独立 evaluator 针对固定 workspace
revision 产生的有效 provenance，才能把 goal 标记为 independently verified。

## 2. 结果概览

| 项目 | Small | Medium |
| --- | --- | --- |
| SWE-EVO 实例 | `pydantic__pydantic_v2.6.0b1_v2.6.0` | `dask__dask_2022.9.2_2022.10.0` |
| Run | `phase4g8-small-6dafeda34c` | clean `phase4g8-medium-223cfadfef` |
| 运行时间 | 约 18 分 54 秒 | 约 57 分 40 秒 |
| Runtime Validation | 通过 | 通过 |
| Capability Validation | 未通过 | 未通过 |
| 最终分类 | `runtime-correct/task-failed` | `runtime-correct/task-failed` |
| 有效 evaluator 结果 | 3 次均为 F2P `0/1`、P2P `51/51` | F2P `33/44 -> 37/44 -> 39/44`；三次 P2P 均为 `2860/2861` |
| 额外 evaluator 记录 | 无 | 无；1 个 implementation node，3 次 same-session resume |
| Real compaction | 1 个 accepted checkpoint，fallback `0` | 1 个 accepted checkpoint，fallback `0` |
| 一致性 | violation/warning `0/0` | violation/warning `0/0` |
| 结束原因 | 3 次有效 evaluator 均失败，达到任务质量预算 | 3 次有效 evaluator 均失败，达到任务质量预算 |

## 3. 两个任务共用的执行路径

```text
SWE-EVO qualification
    -> base 必须失败，gold 必须通过
    -> 锁定 dataset revision、base commit、official image 和 manifest

Runtime job 初始化
    -> Goal Contract: official-evaluator-resolved
    -> verifier_required = true

Decision Provider
    -> 默认只创建一个 coherent primary worker node

Codex worker 独立进程
    -> 调研、实现、测试、debug
    -> 返回 terminal receipt 和 workspace revision

Runtime 本地 reducer
    -> ingest receipt
    -> 不接受 worker 自报为独立验证
    -> 固定 candidate revision
    -> 确定性创建 evaluator node

Official evaluator 独立进程
    -> 使用隔离的 evaluator 环境和 hidden tests
    -> 不继承 worker session
    -> 针对固定 revision 生成 provenance

Evaluator 失败
    -> 写入 contradicted evidence
    -> remediation policy 默认重开原 implementation node
    -> 同一 CODEX_HOME 和 backend session 接收 bounded failure bundle
    -> 只有出现真实结构边界时才请求 Decision Provider 扩 graph
    -> 再次固定 revision 并独立验证
```

共同安全边界：

- gold patch、hidden tests、qualification protected files 不进入 worker workspace；
- worker tool network 与模型 transport network 分离；
- evaluator 不修改 candidate workspace；
- worker receipt、evaluator receipt 和 ledger fact 均走正常 Kanban task/run 路径；
- DB 是事实源，checkpoint 只提供非权威恢复上下文；
- worker 本地测试即使全部通过，也不能绕过 independent evaluator。

## 4. Small 执行流程

### 4.1 任务目标

从 Pydantic `v2.6.0b1` 的代码基线完成 `v2.6.0` 对应行为，并通过固定 SWE-EVO evaluator。
official evaluator 的关键 FAIL_TO_PASS 用例检查：通过 `TypeAdapter` 生成 discriminated union JSON
schema definitions 时，嵌套 `oneOf` 是否包含正确的 `discriminator.mapping`。

### 4.2 阶段一：初始 primary worker

Decision Provider 创建一个完整 implementation responsibility：

```text
implement-srs-and-resolve-evaluator
```

worker 完成：

- 将版本从 `2.6.0b1` 更新为 `2.6.0`；
- 补充最终 release history；
- 修改 `HISTORY.md` 和 `pydantic/version.py`；
- 本地运行结果为 `4662 passed, 198 skipped, 7 xfailed`；
- `git diff --check` 通过。

这份结果只能证明 worker 当前可见测试通过。Runtime 没有因此完成 goal，而是固定 workspace revision
并创建独立 evaluator。

### 4.3 Real compaction 与进程连续性

在 primary worker 执行期间，Runtime 使用真实 compaction provider 生成并接受 checkpoint：

- checkpoint validator：通过；
- deterministic fallback：未使用；
- daemon restart 后从 DB 和 checkpoint 恢复；
- 未重复应用 graph patch 或 materialization。

### 4.4 第一次 official evaluator

结果：

```text
FAIL_TO_PASS: 0 / 1
PASS_TO_PASS: 51 / 51
```

唯一失败：

```text
tests/test_discriminated_union.py::
test_presence_of_discriminator_when_generating_type_adaptor_json_schema_definitions
```

实际 schema 有 `oneOf`，但缺少 evaluator 要求的：

```text
discriminator.propertyName = type
discriminator.mapping = item1/item2 -> corresponding $defs reference
```

这证明“全量本地测试通过”不足以代表 benchmark 完成。

### 4.5 第一轮 evidence-driven recovery

Decision Provider 根据 evaluator failure 创建：

```text
repair-discriminated-union-schema-and-rerun-evaluator
```

worker 修改：

- `pydantic/json_schema.py`；
- `tests/test_json_schema.py`。

本地结果：

- 再次 `4662 passed, 198 skipped, 7 xfailed`；
- `ruff` 和 `git diff --check` 通过。

第二次 official evaluator 仍然得到相同结果：F2P `0/1`、P2P `51/51`。这说明 recovery 修改了
相关逻辑和公开测试，但没有触及 hidden test 实际经过的 discriminator emission 路径。

### 4.6 第二轮 evidence-driven recovery

Runtime 保持原 goal 为 contradicted，再创建：

```text
diagnose-local-codepath-and-fix-discriminator-emission
```

worker 继续跟踪 `GenerateJsonSchema.tagged_union_schema`，调整 discriminator mapping 和测试期望，
并保留版本、history 修改。

本地结果包括：

- discriminator focused tests：`6 passed`；
- 大部分全量测试：`4657 passed`；
- 5 个剩余问题被 worker判断为环境相关 subprocess import failure；
- `ruff` 和 `git diff --check` 通过。

第三次 official evaluator 仍然是 F2P `0/1`、P2P `51/51`。

### 4.7 Small 最终判断

Runtime 正确完成了三轮：

```text
worker -> fixed revision evaluator -> contradicted evidence -> recovery worker
```

但三次独立 evaluator 都暴露同一个 discriminator 缺口，说明 recovery 没有收敛。达到 3 次有效
evaluator 的任务质量预算后，Runtime 停止继续扩图，保留 open gap 和失败 evidence。

结论：

- Runtime correctness：通过；
- 任务解决能力：未通过；
- 失败归类：模型/worker 没有修复 hidden evaluator 所要求的行为，不是 Runtime 把失败误判为成功。

## 5. Medium 执行流程

5.1 至 5.8 记录 v2 前基线 `phase4g8-medium-6b2be98f01`，用于说明 environment parity、runner resume
和 same-session reducer 的形成过程。当前 clean 结论见 5.9，不用新结果覆盖历史事实。

### 5.1 任务目标

在 Dask `2022.9.2` 基线上完成 `2022.10.0` release evolution。任务横跨：

- array/dataframe backend dispatch；
- 可扩展 CLI；
- groupby median 和 shuffle reduction；
- array copy、map overlap、tokenization；
- CSV projection、demo projection、delayed pickleability；
- CI dependency/configuration 更新。

相比 Small，这是一项多模块、长执行时间、需要真实 crash recovery 的 brownfield evolution。

### 5.2 重跑前修复

旧 run `phase4g8-medium-26741ac7ab` 暴露了 worker/evaluator dependency drift 和 evaluator failure 后
创建新 recovery node 的上下文丢失问题。重跑前完成：

- 从 official harness 的 post-setup 环境提取 worker toolchain；
- worker 与 evaluator environment fingerprint 均为
  `8601ded067e25620404a459f6c1ed63bb4ab2fc47fdb474d3262e8fc05415dd2`；
- evaluator failure 由本地 remediation reducer 重开原 implementation node；
- 新 materialization 使用同一 node-scoped `CODEX_HOME` 和同一 Codex thread；
- bounded/redacted failure bundle 进入 resume prompt；
- evaluator failure budget 固定为 3 次有效 unresolved result。

因此 `phase4g8-medium-6b2be98f01` 在当时成为 v2 前 Medium 能力基线。它现已被 5.9 的 clean run
替代，但继续保留历史审计。

### 5.3 初始 worker、进程中断与 `--resume-run`

Decision Provider 只创建一个 primary responsibility：

```text
resolve-official-evaluator
```

该 node 共形成 5 个 materialization attempt：

| Attempt | 结果 | 连续性 |
| --- | --- | --- |
| 1 | `crashed` | fresh thread `019f63b8-...` |
| 2 | `timed_out` | resume 同一 thread |
| 3 | `succeeded` | runner 恢复后继续同一 thread |
| 4 | `succeeded` | evaluator failure 1 remediation |
| 5 | `succeeded` | evaluator failure 2 remediation |

原 runner 停止后，本次新增的 `run-swe-evo --resume-run <existing-run>` 校验既有 run/spec/job/workspace，
拒绝附着仍有活 worker 的 run，恢复 network namespace、lane、provider 和 node-scoped Codex 配置，同时
保留 session SQLite。最终有效 implementation session 只有一个，`resume_count=4`，所有 resume 均为
`context_reacquisition=false`。

第一次 resume 暴露一个真实 Runtime bug：stale timeout 在 dead-PID recovery 前运行，把 attempt 2
误判为普通超时，并让 Decision Provider 创建 `resolve-official-evaluator-timeout-recovery`。该错误 node
只读取 workspace，尚未修改文件即被停止。修复后：

- dead worker recovery 先于 stale timeout；
- 原 implementation node 恢复为可执行；
- 错误 recovery node 标记为 `superseded`；
- 错误 patch、event、timeout fact 全部保留审计；
- consistency 恢复为 `0/0`。

### 5.4 第一次 official evaluator

attempt 3 完成后，独立 evaluator 针对固定 candidate revision 运行：

```text
FAIL_TO_PASS: 32 / 44
PASS_TO_PASS: 2861 / 2861
```

12 个失败集中在：

- CSV synthetic path projection；
- timeseries projection 稳定性；
- dataframe index display；
- groupby median 与 `sort + split_out`；
- CLI versions、entry point registration 和 duplicate warning。

Runtime 写入 contradicted evidence，没有创建新 recovery node，而是重开原 node，将 bounded failure
bundle 注入同一 thread。

### 5.5 第一次同 session remediation

attempt 4 在同一 Codex thread 中继续。Worker：

- 修复 CSV path projection 和 timeseries RNG/列顺序；
- 修复 median graph key、shuffle backend 和 `sort + split_out`；
- 调整 CLI 对不兼容 installed `distributed` 的处理；
- evaluator 指定的本地 retry set 为 `12 passed`；
- 完整 groupby 为 `1487 passed, 291 skipped, 10 xfailed`；
- 最终相关集合为 `20 passed`；
- `git diff --check` 与 `compileall` 通过。

第二次 evaluator：

```text
FAIL_TO_PASS: 34 / 44
PASS_TO_PASS: 2861 / 2861
```

CSV 与 timeseries 失败已消失，剩余 10 项为 index display、median/sort 契约和 CLI 精确行为。

### 5.6 第二次同 session remediation

attempt 5 再次恢复原 thread。Worker 根据第二份 failure bundle：

- 为单函数 groupby reduction 增加 shuffle 入口；
- 调整 median 无 shuffle 时的错误；
- 修复 `sort + split_out`，该失败族在最终 evaluator 中消失；
- 将 index display token 和 CLI registration 调整为更接近 evaluator 诊断的形式；
- evaluator-focused 本地测试为 `17 passed`；
- 完整 groupby 在过滤 toolchain 级 `DeprecationWarning` 后为
  `1491 passed, 291 skipped, 10 xfailed`；
- `git diff --check` 与 `compileall` 通过。

第三次 evaluator：

```text
FAIL_TO_PASS: 36 / 44
PASS_TO_PASS: 2861 / 2861
```

最终 8 项失败：

- `test_index_format`：实际 display token 与期望的 `from_pandas-index` 不一致；
- 4 个 median case：错误文本为 `must use a shuffle-based`，未匹配 evaluator 的
  `must use shuffl` 正则；
- `test_info_versions`：使用 package metadata 得到 `distributed=2024.8.1`，而实际 import 因版本不兼容
  失败，evaluator 期望 `None`；
- 2 个 CLI registration case：行为已接近，但 warning 文案未匹配精确正则。

这些失败没有造成任何 PASS_TO_PASS 回归，但 official evaluator 未 resolved，Runtime 因 3 次有效失败
达到预算而停止，不再 dispatch worker。

### 5.7 这次失败说明什么

本次结果不能解释为“单 worker 无法处理这个规模”。同一个 worker thread 已持续修改约 25 个文件，并将
F2P 从 `32/44` 提升到 `36/44`，同时保持 P2P `2861/2861`。真正暴露的是最后一公里验证缺口：

- worker 无权运行 official evaluator，这是正确的 trust boundary；
- bounded bundle 对 median 和 CLI 已给出 regex/input，但 worker 没有把本地测试改成完全相同的断言；
- `test_index_format` 的 pytest diff 被截断，bundle 未提供完整期望值，诊断信息确实不足；
- worker 多次把“本地相关测试通过”等同于“已修复 evaluator failure”，独立 evaluator 正确否决了自报；
- same-session context continuity 有效，但连续上下文本身不能替代精确、可复现的验收契约。

后续实现已将 evaluator failure bundle 升级为按 case 组织的 v2 诊断：official pytest 使用 `-vv` 保留
完整 assertion diff，evaluator 提取 expected/actual、regex/input、emitted warning 和 exception summary，
Runtime 再按 failed test id allow-list 和分层字符预算转发。该修正不暴露 hidden test source，也不让
worker 直接运行 official evaluator；本次 Medium 历史 run 未重跑，因此不能用新机制改写其任务失败结论。

### 5.8 Medium 最终判断

最终 Runtime 事实：

```text
runtime validation: passed
consistency: 0 violation / 0 warning
duplicate terminal facts: 0
duplicate ledger facts: 0
compaction fallback: 0
credential scan hits: 0
source Codex config unchanged: true
checkpoint chain: valid
```

结论：

- Runtime correctness：通过；
- 任务解决能力：未通过；
- context continuity：通过，1 个有效 implementation node、同一 thread、4 次 resume；
- 能力变化：F2P `32 -> 34 -> 36`，P2P 始终 `2861`；
- 停止原因：`evaluator_failure_budget_exhausted`；
- Large：未调度、未运行、没有结果。

### 5.9 WebSocket + v2 diagnostics clean run

随后依次完成两个新 run：

1. `phase4g8-medium-afab266a85` 在真实执行中发现并修复 pytest traceback separator 误解析和
   WebSocket relay 300 秒 idle TTL。由于 Runtime 代码在 run 中变化，它只作为诊断/恢复证据；最终
   F2P `37/44`、P2P `2861/2861`，Runtime Validation 通过。
2. `phase4g8-medium-223cfadfef` 从全新 workspace、DB、隔离 Codex home 和 backend thread 开始，运行中
   不修改 Runtime 代码，是当前 clean 能力证据。

clean run 的真实流程：

```text
one primary implementation node
  -> worker SIGKILL
  -> same thread resume (019f6512-...)
  -> evaluator 33/44 F2P, 2860/2861 P2P
  -> same-node/session remediation
  -> evaluator 37/44 F2P, 2860/2861 P2P
  -> same-node/session remediation
  -> evaluator 39/44 F2P, 2860/2861 P2P
  -> failure budget exhausted
```

三次 evaluator 结果证明 v2 diagnostics 驱动了持续改进，但没有收敛：最终 5 个 F2P failure 集中在
array copy identity 和 aggregate median，且 array copy 修复始终保留 1 个 P2P regression。Worker 的本地
等价断言全部通过仍不足以证明 official contract，independent evaluator 正确保持 goal 为 contradicted。

clean run 的 Runtime facts：

```text
runtime validation: passed
consistency: 0 violation / 0 warning
duplicate terminal facts: 0
duplicate ledger facts: 0
compaction fallback: 0
credential scan hits: 0
checkpoint chain: valid
effective implementation nodes: 1
distinct backend sessions: 1
session resume count: 3
```

隔离 worker 配置固定为 `supports_websockets=true`、`stream_max_retries=20`、
`websocket_connect_timeout_ms=8000`。clean worker 的长 turn 已跨过旧 300 秒断流边界。为让真实 transport
不再只依赖人工观察，proxy 后续增加匿名 audit；真实 provider smoke 得到 upgrade attempt `3`、HTTP 101
`3`、failure `0`、HTTP fallback `0`，且主 Codex 配置哈希未变化。

Phase 4G8 目标回归为 `192 passed`，Ruff、`py_compile` 和 `git diff --check` 通过。仓库全量 pytest 在
collection 阶段因当前开发环境缺少 `acp`、`aiohttp`、`mcp` 等可选依赖而无效，不应误记为 Phase 4G8
回归失败或全仓通过。

## 6. Small / Medium 测试说明了什么

### 6.1 已被证明的 Runtime 能力

- 一个 coherent primary worker 可以承担调研、实现、测试和 debug，而不需要按开发阶段预拆节点；
- worker 自报测试不会绕过 independent verification；
- evaluator failure 可以进入 DB、ledger 和下一轮 decision evidence；
- evaluator failure 可以在不扩 graph 的情况下重开原 implementation node，并恢复同一 Codex thread；
- crash、daemon restart 和 receipt ingest 不会产生重复 terminal/ledger fact；
- real compaction checkpoint 可以在无 deterministic fallback 的情况下通过 validator；
- fixed revision 和 provenance 能阻止 stale workspace 被错误验证；
- Runtime failure 与 task-quality failure 可以被分别报告。

### 6.2 尚未被证明的能力

- 当前模型/worker 对这两个 SWE-EVO 实例都没有达到 official resolved；
- 多轮 recovery 并不保证收敛；最新 feedback-complete Medium 最终停在 7 个 F2P failure，但已消除全部
  P2P regression；
- worker 的 focused/local suite 与 hidden evaluator 仍存在明显覆盖差距；
- 同 session resume 减少了上下文重新获取，但不能替代与 evaluator assertion 等价的本地验证；
- 最新 diagnostics/remediation 将 F2P 从 `17/44` 提升到 `37/44`，并将 P2P 从 `2823/2861` 恢复到
  `2861/2861`；后两轮 feedback coverage 为 10/10 和 7/7，说明最终 failure 已不能归因于诊断遗漏；
- 本次没有 Large 结果，不能据此声称 Runtime 已完成更大规模任务验证。

## 7. 原始证据

Small：

- [完整中文 trace](pydantic__pydantic_v2.6.0b1_v2.6.0/phase4g8-small-6dafeda34c/capability-trace.md)
- [结构化 trace](pydantic__pydantic_v2.6.0b1_v2.6.0/phase4g8-small-6dafeda34c/capability-trace.json)
- [原始 run report](pydantic__pydantic_v2.6.0b1_v2.6.0/phase4g8-small-6dafeda34c/run-report.json)

Medium：

- [最新 feedback-complete 执行总结](dask__dask_2022.9.2_2022.10.0/phase4g8-medium-85eef83bdd/execution-summary.md)
- [最新完整中文 trace](dask__dask_2022.9.2_2022.10.0/phase4g8-medium-85eef83bdd/capability-trace.md)
- [最新原始 run report](dask__dask_2022.9.2_2022.10.0/phase4g8-medium-85eef83bdd/run-report.json)
- [clean 完整中文 trace](dask__dask_2022.9.2_2022.10.0/phase4g8-medium-223cfadfef/capability-trace.md)
- [clean 结构化 trace](dask__dask_2022.9.2_2022.10.0/phase4g8-medium-223cfadfef/capability-trace.json)
- [clean 原始 run report](dask__dask_2022.9.2_2022.10.0/phase4g8-medium-223cfadfef/run-report.json)
- [混合版本诊断 trace](dask__dask_2022.9.2_2022.10.0/phase4g8-medium-afab266a85/capability-trace.md)
- [v2 前 same-session 基线](dask__dask_2022.9.2_2022.10.0/phase4g8-medium-6b2be98f01/capability-trace.md)
- [旧环境漂移 run](dask__dask_2022.9.2_2022.10.0/phase4g8-medium-26741ac7ab/capability-trace.md)

本目录总览：[README](README.md)。
