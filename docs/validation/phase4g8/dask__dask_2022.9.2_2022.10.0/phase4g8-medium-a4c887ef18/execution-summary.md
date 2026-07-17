# Phase 4G8 Medium interrupted run

## Result

- Run: `phase4g8-medium-a4c887ef18`
- Classification: `runtime-correctness-failed`
- Official evaluator attempts: `0`
- Capability conclusion: none

The controlled worker interruption succeeded and Runtime resumed the same Codex thread. The worker then produced a
locally verified `candidate_ready` receipt covering 25 changed files. Runtime incorrectly projected that receipt to a
failed implementation node before creating the independent evaluator.

## Root cause

`candidate_ready` required trusted `worker_local` provenance, but `bind_runtime_receipt_provenance()` only attached
provenance to verification nodes. The worker correctly emitted `candidate_ready`; the wrapper preserved the fixed
workspace revision and changed files but left `verification_provenance` empty. This made
`_trusted_evaluator_pending_candidate()` reject the candidate.

The run was stopped after the root cause was confirmed. It is not a task-quality failure and does not count as a valid
Medium capability run.

## Preserved evidence

- Candidate patch: `32691` bytes
- Candidate SHA-256: `e24a6d769d8018121d0257c42592845828c4f51f30068e3d05859a9777bc0643`
- Protected oracle included: `false`
- Same Codex thread resumed after the controlled hard interruption: yes

The implementation now binds non-authoritative `worker_local` provenance for required-evaluator implementation
candidates while retaining independent provenance exclusively for evaluator evidence.
