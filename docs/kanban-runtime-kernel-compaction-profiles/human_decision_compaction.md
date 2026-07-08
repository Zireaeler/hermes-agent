# Human Decision Compaction

Profile-Version: 1

## Purpose

Use when a user decision changes the goal contract, authorization boundary, credentials boundary, waiver, or high-impact product preference.

## Input Selection

Prioritize human decision events, affected goal items, active human gates, waivers, default policy changes, and constraints.

## Compaction Goal

Make user decisions visible in the next decision session without relying on old transcript text.

## Must Preserve

- Human decision payloads with source refs.
- Affected goal items.
- New hard constraints.
- Waivers and authorization limits.
- Remaining open human gates.

## Must Not

- Treat absent credentials as available.
- Override DB goal contract state.
- Turn user preference into broader authority than granted.

## Output

Return a checkpoint candidate with human_decisions, open_blockers, and next_strategy_constraints populated with provenance.
