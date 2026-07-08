# Anti-Stuck Compaction

Profile-Version: 1

## Purpose

Use when anti-stuck policy detects repeated non-progress.

## Input Selection

Prioritize repeated gap attempts, failed nodes, uncertain worker receipts, noop decisions, rejected patches, and stale frontier summaries.

## Compaction Goal

Summarize the failure pattern and force the next decision session to change strategy.

## Must Preserve

- Stuck signal type.
- Gap keys with repeated attempts.
- Failed node families.
- Rejected or ineffective strategies.
- Do-not-repeat actions.
- Strategy constraints for the next patch.

## Must Not

- Recommend another identical retry.
- Hide failed verifier or contradicted evidence.
- Convert uncertainty into completion evidence.

## Output

Return a checkpoint candidate with known_failure_boundaries, do_not_repeat, validator_rejection_lessons, and next_strategy_constraints populated with provenance.
