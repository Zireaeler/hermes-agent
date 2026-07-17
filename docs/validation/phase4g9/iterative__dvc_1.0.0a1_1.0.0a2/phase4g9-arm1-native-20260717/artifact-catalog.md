# Artifact 目录：Phase 4G9 Native Arm 1

## Run 身份

| 字段 | 值 |
| --- | --- |
| Phase | `4G9` |
| Instance | `iterative__dvc_1.0.0a1_1.0.0a2` |
| Run ID | `phase4g9-arm1-native-20260717` |
| Base commit | `fc42ca721c25bdd24875c999e37fb4f589ecd63c` |
| Dataset revision | `9b83d5af943ba7a17567336f5b18239f73960219` |
| Protocol commit | `0059774` |
| Protocol SHA-256 | `05578a73404caa1550bceb5a97ba89d3dfc7b3036e5de6939288a2269f792b38` |
| Model | `gpt-5.6-sol` |
| Codex | `0.144.4`，`ultra` client profile，`max` wire effort |
| Parent thread | `019f6e39-5b6a-75e2-8c51-2c856bda9084` |
| 结果 | unresolved，F2P `7/68`，P2P `242/242` |

## Git 中保留的证据

| Artifact | 状态 | SHA-256 / identity | 说明 |
| --- | --- | --- | --- |
| `candidate.patch` | 已保留 | `494c5e7bb04a8a33e85de387e7d541f7197eacfc2b57a73b4565641278636931` | 完整 binary candidate patch，134,809 bytes |
| `candidate.json` | 已保留 | Git object | Base、changed-file manifest 和 patch hash |
| `run-report.json` | 已保留 | Git object | Commands、session summaries、collaboration summaries 和 evaluator diagnostics |
| `execution-summary.md` | 已保留 | Git object | 可读执行过程 |
| `arm1-architecture-conclusion.md` | 已保留 | Git object | 架构结论与质量门禁 |
| official evaluator result | 已保留在 report | invocation count `1` | F2P `7/68`，P2P `242/242` |

## 原始证据清单

| Artifact set | 预期位置 | 状态 | 现存替代证据 |
| --- | --- | --- | --- |
| Parent/subagent rollout JSONL | `codex-home/sessions/` | **cleanup 时丢失** | `run-report.json` 中保留 per-session identity、timing、tokens、compaction count 和 terminal summary |
| Outer Codex event stream | `worker-events/codex-exec.jsonl` | **cleanup 时丢失** | Report 中保留 291 条 normalized commands 和 outer terminal summary |
| Codex stderr | `worker-events/codex-stderr.log` | **cleanup 时丢失** | 无完整替代证据 |
| Collaboration payloads | native rollout JSONL | **cleanup 时丢失** | Report 中保留 109 条 summarized calls、task names、statuses 和 ciphertext hashes |
| Guardian sessions | `codex-home/sessions/` | **cleanup 时丢失** | Report 中保留两个 guardian identities 及 derived usage/terminal summaries |
| 可选 provider telemetry | transport proxy | collector failure 前未持久化 | 分析 worker 或 Runtime 不依赖该信息；aggregate token/cache facts 保存在其他字段 |
| Workspace | `workspace/` | 已删除，可重建 | Base commit 与完整 candidate patch 已保留 |
| Evaluator raw directory | protected evaluator root | diagnostics extraction 后删除 | Report 中保留完整 failed-test list 和 bounded diagnostics |
| Qualification base/gold checkout | protected qualification root | 已删除，可重建 | Protocol/report 中保留 dataset revision、image 与 base/gold qualification outcomes |
| Toolchain | shared toolchain root | 已删除，可重建 | 保留 environment identity 和 worker 自报测试；未保留 raw toolchain |
| Docker image | local image store | 已删除，可重建 | 保留 image tag；最终 report 未记录 immutable digest |

本 run 不存在 stable raw artifact-store copy。对 `/tmp`、`/var/tmp`、主 `~/.codex`、
deleted file descriptors 和 ext4 deleted inodes 的检查均未找到可恢复 session 副本。

## Cleanup 记录

Cleanup 删除了：

- `/tmp/hermes-phase4g9-real`，其中包含 workspace、isolated Codex home、raw worker events
  和 reports 的重复副本；
- `/tmp/phase4g9-qualification`；
- Phase 4G8/4G9 共用 worker toolchain；
- Docker image `xingyaoww/sweb.eval.x86_64.iterative_s_dvc-3760`；
- pytest 与 bytecode caches。

删除 image、toolchain、qualification checkouts、workspace 和 caches 属于可重建 cleanup
范围；删除 isolated Codex home 与 raw worker events 不属于该范围。本目录明确记录这次
证据损失。

## 敏感信息与实验完整性

Git archive 不包含模型源 API key 或真实模型源 base URL。其他 Codex configuration 和
execution content 不属于本项目定义的敏感信息。Gold patch 和 protected tests 因 benchmark
integrity 不进入 Git。
