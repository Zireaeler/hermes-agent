# Token Budget Compaction

Profile-Version: 1

## Purpose

Use when the active decision session segment is approaching the configured token budget.

## Input Selection

Use the active segment entries, current DB-derived goal contract, progress ledger, open gaps, graph frontier, validator rejection history, human decisions, and artifact index.

## Compaction Goal

Remove repeated deltas and old patch details while preserving the current decision state needed for the next graph patch.

## Must Preserve

- Current objective summary.
- Goal contract revision.
- Satisfied goal items with provenance.
- Open goal gaps with provenance.
- Open blockers and human gates.
- Current graph frontier.
- Validator rejection lessons.
- Important artifact index.
- Do-not-repeat constraints.

## Must Not

- Copy full old transcript entries into the checkpoint.
- Treat unverified or partial evidence as confirmed.
- Treat failed verifiers as passed.
- Invent node, artifact, event, decision, patch, or goal item references.

## Output

Return a checkpoint candidate matching the runtime checkpoint payload contract.
