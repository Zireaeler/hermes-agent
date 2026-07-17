# Phase 4G8 Medium 执行总结

## 结论

- Run：`phase4g8-medium-85eef83bdd`
- Job：`rjob_e7e559caf9f0`
- Runtime Validation：通过
- End-to-End Capability Validation：未通过
- 分类：`runtime-correct/task-failed`
- 终止原因：3 次 unresolved evaluator attempt 达到预算
- 总耗时：`4378.042s`，约 73 分钟

本次没有再次出现 evaluator 到 worker 的信息丢失。第一次 official failure 数超过 20-case
预算，状态正确标记为 `budget_limited`；后两次分别有 10 项和 7 项 official failure，诊断覆盖率均为
100%，状态为 `complete`。最终未 resolved 是 task-quality/convergence 结果，不是 feedback extraction
或 Runtime correctness failure。

## 执行过程

### 1. 初始决策与进程恢复

Decision Provider 只创建一个完整 implementation responsibility。Worker 启动后，测试协议真实终止
Hermes-owned worker process group；Runtime 检测到 crash 后恢复同一 Codex thread，而不是创建新对话。

- 有效 implementation node：`1`
- distinct Codex backend session：`1`
- 最终 `resume_count`：`3`
- worker hard interruption：已覆盖
- daemon restart：已覆盖
- real compaction checkpoint：已接受，fallback `0`

### 2. 第一轮 implementation 与 evaluator

Worker 在同一连续 session 中完成仓库调研、跨模块实现、测试和 debug，candidate 一度覆盖 array、
dataframe backend dispatch、groupby、CSV/demo、rolling、CLI、tokenization 和 packaging 等模块。

第一次 independent evaluator：

```text
FAIL_TO_PASS: 17/44
PASS_TO_PASS: 2823/2861
feedback coverage: budget_limited, 20/20 required slots covered
```

该结果否定了 worker 的本地通过结论，并发现 38 个 P2P regression。Runtime 没有完成 job，而是将
bounded diagnostics 回流到同一 worker session。

### 3. 第二轮 remediation 与 evaluator

Worker 针对 array copy、groupby median/split、format name、CSV include-path 等 evaluator outcome 修复并
重跑公开测试。

第二次 independent evaluator：

```text
FAIL_TO_PASS: 37/44
PASS_TO_PASS: 2858/2861
feedback coverage: complete, 10/10 official failures covered
```

F2P 提升 20 项，P2P regression 从 38 项降到 3 项。Runtime 再次恢复同一 worker session。

### 4. 第三轮 remediation 与最终 evaluator

Worker 继续处理 CLI、array copy、groupby median 和 custom aggregation，并报告目标本地测试通过。

最终 independent evaluator：

```text
FAIL_TO_PASS: 37/44
PASS_TO_PASS: 2861/2861
feedback coverage: complete, 7/7 official failures covered
```

P2P regression 全部消除，但 F2P 未继续提升。最终剩余 7 项集中于 groupby median 的 shuffle contract
和三个 CLI contract。由于诊断 7/7 完整，这次不能再归因于 evaluator-worker 信息传递缺陷；模型在三次
bounded attempts 内没有收敛到完整目标行为。

## Runtime 不变量

- consistency：`passed`，violation/warning `0/0`
- duplicate terminal fact：`0`
- duplicate ledger fact：`0`
- premature done：`false`
- independent evaluator process：已覆盖
- fixed candidate revision：已覆盖
- worker/evaluator environment parity：通过
- source Codex config：未修改
- credential scan hit：`0`
- WebSocket：upgrade `29`，101 `29`，failure `0`，HTTP fallback `0`
- evaluator raw cleanup：三次均在完整或预算完整的 evidence extraction 后删除
- `extraction_incomplete`：`0`

## Candidate 证据与清理

- changed files：`32`
- patch bytes：`52908`
- patch SHA-256：`39671e97a49155dfe9366e265b5023eb6e5e8083c22ea6ed7f368361dc992c9e`
- protected oracle included：`false`
- retention 删除：`40505452` bytes
- 删除项：workspace、Hermes DB、Codex homes/cache、service state
- 保留项：整个 `reports/`，包括 candidate patch/hash、run report 和 capability trace

## 报告文件

- `run-report.json`：完整结构化 Runtime 与 capability 结果
- `capability-trace.md`：按事件展开的中文执行过程
- `capability-trace.json`：结构化过程记录
- `candidate.patch`：不含 protected oracle 的最终 candidate diff
- `candidate-evidence.json`：candidate revision、hash、大小和 changed files
- `retention.json`：run state 清理审计
