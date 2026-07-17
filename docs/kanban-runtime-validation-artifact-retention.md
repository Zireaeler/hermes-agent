# Hermes Runtime Validation Artifact Retention

## 1. Purpose

Long-running validation produces two different outputs:

1. public, compact reports used to understand and compare runs;
2. raw execution evidence used for forensic analysis, replay, and resume.

A report is not a replacement for raw evidence. Cleanup may remove rebuildable
execution material, but it must not remove the only copy of a Codex session,
worker event stream, provider trace, Runtime database, or evaluator output.

## 2. Storage Model

Every run has one stable identity:

```text
phase / benchmark instance / run id
```

Git stores a searchable catalog under:

```text
docs/validation/<phase>/<instance>/<run-id>/artifact-catalog.md
```

Raw evidence may remain outside Git under a stable artifact root:

```text
<artifact-root>/<phase>/<instance>/<run-id>/
  ARTIFACTS.md
  manifest.json
  codex-home/
    sessions/
  worker-events/
  provider-trace/
  runtime-state/
  evaluator/
  candidates/
  reports/
  environment/
```

The default operational location should be a persistent filesystem such as
`/root/hermes-validation-artifacts`, not `/tmp`. Moving a run is allowed only
after updating its catalog and verifying manifest hashes.

## 3. Git Catalog

Each `artifact-catalog.md` records:

- phase, instance, run id, result classification, and completion time;
- protocol commit and hash;
- model, Codex version, effort, and orchestration mode;
- parent and child thread IDs;
- candidate and evaluator result links;
- absolute or artifact-store location of raw evidence;
- SHA-256, size, retention state, and sensitivity class for each artifact set;
- whether an artifact is present, moved, intentionally omitted, or lost;
- cleanup actions and the exact entries removed;
- known observability gaps.

The catalog must remain useful when the raw artifact store is offline. It
therefore includes the final result and enough hashes to identify the run,
but it does not duplicate every raw event.

## 4. Required Raw Evidence

The following are irreplaceable and must be retained for real model runs:

### 4.1 Codex execution

- every parent and subagent rollout JSONL under `codex-home/sessions`;
- thread metadata, compaction events, collaboration calls, tool results, and
  terminal messages;
- outer `codex exec --json` event stream;
- stdout and stderr;
- guardian or approval session events;
- session index/state files needed for native resume.

### 4.2 Runtime execution

- Runtime/Kanban SQLite database or a transactionally consistent snapshot;
- graph patches, reducer events, receipts, ledger entries, checkpoints, and
  decision segment entries;
- daemon, worker, evaluator, and fault-injector logs;
- leases, attempts, materializations, process IDs, and terminal facts;
- policy resolution and capability footer evidence.

### 4.3 Provider transport

- request identity, model, timestamps, retry and timeout events;
- HTTP/WebSocket selection, upgrade, reconnect, and failure events;
- per-request input, cached input, output, and reasoning token counts;
- provider response identifiers and request/response hashes;
- request and response bodies when available.

Provider traces must remove the model-source API key and replace the real
model-source base URL. Other Codex configuration and model execution content
are not treated as sensitive for this project.

### 4.4 Candidate lineage

- base commit and repository identity;
- every evaluator-targeted candidate patch and SHA-256;
- changed and untracked file manifests;
- best-known candidate and promotion/rollback decisions;
- candidate revision or Git bundle when commits were created;
- the final workspace status before cleanup.

The workspace itself is rebuildable when the base revision and a complete
binary patch are retained. It may be removed after candidate reconstruction
has been tested.

### 4.5 Evaluator and environment

- one standardized result per evaluator invocation;
- raw evaluator output, bounded diagnostics, exit status, and wall time;
- exact candidate hash evaluated;
- image tag and immutable image digest;
- benchmark dataset revision and protected-test hash;
- Python, Codex, dependency/toolchain, OS, and environment fingerprints;
- qualification base/gold outcome and manifest hashes.

Gold patches and protected tests are benchmark-integrity assets. They are not
published to Git or exposed to workers. A private artifact store may retain
them, while the public catalog records only hashes and qualification facts.

## 5. Credential Boundary

The model-source API key and real model-source base URL are the credential
sensitive values for these validation archives.

- `auth.json` is never committed;
- API keys are removed from local exported traces and reports;
- the real provider base URL is replaced with a stable placeholder;
- headers, query parameters, environment variables, commands, and exception
  strings are scanned for both values before publication;
- redaction records the original artifact hash and archived artifact hash.

Other Codex configuration, session messages, reasoning, tool calls, and
subagent communication may be retained. Files containing benchmark-protected
content remain private for integrity rather than credential secrecy.

## 6. Cleanup Allowlist

Automatic cleanup may remove only explicitly rebuildable entries:

- Docker images after image digest is recorded;
- dependency toolchains after their manifest and fingerprint are recorded;
- qualification checkouts after protected hashes and outcomes are recorded;
- workspace after candidate reconstruction has been verified;
- pytest caches, bytecode, package caches, and temporary build products;
- seed homes after effective redacted config and hashes are retained.

Automatic cleanup must not remove:

- `codex-home/sessions` or node Codex homes;
- `worker-events`;
- provider traces;
- Runtime database/service state;
- evaluator raw results;
- candidate lineage;
- manifests, catalogs, or reports.

No code may delete an entire run root. Terminal status and a generated report
are not sufficient cleanup predicates.

## 7. Deletion Gate

Deletion of irreplaceable evidence requires all of the following:

1. a verified copy exists in a stable artifact store;
2. `manifest.json` contains hashes and sizes for the copied files;
3. the Git catalog points to that artifact-store identity;
4. credential and benchmark-integrity scans have completed;
5. the user explicitly authorizes deletion of the source copy.

Without all five conditions, cleanup stops and reports the retained path.

## 8. Current Evidence Loss

Phase 4G9 Arm 1 `phase4g9-arm1-native-20260717` predates this policy. Its
derived report, candidate, command list, session summary, and collaboration
event summary survive in Git, but the original isolated Codex homes and raw
worker event stream were deleted without a stable raw archive. Its per-run
catalog records those entries as lost rather than implying complete retention.

## 9. Implemented Gate

`hermes_cli.validation_artifacts` implements the archive and cleanup gate:

- raw evidence is copied through a staging directory and atomically promoted;
- `auth.json` is omitted;
- exact model-source API key and base URL values are replaced in text traces;
- every archived payload file receives source/archive SHA-256, size, and
  redaction-count metadata;
- the completed manifest is read back and every archived hash is verified;
- cleanup rejects missing, invalid, tampered, or wrong-run manifests;
- cleanup rejects every entry outside the rebuildable allowlist.

Phase 4G8 completed-run compaction now archives `codex-homes`, `service`,
`hermes-home`, reports, and any worker/provider event directories before it
removes workspace, home, or seed state. Phase 4G9 native runs archive
`codex-home`, `worker-events`, `provider-trace`, and reports before returning a
successful real-run result.

The artifact root is configurable with `HERMES_VALIDATION_ARTIFACT_ROOT` or the
Phase 4G9 `--artifact-root` option. The default is:

```text
/root/hermes-validation-artifacts
```

The current provider trace records the transport audit and counters. Full
per-request HTTP/WebSocket body capture remains a separate observability
extension; its absence must be stated in a run catalog rather than inferred
from the transport summary.
