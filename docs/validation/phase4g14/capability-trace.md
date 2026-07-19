# Phase 4G14 Durable Contribution Handoff 验证

## 结论

两个 isolated child 都执行了真实文件修改和 Python unittest。`component-b` 第一份 receipt
被确定性注入非法 goal key 和自然语言 contribution 值；Runtime 在拒绝 receipt 前已保存其
immutable attempt patch。重开数据库连接后，protocol repair 没有运行 shell、没有修改
workspace，随后从同一个 attempt artifact 晋升正式 contribution。

Primary 收到并应用两个正式 artifact，完整 unittest 通过，最终 job 状态为 `done`。

## 指标

- Attempt patch captured: `2`
- Promoted contribution: `2`
- Receipt repair: `1`
- Implementation reexecution due to receipt: `0`
- Integrated contribution: `2`
- Preservation ratio: `1.0`
- Consistency: `passed`

本验证不调用 Decision Provider、模型 worker 或 official evaluator；它验证真实 git/worktree、
subprocess test、DB restart、Kanban task/receipt、artifact promotion、Primary integration 和 cleanup
路径，不评价模型编码能力。
