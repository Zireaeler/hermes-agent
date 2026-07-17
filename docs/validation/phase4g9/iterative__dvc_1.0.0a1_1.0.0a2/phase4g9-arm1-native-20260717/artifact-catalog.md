# Artifact Catalog: Phase 4G9 Native Arm 1

## Run Identity

| Field | Value |
| --- | --- |
| Phase | `4G9` |
| Instance | `iterative__dvc_1.0.0a1_1.0.0a2` |
| Run ID | `phase4g9-arm1-native-20260717` |
| Base commit | `fc42ca721c25bdd24875c999e37fb4f589ecd63c` |
| Dataset revision | `9b83d5af943ba7a17567336f5b18239f73960219` |
| Protocol commit | `0059774` |
| Protocol SHA-256 | `05578a73404caa1550bceb5a97ba89d3dfc7b3036e5de6939288a2269f792b38` |
| Model | `gpt-5.6-sol` |
| Codex | `0.144.4`, `ultra` client profile, `max` wire effort |
| Parent thread | `019f6e39-5b6a-75e2-8c51-2c856bda9084` |
| Result | unresolved, F2P `7/68`, P2P `242/242` |

## Git-Preserved Evidence

| Artifact | Status | SHA-256 / identity | Notes |
| --- | --- | --- | --- |
| `candidate.patch` | preserved | `494c5e7bb04a8a33e85de387e7d541f7197eacfc2b57a73b4565641278636931` | Complete binary candidate patch, 134,809 bytes |
| `candidate.json` | preserved | Git object | Base, changed-file manifest, patch hash |
| `run-report.json` | preserved | Git object | Commands, session summaries, collaboration summaries, evaluator diagnostics |
| `execution-summary.md` | preserved | Git object | Human-readable execution account |
| `arm1-architecture-conclusion.md` | preserved | Git object | Architecture interpretation and quality gate |
| official evaluator result | preserved in report | invocation count `1` | F2P `7/68`, P2P `242/242` |

## Raw Evidence Inventory

| Artifact set | Expected location | State | What remains |
| --- | --- | --- | --- |
| Parent/subagent rollout JSONL | `codex-home/sessions/` | **lost during cleanup** | Per-session identity, timing, tokens, compaction count, terminal summary in `run-report.json` |
| Outer Codex event stream | `worker-events/codex-exec.jsonl` | **lost during cleanup** | 291 normalized commands and outer terminal summary in report |
| Codex stderr | `worker-events/codex-stderr.log` | **lost during cleanup** | No complete substitute |
| Collaboration payloads | native rollout JSONL | **lost during cleanup** | 109 summarized calls, task names, statuses, and ciphertext hashes in report |
| Guardian sessions | `codex-home/sessions/` | **lost during cleanup** | Two guardian identities and derived usage/terminal summaries in report |
| Optional provider telemetry | transport proxy | not persisted before collector failure | Not required for worker or Runtime analysis; aggregate token/cache facts survive elsewhere |
| Workspace | `workspace/` | removed, reconstructable | Base commit plus complete candidate patch retained |
| Evaluator raw directory | protected evaluator root | removed after diagnostic extraction | Standardized full failed-test list and bounded diagnostics retained in report |
| Qualification base/gold checkout | protected qualification root | removed, rebuildable | Dataset revision, image, base/gold qualification outcomes retained in protocol/report |
| Toolchain | shared toolchain root | removed, rebuildable | Environment identity and test claims retained; raw toolchain not retained |
| Docker image | local image store | removed, rebuildable | Image tag retained; immutable digest was not added to the final report |

No stable raw artifact-store copy exists for this run. Searches of `/tmp`,
`/var/tmp`, the main `~/.codex`, deleted file descriptors, and ext4 deleted
inodes found no recoverable session copy.

## Cleanup Record

The cleanup removed:

- `/tmp/hermes-phase4g9-real` including workspace, isolated Codex home, raw
  worker events, and duplicate reports;
- `/tmp/phase4g9-qualification`;
- the shared Phase 4G8/4G9 worker toolchain;
- Docker image `xingyaoww/sweb.eval.x86_64.iterative_s_dvc-3760`;
- pytest and bytecode caches.

Removing image, toolchain, qualification checkouts, workspace, and caches was
within the rebuildable cleanup scope. Removing the isolated Codex home and raw
worker events was not. This catalog records the evidence loss explicitly.

## Sensitivity And Integrity

The Git archive contains no model-source API key or real model-source base URL.
Other Codex configuration and execution content are not classified as
sensitive by this project. Gold patch and protected tests remain excluded from
Git for benchmark integrity.
