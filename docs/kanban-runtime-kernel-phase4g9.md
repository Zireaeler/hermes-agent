# Hermes Kanban Runtime Kernel Phase 4G9

# Native Orchestra Comparison

## 1. Purpose

Phase 4G8 proved that the Runtime Kernel can preserve execution continuity,
recover from process failures, and keep completion evidence honest during a
real long-running SWE-EVO task. It did not prove that durable system-level
orchestration improves final implementation quality. The best DVC Large
primary worker reached `58/68` FAIL_TO_PASS, while the later durable strategy
worker reached only `55 -> 56 -> 55/68`.

Phase 4G9 defines a controlled comparison between:

```text
Arm 1: one native Codex parent with ephemeral internal subagents

Arm 2: Hermes Runtime Kernel with durable system-level workers
```

The comparison asks whether Runtime-level orchestration can match the final
quality of native parent/subagent orchestration. Runtime may be slower, but it
must not obtain a worse candidate merely because work crossed durable worker
boundaries.

This document freezes the comparison protocol and the Arm 1 execution
contract. It does not design or implement Arm 2.

---

## 2. Benchmark

The frozen task is the qualified SWE-EVO DVC evolution instance:

```text
instance: iterative__dvc_1.0.0a1_1.0.0a2
base commit: fc42ca721c25bdd24875c999e37fb4f589ecd63c
dataset revision: 9b83d5af943ba7a17567336f5b18239f73960219
official image: xingyaoww/sweb.eval.x86_64.iterative_s_dvc-3760
worker-visible SRS items: 34
FAIL_TO_PASS: 68
PASS_TO_PASS: 242
```

The locked oracle qualification is:

| Revision | FAIL_TO_PASS | PASS_TO_PASS |
|---|---:|---:|
| base | 0/68 | 242/242 |
| gold | 68/68 | 242/242 |

Gold is used only to qualify the evaluator. It is never available to either
comparison arm.

---

## 3. Frozen Arm 1 Contract

Arm 1 is one standalone native Codex execution:

```text
fresh base workspace
        |
        v
one native Codex parent
        |
        +-- optional ephemeral internal subagents
        |
        v
terminal candidate revision
        |
        v
one fixed official evaluator run
```

Arm 1 must use:

- Codex CLI `0.144.4`;
- model `gpt-5.6-sol`;
- `model_reasoning_effort = "ultra"`;
- native MultiAgentV2 enabled;
- at most four simultaneously active Codex threads including the parent;
- a fresh isolated `CODEX_HOME`, thread history, and workspace;
- the configured model provider's WebSocket transport settings;
- unrestricted workspace execution with non-interactive approval handling;
- one parent responsible for integration and the terminal result.

In this Codex build, `ultra` has two relevant semantics:

1. model requests use `max` reasoning effort;
2. the client selects proactive native multi-agent delegation instructions.

It is therefore the native orchestra profile for Arm 1, not a model effort
above `max`. The runner must not replace it with a hand-written approximation.

Subagents are ephemeral Codex execution threads. They may inspect, implement,
test, and communicate with the parent through native collaboration tools. They
do not become Hermes execution nodes and do not use Hermes Runtime state.

---

## 4. Execution Prompt Boundary

The parent receives:

- the complete worker-visible 34-item SRS;
- the exact base workspace;
- the trusted worker environment setup needed to run project tests;
- responsibility for understanding, planning, implementation, integration,
  testing, debugging, and final verification;
- explicit permission to use native subagents proactively where that improves
  speed or quality;
- the requirement to continue until a terminal candidate or a real blocker.

The prompt must not prescribe a planner/coder/tester topology. Subagent count,
roles, task allocation, communication, and concurrency are outputs of the
native orchestra, not harness decisions.

The parent and all subagents are forbidden from reading or receiving:

- the gold patch or upstream target implementation;
- protected test patch content or protected evaluator files;
- Phase 4G8 historical candidate patches or worker transcripts;
- Phase 4G8 evaluator scores or diagnostics;
- Hermes memory, Decision Session, checkpoint, graph, ledger, or provider
  guidance;
- any official evaluator result during execution.

Project-visible tests and tests written by the agents are allowed. The hidden
oracle is not.

---

## 5. Isolation and Integrity

Arm 1 uses independent roots for:

```text
phase4g9/<run-id>/
  codex-home/
  workspace/
  protected/
  worker-events/
  evaluator-runs/
  reports/
```

Required checks before execution:

- workspace `HEAD` equals the locked base commit and is clean;
- the workspace has no remote after materialization;
- `CODEX_HOME` contains no copied sessions, memories, skills, plugins, or
  unrelated project trust entries;
- only the selected provider configuration and credential are copied;
- protected paths are neither readable nor writable by the Codex execution
  identity;
- no historical Phase 4G8 run directory is mounted or added to the workspace;
- source `~/.codex/config.toml` and `auth.json` hashes remain unchanged.

The evaluator runs only after the parent has terminated and the candidate
revision and patch hashes have been frozen. The evaluator operates on that
fixed candidate and cannot alter the worker workspace.

---

## 6. Evaluator Rule

The official evaluator is benchmark measurement only. It is not an Arm 1
worker, verifier node, or feedback source.

The harness must enforce:

```text
evaluator invocations before terminal candidate = 0
evaluator invocations after terminal candidate  = 1
evaluator feedback turns sent to Codex           = 0
```

The single evaluator result records:

- resolved status;
- FAIL_TO_PASS passed/total;
- PASS_TO_PASS passed/total and regression count;
- candidate patch hash and bytes;
- fixed target revision;
- evaluator image and dataset revision;
- evaluator wall time and infrastructure status.

No rerun, retry with model changes, best-of-N selection, or post-evaluator fix
is allowed. Infrastructure failure may invalidate the run, but it does not
authorize silently scoring a different candidate.

---

## 7. Resource Ceiling

Arm 1 is allowed to run long enough to represent a serious native Codex
attempt, while retaining a finite safety boundary:

- parent wall-time ceiling: 6 hours;
- maximum active threads: 4 including the parent;
- maximum one root Codex execution;
- maximum one official evaluator invocation;
- no artificial daemon restart, worker kill, lease expiry, or compaction
  trigger;
- terminate only the process group owned by this run when the ceiling expires.

Hitting the wall-time ceiling produces a terminal resource-limit candidate,
not a hidden continuation or a second attempt.

---

## 8. Required Evidence

The Arm 1 archive must contain:

- frozen protocol version and runner configuration;
- source/config integrity hashes with credentials redacted;
- parent thread ID;
- child thread IDs, native task names, prompts, statuses, and timing;
- spawn, message, follow-up, wait, and close events;
- peak and time-weighted concurrency where observable;
- parent command/test activity and changed-file summary;
- terminal parent message and exit status;
- exact candidate patch and candidate revision/hash;
- one official evaluator result;
- wall time;
- input, cached input, output, and reasoning output tokens;
- model call/turn count where observable;
- cache-hit ratio;
- process cleanup result;
- a readable execution narrative explaining how the parent allocated work,
  integrated results, tested the implementation, and where it failed if
  unresolved.

The archive may retain redacted native Codex JSONL events needed to audit
orchestration. It must not publish credentials, hidden tests, gold content, or
private model reasoning.

---

## 9. Frozen Comparison Gates

Arm 1 is the single baseline candidate. Future Arm 2 must use the same task,
base commit, dataset revision, official image, SRS, model family, and one-shot
evaluator rule.

Quality is compared in this order:

1. If Arm 1 is resolved, Arm 2 must also be resolved.
2. Arm 2 must not introduce more PASS_TO_PASS regressions than Arm 1.
3. If both are unresolved, Arm 2 FAIL_TO_PASS passed count must be greater
   than or equal to Arm 1.
4. No arm may use best-of-N, evaluator-guided retries, gold knowledge, or a
   historical candidate.

Wall time, token cost, cached input, model calls, concurrency, handoffs, and
changed files are reported as secondary metrics. Runtime may be slower; the
primary gate is final task quality non-inferiority.

The comparison does not claim statistical model superiority from one sample.
It answers a narrower architecture question: on this qualified large task,
did durable Runtime orchestration preserve or improve the quality achieved by
native parent/subagent orchestration?

---

## 10. Arm 1 Acceptance Criteria

Arm 1 is complete when:

- the frozen config and isolation preflight pass;
- one standalone native Codex parent starts on the clean base workspace;
- native subagent behavior is captured without being prescribed by the
  harness;
- the execution ends at a terminal candidate or documented resource/blocker
  boundary;
- the candidate is frozen before evaluator access;
- the official evaluator runs exactly once;
- no evaluator feedback reaches the parent or any subagent;
- required process, orchestration, token, cache, candidate, and quality
  evidence is archived;
- rebuildable image, toolchain, workspace, and transient caches are removed
  after the report is produced;
- the protocol, runner, tests, and Arm 1 report are committed and pushed.

Arm 1 completion does not require `68/68`. A non-resolved result is still a
valid baseline if the execution and measurement protocol remained intact.

---

## 11. Deferred Arm 2

Arm 2 requires separate design work for early structure assessment, durable
worker write isolation, integration ownership, best-revision preservation,
and suppression of nested subagent orchestration inside Runtime workers.

Those changes are explicitly outside the Arm 1 goal and must not be inferred
from or implemented by this document.

---

## 12. Frozen Arm 1 Baseline

Arm 1 completed on 2026-07-17. The immutable comparison baseline is:

```text
resolved: false
FAIL_TO_PASS: 7/68
PASS_TO_PASS: 242/242
parent wall time: 4667.077 seconds
implementation subagents: 8
peak implementation concurrency: 4 including parent
time-weighted average implementation concurrency: 3.270567
official evaluator invocations: 1
evaluator feedback turns: 0
```

The pre-run frozen protocol is the Section 1-11 document at commit `0059774`,
with SHA-256 `05578a73404caa1550bceb5a97ba89d3dfc7b3036e5de6939288a2269f792b38`.
This result section was appended only after the terminal candidate and one-shot
evaluation were complete.

The exact candidate patch SHA-256 is:

```text
494c5e7bb04a8a33e85de387e7d541f7197eacfc2b57a73b4565641278636931
```

The native Codex rollout stores collaboration message bodies as encrypted
content. The archive therefore records task names, sender/target, timing,
tool result, and ciphertext hashes rather than pretending plaintext prompts
were observable. The redacted outer event stream did not expose spawn calls.
This limitation does not affect child-session identity or concurrency evidence.

The post-terminal patch collector initially failed on generated non-UTF-8
pytest artifacts. Recovery removed only top-level `.pytest-*` directories,
did not resume Codex, and invoked the evaluator exactly once. Exact model-proxy
request counters were not persisted before that collector failure and remain
an explicit observability gap.

The archived report and architecture conclusion are under:

```text
docs/validation/phase4g9/
  iterative__dvc_1.0.0a1_1.0.0a2/
    phase4g9-arm1-native-20260717/
```
