# Phase 4G14 Durable Contribution Handoff 验证索引

Phase 4G14 修复 Phase 4G13 暴露的 contribution 交接故障：isolated child 的工程 patch
不再依赖 terminal receipt 一次写对。Runtime 先从真实 git worktree 捕获不可变 attempt
artifact，再独立校验 receipt；协议错误只修 metadata，不能要求 worker 重做实现。

## Controlled Two-Child Handoff

Run：`hermes-phase4g14-handoff-20260719-234235`

```text
two isolated children
  -> two real file changes and focused tests
  -> two immutable attempt patches captured
  -> child B receipt rejected with field-level diagnostics
  -> database connection reopened
  -> metadata-only repair, no shell and no workspace change
  -> two promoted contributions
  -> primary applies both patches and runs complete tests
  -> job done and consistency passed
```

| 指标 | 结果 |
| --- | ---: |
| Terminal contribution attempts | `2` |
| Attempt patches captured | `2` |
| Promoted contributions | `2` |
| Receipt repairs | `1` |
| Receipt 导致的实现重做 | `0` |
| Integrated contributions | `2` |
| Contribution preservation ratio | `1.0` |
| Consistency violations / warnings | `0 / 0` |
| Final state | `done` |

本 case 使用两个真实 isolated git worktree、真实 subprocess unittest、Kanban task/receipt、
DB reopen、artifact promotion、Primary integration 和 cleanup gate。它不调用模型 worker、
Decision Provider 或 official evaluator，因此只验证 handoff 机制，不评价模型编码能力。

- [中文能力过程](capability-trace.md)
- 稳定原始 archive：`/root/hermes-validation-artifacts/phase4g14/controlled-two-child-handoff/hermes-phase4g14-handoff-20260719-234235`
- Manifest SHA-256：`a8ec8aaedf6acc0defaaa4cd52c47f6ffe5473ed79bd5229df5295ac877d691e`
- Archive 状态：`verified`，7 个 manifest 文件，475,202 bytes

归档保留 Runtime DB、结构化 run report、两份 attempt metadata 和两份 patch。临时 child
worktree 只在 manifest 校验通过后删除。

## Natural Medium

Run：`phase4g14-runtime-medium-1c43cd09ba`

Runtime 只获得真实 Dask 2023.7.0 SRS 和 repository。Primary 自然检查代码后提出 3 个
不重叠 durable child；三个 isolated patch 均完成 attempt capture、promotion 和 Primary
integration，没有 receipt repair 或 recovery worker。

| 指标 | 结果 |
| --- | ---: |
| 编码 worker | `1 Primary + 3 children` |
| Attempt / promotion / integration | `3 / 3 / 3` |
| Receipt 导致的实现重做 | `0` |
| Preservation ratio | `1.0` |
| Wall time | `1,877.121s` |
| Official evaluator | `1` 次，feedback consumed `0` |
| F2P / P2P | `3/5` / `707/707` |

相对 Phase 4G13 Runtime，handoff 从两个 child 成果不可交付修复为 `3/3`，wall time 下降约
`45.9%`；相对 coherent single worker，最终质量相同且仍更慢。因此本 run 证明 durable
handoff 和恢复成本改善，不证明多 worker 提高了代码正确率。

- [Natural Medium 中文执行报告](natural-medium-execution-report.md)
- Stable archive：`/root/hermes-validation-artifacts/phase4g14/dask__dask_2023.6.1_2023.7.0/phase4g14-runtime-medium-1c43cd09ba`
- Manifest SHA-256：`be6b08a62db4b366a393f9fa2b91148366b9bca37aba9553eade7f06e3e73bac`
