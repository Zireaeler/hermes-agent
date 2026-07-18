# Phase 4G10 普通 Runtime Durable Orchestra Smoke

## 1. 结论

Run：`rjob_74527b2a65c0`

该 run 使用普通 `runtime create`、本地配置和 production worker-smoke 路径完成，不使用 Phase 4G8/4G10
benchmark harness，不使用 hidden oracle，也没有创建 evaluator node。

| 验收项 | 结果 |
| --- | --- |
| Runtime job | `done` |
| Goal item | `release-report = satisfied` |
| Early assessment | `expand` |
| Durable children | `2`，隔离 worktree 并行执行 |
| Frozen contributions | `2`，共 `7,211` bytes，hash/scope verified |
| Primary integration | 原 session resume，`resume_count=1` |
| 本地验收 | `8/8` unittest passed，`git diff --check` passed |
| Evaluator nodes | `0` |
| Runtime consistency | `0 violation / 0 warning` |
| Operator cleanup | 两个 worktree 删除，两个 contribution 保留 |

这证明 Phase 4G10 的 durable orchestra 不再只存在于 SWE-EVO harness 中，普通 Runtime job 可以通过
显式 opt-in 使用相同的结构，同时保留默认 single-primary 和无 oracle 不创建 evaluator 的边界。

## 2. 测试任务

测试仓库是一个小型 release-report brownfield fixture。初始代码包含三个未实现入口和 5 个失败测试：

- `metrics.py`：加载/验证 JSONL event，并按 service 聚合；
- `render.py`：生成格式稳定的 Markdown；
- `cli.py`：组合前两者并实现 `--output`；
- 验收命令：`PYTHONPATH=src python3 -m unittest discover -s tests -v`。

任务有两个真实低耦合责任：metrics 与 renderer 各自拥有独立模块和测试；CLI、跨模块 contract、最终集成
和 broad verification 必须由 primary 保留。这使测试既可拆分，又不需要人为制造 evaluator。

## 3. 实际执行结构

```text
Decision Provider
  -> release-report-primary
       -> read-only early assessment
            -> release-report-metrics (isolated worktree / fresh Codex session)
            -> release-report-render  (isolated worktree / fresh Codex session)
                  -> two frozen contributions
       -> original release-report-primary session resumes
            -> verifies hashes and patch applicability
            -> integrates both contributions unchanged
            -> implements cli.py
            -> runs focused and full tests
                  -> goal satisfied / job done
```

Runtime 共创建 `3` 个 execution nodes、`4` 次 materialization：primary assessment、两个并行 child、primary
integration。Decision Provider 只调用 `2` 次，两个 patch 均 accepted，没有 strategy node 或 verifier。

## 4. Early Assessment

Primary 第一次 materialization 只读检查仓库后，返回：

```text
recommendation: expand
integration owner: release-report-primary
child limit: 2
```

拆分结果：

| Child | Declared write scope | 责任 |
| --- | --- | --- |
| `release-report-metrics` | `src/release_report/metrics.py`、`tests/test_metrics.py` | JSONL、validation、aggregation |
| `release-report-render` | `src/release_report/render.py`、`tests/test_render.py` | deterministic Markdown |

Primary 明确保留 `cli.py`、`test_cli.py`、summary contract、imports 和 full-suite verification。两个 child
同时开始和结束，Runtime 没有把传统的 analysis/implementation/test 阶段拆成 durable nodes。

## 5. Child 贡献

| Child | Artifact | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| metrics | `art_7d1353de8f55` | `5,490` | `aae971d43f92ee05596b885b7ba5748ee91843ccf34fe8345d32acc92b182354` |
| render | `art_dbbf0c385c74` | `1,721` | `84ad27975fa077df399faf8e8c7bfb84160b5e71a9560f5818378a850e7ecfe7` |

两份 contribution 都通过 declared-scope verification。Primary 在集成前重新计算 SHA-256、运行
`git apply --check`，最终将两份 artifact 都分类为 `accepted`，没有 modified/rejected contribution。

## 6. Primary Resume 与结果

Primary assessment 和 integration 使用同一个 Codex session：

```text
019f771d-d84e-7bf0-aa4d-d2b5becbffed
```

Runtime 记录 `resume_count=1`。Primary 集成两个 patch 后实现共享 CLI，并完成：

- metrics focused tests：`5/5`；
- render focused tests：`2/2`；
- CLI focused tests：`1/1`；
- full suite：`8/8`；
- `git diff --check`：passed；
- 最终 candidate patch SHA-256：`d96f0359b6f456fee8b44cbb08a4785570b08aa0ad4c9083b05425b373d4d829`。

Goal ledger 最终包含两条 child partial evidence 和一条 primary full、`implementation_verified` evidence。

## 7. Provider Transport 观察

两个 child 并行启动时，WebSocket 均遇到一次 `HTTP 403` 后的重连序列，各记录 `9` 条
`Reconnecting` event。Codex 按 `stream_max_retries=20` 自动恢复，两路 worker 都保留原 session 并完成，
没有 Runtime retry、fresh-session replacement 或人工 patch。

这说明普通 orchestra 的进程并行成立，但 provider transport 可能限制同时建立 WebSocket；Runtime 不应
把短暂传输波动误判成结构失败。该 run 没有为此禁用 WebSocket，也没有重建 job。

## 8. 时间与 Token

- Job wall time：`500s`（约 8 分 20 秒）；
- assessment：`65s`；
- 两个 child 并行窗口：`235s`；
- primary integration：`150s`；
- worker input：`625,295`；
- worker cached input：`478,464`；
- worker output：`21,098`；
- worker reasoning output：`6,878`；
- Decision Provider input/output：`11,185 / 1,521`；
- Decision Provider latency：`19,223ms + 18,452ms`。

## 9. 架构判断

该 run 验证的是普通任务中的系统级 orchestra，而不是 evaluator 流程：

- primary 先从仓库证据决定是否拆分；
- child 在独立 workspace、process 和 context 中产出可持久化贡献；
- Runtime 负责 scope、hash、依赖、恢复和 attribution；
- primary 保留共享入口和最终结果责任；
- 无外部 oracle 时，worker 自测 evidence 可以满足普通 goal，不创建形式化 evaluator。

该能力仍是显式 opt-in。对于没有低耦合写边界的任务，early assessment 应返回
`continue_single_node`；不能为了显示 orchestra 存在而强制拆分。

## 10. 原始证据

稳定 artifact archive：

```text
/root/hermes-validation-artifacts/phase4g10-ordinary-runtime/rjob_74527b2a65c0
```

Archive 共 `34` 个文件、约 `1.9 MiB`，包含 DB、worker logs、三份 Codex session JSONL、两份 frozen
contribution、最终 candidate patch、中文报告和 `manifest.sha256`。归档后已执行完整 SHA-256 校验。

稳定归档完成后通过正常 `runtime orchestration --cleanup-worktrees` 入口清理了两个临时 child worktree；
Runtime 重新验证 contribution hash 后执行删除，并记录 `runtime_orchestration_worktrees_cleaned` event。
