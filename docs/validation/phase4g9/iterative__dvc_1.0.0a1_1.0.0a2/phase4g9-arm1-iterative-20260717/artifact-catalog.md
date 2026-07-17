# Phase 4G9 Arm 1 Iterative Artifact 目录

## 远端可读证据

- [执行总结](execution-summary.md)：结果、逐轮分数、关键过程和停止原因；
- [完整能力过程](capability-trace.md)：运行期间的细粒度观察记录；
- [架构结论](architecture-conclusion.md)：native orchestra 的优缺点及 Arm 2 约束；
- [结构化报告](run-report.json)：12 轮 candidate/evaluator lineage、worker/session 和 integrity；
- [最佳 candidate metadata](candidate.json)：Round 10 冻结候选；
- [最佳 candidate patch](candidate.patch)：Round 10 完整 binary-safe patch。

这些文件不包含模型源 API key、认证文件或真实 base URL。

## 本机稳定原始档案

```text
/root/hermes-validation-artifacts/phase4g9/
  iterative__dvc_1.0.0a1_1.0.0a2/
  phase4g9-arm1-iterative-20260717/
```

Manifest 状态：

```text
status: verified
file_count: 198
total_bytes: 116092457
```

原始档案包含：

- 12 轮 worker outer JSONL 与 stderr；
- parent 和 54 个 native subagent 的 isolated Codex session 数据；
- 12 次 official evaluator invocation；
- 每轮 candidate metadata 与 patch；
- runner state、最终 report 和监控笔记；
- credential-redacted isolated Codex 配置。

原始档案未进入 Git，原因是体积约 116 MB，且包含大量机器级 session/SQLite 数据。它不是被清理
或丢失，而是保存在上述稳定路径，并由 `manifest.json` 逐文件校验。

## 已知完整性边界

- `codex-home/auth.json` 已从 archive 排除；
- worker 未读取 gold patch 或 protected test source；
- worker 曾读取 `/tmp/phase4g9-arm1-finalize.json` 与 `/tmp/py36-review.diff`；
- 因此 run 标记为 `historical-artifact-contaminated`；
- Round 13 未产生 candidate/evaluator，原始 live event 保留，但不计入正式 lineage。

`run-report.json` 中的 `integrity.protocol_sha256` 固定指向运行时版本的 Phase 4G9 Iterative
协议。顶层协议文档在实测后追加了第 10 节基础设施修正规则，因此当前文件 hash 与运行时冻结
hash 不同；运行时版本可由本报告提交的父版本恢复，原始 hash 仍保留在结构化报告中。
