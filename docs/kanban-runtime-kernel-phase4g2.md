# Hermes Kanban Runtime Kernel Phase 4G2：Real Provider Bounded Loop

Phase 4G2 的目标是在已验证真实模型源单次调用边界后，运行一个有上限的多轮 runtime
loop：decision provider 使用真实模型源，worker evidence 仍使用 synthetic receipt。

它验证真实 provider 是否能在连续的 goal gap、validator、patch、ledger、liveness 和
consistency feedback 下保持 runtime kernel 边界。它不接真实 worker lane，也不把
synthetic receipt 误写成真实 worker 运行结果。

## 1. 前置条件

进入 Phase 4G2 前必须满足：

- Phase 4G deterministic baseline 已通过；
- Phase 4G1 的 L1 decision execute 与 L2 one-step apply 已在隔离环境中验证；
- 至少有一次 accepted patch 和一次 rejected patch 的真实 audit；
- real compaction fallback safety 已验证；
- credential scan 和 consistency checker 通过。

真实 compaction candidate 无 fallback 通过 validator 不是 Phase 4G2 前置条件。Phase 4G2
可以继续使用 deterministic fallback，但必须把 candidate quality 缺口作为风险记录。

## 2. 目标

Phase 4G2 要证明：

- 真实 decision provider 可以被调用 3 到 5 次；
- 每次 graph 变化仍只经过 parser、validator 和 `apply_graph_patch()`；
- synthetic worker receipt 通过真实 Kanban task / runtime ingest 路径进入 ledger；
- failed receipt 会产生 goal gap 并再次请求真实 provider；
- accepted / rejected patch 都可审计；
- final或中间状态的 liveness、capability、memory 和 consistency 仍由本地 runtime 控制；
- report 不包含 credential、完整 prompt、完整 raw response 或 synthetic receipt 以外的
  worker 事实。

## 3. 非目标

Phase 4G2 不启动 Codex、Claude Code 或任何真实 worker backend。

Phase 4G2 不验证真实代码修改、真实测试命令或真实 workspace artifact。

Phase 4G2 不要求任务一定 `done`。模型可能被 validator 拒绝，或者 loop 在预算耗尽时
保持 `waiting_decision`；关键是状态一致、可审计、可恢复。

Phase 4G2 不把 synthetic receipt 计入真实 worker lane 的 L5 证据。

## 4. 运行模型

实现入口：

```text
hermes_cli/kanban_runtime_bounded_loop.py
```

每个 bounded step：

```text
ready node
      |
      v
Kanban task materialization
      |
      v
synthetic receipt completion
      |
      v
runtime evidence ingest / ledger / goal gap update
      |
      v
waiting_decision
      |
      v
real decision provider
      |
      v
parser / validator / graph patch apply or reject
      |
      v
consistency checker
```

synthetic receipt 在前几轮默认返回 `failed`，保留 goal gap；达到 decision tick budget 后，
下一条 materialized node 返回 verified success receipt。这样 loop 能覆盖重复 decision
feedback，同时 completion 仍由 progress ledger 决定。

## 5. CLI

```bash
hermes kanban runtime bounded-loop <job_id> \
  --codex-config \
  --max-decision-ticks 3 \
  --max-steps 16 \
  --json
```

命令必须显式指定 `--codex-config` 或 `--model-provider` 加 `--model`。没有模型源时必须
拒绝，不能隐式调用用户默认聊天模型。

输出是 bounded report，至少包括：

- decision tick 数量；
- accepted / rejected patch 数量；
- synthetic receipt 数量和 verdict；
- final job / goal state；
- 每步 legal waiting reason；
- consistency summary；
- credential leak flag。

## 6. 验收标准

Phase 4G2 MVP 完成时必须满足：

- fake-real deterministic tests 覆盖 3 个 decision tick、synthetic failed/succeeded receipt、
  ledger completion 和 consistency；
- CLI 必须要求显式模型源；
- 真实模型源在隔离 job 至少运行一次 3 tick bounded loop；
- 真实运行结果追加到真实集成验证台账；
- accepted / rejected patch 都保留审计；
- synthetic receipt 不被表述为真实 worker evidence；
- consistency 无未解释 violation；
- API key 不进入 report、event summary 或隔离 runtime DB。

## 7. 与后续阶段的关系

Phase 4G2 成功后，再进入 Phase 4G3：真实 worker lane smoke。

Phase 4G3 必须把 synthetic receipt 替换为由 Kanban 启动的真实 worker receipt，并验证
node -> task -> run -> evidence -> runtime ingest 的端到端路径。Phase 4G2 的通过不能替代
这个验证。

## 8. 当前真实验证结果

2026-07-10 已在一次性隔离 `HERMES_HOME` 中完成一轮 L4 验证，运行代码为
`17df67a feat(kanban): add real provider bounded loop`。当前 `.codex` 模型源仅被读取，
未启动真实 worker lane。

脱敏模型标识为 `codex:MySub2api` / `gpt-5.6-terra`。在进入 bounded loop 前，独立 G1
repeat 的 decision execute 解析成功但 validator 拒绝；one-step apply 也被拒绝，graph
revision 保持不变。这证明真实模型输出仍只能作为 proposal，不能绕过 validator。

随后 G2 bounded loop 完成 3 个真实 decision tick：2 个 patch 被 apply、1 个 patch 被
拒绝；两个 synthetic receipt 按 `failed -> succeeded` 经 Kanban task 和 runtime ingest
进入 ledger，job 最终 `done`。最终 consistency 为 `passed`，0 violations、0 warnings；
report 和全隔离 DB credential scan 均未发现 API key。

本结果证明 L4 的 transport、parser、validator、patch audit、synthetic evidence ingest 和
ledger completion 路径可以协同工作。它不构成真实 worker lane 的 L5 证据，也不改变真实
compaction candidate 仍缺 provenance、尚未达到 L3 的结论。完整脱敏台账见
`docs/kanban-runtime-kernel-real-integration-validation.md`。
