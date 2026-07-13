# Phase 4G8 真实验证证据

本目录保存 Phase 4G8 正式真实任务的不可变报告副本。报告同时记录 Runtime correctness 与
任务解决能力，不以 worker 自报测试通过替代 independent evaluator 结果。

## Small

实例：`pydantic__pydantic_v2.6.0b1_v2.6.0`

Run：`phase4g8-small-6dafeda34c`

结论：

- Runtime Validation：通过；
- End-to-End Capability Validation：未通过；
- 分类：`runtime-correct/task-failed`；
- official evaluator：连续 3 次 `FAIL_TO_PASS 0/1`、`PASS_TO_PASS 51/51`；
- consistency violation/warning：`0/0`；
- duplicate terminal/ledger fact：`0/0`；
- real compaction fallback：`0`。

报告：

- [中文能力过程记录](pydantic__pydantic_v2.6.0b1_v2.6.0/phase4g8-small-6dafeda34c/capability-trace.md)
- [结构化能力过程记录](pydantic__pydantic_v2.6.0b1_v2.6.0/phase4g8-small-6dafeda34c/capability-trace.json)
- [Runtime 原始运行报告](pydantic__pydantic_v2.6.0b1_v2.6.0/phase4g8-small-6dafeda34c/run-report.json)

该 run 的任务失败不能解释为 Runtime 失败。Runtime 正确拒绝了 worker 的本地 `4662 passed`
自报，使用固定 revision independent evaluator 发现 discriminated-union JSON schema 缺口，并连续
创建两个 evidence-backed recovery responsibility。两轮 recovery 均未修复 evaluator 所要求的
discriminator emission，最终由 task-quality 预算停止继续扩图，完整保留 open gap 与失败证据。

## Medium

实例：`dask__dask_2022.9.2_2022.10.0`

Run：`phase4g8-medium-26741ac7ab`

结论：

- Runtime Validation：通过；
- End-to-End Capability Validation：未通过；
- 分类：`runtime-correct/task-failed`；
- 最终有效 official evaluator：`FAIL_TO_PASS 32/44`、`PASS_TO_PASS 2860/2861`；
- consistency violation/warning：`0/0`；
- duplicate terminal/ledger fact：`0/0`；
- real compaction fallback：`0`；
- evaluator 记录：2 次有效结果，另有 1 次 `stale_target_revision` infrastructure-invalid；
- 停止原因：medium 结论形成后按 operator 要求停止，不再继续 remediation。

报告：

- [中文能力过程记录](dask__dask_2022.9.2_2022.10.0/phase4g8-medium-26741ac7ab/capability-trace.md)
- [结构化能力过程记录](dask__dask_2022.9.2_2022.10.0/phase4g8-medium-26741ac7ab/capability-trace.json)
- [Runtime 原始运行报告](dask__dask_2022.9.2_2022.10.0/phase4g8-medium-26741ac7ab/run-report.json)

该 run 覆盖了 worker `SIGKILL`、同一 Codex backend session resume、real compaction、独立
evaluator、fixed revision、stale-target 拒绝、receipt ingest 和 backend session projection 收敛。
Worker 将 PASS_TO_PASS 失败从首次有效 evaluator 的 10 个降到最终 1 个，但 12 个
FAIL_TO_PASS 仍未解决，因此不能判定任务完成。

## Large

未运行。operator 明确要求 medium 结束后停止，不再尝试 large；本目录不生成或暗示 large
验证结果。
