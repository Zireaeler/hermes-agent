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
