# Hermes Kanban Runtime Kernel Phase 4G10

# Early Structure Assessment 与 Durable Orchestra Arm 2

## 1. 背景

Phase 4G8 DVC Large 证明了当前 Runtime Kernel 的恢复、固定 evaluator、best candidate 和
evidence lifecycle 可以工作，但没有证明系统级 orchestra 对任务质量产生了正向贡献。

该运行的主要执行形态是：

```text
一个 primary worker
    -> 长时间独自实现
    -> terminal structure_request
    -> fresh strategy workers
```

Primary 在完成前没有正式的早期结构评估机会。现有 Worker Delegation Policy 又同时规定：

- initial graph 默认只创建一个 primary node；
- 不确定时不拆分；
- `structure_request` 只能出现在 terminal receipt。

这使 Runtime 只能在 primary 已经执行很久甚至失败后扩图。新增 node 更像迟到 handoff，而不是
系统级并行 orchestra。

Phase 4G9 Native Arm 1 则证明 `gpt-5.6-sol ultra` parent/subagent 系统能在理解仓库后动态形成
实现 cluster，并通过高频通信和 shared workspace 快速集成。其完整 iterative run 最佳结果为
F2P `63/68`、P2P `242/242`，但最终仍未 resolved。

Phase 4G10 不要求 Runtime 模仿 native subagent 的局部通信。它要验证另一种机制：

```text
真实仓库审查
    -> early structure assessment
    -> evidence-backed durable nodes
    -> isolated contributions
    -> primary integration owner
    -> fixed evaluator
```

## 2. 目标

Phase 4G10 的目标是：

1. 让初始 primary 在广泛实现前完成一次有限的 repository/goal structure assessment；
2. 允许 worker 结束当前 materialization attempt，但不终结 execution node；
3. 由 Decision Provider 根据 assessment evidence 决定继续单 node 或扩展 graph；
4. 扩图时创建少量、低耦合、隔离 workspace 的 durable worker nodes；
5. 保留初始 primary backend session，并在 child 完成后恢复为 integration owner；
6. 冻结每个 child contribution patch、revision、changed files 和 provenance；
7. 让最终 candidate 能说明哪些 child contribution 被 primary 接受、修改或拒绝；
8. 在 DVC Large Arm 2 中证明 Runtime 确实有效拆分并集成任务，而不是一个 worker 干到底。

## 3. 非目标

Phase 4G10 不实现：

- worker 间自由聊天；
- 把 native subagent message stream 复制到 Runtime；
- 每个步骤、文件、角色或测试一个 runtime node；
- 自动认为任务越大越应该拆；
- 让 child 修改 goal contract、capability policy 或 completion truth；
- 无外部 oracle 时强制创建 evaluator；
- 以固定 benchmark 分数作为 Runtime correctness 判定。

## 4. 核心原则

### 4.1 Primary-first 保留，但不再 terminal-only

Runtime 仍从一个 coherent primary node 开始。变化不是预先创建很多节点，而是给 primary 一个
真实、可恢复的早期结构评估点。

```text
Goal Contract
      |
      v
Primary assessment attempt
      |
      +-- continue_single_node
      |
      `-- propose_expansion
              |
              v
         structural decision
```

### 4.2 Assessment 基于执行证据

Worker 必须先检查目标、代码布局、主要调用边界、测试布局和可能的共享入口。不得仅根据任务描述
中的名词数量提出拆分。

有效拆分至少要说明：

- 每个责任的完整 outcome；
- 可独立验收的 acceptance criteria；
- declared write scope；
- 与其他责任的依赖；
- 为什么并行收益超过 handoff 成本；
- 谁负责共享入口和最终集成。

### 4.3 Assessment 不是 goal evidence

Early checkpoint 不得：

- claim goal item；
- 写 progress ledger completion；
- 将 node 标记为 succeeded/failed/blocked；
- 关闭 backend session；
- 直接修改 execution graph。

### 4.4 Primary 始终是 integration owner

若 graph expansion 被接受，初始 primary 不被 supersede，也不换成 fresh strategy worker。它进入
dependency wait，child 完成后恢复同一 backend session，负责：

- 读取 child receipts 和 frozen contribution patches；
- 按明确顺序应用或重做贡献；
- 解决共享入口与 patch conflict；
- 运行 merged verification；
- 生成最终 candidate receipt。

## 5. Early Structure Checkpoint

Worker 的第一轮 assessment 使用独立 schema：

```json
{
  "schema": "runtime_worker_structure_checkpoint_v1",
  "kind": "early_structure_assessment",
  "recommendation": "expand",
  "summary": "仓库包含三个低耦合演进责任，共享入口可由 primary 集成",
  "inspected_scope": [
    "dvc/repo/plots",
    "dvc/stage",
    "dvc/repo/tree.py",
    "tests"
  ],
  "repository_facts": [
    {
      "fact": "plots 与 tree/remote 的主要写域不重叠",
      "evidence_refs": ["workspace:path:dvc/repo/plots", "workspace:path:dvc/repo/tree.py"]
    }
  ],
  "proposed_nodes": [
    {
      "node_key": "plots-evolution",
      "outcome": "完成 plots 目标版本行为并提供 focused tests",
      "acceptance_criteria": ["plots focused tests pass"],
      "declared_write_scope": ["dvc/repo/plots/**", "tests/**/plots/**"],
      "requested_capabilities": [
        "filesystem_read",
        "workspace_write",
        "git_read",
        "process_spawn"
      ]
    }
  ],
  "integration_owner_node_key": "primary-implementation",
  "shared_integration_scope": ["dvc/cli.py", "dvc/repo/__init__.py"],
  "risks": ["shared exports may conflict"],
  "worker_session_should_resume": true
}
```

`recommendation` 只能是：

- `continue_single_node`；
- `expand`。

`expand` 至少需要两个 proposed nodes。单个额外责任仍可由 primary 自己处理，除非存在 capability、
human、workspace isolation 或 independent verification 边界。

Checkpoint 必须通过 schema、node identity、scope、敏感信息和 workspace mutation 检查。Assessment
attempt 默认 read-only；若 worker 在 assessment 前广泛修改 source，checkpoint 被拒绝并按普通
worker failure 处理。

## 6. Runtime 状态机

新增 node state：

```text
waiting_structure
```

新增 materialization status：

```text
structure_checkpoint
```

路径：

```text
primary running
    -> checkpoint ingested
    -> materialization=structure_checkpoint
    -> backend session=interrupted/resumable
    -> primary=waiting_structure
    -> job=waiting_decision
```

Decision Provider 必须二选一：

### 6.1 继续单 node

```json
{
  "op": "continue_node",
  "node_key": "primary-implementation",
  "checkpoint_event_id": 123
}
```

Runtime 将 primary 置为 `ready`，下一 materialization 必须 resume 同一 backend session。

### 6.2 扩展 graph

Patch 创建 child nodes，并建立：

```text
child A ----\
child B -----+--> primary integration resume
child C ----/
```

Child 到 primary 的 dependency 被应用后，primary 从 `waiting_structure` 转为
`waiting_dependency`。全部 required child terminal success 后，primary 变为 `ready` 并 resume。

## 7. Decision Provider 规则

新增结构决策输入：

- checkpoint event ID；
- primary node/session identity；
- inspected scope；
- repository facts；
- proposed nodes；
- declared/shared write scopes；
- integration owner；
- current graph 和 capability policy。

Decision Provider 可以拒绝 worker 的拆分建议。它负责判断 durable boundary 是否成立，不应机械
照抄 proposed nodes。

Arm 2 中允许的主要理由：

- `durable_parallelism`；
- `workspace_isolation`；
- `capability_boundary`；
- `independent_verification`；
- `execution_discovered_gap`。

`task_is_complex`、`different_phase`、`different_role`、`different_module` 和
`could_be_parallelized` 仍然无效。

当采用 `durable_parallelism` 时，patch 必须：

- 引用 checkpoint event evidence；
- 创建 2 至 3 个 child implementation nodes；
- 声明互不重叠的 primary write scope；
- 声明 shared integration scope；
- 指定现有 primary 为 integration owner；
- 给每个 child 建立到 primary 的 dependency；
- 使用 isolated worktree workspace mode。

## 8. Workspace Isolation 与 Contribution Artifact

每个 durable child 使用独立 detached worktree：

```text
<run-root>/runtime-worktrees/<node-key>/
```

所有 child 从 assessment 时固定的 base revision 创建。不得把 sibling 的未集成改动预先复制进
child workspace。

当 supervisor 与 worker 使用不同 OS 身份时，worktree 创建、Git 状态读取和 contribution
freeze 必须使用 orchestration policy 声明的 `workspace_owner`。Worktree 根目录、工作文件及
共享 `.git/worktrees/<node-key>` metadata 必须属于同一 owner；不得依赖全局
`safe.directory=*` 绕过 ownership 边界。

Child terminal receipt 后，Runtime 本地冻结：

- binary patch；
- patch SHA-256；
- base revision；
- changed files；
- declared scope verification；
- worker receipt；
- backend session/materialization identity；
- focused test evidence。

Artifact schema：

```json
{
  "schema": "runtime_node_contribution_v1",
  "node_key": "plots-evolution",
  "base_revision": "git:...",
  "patch_sha256": "...",
  "patch_ref": "artifact:...",
  "changed_files": [],
  "scope_status": "verified",
  "materialization_id": "mat_...",
  "backend_session_id": "..."
}
```

Primary integration context 必须列出每个 contribution，不只给 child summary。Primary receipt 必须
记录：

- `accepted_contributions`；
- `modified_contributions`；
- `rejected_contributions`；
- 每项 decision 的理由；
- merged candidate revision。

稳定归档必须保留 frozen contribution、hash、receipt、session 和 DB evidence。完整 child
worktree 只属于可由 base revision 与 contribution 重建的运行缓存，在 verified artifact
manifest 生成后允许清理，不应作为长期原始证据复制。

## 9. Session Continuity

Early checkpoint 后 primary session 的 resume eligibility 需要：

- backend kind 支持 resume；
- node identity 不变；
- capability fingerprint 不变；
- node contract fingerprint 不变；
- primary workspace path 不变；
- workspace revision 与 checkpoint 一致；
- checkpoint event 尚未被消费；
- integration dependencies 已满足或明确被 waiver。

恢复同一 backend session 不等于复用旧 prompt。每次 materialization 的最新 worker context 必须作为
新的 user turn 进入原 session，至少包含本轮新增 dependency summary、frozen contribution
artifact、patch ref/hash、integration classification contract 和 Runtime footer。Reason-specific
resume instruction 只能追加到最新 worker context，不能替代它；也不得把所有普通 resume
错误描述为 infrastructure failure。

可观测性必须证明恢复后的 session JSONL 同时包含：

- 原 backend session id；
- 新 materialization id；
- 本轮新增 artifact id 与 patch ref；
- integration 或 remediation 的原因特定指令。

Assessment checkpoint 是预期 pause，不计入 crash recovery resume limit。Resume reason 固定为：

```text
early_structure_integration
```

## 10. Arm 2 冻结实验

### 10.1 任务

沿用 DVC Large：

```text
instance: iterative__dvc_1.0.0a1_1.0.0a2
base commit: fc42ca721c25bdd24875c999e37fb4f589ecd63c
dataset revision: 9b83d5af943ba7a17567336f5b18239f73960219
official image: xingyaoww/sweb.eval.x86_64.iterative_s_dvc-3760
FAIL_TO_PASS: 68
PASS_TO_PASS: 242
```

### 10.2 Worker 配置

- 所有 Runtime implementation workers 使用 `gpt-5.6-sol`、`max`；
- 不使用 `ultra` proactive native orchestra；
- 实验中禁止 implementation worker 创建 native internal subagents；
- 最多 3 个 child implementation workers 并行；
- primary assessment/integration 使用同一 Codex thread；
- evaluator 与 worker process/session/workspace 独立；
- worker tool network 与模型 transport network 保持隔离。

这项限制只用于区分 Runtime-level orchestra 的贡献，不代表生产环境永久禁止 worker 内部
subagent。

### 10.3 强制 Orchestra 断言

Arm 2 必须证明：

1. 初始 graph 只有一个 primary node；
2. primary 在广泛 source 修改前提交一次 accepted early structure checkpoint；
3. Decision Provider 基于 checkpoint 创建 2 至 3 个 durable implementation child nodes；
4. 每个 child 使用不同 worktree 和 backend session；
5. child declared write scopes 互不重叠，shared scope 归 primary；
6. 至少两个 child 产生非空 frozen contribution patch；
7. 至少两个 child contribution 被 primary 接受或经过修改后进入 integrated candidate；
8. primary 以同一 backend session 恢复并完成集成；
9. official evaluator 只运行在 integrated frozen candidate；
10. 报告能从 candidate 文件追溯到 child contribution。

若未满足这些断言，即使最终分数较高，也只能称为 Runtime single-worker capability run，不能称为
有效 Arm 2 system-level orchestra。

### 10.4 质量不设硬阈值

以下结果都作为参考，不是 pass/fail gate：

- Phase 4G8 coherent primary peak；
- Phase 4G8 expanded strategy final；
- Phase 4G9 Native Ultra best `63/68`、`242/242`。

Arm 2 的结论分为三个独立轴：

```text
Runtime correctness
Effective orchestration
Task capability
```

可能出现：

- runtime-correct / orchestration-effective / task-resolved；
- runtime-correct / orchestration-effective / task-failed；
- runtime-correct / orchestration-ineffective / task-failed；
- infrastructure-invalid。

最终分数低于 `63/68` 不自动否定 Runtime orchestra；需要结合 child contribution、primary
integration、单 worker参考、错误类型、耗时和 token 成本判断。反过来，分数高于 `63/68` 但实际
只有 primary 工作，也不能证明系统级 orchestra。

### 10.5 Evaluator 反馈循环

Integrated candidate 失败后，完整 source-safe diagnostics 优先回到同一 primary integration
session。Primary 可以：

- 自己继续修复；
- 基于新 evidence 再请求 durable expansion；
- 请求恢复某个仍适用的 child responsibility。

不得为满足“更多节点”而机械扩图。首轮强制 orchestra 断言已经测量系统拆分能力，后续扩图必须
由 evaluator evidence 支持。

## 11. 可观测指标

必须记录：

- early assessment wall time、token 和 inspected scope；
- Decision Provider 是否接受、修改或拒绝 proposed decomposition；
- durable node 数和 runnable concurrency；
- 每个 node 的 workspace、session、attempt、scope 和 contribution hash；
- child patch 接受/修改/拒绝结果；
- primary resume identity；
- conflict 数量和 integration wall time；
- evaluator round progression；
- worker input/cached/output/reasoning tokens；
- Decision Provider tokens；
- context reacquisition 和 handoff 次数；
- time to first evidence、time to first integrated candidate；
- orchestration wall time 占总 wall time 的比例。

## 12. 实现顺序

1. 增加 checkpoint schema、validator、events 和 `waiting_structure` state；
2. 支持 checkpoint ingest 不更新 progress ledger；
3. 支持 `continue_node` 和 checkpoint-backed decomposition；
4. 将 expected pause 投影为 resumable backend session；
5. 增加 node workspace policy 和 isolated worktree materialization；
6. 冻结 child contribution artifact；
7. 将 contribution bundle 注入 primary resume context；
8. 增加 integration attribution receipt；
9. 补 deterministic state/session/worktree/contribution tests；
10. 运行真实 provider control smoke；
11. 启动并持续监控 Arm 2；
12. verified archive 后生成中文报告并清理可重建 source artifacts。

## 13. 验收标准

Phase 4G10 实现完成需要：

- checkpoint schema 和状态机确定性测试通过；
- assessment 不污染 goal truth；
- continue-single 路径恢复同一 primary session；
- expand 路径创建隔离 child worktrees；
- dependency 完成后恢复同一 primary integration session；
- child contribution 可冻结、校验并注入 integration context；
- 非法 speculative split、重叠 scope、缺 integration owner 和缺 evidence 被拒绝；
- crash/restart 不重复 checkpoint、node、worktree 或 contribution fact；
- consistency checker 无新增 violation。

Arm 2 实测完成需要：

- 强制 orchestra 断言全部有 DB、session、workspace 和 artifact evidence；
- official evaluator 至少运行一次 integrated candidate；
- 无论 resolved 与否都保留 best candidate 和完整过程；
- 中文报告明确区分 Runtime correctness、effective orchestration 和 task capability；
- 与 Phase 4G8/4G9 的比较不使用预设分数结论。

## 14. 实施与真实验证结果

Phase 4G10 已完成实现和 DVC Large Arm 2 实测。

Run：`phase4g10-arm2-large-059ea4b541`

```text
Runtime correctness: passed
Effective orchestration: passed
Task capability: task-failed
F2P: 63/68
P2P: 242/242
```

真实执行创建了一个 primary 和三个 durable implementation children。三个 child 使用独立 worktree
和 Codex thread，分别产出 `38,778`、`25,151`、`12,898` bytes 的 frozen contribution；原
primary thread 恢复后将三项都作为 modified contribution 集成。全部强制 orchestra 断言通过。

Official evaluator progression：

```text
13 -> 52 -> 54 -> 56 -> 58 -> 63 -> 63 / 68
```

第 6、7 轮失败集合完全相同，使用经过 evaluator candidate hash 校验的 operator stop 结束。该停止
不是固定轮数预算，也没有将未评估 workspace 归档为最终 candidate。

运行中发现并修复 worktree ownership、resume context、contribution attribution lineage、receipt
budget、progress ledger 幂等键和 resume boundary reporting 等问题。最终 consistency 为
`0 violation / 0 warning`，duplicate terminal/ledger facts 均为 `0`。

完整结果见 [Phase 4G10 验证索引](validation/phase4g10/README.md)。

## 15. 实测问题的设计审计

Arm 2 暴露的八项问题不能统一归类为“测试脚本问题”，也不能统一归类为仍未修复的 Runtime
缺陷。当前处置如下。

| 问题 | 判断 | 当前保证 |
|---|---|---|
| child worktree ownership | 核心执行边界缺陷 | Git lifecycle 以本地 control-plane 声明的 workspace owner 运行；共享 worktree root 只修改根目录 owner，不能递归影响 sibling worktree；递归 owner 修正不跟随 symlink，只作用于当前 child tree。LLM graph patch 不能写入该 owner policy。 |
| primary resume 丢失 contribution context | 核心 continuity 缺陷，已修复 | 每次 resume 重新查询 dependency artifacts，将最新 frozen contribution bundle 放入完整 task context，再构造 continuation instruction。 |
| attribution 要求每轮重新触碰所有 child files | attribution 语义错误，已修复 | 首次 integration 必须以当前 changed files 证明接受或修改；后续 remediation 可以引用已验证 attribution lineage，不要求无意义地重复修改文件。 |
| resume 后 attribution lineage 丢失 | 持久化缺陷，已修复 | `contribution_lineage_refs` 从历史 verified event 恢复，并随 materialization resume 继续校验。 |
| 大型 receipt 被预算截断 | transport/ingest 缺陷，已修复 | 完整 `runtime_worker_receipt_v1` 从 Codex JSON event 的 agent message 单独捕获；bounded output tail 只用于可读日志，不能作为 receipt 事实源。 |
| receipt recovery 创建无效 strategy branch | 协议升级期间的错误恢复，当前正常路径已修复 | terminal evidence 先经过受限 adapter，再做 canonical schema/provenance validation；可适配 receipt 不计为 `receipt_invalid`，不消耗 recovery budget。Phase 4G8 repair 函数只用于迁移已经落库的历史坏状态。 |
| adapter recovery 重复 ledger fact | ledger 幂等缺陷，已修复 | `(job_id, goal_item_id, evidence_ref)` 是唯一事实键；重复 ingest 使用 upsert，不生成第二条 committed progress fact。 |
| operator-stop resume 丢失 daemon-start boundary | lifecycle observability 缺陷 | supervisor owner 首次取得 job lease 时写入一次 `runtime_supervisor_started`；resume 报告优先从该 DB event 恢复边界。旧数据库没有该事件时才使用 materialization 作为兼容推断。 |

### 15.1 Receipt 正常路径与历史修复

当前顺序必须保持为：

```text
terminal task evidence
    -> bounded deterministic adapter（仅匹配已知旧协议）
    -> canonical schema / provenance validation
    -> receipt ingest
    -> progress ledger / runtime events
    -> reducer
    -> 仍存在结构未知时才请求 Decision Provider
```

不得恢复为：

```text
先标记 receipt_invalid
    -> 创建 recovery strategy node
    -> 稍后再尝试理解原 receipt
```

`_ingest_adaptable_*` 和 `_repair_resume_*` 的存在表示系统能够修复旧 DB，不表示新运行应经过这些
函数。回归测试必须同时覆盖：当前可适配 receipt 不产生 receipt recovery event，以及历史坏状态仍可
确定性迁移。

### 15.2 Ownership 信任边界

`workspace_owner` 是本地 materialization/control-plane policy，不是 Decision Provider 提案内容。
它只决定 supervisor 以哪个既有 OS identity 创建和读取隔离 worktree，不授予新的 Runtime
capability。若未来允许多种 worker lane 使用不同 OS identity，应将 owner 直接绑定到经过本地注册的
lane profile；不得允许模型通过 node metadata 选择任意 UID/GID。
