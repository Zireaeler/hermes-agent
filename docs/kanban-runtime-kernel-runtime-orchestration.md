# Hermes Runtime Kernel 普通 Job Durable Orchestra

## 1. 状态与范围

本文定义 Phase 4G10.1 之后，普通 Hermes Runtime job 如何显式启用 early structure assessment、
isolated durable children、frozen contributions 和 original primary integration。

该能力是 opt-in。默认行为仍是：

```text
coherent single primary
```

普通 job 不继承 SWE-EVO benchmark harness、fixed official evaluator、assessment replay topology、
故障注入或 evaluated coverage stop。

## 2. 目标

普通 Runtime job 可以通过 CLI、Python API 或配置选择：

```text
single primary
    -> early read-only structure assessment
    -> continue same primary

或

single primary
    -> early read-only structure assessment
    -> 2-3 isolated durable children
    -> frozen contribution artifacts
    -> original primary session resumes
    -> integration and verification
```

拆分依据仍是责任、权限、workspace、恢复和独立验证边界，不是任务包含多个步骤或文件。

## 3. Policy Contract

普通 job 使用：

```json
{
  "schema": "runtime_orchestration_policy_v1",
  "mode": "early_structure_assessment",
  "worker_lane": "codex-runtime",
  "max_child_nodes": 3,
  "require_contribution_attribution": true,
  "minimum_integrated_contributions": 1,
  "required_child_capabilities": [
    "filesystem_read",
    "workspace_write",
    "git_read",
    "process_spawn"
  ],
  "base_revision": "<git-sha>",
  "worktree_root": "<local-control-plane-path>",
  "contribution_root": "<local-control-plane-path>",
  "workspace_owner": {
    "source": "trusted_worker_lane",
    "lane": "codex-runtime",
    "uid": 1000,
    "gid": 1000
  },
  "retention": {
    "worktrees": "retain",
    "contributions": "retain"
  }
}
```

默认 single-primary policy 为：

```json
{
  "schema": "runtime_orchestration_policy_v1",
  "mode": "coherent_single_primary",
  "enabled": false
}
```

Decision Provider 和 worker 不能修改 policy、owner、artifact root 或 retention。

## 4. CLI 与配置

普通创建和 promote 命令支持：

```text
--orchestration-mode coherent_single_primary|early_structure_assessment
--orchestration-root PATH
--orchestration-max-children 2|3
--orchestration-retention retain|cleanup_on_terminal
```

状态与清理入口：

```text
hermes kanban runtime orchestration <job-id> --json
hermes kanban runtime orchestration <job-id> --cleanup-worktrees --json
```

配置默认值：

```yaml
kanban:
  runtime_orchestration:
    mode: coherent_single_primary
    worker_lane: ""
    max_child_nodes: 3
    artifact_root: ""
    retention: retain
```

CLI 显式值覆盖配置。Python API 通过 `orchestration_policy=` 接受同一语义请求，并由本地
normalizer 生成最终 policy；调用方不能直接伪造 resolved owner identity。

Dashboard HTTP API 可通过以下只读入口观察同一投影：

```text
GET /api/runtime/jobs/<job-id>
GET /api/runtime/jobs/<job-id>/orchestration
```

清理属于有副作用的 control-plane 操作，第一版只开放 CLI 和本地 Python API，不通过 dashboard
GET surface 触发。

## 5. 创建时校验

`early_structure_assessment` 必须满足：

1. job workspace 是现存 Git repository root；
2. workspace 没有未提交或 untracked 修改；
3. 已指定并注册 trusted external worker lane；
4. lane 不是 read-only，且并发容量至少为 2；
5. `max_child_nodes` 为 2 或 3，且不超过 lane 并发容量；
6. artifact root 不位于 job workspace 内；
7. base revision 在创建时由本地 `git rev-parse HEAD` 固定；
8. child capability envelope 只允许本地实现所需的受支持 capability。

校验失败时 job 不创建，不能降级后仍声称 early orchestra 已启用。

## 6. Trusted Worker-Lane Owner

Workspace owner 只能由本地 worker-lane registry 解析：

- lane 使用隔离 network identity 时，采用经过 lane validator 接受的 numeric UID/GID；
- 普通同用户 Codex lane 使用 Runtime 进程的 effective UID/GID；
- provider patch、worker receipt 和用户 goal text 中的 UID/GID 均不可信；
- owner 只用于 Runtime 创建 worktree 和运行 trusted git 命令，不授予 capability。

`_apply_workspace_owner` 必须继续使用 `follow_symlinks=false`，不能递归修改 symlink target 或
worktree root 的 sibling owner。

## 7. Read Path 与执行路径

初始 Decision Provider 仍只创建一个 coherent primary node。Primary 第一次 materialization 被改为只读
assessment，并输出 `runtime_worker_structure_checkpoint_v1`。

若 recommendation 为 `continue_single_node`：

- 不创建 child；
- 原 primary session resume；
- primary 承担完整实现、测试和 debug loop。

若 recommendation 为 `expand`：

- 只能创建 2 到 policy 上限个 child；
- 每个 child 必须匹配 checkpoint 中的 outcome、acceptance、scope 和 capability request；
- 每个 child 使用 `workspace_mode=isolated_worktree`；
- write scopes 必须不重叠；
- 每个 child 必须依赖回 primary integration owner；
- decomposition 必须引用 checkpoint event，并使用 `durable_parallelism` 理由。

## 8. Frozen Contribution 与集成

Child 成功时 Runtime：

1. 根据固定 base revision 收集 binary-safe Git patch；
2. 计算 patch SHA-256、changed-file hashes 和 scope status；
3. 写入 `runtime_node_contribution_v1` artifact；
4. 将 child goal claim 降级为 non-authoritative partial evidence；
5. 在全部 required children 完成后恢复 original primary session。

Primary 必须将每份 artifact 分类为 `accepted`、`modified` 或 `rejected`。后续 resume 可以引用已验证
attribution lineage，不要求每轮重新触碰 child 文件。

## 9. Retention 与 Cleanup

Contribution patch、metadata 和 DB artifact 记录始终保留。Worktree policy：

- `retain`：默认，不自动删除；
- `cleanup_on_terminal`：job 进入 `done`、`failed` 或 `cancelled` 后，只有所有 frozen patch hash
  重新验证通过，才删除 isolated worktrees；
- operator 可在 terminal job 上通过 `--cleanup-worktrees` 执行同一门禁；
- active job、缺失 contribution、hash mismatch、越出 configured worktree root 或 cleanup 失败时拒绝删除；
- cleanup 写入 Runtime event，状态和失败原因进入 observability。

## 10. Observability

`runtime status`、`runtime inspect` 和 `runtime orchestration` 至少公开：

- effective mode 与 policy schema；
- trusted lane 和 owner source；
- base revision、worktree/contribution roots；
- child count、isolated workspace 状态和 session identity；
- frozen contribution count、bytes、hash 和 integration owner；
- attribution summary；
- retention policy 与最后 cleanup result。

这些字段是可观测投影，不替代 DB 中的 graph、artifact 和 event truth。

## 11. Evaluator 边界

普通 job 不因为启用 durable orchestra 而自动创建 evaluator。

只有 goal item 明确设置 `verifier_required=true`，并且存在本地 verification policy 或外部 oracle 时，
Runtime 才创建独立 verifier。没有预先定义 oracle 的普通开发任务由 primary 自测并提交证据；不能把
重复运行 worker 自己写的测试伪装成独立需求验证。

## 12. 向后兼容

- 未设置 orchestration policy 的历史 job 按 coherent single primary 解释；
- Phase 4G10 benchmark 的 legacy policy 继续可读，不要求迁移正在运行的 DB；
- 已存在的 shared workspace node 行为不变；
- `isolated_worktree` 仍只在受控 early-assessment expansion 中启用；
- 默认 CLI/config 不创建额外 node、目录或 evaluator。

## 13. 验收

必须验证：

1. 默认 create 仍是 single-primary；
2. early mode 的无拆分 checkpoint 恢复同一 primary，child count 为 0；
3. 合法拆分创建 2-3 个隔离 child，贡献被 freeze 并由 original primary 集成；
4. read-only lane、脏 workspace、非 Git workspace、非法 root、并发不足和 capability 越界均被拒绝；
5. status/inspect 可以看到 policy、child、contribution 和 cleanup 状态；
6. terminal cleanup 在 hash 校验通过时删除 worktrees，保留 contributions；
7. hash mismatch 或 active job 时 cleanup 拒绝；
8. 普通无 oracle job 不创建 evaluator；
9. deterministic suite 和至少一次真实模型 smoke 通过；
10. 历史 runtime job 和 Phase 4G10 harness tests 不回归。
