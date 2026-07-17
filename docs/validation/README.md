# Hermes Runtime Validation Index

This directory is the Git-visible catalog for real Runtime and model-provider
validation. Compact reports live in Git; raw sessions, worker events, provider
traces, Runtime state, and evaluator artifacts may live in the persistent
artifact store identified by each run's `artifact-catalog.md`.

Retention rules: [Runtime Validation Artifact Retention](../kanban-runtime-validation-artifact-retention.md).

## Phase 4G8

- [Phase 4G8 validation index](phase4g8/README.md)
- DVC Large: `phase4g8-large-a101c61de3`, runtime-correct/task-failed,
  best primary `58/68`, final `55/68`.

Historical Phase 4G8 runs were created before raw-evidence retention became a
hard requirement. Their run directories may contain only reports; inspect each
run's retention record before assuming native sessions or Runtime DB state
still exist.

## Phase 4G9

- [Phase 4G9 validation index](phase4g9/README.md)
- Native Arm 1: `phase4g9-arm1-native-20260717`, unresolved, `7/68` F2P,
  `242/242` P2P.

Phase 4G9 Arm 1 preserves a detailed derived report but lost its original raw
Codex session and worker event files during cleanup. This is explicitly marked
in its artifact catalog.
