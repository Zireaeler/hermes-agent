# Hermes Kanban Runtime Kernel Phase 3B 实现计划

Phase 3B 的目标是让真实 decision provider 不只是“能调用”，而是能在
runtime validator 边界内稳定地产生可用 graph patch。

Phase 3 已经完成真实 provider 接入、显式 CLI 调用、`--codex-config`
桥接、no-tools single-shot、provider-smoke dry-run/execute、错误分类和
审计记录。真实 smoke 证明模型源可用、parser 可用、validator 边界有效，
但也暴露了一个正常问题：真实模型第一次返回的 patch 可能 parsed，但被
validator 拒绝。

Phase 3B 专门处理这个质量闭环。

## 目标

第一，强化 decision profiles，让真实模型更容易输出 validator 可接受的
patch，尤其避免空 node key、未知 dependency、没有 goal/gap linkage、
无 target 的 `insert_verifier` 等常见错误。

第二，新增 validator recovery profile。模型输出通过 parser 但被 validator
拒绝时，runtime 可以把 rejected patch 和 validator reason 作为显式 feedback
喂给同一个 no-tools provider，请它返回 corrected patch。

第三，`provider-smoke --execute` 支持 validate-but-no-apply recovery retry。
该机制只用于 smoke/integration 验证，不自动落库，不插入 `graph_patches`，
不创建 `kernel_decisions`，不改变 graph revision。

第四，真实 `.codex` 模型源 smoke 至少应在隔离 job 上产出一次
`validation.status=accepted`。之后可以手动执行一次
`runtime advance --provider real --codex-config`，确认真实 patch 能被
validator apply，并产生完整 audit。

第五，默认单元测试继续使用 fake provider，不依赖真实网络、API key 或
真实模型输出稳定性。

## 非目标

不放宽 validator。

不让 provider 拥有 tools、web_search、worker dispatch 或 DB write 权限。

不把 validator rejection 自动改写成本地 patch。

不在默认 `advance_runtime_job()` 中启用 validator recovery。真实落库路径
仍然是 provider proposal -> parser -> validator -> apply/reject。Recovery
先限制在 `provider-smoke` 和手动集成验证中。

不接真实 compaction provider。

## CLI

新增/强化：

```bash
hermes kanban runtime provider-smoke <job_id> \
  --execute \
  --codex-config \
  --profile graph_patch_decision \
  --validator-retries 1 \
  --json
```

输出应包含：

- `provider_result`
- `validation`
- `recovery_attempts`
- `applied=false`

当第一次 validation rejected 时，`recovery_attempts[1]` 应显示 recovery
profile 下的 corrected patch 和新的 validation result。

## Profiles

Phase 3B 至少包含：

- `graph_patch_decision.md`
- `validator_recovery_decision.md`

`validator_recovery_decision.md` 必须强调：

- 不重复同一个 rejected op shape；
- stale revision 使用当前 request revision；
- unknown node key 不得继续引用；
- `insert_verifier` 必须带 target；
- 不确定时优先创建小的 goal-linked `create_node`。

## 验收标准

第一，fake tests 覆盖 validator recovery retry，且不触网。

第二，`provider-smoke --execute --validator-retries 1` 不落库、不改变 graph
revision。

第三，真实 `.codex` 隔离 smoke 能完成一次 parsed + accepted validation。

第四，真实 `.codex` 隔离 advance 能完成一次 patch applied，graph revision
增加，`runtime decision --json` 能看到 provider/model/profile/request_ref/
response_ref/validator_result。

第五，所有默认相关测试通过。
