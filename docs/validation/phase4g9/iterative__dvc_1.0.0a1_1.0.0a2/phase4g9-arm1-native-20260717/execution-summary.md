# Phase 4G9 Arm 1：Native Codex Orchestra

## 结果

- Official resolved：`False`
- FAIL_TO_PASS: `7/68`
- PASS_TO_PASS: `242/242`
- Wall time：`4667.077s`
- Parent thread：`019f6e39-5b6a-75e2-8c51-2c856bda9084`
- Native implementation/audit subagents：`8`
- Guardian approval sidecars（不计入 worker 数量）：`2`
- 实现侧峰值并发（包含 parent）：`4`
- 时间加权平均实现并发：`3.270567`
- 可观察 implementation turns：`21`
- Native implementation context compactions：`6`
- Candidate patch：`134809 bytes`，`129` 个 changed files

## 冻结协议

一个 standalone Codex parent 使用 `gpt-5.6-sol` 和 `ultra` client semantics（`max` model reasoning 加主动 native multi-agent delegation）。执行期间没有 Hermes Runtime、Decision Provider 或 evaluator feedback。Candidate patch 冻结后，official evaluator 运行一次。

## Native 任务分配

| Agent | Depth | Duration | Compactions | 责任范围 |
| --- | ---: | ---: | ---: | --- |
| `plots_diff` (Faraday) | 1 | 989.144s | 0 | plots、diff 与 CLI 行为 |
| `tree_stream` (Copernicus) | 1 | 846.485s | 0 | tree streaming 与 pulling |
| `stage_run` (Hooke) | 1 | 1206.997s | 0 | stage、run cache 与 dry-run |
| `integration_audit` (Linnaeus) | 1 | 2850.005s | 1 | 跨领域集成审计 |
| `unit_runner` (Hubble) | 1 | 551.482s | 0 | 大范围 unit-test 验证 |
| `compat_edges` (Parfit) | 1 | 2863.217s | 1 | 兼容性与 target normalization |
| `targets_scan` (Helmholtz) | 2 | 295.008s | 1 | 嵌套 target API 扫描 |
| `pyupgrade_audit` (Beauvoir) | 1 | 994.571s | 1 | Python 3.6 migration 审计 |

所有 native subagents 共享 parent workspace。它们是 ephemeral Codex threads，不是 durable Hermes nodes 或隔离 worktrees。一个 depth-1 agent 创建了 depth-2 `targets_scan` agent。

观察到的 collaboration calls：`followup_task=6`, `list_agents=25`, `send_message=49`, `spawn_agent=9`, `wait_agent=20`。
共调用 `9` 次 `spawn_agent`，形成 `8` 个 subagent sessions；`2` 次 collaboration call 失败，其中 `2` 次因 thread limit 被拒绝。Slot 可用后，parent 进行了重试或复用。

## Token 与 Cache 观测

- Implementation input tokens：`162562815`
- Cached input tokens：`156236288`
- Implementation output tokens：`554232`
- Reasoning output tokens：`248325`
- 可观察 implementation cache ratio：`0.961083`
- Guardian input/output tokens：`72355` / `267`

以上数据是各 rollout 最终 cumulative token counters 之和。Implementation 行不包含 guardian usage；后者可在 `run-report.json` 中单独识别。Terminal 后 collector failure 导致精确 model-proxy request count 无法恢复，但该可选遥测不影响 worker 行为分析。

## Parent 自报 Terminal Summary

以下内容是 parent 在接触 official evaluator 前的 terminal 自报。Benchmark 质量以上方 official `7/68` 结果为准。

Implemented and integrated the full software evolution scope.

Key changes:

- Added plural `dvc plots`, plot markers, merged output schema, template newlines, HTML doctype, and non-zero Y-axis behavior.
- Refactored stage commit/run/dry-run behavior, run-cache terminology and transfers, including uncached and directory outputs.
- Added RepoTree/DvcTree remote streaming, pull fallback, historical-tree handling, and GitTree-compatible utilities.
- Added unborn-repository diff, recursive update, Markdown diffs, strict stage names, remote validation/unset, S3 KMS configuration, and scalar-target normalization.
- Dropped Python 3.5 metadata and applied safe Python 3.6 modernization.

Subagents handled plots/diff, tree streaming, stage/run-cache, test execution, compatibility review, and modernization auditing. I integrated their work, then added final fixes for no-remote RepoTree fallback and update completion coverage.

Verification:

- Final unit suite: **432 passed, 9 skipped**.
- Final focused functional suite: **49 passed**.
- Broad functional run: **774 passed, 56 skipped, 11 environment-related deselections**.
- Post-modernization affected functional suite: **344 passed, 7 skipped, 2 deselected**.
- Flake8: **0 findings**.
- `compileall`, `setup.py check`, `git diff --check`, and Bash completion syntax all passed.

Limitations:

- Exact `pyupgrade==2.4.4`/pre-commit execution was unavailable offline. Safe deterministic rewrites were applied and audited, but the exact hook remains unverified.
- Optional HDFS tests were unavailable because `pyarrow` is not installed; Zsh syntax could not be executed because Zsh is unavailable.
- Cleanup-only blocker: generated `.pytest-*` directories remain untracked because the destructive cleanup approval failed with a 502. The intentional untracked source files are `dvc/command/plots.py` and `tests/func/test_plot_markers.py`.

No official evaluator result was available or claimed.

## 测量边界

这是单次运行的架构 baseline，不是模型排行榜结果。Native orchestra 终止前无法访问 hidden tests、gold content、历史 candidates 或 evaluator diagnostics。

## 架构解读

Native parent 确实主动使用了 orchestra：占满 4-thread implementation budget、交换 follow-up messages、复用已完成 slots，并委派一次 nested scan。因此这是真实的 native orchestra baseline，不是伪装成多 agent 的单 agent run。

但 one-shot hidden-oracle 结果仍只有 `7/68` FAIL_TO_PASS with `242/242` PASS_TO_PASS。这不能证明 Hermes orchestration 更强：此前 Kernel Large run 获得了多轮 official evaluator feedback，而冻结 Arm 1 没有。公平的 Arm 2 对照必须使用相同 evaluator boundary 和质量门禁。
