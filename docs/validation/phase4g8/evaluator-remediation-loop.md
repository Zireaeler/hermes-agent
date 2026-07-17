# Phase 4G8 Evaluator Remediation Loop

## 1. 背景

Phase 4G8 已经证明 independent official evaluator、fixed revision、worker crash resume 和
evidence-driven recovery 可以工作，但真实 Small/Medium 运行暴露了一个执行连续性问题：

- Small 为一个目标使用了 3 个 Codex backend session；
- Medium 为一个目标使用了 7 个 backend session；
- official evaluator 每次失败后，Runtime 通常进入 `waiting_decision`；
- Decision Provider 再创建新的 durable recovery node；
- 新 node 获得新的 `CODEX_HOME` 和 Codex thread，需要重新理解仓库、既有修改和失败背景。

这不是 evaluator 独立性要求。Evaluator 必须使用独立进程、独立 session 和固定 candidate revision，
但 evaluator 失败后的实现修复仍属于原 implementation responsibility。只要 workspace、capability、
node contract 和责任边界没有变化，默认应恢复原 worker session，而不是创建新的 runtime node。

## 2. 目标

建立以下确定性闭环：

```text
primary worker terminal receipt
  -> freeze candidate revision
  -> independent official evaluator
  -> unresolved result
  -> bounded/redacted failure bundle
  -> reopen original implementation node
  -> resume same CODEX_HOME and backend session
  -> new terminal receipt and candidate revision
  -> new independent evaluator node
```

目标是减少 context reacquisition 和无意义 graph expansion，同时保持 evaluator 独立性、DB 权威性和
可恢复性。

## 3. 非目标

本阶段不实现：

- 让 worker 直接运行 official evaluator；
- 向 worker 暴露 hidden test patch、gold patch、evaluator source、raw harness log 或 protected path；
- 把 evaluator session 与 implementation session 合并；
- 自动提高 capability；
- 绕过 fixed revision、provenance 或 stale-target 检查；
- 无预算的无限修复循环；
- 为每次测试失败自动创建新 runtime node；
- 把完整 Medium SWE-EVO 重跑作为本机制实现本身的完成前提；真实重跑属于独立 validation goal。

## 4. 启用策略

该行为必须由 job 的 verification policy 显式启用：

```json
{
  "verification_policy": {
    "mode": "required_evaluator",
    "assignee": "phase4g8-evaluator",
    "require_workspace_revision": true,
    "remediation": {
      "mode": "resume_target_session",
      "max_no_progress_streak": 2,
      "diagnostic_batch_size": 20,
      "max_diagnostics_chars_per_case": 4000
    }
  }
}
```

未显式启用的 job 保持现有行为。固定 evaluator attempt 数不是 task-quality budget；只要 evaluator
结果或 failure signature 仍在推进，Runtime 就继续恢复同一 worker。`stale_target_revision`、反馈提取
不完整等 infrastructure-invalid 结果不进入进展统计。Evaluator remediation 不设置独立的 session
resume 次数上限；总 wall/token/cost budget 才是 operational guard。`max_no_progress_streak` 在本阶段
只控制 anti-stuck/audit 信号，不单独终止任务。

## 5. Failure Bundle

Runtime 只能从受信 evaluator receipt 中提取固定字段，禁止转发任意原文：

```json
{
  "schema": "runtime_evaluator_failure_bundle_v1",
  "bundle_id": "efb_<stable-hash>",
  "failure_ordinal": 1,
  "source_verifier_node_id": "rnode_xxx",
  "source_task_id": "t_xxx",
  "target_node_id": "rnode_primary",
  "target_materialization_id": "mat_xxx",
  "target_revision": "git:<sha>:dirty:<hash>",
  "target_evidence_ref": "node:<id>:materialization:<id>",
  "fail_to_pass": {
    "passed": 32,
    "failed": 12,
    "total": 44,
    "failed_tests": ["bounded test ids"]
  },
  "pass_to_pass": {
    "passed": 2860,
    "failed": 1,
    "total": 2861,
    "failed_tests": ["bounded test ids"]
  },
  "failure_diagnostics": {
    "schema": "runtime_evaluator_failure_diagnostics_v3",
    "cases": [
      {
        "test_id": "tests/test_runtime.py::test_behavior",
        "failure_kind": "assertion_comparison_failed",
        "comparisons": [
          {
            "operator": "==",
            "left": "'array-key'",
            "right": "'copy-key'",
            "required_relation": "equal"
          }
        ],
        "conditions": ["shuffle=False"],
        "expected": ["expected value"],
        "actual": ["actual value"],
        "regex": ["required pattern"],
        "emitted_warnings": ["actual warning"],
        "exception_summary": ["AssertionError: behavior mismatch"],
        "diagnostic_excerpt": "bounded redacted pytest E-lines",
        "truncated": false
      }
    ],
    "case_count": 1,
    "omitted_case_count": 0,
    "truncated": false,
    "source_sha256": "..."
  },
  "environment_sha256": "...",
  "result_ref": "evaluator:<instance>:<sha>"
}
```

约束：

- F2P/P2P 的全部 official failed test id 必须保留；不得用固定条数截断；
- 每个 official failed test 至少对应一个 bounded structured diagnostic；诊断按固定 batch size 组织，
  batch 只控制输入组织方式，不能丢弃 case；
- 单个 case 可以做字段级安全裁剪，但必须显式记录 `detail_bounded=true`；它不能被报告为完整的测试
  contract，整体状态只能称为 `current_failure_coverage`；
- evaluator parser 只消费 pytest 的 `E` 诊断行，不转发 assertion source line 或 traceback source；
- Runtime 只接受 `test_id` 存在于本次 F2P/P2P failed list 的 case，其他 case 必须丢弃；
- evaluator 在 case budget 前先按 F2P/P2P failed list 过滤，并先为每个 official failed test 预留一个
  diagnostic case；一个测试的重复 section 不能挤掉其他 failed test；
- 无法提取 outcome 的 official failed test 必须进入 `missing_test_ids`，该 evaluator attempt 为
  infrastructure-invalid，不得恢复 worker、删除 protected raw 或消耗 task-quality/no-progress budget；
- case 只允许枚举 failure kind、`==`/`equal` comparison、格式受限的安全标量 keyword condition、
  expected、actual、regex、emitted warnings、exception summary 和 bounded excerpt；
- diagnostics 必须经过 secret/protected-material redaction，不保留 raw stdout/stderr；
- 不保留 candidate patch、hidden test source、gold 内容或完整外部响应；
- v1 `text` 只作为旧 evaluator result 的兼容 fallback，不得与有效 v2/v3 cases 同时转发；
- bundle 使用 stable hash 标识，重复 ingest 不得创建第二次 remediation fact。

Official evaluator 在执行阶段可向原 pytest 命令追加环境变量 `PYTEST_ADDOPTS=-vv`，用于避免 pytest
自行截断 assertion diff。该变量只改变诊断 verbosity，不修改 test id、测试选择、protected test
command，也不进入 worker 可见的 toolchain setup/hash。

## 6. 自动恢复资格

只有同时满足以下条件才自动恢复原 session：

1. job 显式启用 `resume_target_session`；
2. receipt 来自 `official_evaluator`，且 provenance 与固定 target 有效；
3. evaluator result 为有效 unresolved，不是 stale/infrastructure-invalid；
4. target 是非 verification execution node；
5. target 没有 active materialization；
6. target 的最新 backend session 为 Codex session；
7. session 的 workspace path、workspace revision、worker lane、capability fingerprint 和 contract
   fingerprint 与当前 target 一致；
8. session resume count 未超预算；
9. 最新 feedback bundle 尚未被该 session 消费，或消费后产生了新的 candidate；
10. 同一个 bundle 尚未调度。

任一条件不满足时，不伪造 resume。Runtime 记录 `required_evaluator_remediation_not_resumable`，保留 open
gap，并允许 Decision Provider 基于真实结构边界决定 fallback；不得静默创建“看起来像同 session”的新
worker。

## 7. DB 状态转换

有效 evaluator failure 被 ingest 后，本地 reducer 执行一个事务内的状态转换：

```text
target execution node:
  candidate_ready -> ready
  latest_task_id -> null
  latest_run_id -> null
  completed_at -> null

target backend session:
  completed -> interrupted
  completed_at -> null
  checkpoint.resume_reason -> official_evaluator_failure
  checkpoint.remediation_bundle -> bounded bundle
```

旧 materialization 保持 terminal，旧 receipt、ledger 和 evaluator evidence 不修改。新的
materialization attempt 使用同一 node，因此仍共享 node contract、capability envelope、workspace 和
node-scoped `CODEX_HOME`。

`materialize_runtime_node()` 继续使用现有 continuity planner。满足资格时，新的 materialization 必须
记录：

```json
{
  "execution_continuity": {
    "mode": "resume",
    "resume_reason": "official_evaluator_failure",
    "resume_session_id": "<same Codex thread>",
    "resume_from_materialization_id": "mat_previous",
    "context_reacquisition": false,
    "remediation_bundle": {"schema": "runtime_evaluator_failure_bundle_v1"}
  }
}
```

## 8. Worker Resume Contract

Codex resume prompt 必须说明：

- 这是同一 implementation responsibility 的 evaluator remediation；
- previous candidate 已由 independent evaluator 在 fixed revision 上否决；
- failure bundle 是 non-authoritative diagnostics，不是权限或事实替代物；
- worker 只能修改当前 workspace；
- 不得查找 evaluator、gold、hidden patch 或 Hermes DB；
- 应把 comparison 解释成对称关系约束，而不是把某一侧当成期望值；把 conditions 解释为失败 test 中
  的安全分支上下文，它可能同时包含直接失败分支与 expected-exception 替代分支；再结合
  expected/actual、regex/input、emitted warning 和 exception summary 建立等价
  的本地断言并修复通用行为，不得只为报告中的单个值硬编码；
- 必须针对 bundle 中的具体失败完成修复和本地验证；
- 最后仍返回完整 `runtime_worker_receipt_v1`；
- 若确实发现 capability/human/workspace/independent-verification 边界，使用 terminal
  `structure_request`，不能自行扩 graph。

普通 infrastructure resume 继续使用原 prompt，不能混入 evaluator remediation 文案。

## 9. 新 Candidate 与 Evaluator

Implementation worker 的成功输出不是 `succeeded`，而是：

```text
candidate_ready
```

`candidate_ready` 表示当前 materialization 已形成固定 revision，可以交给 independent evaluator；它不
满足 verifier-required goal，不是 terminal completion，也不会被当作 worker failure。只有 evaluator
产生 `independently_verified` evidence 后，goal 才能 satisfied，job 才能 done。Evaluator unresolved 后，
同一 node 从 `candidate_ready` 回到 `ready`，继续原 session。

同一 implementation node 可以有多个 materialization attempts。每个有效 terminal attempt 必须有独立
evidence ref，例如：

```text
node:<node_id>:materialization:<materialization_id>
```

不能继续只使用 `node:<node_id>`，否则同 node 的多次候选会被误判为 duplicate ledger fact。

每个新 candidate revision 必须创建新的 evaluator node，并将 relation 固定到新的 target materialization
attempt。已有 verifier 只能阻止同一 materialization 被重复验证，不能阻止同 node 的后续 attempt。

## 10. Decision Provider 调用规则

Evaluator terminal receipt 本身仍不是必然的 Decision Provider trigger。处理顺序必须是：

```text
ingest evaluator receipt
  -> validate provenance and fixed target
  -> persist complete current-failure bundle and signature
  -> eligible: reopen and materialize original node
  -> record that the same worker session consumed the bundle
  -> new candidate: evaluate again
  -> repeated identical signature: increase no_progress_streak / anti-stuck
  -> ineligible structural boundary: waiting_decision
  -> total resource exhausted: stop as resource-exhausted, not task-quality failure
```

任何资源预算终止前，最新 feedback bundle 都必须已经由同一 worker session 消费。如果最新 evaluator
result 尚未进入 worker turn，Runtime 必须先恢复 worker；固定 attempt count 和 no-progress streak 都不能
越过这个 invariant。进展包括 F2P/P2P 单调改善或 structured failure signature 改变。相同 ID 到达更深
断言会改变 signature，属于新的可消费信息，不得因为计数未变化而提前终止。连续相同 signature 只触发
anti-stuck/observability；真实 Medium 在 streak 达到 3 后仍从 `40/44` 推进到 `44/44`，因此 streak 不能
单独作为硬 task-quality gate。

当同 session remediation 已调度或正在运行时，Decision Provider 不得同时创建 recovery node。预算耗尽
后也不得在 harness 检测前抢先扩 graph。

## 11. Restart 与幂等

必须覆盖：

- evaluator receipt 已提交、remediation 尚未调度时 daemon crash；
- remediation 已调度、task 尚未 dispatch 时 restart；
- resume worker 运行中 crash；
- resume receipt 已提交、尚未 ingest 时 restart；
- new candidate 已 ingest、new evaluator 尚未创建时 restart。

不变量：

- 同一 bundle 最多一个 remediation schedule fact；
- 同一 node attempt 最多一个 materialization；
- 同一 materialization 最多一个 terminal fact；
- 同一 materialization evidence ref 最多一条等价 ledger fact；
- 新 evaluator 每个 target materialization 最多一个；
- retry/restart 不增加有效 evaluator failure count；
- resume 失败可以按既有 continuity policy fallback fresh，但必须记录 context reacquisition。

## 12. Observability

至少记录：

- `evaluator_failure_bundle_created`；
- `required_evaluator_remediation_scheduled`；
- `required_evaluator_remediation_not_resumable`；
- `required_evaluator_remediation_budget_exhausted`；
- `worker_session_resume_scheduled/resumed/resume_failed`；
- source verifier、target node/materialization/revision、bundle id/hash；
- same-session remediation count；
- context reacquisition count；
- evaluator failure ordinal 与剩余预算。

Capability trace 必须能区分：

- infrastructure resume；
- evaluator remediation resume；
- fallback fresh；
- evidence-backed graph expansion。

## 13. 实现范围

本 goal 修改：

- `kanban_runtime_kernel.py`：failure bundle、deterministic remediation reducer、continuity metadata、
  per-materialization evidence ref、新 evaluator target 去重和 provider suppression；
- `codex_worker.py`：evaluator remediation resume prompt；
- `kanban_runtime_phase4g8_run.py`：policy wiring、预算一致性和报告指标；
- Phase 4G8/kernel/worker lane tests；
- 本文档和 Phase 4G8 canonical cross-reference。

不引入新的数据库表。第一版使用 node/session/materialization metadata 与 execution events；只有后续查询
成本证明有必要时才正规化。

## 14. 验收标准

确定性测试必须证明：

1. implementation attempt 1 -> evaluator failed -> implementation attempt 2；
2. attempt 2 使用同一 node、同一 `CODEX_HOME`、同一 backend session id；
3. resume prompt 包含按 case 组织的 bounded bundle，不含 hidden/gold/protected path/raw log；
4. attempt 2 产生新 evidence ref 和新 fixed target evaluator；
5. 重复 tick 不重复 schedule/materialization/evaluator；
6. stale target 和 infrastructure-invalid 不触发 remediation，也不消耗业务失败预算；
7. capability/contract/workspace/session 不匹配时拒绝伪 resume；
8. 达到预算后不调用 Decision Provider；
9. consistency、duplicate terminal/ledger 检查通过；
10. 受控真实 provider smoke 证明 `codex exec resume` 收到 failure bundle 后继续同一 thread。
11. pytest `-vv` 不改变 protected test command；长 case 不得吞掉后续失败 case。
12. 不在 failed test list 中的注入 case 必须被 Runtime 丢弃，安全 expected/actual/regex/warning 保留。

完整 Medium SWE-EVO 重跑属于后续独立 validation goal，不阻塞本机制的确定性验收。

## 15. 2026-07-15 实现与验证事实

本机制已完成实现与受控真实 provider 验证。15.1 和 15.2 描述的是机制实现阶段，当时尚未重跑
Medium SWE-EVO，也没有把 synthetic evaluator 结果描述为真实 benchmark 结果。后续独立 Medium
重跑事实记录在 15.4。

### 15.1 确定性回归

定向测试结果：

- `tests/hermes_cli/test_kanban_runtime_kernel.py`：88 项通过；
- `tests/hermes_cli/test_kanban_runtime_phase4g8.py`：37 项通过；
- `tests/hermes_cli/test_worker_lanes.py`：60 项通过；
- 合计 185 项通过。

新增覆盖包括：

- evaluator failure 回流原 node 和同一 backend session；
- bundle 脱敏及 hidden/gold/raw output 排除；
- 重开 SQLite 连接后的重复 tick 幂等；
- budget exhausted 时禁止 Decision Provider 抢先扩 graph；
- provenance 无效的 evaluator 结果不计入业务失败预算；
- stale target、顶层及 verification 内的 infrastructure-invalid 结果不触发 remediation；
- workspace、worker lane、capability fingerprint、node contract fingerprint 和 session status 不匹配时
  拒绝伪 resume；
- attempt 2 生成新 evidence ref 和新 fixed-target evaluator；
- consistency 收敛。

测试同时发现并修复了三个实现问题：

1. verifier relation 只固定 materialization attempt 和 revision，却读取 target node 的最新 ledger
   evidence，导致 attempt 2 写入后 attempt 1 provenance 被误判 stale。现在 relation 同时固定
   `target_evidence_ref`。
2. evaluator contradiction 与 remediation evidence 可能在同一秒写入。原查询只比较
   `created_at > contradiction_at`，会漏读后写事实；现在按 `(created_at, rowid)` 排序比较。
3. supervisor tick 先同步 backend session、再 ingest terminal receipt，曾造成 tick 结束时 node 已 terminal
   而 session 仍为 active。现在 terminal ingest 后立即重投影 session。

### 15.2 受控真实 provider smoke

隔离样本：

```text
/tmp/hermes-phase4g8-remediation-real-or24_lvt
```

结构化报告：

```text
/tmp/hermes-phase4g8-remediation-real-or24_lvt/report.json
```

模型源为 `MySub2api / gpt-5.6-sol`。测试使用独立 `HERMES_HOME`、Git workspace 和 node-scoped
`CODEX_HOME`；没有复制主 Codex session history。主 `config.toml` 和 `auth.json` 的 mtime 均早于本次
运行，worker 日志中的有效 `CODEX_HOME` 为隔离 node 目录。

执行流程：

1. 真实 attempt 1 创建 `result.txt`，内容为精确的 `candidate-one\n`，输出合法
   `runtime_worker_receipt_v1`；
2. 测试控制路径通过正常 evaluator task、backend session event、fixed-target provenance 和 terminal
   receipt 注入一个 synthetic unresolved official-evaluator 结果；
3. Runtime 生成 `runtime_evaluator_failure_bundle_v1`，排除其中的 protected raw marker，重开原
   `primary-result` node；
4. attempt 2 使用 `codex exec resume <same-thread-id>`；真实 worker 明确读取 bounded diagnostics，将
   文件改为 `candidate-two\n`，完成 byte-level exact comparison，并输出第二份合法 receipt；
5. 最终 advance ingest attempt 2，backend session 收敛为 completed，consistency 为 0 violation、
   0 warning。

最终事实：

| 断言 | 结果 |
| --- | --- |
| materialization | `fresh/succeeded -> resume/succeeded` |
| execution node | 同一个 `primary-result` node |
| backend session | 只有 1 个，`resume_count=1` |
| resume identity | `thread.started.thread_id` 与 attempt 1 相同 |
| context reacquisition | `false` |
| output | 精确为 `candidate-two\n` |
| protected raw | 未进入 remediation bundle |
| consistency | passed，0 violation，0 warning |

真实 token 观测：

| attempt | input | cached input | output | reasoning output |
| --- | ---: | ---: | ---: | ---: |
| fresh | 76,974 | 69,376 | 997 | 111 |
| resume | 146,531 | 134,912 | 2,069 | 236 |

resume turn 的 cached input 占 input 约 92.1%，证明这次不是创建新 session 后重新获取上下文。

### 15.3 测试驱动观察

测试驱动本身出现过两次轮询问题，但没有形成 Runtime correctness failure：

- 第一版 driver 使用长寿命连接观察外部 worker 提交，worker 已在约 143 秒内写入 terminal receipt，
  driver 却直到等待上限才退出；
- 第二版短连接轮询在 worker 首次配置 WAL 时遇到一次瞬态 `database disk image is malformed`，进程退出后
  `PRAGMA integrity_check` 为 `ok`，task/run/receipt 均完整；
- 最终样本改为只消费已经提交的 task fact，未重新调用模型；attempt 1 和 attempt 2 都来自同一个隔离
  样本，最终报告由正常 Runtime advance 生成。

这些现象说明真实 smoke driver 不应在持有主写连接时自行实现高频 DB 轮询。后续若把该 smoke 固化为
正式 CLI，应复用 daemon/dispatcher 的短事务观察路径，而不是复制一次性 polling loop。

### 15.4 Medium 真实重跑

后续 run `phase4g8-medium-6b2be98f01` 已真实覆盖本机制：

```text
implementation attempt 3
  -> evaluator 32/44 F2P
  -> attempt 4, same node/session, context_reacquisition=false
  -> evaluator 34/44 F2P
  -> attempt 5, same node/session, context_reacquisition=false
  -> evaluator 36/44 F2P
  -> failure budget exhausted
```

最终有效 implementation node 只有一个，backend session key 始终为
`019f63b8-0e17-7300-9b5a-7dbf1d26a391`，累计 `resume_count=4`。两次 evaluator remediation 均未创建
新的有效 recovery responsibility。Runner resume 期间曾因 stale-timeout/recovery 顺序 bug 错误创建一个
strategy node；该 node 在修改 workspace 前终止并标记 `superseded`，不能计入有效 worker/session 数量。

该 run 的 Runtime Validation 通过，consistency、duplicate facts、compaction fallback 和 credential scan
均为 0；任务能力未通过，最终 FAIL_TO_PASS `36/44`、PASS_TO_PASS `2861/2861`。这证明机制能够保持
上下文连续性并驱动多轮真实修复，但不能保证模型在 bounded diagnostics 下收敛到 hidden evaluator 的
精确契约。

### 15.5 诊断反馈 v2 修正

Medium run 结束后的审计确认，最终 8 项失败并非 worker/evaluator environment drift，而是失败诊断在
三层被压缩：official pytest 未启用 `-vv`、evaluator 只提取少量行、Runtime 再做一次 6000 字符全局
裁剪。该历史 run 不重跑，结论仍保持 `runtime-correct / task-failed`。

后续实现将诊断路径升级为 `hermes_phase4g8_pytest_failure_diagnostics_v2`：evaluator 按失败 case 提取
expected/actual、regex/input、emitted warnings 和 exception summary；Runtime 再按 official failed test
list 做 allow-list 关联并生成 `runtime_evaluator_failure_diagnostics_v2`。Hidden source、gold/test patch、
protected path 和 raw harness log 继续隔离。该修正只改善下一次真实运行的 remediation 输入，不回写、
重解释或伪造既有 Medium 证据。

2026-07-15 的最终确定性验证结果为三个目标测试文件合计 `192 passed`；相关 Python 文件通过 Ruff 和
`py_compile`，工作树通过 `git diff --check`。其中包含 v2 cases 全部因 failed-test-ID 不匹配而被拒绝时
也不得回退 raw `text` 的旁路测试。

### 15.6 v2 clean Medium 结果

诊断 run `phase4g8-medium-afab266a85` 首次真实使用 v2 bundle，并发现 pytest traceback separator 被误当成
case heading。修正 parser 后，同一 run 将 F2P 从 `23/44` 提升到 `37/44`，P2P 保持 `2861/2861`；但因
运行中修改 Runtime 代码，该 run 只能作为诊断证据。

全新 clean run `phase4g8-medium-223cfadfef` 没有继承 workspace、DB、Codex home 或 backend thread：

```text
evaluator 1: F2P 33/44, P2P 2860/2861
evaluator 2: F2P 37/44, P2P 2860/2861
evaluator 3: F2P 39/44, P2P 2860/2861
```

三个 failure bundle 都重开同一个 implementation node，并恢复同一 backend session；没有创建新的有效
recovery responsibility。结果证明 v2 diagnostics 可以驱动真实进展，但仍不能保证收敛，也不能自动防止
worker 为修复 F2P 引入 P2P regression。Runtime 保持 `runtime-correct/task-failed`，没有接受本地通过的
错误自报。

### 15.7 v3 relation/condition 修正与保留策略

对 `phase4g8-medium-223cfadfef` 的公开 Dask 2022.10.0 契约复核确认，v2 仍存在两类有损反馈：

1. `assert y.name == y_c.name` 的对称相等断言被表示成单向 expected/actual，worker 因而把 array key
   反向改成 `copy-*`；
2. aggregate median 的失败只保留异常结果，没有保留 `shuffle=False` 等调用条件，worker 无法区分
   “disk/tasks 应成功”和“False 应拒绝”的条件契约。

v3 diagnostics 增加枚举 `failure_kind`、对称 `comparisons` 和安全标量 `conditions`。Runtime 在第二道
trust boundary 仅接受 `==`/`equal`、固定 failure kind 和符合 `key=<scalar literal>` 的 condition，并
继续执行 failed-test allow-list、字符预算、secret/protected-material redaction。Resume prompt 明确禁止
把 comparison 任一侧当作天然 expected，也禁止因不同条件下出现相反失败而全局翻转行为。

同时增加两级 retention：evaluator outcome 提取完成后立即删除 protected raw run；下一次 fresh run
开始前，压缩同实例已有且已落最终报告的 run，只保留 `reports/` 与审计清单。未完成、可能 resume 的
run 以及 qualification/mirror/shared toolchain 不清理。v3 clean Medium 的真实结果在本轮重跑后追加，
不预先声明 resolved。

v3 clean run `phase4g8-medium-559848012d` 最终得到：

```text
evaluator 1: F2P 30/44, P2P 2860/2861
evaluator 2: F2P 35/44, P2P 2861/2861
evaluator 3: F2P 41/44, P2P 2861/2861
```

三轮均使用同一个 implementation node 和同一 Codex backend session，`resume_count=3`，没有 recovery
node。第一次 v3 bundle 的 copy case 只表达两个具体 key 的 `equal` relation；第二轮 P2P regression 随即
消失并未复发。Median 与 `shuffle=False` 条件契约、CSV projection 和其他失败也在第三轮前消失。最终
仅剩 3 个 CLI F2P，达到 evaluator failure budget，结论为 `runtime-correct/task-failed`，不是 resolved。

三次 evaluator raw cleanup 分别删除 `1499537`、`1500434`、`1466901` bytes；Runtime consistency、
duplicate facts、compaction fallback 和 credential scan 均为 0，WebSocket transport 为
`36/36` upgrade/101、failure 0、HTTP 0。

Run 后复盘发现最后三项 CLI failure 没有对应 diagnostics：第二轮的 20-case budget 被 CSV、median 和
无关 groupby section 占满，第三轮 20 个 case 全是未列入 official failed list 的 groupby section。Runtime
allow-list 正确拒绝了这些无关 case，但 worker 因而只收到 CLI test id，只能猜测精确 warning/output/
Click command identity 契约。该 run 的 task-failed 结论不重写；后续实现改为 failed-test-first selection，
并增加“30 个无关 failure 在前、3 个 official CLI failure 在后、预算仅 3”仍须完整传递三项诊断的
确定性门禁。

### 15.8 Feedback completeness 与 candidate evidence 门禁

Failed-test-first selection 之后又增加端到端门禁：相同的 30 个无关 failure 与 3 个 official CLI
failure 必须经过 evaluator extraction、Runtime allow-list/budget、execution continuity，最终完整进入
`build_codex_resume_prompt()`；worker prompt 必须同时包含三个 test id、两个 warning regex 和 version
comparison，且不得包含任一无关 failure。

Evaluator result 记录 `feedback_coverage.status`。旧的 `budget_limited` 语义已被废弃，因为它只证明
固定槽位被填满，不能证明当前失败集合完整。新语义为：

- `current_failure_complete`：全部 official failed test id 都有 bounded diagnostic；
- `extraction_incomplete`：至少一个 official failure 没有 diagnostic。

第三种状态不再进入 remediation：Runtime 将其视为 infrastructure invalid，不恢复 worker、不增加
task-quality failure count，并保留 protected raw evaluator directory。Real-case runner 发现最新 evaluator
为该状态后立即停止，写 infrastructure-invalid report，而不是继续消耗一次模型调用。

另外，runner 在任何最终报告前归档 `reports/candidate.patch` 和
`reports/candidate-evidence.json`。后续 run compaction 可以删除 workspace、DB 和 Codex cache，但必须保留
candidate patch/hash，确保 task-failed 与 infrastructure-invalid case 都能在不接触 protected oracle 的
前提下复盘模型实际修改。

Medium `phase4g8-medium-85eef83bdd` 暴露了旧门禁仍不充分：

```text
evaluator 1: F2P 17/44, P2P 2823/2861, budget_limited 20/20
evaluator 2: F2P 37/44, P2P 2858/2861, complete 10/10
evaluator 3: F2P 37/44, P2P 2861/2861, complete 7/7
```

第一轮实际有 65 个 official failure，但只有 40 个 ID、20 个 diagnostics 被回流；第三轮的 7 项新
diagnostic 又因固定三次 evaluator budget 没有被 worker 消费。因此该 run 只能证明 Runtime 的恢复、
固定 revision 和独立 evaluator 不变量，不能证明 single-worker capability ceiling，也不能作为最终
task-quality/convergence 结论。后续 Medium 必须使用本节的新生命周期重新验证。
