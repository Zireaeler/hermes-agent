# Hermes Kanban Runtime Kernel Phase 4G14

# Durable Contribution Handoff

## 1. 背景

Phase 4G13 已经证明 Runtime 能够从真实 repository evidence 中自然发现 durable
responsibility，并形成以下执行结构：

```text
coherent primary
    -> early structure assessment
    -> evidence-backed graph expansion
    -> isolated child worktrees
    -> parallel implementation
```

但自然 Medium 同时暴露出 contribution handoff 的关键缺陷。三个 child 都完成了真实代码修改
和本地验证，其中两个 child 因 terminal receipt 中的机器字段使用了自然语言而被判为
`receipt_invalid`。当前实现只在 receipt 通过 canonical validation 后捕获 worktree patch，因此
代码成果和 receipt 协议被错误地绑定在同一个成功条件上：

```text
implementation completed
    + tests passed
    + receipt metadata invalid
    -> no frozen contribution
    -> child failed
    -> integration owner stranded
    -> recovery worker reconstructs work
```

这不是 evaluator failure，也不是 child coding failure。Official evaluator 在最终 candidate 固定后
才运行；handoff failure 发生在 evaluator 之前，属于 Runtime 内部成果保存和协议验收边界错误。

Phase 4G14 建立 durable contribution handoff：先无损保存 isolated attempt 的工程结果，再独立
验证和晋升其语义 receipt。模型输出错误不能让已经完成的 workspace patch 消失。

---

## 2. 目标

Phase 4G14 实现：

1. isolated contribution attempt 到达 terminal task/run 状态时，Runtime 先确定性捕获 workspace
   patch，不依赖 worker receipt 是否有效；
2. attempt patch 是不可变、可校验、可重启恢复的 quarantined artifact；
3. 只有合法 contribution receipt 才能把 attempt patch 晋升为正式
   `runtime_node_contribution`；
4. child receipt 与 integration receipt 使用角色化 contract，不再共享含义相反的 contribution
   分类字段；
5. goal key、directive ID 和 contribution artifact ID 使用当前 DB facts 生成的动态约束；
6. receipt validator 返回字段级、可执行的诊断，不再只返回笼统失败；
7. receipt recovery 只修复协议，不重新执行已经完成的实现；
8. replacement/recovery integration owner 继承旧 owner 已晋升的 contribution lineage；
9. worktree cleanup 必须证明 attempt patch 已持久化并通过 hash 校验；
10. 用不依赖 official evaluator 的轻量真实双 child case 验证完整 handoff。

核心路径：

```text
isolated worker terminal
    -> deterministic attempt patch capture
    -> immutable quarantined artifact
    -> role-specific receipt validation
    -> promotion
    -> integration dependency bundle
    -> primary integration and attribution
```

---

## 3. 非目标

Phase 4G14 不实现：

- worker peer-to-peer communication；
- 让 child 直接修改 primary workspace；
- receipt 无效时自动信任 patch；
- 用 evaluator 修复 Runtime handoff 协议；
- 自动把任何 quarantined patch 注入 integration owner；
- 默认将所有独立 child patch 无条件 `git apply`；
- 为验证 handoff 重跑 Phase 4G13 Medium；
- 通过增加 receipt retry 次数掩盖协议设计缺陷；
- 将模型自然语言作为 artifact identity、goal identity 或 directive identity。

Quarantine 只保证成果不会丢失，不代表成果已被接受、已满足 goal 或已获授权集成。

---

## 4. 核心不变量

### 4.1 工程成果与语义声明分离

Runtime 必须分别处理：

```text
Engineering fact:
    isolated worktree 相对 base revision 的实际 patch

Semantic claim:
    worker 对完成状态、goal evidence、verification 和风险的声明
```

Engineering fact 由本地 git 和文件 hash 确定；semantic claim 由 receipt 提供。后者无效不能删除
或阻止前者持久化。

### 4.2 Quarantined artifact 非权威

`runtime_attempt_patch`：

- 不更新 Progress Ledger；
- 不满足 execution dependency；
- 不进入 integration owner 的 accepted contribution bundle；
- 不影响 goal completion；
- 不绕过 capability 或 declared write scope；
- 只作为可恢复的 immutable execution evidence。

### 4.3 Promotion 必须绑定同一个 attempt

正式 contribution 必须引用：

- 原始 `runtime_attempt_patch` ID；
- 同一 `node_id`；
- 同一 `materialization_id`；
- 同一 `base_revision`；
- 同一 `patch_sha256`；
- 已通过的 canonical receipt ref。

Promotion 不重新从 live worktree 收集第二份 patch，避免 capture 与 promotion 之间发生漂移。

### 4.4 幂等事实

同一 `(node_id, materialization_id, patch_sha256)`：

- 最多一个 attempt patch artifact；
- 最多一个 promotion artifact；
- 最多一个 `runtime_attempt_patch_captured` event；
- 最多一个 `node_contribution_promoted` event。

Daemon crash、lease takeover 和重复 reconcile 可以重复读取，但不能重复提交事实。

---

## 5. Artifact 生命周期

### 5.1 Attempt Patch

新增 artifact type：

```text
runtime_attempt_patch
```

建议 metadata：

```json
{
  "schema": "runtime_attempt_patch_v1",
  "artifact_id": "art_attempt_xxx",
  "job_id": "rjob_xxx",
  "node_id": "rnode_xxx",
  "node_key": "dataframe-maintenance",
  "materialization_id": "mat_xxx",
  "materialization_attempt": 1,
  "base_revision": "git-sha",
  "workspace_revision": "git:...:worktree:...",
  "patch_ref": "/stable/contributions/.../attempt-1.patch",
  "patch_sha256": "...",
  "patch_bytes": 6300,
  "changed_files": ["src/a.py", "tests/test_a.py"],
  "file_sha256": {"src/a.py": "..."},
  "declared_scope_status": "verified",
  "capture_status": "quarantined"
}
```

`changed_files` 必须来自 Runtime 对 patch/worktree 的确定性检查，不能信任 worker 自报数组。

### 5.2 Promotion

合法 child receipt ingest 后新增正式 artifact：

```text
runtime_node_contribution
```

其 metadata 增加：

```json
{
  "schema": "runtime_node_contribution_v2",
  "source_attempt_artifact_id": "art_attempt_xxx",
  "receipt_ref": "node:rnode_xxx:materialization:mat_xxx",
  "promotion_status": "promoted"
}
```

正式 contribution 复用 attempt patch 的 `patch_ref`、`patch_sha256`、base、changed files 和 file
hashes，不重新采集 workspace。

### 5.3 Rejection 与保留

Receipt 最终无法修复、scope violation 或 worker 明确失败时：

- attempt patch 保持 quarantined；
- 写入 `runtime_attempt_patch_rejected` 或等价 terminal event；
- 记录 rejection reason；
- 不创建正式 contribution；
- recovery node 可以通过显式 evidence ref 请求人工或受控检查，但不能静默消费。

---

## 6. 捕获时机

对 `non_authoritative_contribution=true` 且使用 `isolated_worktree` 的 materialization，在 Kanban
task/run 首次进入 terminal 状态后，reconcile 顺序必须是：

```text
1. inspect terminal snapshot
2. capture attempt patch
3. verify patch hash and manifest
4. commit artifact row and capture event
5. validate receipt
6. promote / schedule protocol repair / reject
```

即使出现以下情况，步骤 2-4 仍然执行：

- receipt missing；
- receipt malformed；
- goal key 不存在；
- contribution 字段包含 prose；
- directive acknowledgment 不匹配；
- worker verdict 与 child role 不一致。

若 git patch 本身无法捕获、base revision 不匹配或 workspace 越界，则这是 artifact capture
failure，不得伪装成 receipt failure。

---

## 7. Role-Specific Receipt Contract

### 7.1 Contribution Child

Child 使用逻辑 schema：

```text
runtime_contribution_receipt_v1
```

最小结构：

```json
{
  "schema": "runtime_contribution_receipt_v1",
  "verdict": "succeeded",
  "summary": "完成的 bounded responsibility",
  "partial_goal_items": ["goal-item-key"],
  "unmet_goal_items": [],
  "verification": {
    "passed": true,
    "summary": "命令和结果"
  },
  "artifacts": [],
  "active_assumptions": [],
  "known_failure_boundaries": [],
  "consumed_directive_ids": []
}
```

Child contract 不包含：

- `accepted_contributions`；
- `modified_contributions`；
- `rejected_contributions`；
- authoritative `claimed_goal_items`。

Transport 可以继续使用 `runtime_worker_event_v1` envelope，但 wrapper 必须根据 node role 选择
不同的 inner schema。为兼容旧 receipt，Runtime 可以明确适配旧 schema，但不得接受 prose ID。

### 7.2 Integration Owner

Integration owner 使用逻辑 schema：

```text
runtime_integration_receipt_v1
```

它必须对 dependency bundle 中每个正式 contribution artifact ID 分类一次：

```json
{
  "accepted_contributions": ["art_contribution_a"],
  "modified_contributions": ["art_contribution_b"],
  "rejected_contributions": []
}
```

只有 integration owner 可以写这三个字段。Unknown、重复或遗漏 ID 继续由本地 validator 拒绝。

### 7.3 Dynamic Schema

Materialization 时根据 DB facts 生成 schema enum：

```text
goal item enum
    = 当前 node 关联的 goal item keys

directive enum
    = 当前 node 已 delivered 且未 ACK 的 directive IDs

contribution enum
    = 当前 integration dependencies 的 promoted artifact IDs
```

Contribution child 的 contribution 分类字段应在 schema 中不存在；兼容 schema 中则必须
`maxItems=0`。

动态 schema 只限制当前 materialization 已知 ID，不从 Decision Session、memory 或模型输出生成。

---

## 8. 字段级 Validation Diagnostics

Canonical validator 从单一 `Optional[receipt]` 结果升级为结构化结果：

```json
{
  "valid": false,
  "errors": [
    {
      "code": "unknown_goal_item_key",
      "field": "partial_goal_items[0]",
      "received": "自然语言总结",
      "allowed": ["official-evaluator-resolved"]
    },
    {
      "code": "child_contribution_classification_forbidden",
      "field": "accepted_contributions",
      "received_count": 2,
      "allowed": []
    }
  ]
}
```

至少覆盖：

- `unknown_goal_item_key`；
- `goal_item_outcome_overlap`；
- `unknown_directive_id`；
- `directive_ack_set_mismatch`；
- `child_contribution_classification_forbidden`；
- `unknown_contribution_id`；
- `contribution_not_classified`；
- `contribution_classification_overlap`；
- `invalid_child_verdict`；
- `declared_write_scope_violation`。

诊断进入 materialization metadata 和 `receipt_invalid` event。敏感内容仍需 redaction。

---

## 9. Metadata-Only Receipt Recovery

Receipt failure 已有有效 attempt patch 时，Runtime 生成 protocol repair task/context：

```text
Implementation is already captured and immutable.
Do not inspect, modify, or retest the repository.
Return only a corrected receipt for attempt artifact art_attempt_xxx.
```

Recovery context 包含：

- 原 receipt；
- typed validation errors；
- allowed goal keys；
- required directive IDs；
- role-specific schema；
- attempt artifact ID 和 hash。

协议修复不得创建新的 implementation attempt，不得把 repair worker 的 workspace diff 当成新成果。
若 backend session 已终止，可使用低成本 schema-only provider 或 deterministic adapter；不能为了修
receipt 重新扫描 repository。

指标必须区分：

```text
receipt_repair_count
receipt_repair_same_session_count
receipt_repair_schema_only_count
implementation_reexecution_due_to_receipt_count
```

最后一项在 Phase 4G14 acceptance case 中必须为 `0`。

---

## 10. Replacement Integration Owner

当旧 integration owner 因 child failure、context/runtime limit 或 strategy replacement 不再可执行，
新 owner 必须显式声明：

```json
{
  "replaces_node_key": "old-integration-owner",
  "inherit_promoted_contributions": true
}
```

Validator 检查：

- old owner 存在；
- old owner 与 new owner 链接相同 goal gap；
- replacement reason 有 evidence；
- 只继承正式 promoted contribution；
- 不继承 quarantined patch；
- 不产生 dependency cycle。

应用 patch 时，Runtime 确定性复制旧 owner 的 promoted contribution dependency edges，并记录：

```text
integration_contribution_lineage_inherited
```

新 owner 的 task context 必须包含继承后的 Frozen dependency bundle。它不能在 receipt 中省略这些
artifact 的 attribution。

---

## 11. Worktree Cleanup Gate

清理 contribution worktree 前必须满足：

1. 每个 terminal child 至少存在一个 `runtime_attempt_patch`；
2. artifact file 存在；
3. patch SHA-256 与 DB metadata 一致；
4. capture event 已存在；
5. attempt manifest 指向正确 materialization；
6. promoted/rejected/quarantined 状态可由 DB 事实解释；
7. contribution root 不位于 worktree root 内；
8. job terminal 或显式 operator cleanup policy 允许清理。

正式 contribution 不是 cleanup 的唯一合法凭据；最终 rejected child 只要 quarantined attempt 已
完整保存，也可以安全清理 worktree。任何 artifact 缺失或 hash mismatch 都必须拒绝清理。

---

## 12. Observability

新增或规范化事件：

```text
runtime_attempt_patch_captured
runtime_attempt_patch_capture_failed
runtime_attempt_patch_rejected
node_contribution_promoted
receipt_protocol_repair_requested
receipt_protocol_repaired
integration_contribution_lineage_inherited
runtime_orchestration_cleanup_refused
```

报告至少包含：

```text
terminal_contribution_attempt_count
attempt_patch_captured_count
attempt_patch_capture_failure_count
quarantined_attempt_count
promoted_contribution_count
receipt_invalid_count
receipt_repair_count
implementation_reexecution_due_to_receipt_count
inherited_contribution_count
integrated_contribution_count
contribution_preservation_ratio
```

其中：

```text
contribution_preservation_ratio
    = 最终 candidate 中有 verified lineage 的 promoted contribution 数
      / 所有 promoted contribution 数
```

不能把 quarantined patch 数量计为成功集成。

---

## 13. 实现顺序

### 13.1 Attempt Artifact Foundation

- 新增 deterministic changed-file collection；
- 新增 `_capture_runtime_attempt_patch()`；
- terminal reconcile 在 receipt validation 前调用；
- 写入 immutable file、metadata、artifact row 和 event；
- 增加 crash/retry 幂等。

### 13.2 Promotion

- `_freeze_runtime_node_contribution()` 改为从 attempt artifact 晋升；
- 禁止 promotion 重新读取 live worktree patch；
- contribution metadata 保存 source attempt ID；
- scope violation 和 invalid receipt 不晋升。

### 13.3 Receipt Contract

- role-specific inner schema；
- dynamic ID enum；
- typed diagnostics；
- metadata-only repair；
- 旧 `runtime_worker_receipt_v1` 保持可审计兼容。

### 13.4 Replacement Lineage 与 Cleanup

- graph patch 增加 replacement/inheritance 字段；
- validator 和 apply path 复制 promoted dependencies；
- cleanup 以 attempt artifact completeness 为门禁。

### 13.5 验证与报告

- focused unit/integration/restart regressions；
- lightweight real two-child handoff case；
- 中文 capability trace 和结果报告；
- 不运行 Phase 4G13 Medium。

---

## 14. 轻量真实验证场景

构造一个本地 git repository 和一个完整目标，其中两个低耦合 child 修改不重叠文件：

```text
primary integration owner
    +-- child-a: 修改 src/a.py 和 tests/test_a.py
    +-- child-b: 修改 src/b.py 和 tests/test_b.py
```

要求：

- 不使用 official evaluator；
- child 执行真实 shell、文件修改和本地测试；
- child A 返回合法 receipt；
- child B 第一份 receipt 故意包含非法机器字段；
- Runtime 在判定 child B receipt invalid 前已经保存其 attempt patch；
- protocol repair 不修改 workspace、不重新运行实现；
- 两个 attempt 最终都晋升；
- primary 收到两个正式 artifact ID；
- primary candidate 同时包含 A/B 修改；
- contribution attribution 通过；
- daemon restart 不产生重复 artifact/event。

该 case 验证 Runtime handoff，不评价模型能否猜中隐藏 benchmark contract。

---

## 15. Acceptance Criteria

Phase 4G14 完成必须满足：

- malformed receipt 不能导致 isolated patch 丢失；
- attempt patch capture 不依赖 worker 自报 changed files；
- quarantined artifact 不影响 ledger、readiness 和 goal completion；
- 合法 receipt 只晋升对应 materialization 的 immutable attempt patch；
- child 无法填写 integration-only contribution classification；
- dynamic schema 拒绝未知 goal/directive/artifact ID；
- receipt invalid event 包含字段级诊断；
- protocol repair 不重新执行 implementation；
- replacement owner 能继承 promoted contribution lineage；
- cleanup 在 attempt artifact 缺失或 hash mismatch 时拒绝；
- restart/reconcile 不重复 artifact、promotion 或 event；
- 轻量真实双 child case 的两个 patch 都进入最终 candidate；
- `implementation_reexecution_due_to_receipt_count = 0`；
- focused regression 通过；
- 中文设计和验证报告已提交并推送。

Phase 4G14 的成功标准不是 receipt 永远不会写错，而是：

```text
协议错误可以单独修复，
工程成果不会因此丢失，
每个进入最终 candidate 的 child patch 都具有可验证 lineage。
```

---

## 16. 完成记录

2026-07-19，Phase 4G14 已完成实现和 controlled two-child handoff 验证：

- 两个 terminal child attempt 均在 receipt 验收前捕获不可变 patch；
- 一份 malformed receipt 产生字段级诊断和 metadata-only repair；
- repair 未运行 shell、未修改 worktree、未重新执行 implementation；
- 两个 attempt artifact 均晋升并由 Primary 集成；
- 完整 unittest、Runtime consistency 和 cleanup gate 均通过；
- contribution preservation ratio 为 `1.0`；
- `implementation_reexecution_due_to_receipt_count = 0`；
- 受影响测试集为 `290 passed`。

验证入口见 [Phase 4G14 验证索引](validation/phase4g14/README.md)。本阶段没有重跑
Phase 4G13 Medium。
