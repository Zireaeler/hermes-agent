# Milestone Transition Compaction

Profile-Version: 1

## Purpose

Use when the active milestone changes or a major goal slice completes.

## Input Selection

Prioritize satisfied goal items, verified artifacts, completed verifier nodes, remaining open gaps, and the next active frontier.

## Compaction Goal

Close the previous working slice and produce a concise starting point for the next milestone.

## Must Preserve

- Completed milestone summary.
- Verified goal evidence.
- Artifact index needed by the next slice.
- Remaining required gaps.
- Known failure boundaries from the previous slice.

## Must Not

- Mark the whole job complete unless local completion rules already did.
- Drop hard constraints or active human gates.
- Reintroduce old phase workflow assumptions.

## Output

Return a checkpoint candidate with satisfied_goal_items, artifact_index, open_goal_gaps, and next_strategy_constraints populated with provenance.
