# Phase 4G8 Medium 执行总结

## 结论

- Run：`phase4g8-medium-c1e87ae82e`
- Job：`rjob_215296d0b6f8`
- Runtime Validation：通过
- End-to-End Capability Validation：通过
- 分类：`resolved`
- Official evaluator：FAIL_TO_PASS `44/44`，PASS_TO_PASS `2861/2861`
- 最终终止：`runtime_terminal`
- 本次 resume segment 耗时：`3909.531s`

这是当前首个 resolved Medium SWE-EVO 结果。Runtime 使用一个 implementation responsibility、一个
Codex backend thread 和 7 个 independent evaluator attempts，完成 crash recovery、同 session
remediation、固定 revision 验证和最终 completion。

该 run 同时是诊断性 resumed run：运行过程中修复了两个 `candidate_ready` 状态迁移问题，并对旧代码
留下的半迁移 session 记录做过一次严格断言后的 DB 状态修复。因此它证明修复后的闭环和任务能力，但不
应冒充“从启动到结束完全冻结 Runtime 代码”的 release-grade clean run。

## 执行结构

- Decision Provider patch：`1`
- 有效 implementation node：`1`
- implementation materialization：`8`，其中 attempt 1 被真实 SIGKILL，attempt 2-8 复用同一 thread
- Codex backend thread：`019f6982-a769-7c01-becc-f9561ab5ad96`
- session resume count：`7`
- recovery node：`0`
- independent evaluator node：`7`
- accepted real compaction checkpoint：`1`

Worker 的 inspection、实现、测试和 debug 始终留在同一个 durable responsibility 中。Evaluator 使用
独立进程和 session，且每轮固定到新的 candidate materialization/revision。

## Evaluator 收敛过程

| 轮次 | FAIL_TO_PASS | PASS_TO_PASS | 结果 |
| --- | ---: | ---: | --- |
| 1 | 34/44 | 2861/2861 | 10 个失败，完整回流 |
| 2 | 38/44 | 2861/2861 | 降至 6 个失败 |
| 3 | 40/44 | 2861/2861 | 降至 4 个 median/shuffle 失败 |
| 4 | 40/44 | 2861/2861 | signature 不变 |
| 5 | 40/44 | 2861/2861 | signature 不变 |
| 6 | 40/44 | 2861/2861 | signature 不变 |
| 7 | 44/44 | 2861/2861 | resolved |

前 6 个 unresolved result 都满足：

- 全部 official failed test ID 保留，`failed_tests_truncated=0`；
- 每个 failed test 有结构化 diagnostic；
- feedback coverage 为 `current_failure_complete`；
- bundle 先由同一 Codex thread 消费，再产生下一 candidate；
- 每个 candidate 使用新的 materialization evidence ref 和固定 revision evaluator。

第 3-6 轮 failure signature 连续相同，但第 7 轮仍然 resolved。这证明 `no_progress_streak` 只能作为
anti-stuck/observability 信号，不能单独成为硬停止条件。固定 evaluator 轮数同样不能终止有后续修复空间
的任务；Phase 4G8 的硬 operational guard 保持为总 wall/token/cost budget。

## Worker 实际解决过程

首轮 evaluator 发现 index naming、groupby median、groupby sort/shuffle 和 CLI contract 等 10 个问题。
同一 worker 逐轮修复后，CLI 和其他 dataframe failure 均被消除，最后只剩四个
`aggregate_median(..., shuffle=False)` 条件测试。

最后几轮的关键定位不是继续堆入口 guard，而是发现配置读取使用 falsey coalescing，将显式
`shuffle=False` 转成默认 `"tasks"`。Worker 最终区分“配置缺失”和“显式 false”，并覆盖 direct、aggregate、
single/mixed spec 及 disk/tasks 路径。最终 candidate 的本地 groupby suite 为 `1489 passed`，随后 official
evaluator 全部通过。

## Runtime 修复

本次实测发现并修复：

1. required evaluator selector 只读取 `satisfaction='full'`，无法选择合法的
   `partial + candidate_ready` ledger evidence；
2. evaluator remediation SQL 只允许从 `succeeded/blocked/failed` reopen，漏掉
   `candidate_ready`，导致 event 已记录但 node 未变为 `ready`；
3. no-progress streak 达到 3 后任务仍最终 resolved，确认 streak 不应被实现成固定轮数的替代硬门槛。

对应受影响回归最终为 `305 passed`，并通过 Ruff、`py_compile` 和 `git diff --check`。

## Runtime 不变量

- consistency：`passed`，violation/warning `0/0`
- duplicate terminal fact：`0`
- duplicate ledger fact：`0`
- premature done：`false`
- independent evaluator process：已覆盖
- fixed candidate revision：已覆盖
- worker hard interruption 和同-session recovery：已覆盖
- worker/evaluator environment parity：通过
- compaction fallback：`0`
- credential scan hit：`0`
- source Codex config：未修改
- WebSocket：upgrade `18`，101 `18`，failure `0`，HTTP fallback `0`

## Candidate 证据

- changed files：`28`
- patch bytes：`47333`
- patch SHA-256：`6bab240590a45bd3e9eabdc6afcea0ee6890d51acb3befe47223bd2d73c770cc`
- protected oracle included：`false`
- retention 删除：`42197931` bytes
- 删除项：workspace、Hermes DB、Codex homes/cache、service state
- 保留项：整个 `reports/`

报告文件：

- `run-report.json`：结构化 Runtime 与 capability 结论
- `capability-trace.md`：完整中文过程记录
- `capability-trace.json`：结构化时间线和 evidence
- `candidate.patch`：不含 protected oracle 的最终 candidate diff
- `candidate-evidence.json`：candidate revision、hash、大小和 changed files
- `retention.json`：归档后 bulky run state 清理审计

Large 按 operator 要求未运行。
