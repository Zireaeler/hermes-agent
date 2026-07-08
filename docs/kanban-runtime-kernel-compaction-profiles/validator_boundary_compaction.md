# Validator Boundary Compaction

Profile-Version: 1

## Purpose

Use after repeated patch rejections or parser failures.

## Input Selection

Prioritize validator results, rejected patches, parse failures, allowed patch ops, current graph revision, and current open gaps.

## Compaction Goal

Preserve the validator boundary so the next decision session does not repeat invalid patch shapes.

## Must Preserve

- Rejected ops and reasons.
- Unknown references that caused rejection.
- Stale revision lessons.
- Goal linkage requirements.
- Forbidden ops such as release_node and direct complete_job.

## Must Not

- Convert rejected patches into accepted facts.
- Preserve invalid patch JSON as a suggested next action.
- Omit currently open blockers.

## Output

Return a checkpoint candidate with validator_rejection_lessons and do_not_repeat populated with provenance.
