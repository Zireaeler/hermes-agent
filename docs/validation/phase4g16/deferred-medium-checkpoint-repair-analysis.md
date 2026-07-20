# Phase 4G16 Deferred Medium 首轮复盘

## 结论

本次自然 Medium 证明真实 worker 能从 repository evidence 中识别 `defer_until_milestone`，但没有证明
Runtime orchestra 已经成功。自然结构决策在 checkpoint 字段校验失败后的恢复链路中丢失，Runtime 随后
创建新的 full-workspace strategy worker 从头完成任务。最终质量通过，但这部分重复实现不能算作有效编排。

旧 archive 中自动生成的 `calibration_fixture_gap` 结论不再作为本 run 的权威架构结论。Archive 与 manifest
保持不可变；本报告根据同一 archive 的 Kanban DB、task receipt 和 session evidence 对其作事实更正。

## 冻结任务与结果

- 任务：升级事件处理平台到 schema v2，同时保持 legacy v1 transform 与 audit JSONL consumer。
- Baseline：一个 coherent worker，465.119 秒，27/27 tests 通过。
- Runtime treatment：840.854 秒，27/27 tests 通过，consistency 0 violations。
- Runtime treatment 共 2 个 node、3 次 materialization。
- Primary 两次 materialization 均为 `receipt_invalid`；replacement strategy node 最终成功。
- 最终任务质量不低于 baseline，但 treatment 多出一次完整实现责任和明显 coordination overhead。

## 实际 Orchestra 过程

### 1. Worker 自然识别延迟拆分

Primary 在首轮只读 assessment 中识别出：

1. core schema v2 是两个扩展共同依赖的尚未稳定合同；
2. legacy v1 transform 和 audit JSONL consumer 是两个低耦合、可独立测试的责任；
3. 两个 child 不应在 core contract 冻结前启动；
4. 合理结论是 `defer_until_milestone`，而不是立即扩图或继续单节点到底。

Worker 自然提出两个候选责任和 milestone `core-schema-v2-contract-verified`。Prompt 没有提供 candidate key、
文件拆分答案或 graph operation，因此这是有效的自然结构证据。

### 2. 首轮 checkpoint 被字段约束拒绝

Worker 把 core 测试文件列入 `milestone_contract.artifact_scope`，但没有把同一测试文件列入
`shared_integration_scope`。Validator 正确拒绝：

```text
milestone_contract artifact_scope must be within shared_integration_scope
```

这是一个可在原 checkpoint 上最小修正的 metadata 错误，不要求重新检查仓库、运行测试或改变结构决策。

### 3. Runtime 给出了互相冲突的恢复协议

恢复 materialization 同时包含两种要求：

```text
输出 terminal runtime_worker_receipt_v1
输出 nonterminal runtime_worker_structure_checkpoint_v1
```

它没有把精确 validation error 和原 checkpoint 作为修复输入。原 Codex thread 再次输出结构 checkpoint，
但相同 scope mismatch 未被修正，因此第二次仍为 `receipt_invalid`。

此外，旧 ingest 只接受 `attempt=1` 的 structure checkpoint；即使第二轮字段正确，也无法进入正常 reducer。

### 4. 错误恢复掩盖了 Orchestra 失败

receipt retry 用尽后，Decision Provider 创建 `retry-event-schema-v2-valid-receipt`。该节点拥有 full workspace，
重新实现并验证整个任务，最终让 job 进入 `done`。

因此本 run 的事实应拆开解释：

- Task quality：通过；
- Runtime consistency：通过；
- Natural structure discovery：通过；
- Checkpoint protocol repair：失败；
- Candidate activation / child handoff / Primary integration：未发生；
- System-level orchestra positive value：未证明。

## 修复

本阶段增加 `runtime_checkpoint_protocol_repair_v1`：

1. Runtime 从上一 materialization 读取原 checkpoint、精确错误和字段级 diagnostics；
2. 修复轮复用原 backend session，只允许最小 metadata correction；
3. Prompt 禁止重新检查、修改、测试、实现或重新判断 topology；
4. Codex lane 继续使用原 structure/coordination output schema，不切换 terminal receipt；
5. structure repair attempt 可以引用首轮 invalid assessment 进入正常 checkpoint reducer；
6. 修复前后校验 workspace patch identity，变化时拒绝并记录；
7. 报告将“已有自然 candidate 的 invalid checkpoint”归类为
   `coordination_protocol_failure`，不再归类为 `calibration_fixture_gap`。

## 重跑约束

下一次只重跑 Runtime treatment。Baseline 从本次 manifest-verified archive 复用，必须满足：

- manifest 全量 hash 校验通过；
- case key、kind、title、objective 完全一致；
- 历史 baseline transport completed 且 oracle passed；
- 新 report 记录 manifest hash、source run 与 fixture identity mode。

Treatment 必须观察以下链路：

```text
自然 defer checkpoint
    -> 必要时同 session metadata-only repair
    -> Primary 完成并验证 core milestone
    -> Runtime 冻结 seed revision/artifact
    -> Provider 消费 retained candidates
    -> isolated children 产生 durable contributions
    -> Primary 集成并完成最终验证
```

不能再次用 full-workspace recovery worker 的任务成功替代上述 orchestra 验证。

## Evidence

- Archive：`phase4g16/durable-boundary-medium-hermes-phase4g16-natural-deferred-20260720-232616/durable-boundary-medium`
- Artifact root：`/root/hermes-validation-artifacts`
- Learning bundle SHA-256：`57ce41ca4a5bbcae09f3d89eddfb2e4b6462eac5226015622abd0def3d1173af`
- Source reports：`/tmp/hermes-phase4g16-natural-deferred-20260720-232616/durable-boundary-medium/reports`

