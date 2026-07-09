"""Deterministic long-run soak scenarios for the Kanban runtime kernel.

The soak harness is a validation tool, not a production workflow.  It drives
the public runtime APIs with scripted provider and worker outcomes so Phase 4G
can verify recovery, capability, memory, compaction, and consistency together.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_decision as rd
from hermes_cli import kanban_runtime_kernel as rk
from hermes_cli import kanban_runtime_memory as rm


PHASE4G_SCENARIO = "phase4g-baseline"
OLD_SEGMENT_SENTINEL = "phase4g_old_segment_sentinel_should_not_reappear"


@dataclass
class _ProviderStep:
    name: str
    patch: dict[str, Any]


class Phase4GScriptedDecisionProvider:
    """Scripted no-tools provider used only by the deterministic soak."""

    profile_name = "graph_patch_decision"

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._index = 0

    def decide(self, request: rd.DecisionProviderRequest) -> rd.DecisionProviderResult:
        self.requests.append(request.to_dict())
        steps = self._steps(request)
        if self._index >= len(steps):
            patch = {
                "schema": rk.PATCH_SCHEMA,
                "expected_revision": request.db_revision,
                "rationale_summary": "phase4g no structural change needed",
                "ops": [],
            }
            name = "noop"
        else:
            step = steps[self._index]
            patch = step.patch
            name = step.name
            self._index += 1
        return rd.DecisionProviderResult(
            patch=patch,
            raw_output={"scripted_step": name, "patch": patch},
            provider_name="phase4g-scripted",
            model="deterministic",
            profile_name=self.profile_name,
            parse_status="parsed",
        )

    def _steps(self, request: rd.DecisionProviderRequest) -> list[_ProviderStep]:
        stale_revision = int(request.db_revision) - 1
        return [
            _ProviderStep(
                "missing_goal_linkage_rejection",
                {
                    "schema": rk.PATCH_SCHEMA,
                    "expected_revision": request.db_revision,
                    "rationale_summary": "exercise validator rejection",
                    "ops": [
                        {
                            "op": "create_node",
                            "node_key": "invalid-unlinked-node",
                            "node_type": "implementation",
                            "title": "Invalid unlinked node",
                            "description": "This node intentionally has no goal or gap linkage.",
                        }
                    ],
                },
            ),
            _ProviderStep(
                "stale_revision_rejection",
                {
                    "schema": rk.PATCH_SCHEMA,
                    "expected_revision": stale_revision,
                    "rationale_summary": "exercise stale revision rejection",
                    "ops": [
                        {
                            "op": "create_node",
                            "node_key": "stale-revision-node",
                            "node_type": "implementation",
                            "title": "Stale revision node",
                            "description": "This patch intentionally uses a stale revision.",
                            "goal_item_keys": ["runtime-verified"],
                        }
                    ],
                },
            ),
            _ProviderStep(
                "capability_gated_implementation",
                {
                    "schema": rk.PATCH_SCHEMA,
                    "expected_revision": request.db_revision,
                    "rationale_summary": "create the implementation after validator feedback",
                    "ops": [
                        {
                            "op": "create_node",
                            "node_key": "secure-runtime-implementation",
                            "node_type": "implementation",
                            "title": "Secure runtime implementation",
                            "description": "Produce verified runtime evidence after capability authorization.",
                            "goal_item_keys": ["runtime-verified"],
                            "gap_keys": ["runtime-verified:missing_evidence"],
                            "requested_capabilities": ["secret_access"],
                        }
                    ],
                },
            ),
        ]


def run_runtime_soak(
    conn: sqlite3.Connection,
    scenario: str = PHASE4G_SCENARIO,
    *,
    max_ticks: Optional[int] = None,
    workspace_path: Optional[str] = None,
) -> dict[str, Any]:
    """Run the Phase 4G deterministic soak and return a bounded report."""

    if scenario != PHASE4G_SCENARIO:
        raise ValueError(f"unknown runtime soak scenario {scenario!r}")
    rk.ensure_runtime_schema(conn)
    tick_limit = max(20, int(max_ticks or 40))
    workspace = Path(workspace_path) if workspace_path else _default_workspace()
    workspace.mkdir(parents=True, exist_ok=True)
    root_task_id = kb.create_task(
        conn,
        title="phase4g runtime soak",
        body="phase4g runtime soak",
        created_by="runtime_soak",
        workspace_kind="worktree",
        workspace_path=str(workspace),
        tenant="runtime-soak",
        initial_status="running",
    )
    job_id = rk.create_runtime_job(
        conn,
        root_task_id,
        "phase4g synthetic long-run soak for runtime recovery capability memory compaction",
        workspace_path=str(workspace),
        goal_items=[
            {
                "item_key": "runtime-plan",
                "description": "runtime plan evidence is verified",
                "required": True,
                "verifier_required": True,
            },
            {
                "item_key": "runtime-verified",
                "description": "runtime implementation evidence is verified after recovery",
                "required": True,
                "verifier_required": True,
            },
        ],
    )
    provider = Phase4GScriptedDecisionProvider()
    ticks: list[dict[str, Any]] = []
    _prepare_memory(conn, job_id, workspace)
    _append_old_segment_sentinel(conn, job_id)
    first_compaction = rd.compact_decision_session(
        conn,
        job_id,
        profile_name="token_budget_compaction",
        reason="phase4g_initial_rollover",
    )

    # Exercise lease takeover before the first materialization.  This uses the
    # production lock path and then lets a new owner run the tick.
    first_lock = rk.acquire_runtime_advance_lock(conn, job_id, owner="phase4g-stale-owner", ttl_seconds=60)
    conn.execute("UPDATE runtime_jobs SET claim_expires_at = 0 WHERE id = ?", (job_id,))
    ticks.append(_tick(conn, job_id, provider, label="lease-takeover-materialize", owner="phase4g-takeover"))

    _complete_node(
        conn,
        job_id,
        "understand-scope",
        {
            "verdict": "succeeded",
            "summary": "runtime plan verified",
            "claimed_goal_items": ["runtime-plan"],
            "verification": {"passed": True, "summary": "plan verified"},
        },
    )
    ticks.append(_tick(conn, job_id, provider, label="ingest-plan-and-reject-unlinked"))
    ticks.append(_tick(conn, job_id, provider, label="reject-stale-revision"))
    second_compaction = rd.compact_decision_session(
        conn,
        job_id,
        profile_name="validator_boundary_compaction",
        reason="phase4g_validator_feedback_rollover",
    )
    ticks.append(_tick(conn, job_id, provider, label="create-capability-node"))
    ticks.append(_tick(conn, job_id, provider, label="capability-block"))
    authorization = rk.authorize_runtime_capability(
        conn,
        job_id,
        ["secret_access"],
        reason="phase4g human authorization for one synthetic secret access",
    )
    ticks.append(_tick(conn, job_id, provider, label="materialize-after-authorization"))
    _mark_latest_run_crashed(conn, job_id, "secure-runtime-implementation")
    ticks.append(_tick(conn, job_id, provider, label="worker-crash-recovery"))
    ticks.append(_tick(conn, job_id, provider, label="retry-materialization"))
    _complete_node(
        conn,
        job_id,
        "secure-runtime-implementation",
        {
            "verdict": "succeeded",
            "summary": "runtime implementation verified after recovery",
            "claimed_goal_items": ["runtime-verified"],
            "verification": {"passed": True, "summary": "implementation verified"},
            "artifacts": [
                {
                    "artifact_type": "soak_report",
                    "path_or_ref": "artifact://phase4g/runtime-verified",
                    "summary": "synthetic verification artifact",
                }
            ],
        },
    )
    ticks.append(_tick(conn, job_id, provider, label="ingest-final-evidence"))

    # Pad the deterministic long-run with no-op leased ticks after completion.
    # The report distinguishes terminal skipped ticks from active progress.
    while len(ticks) < tick_limit:
        ticks.append(_tick(conn, job_id, provider, label=f"terminal-noop-{len(ticks) + 1}"))

    consistency = rk.check_runtime_consistency(conn, job_id, write_events=True)
    status = rk.status_runtime_job(conn, job_id)
    report = _build_report(
        conn,
        job_id,
        scenario=scenario,
        ticks=ticks,
        provider=provider,
        first_lock=first_lock,
        first_compaction=first_compaction,
        second_compaction=second_compaction,
        authorization=authorization,
        consistency=consistency,
        status=status,
    )
    rk._event(conn, job_id, "runtime_soak_completed", report)
    return report


def _default_workspace() -> Path:
    home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()
    return home / "runtime-soak" / PHASE4G_SCENARIO


def _tick(
    conn: sqlite3.Connection,
    job_id: str,
    provider: Phase4GScriptedDecisionProvider,
    *,
    label: str,
    owner: Optional[str] = None,
) -> dict[str, Any]:
    before = _counts(conn, job_id)
    result = rk.supervisor_runtime_tick(
        conn,
        job_id,
        owner=owner or f"phase4g-{label}",
        create_tasks=True,
        decision_provider=provider,
        max_patches=1,
        auto_compact=False,
    )
    consistency = rk.check_runtime_consistency(conn, job_id, write_events=False)
    after = _counts(conn, job_id)
    return {
        "label": label,
        "status": result.get("status"),
        "reason": result.get("reason"),
        "job_state": rk._job(conn, job_id)["state"],
        "legal_waiting_reason": rk.runtime_legal_waiting_reason(conn, job_id),
        "result": result.get("result"),
        "event_delta": {key: after[key] - before.get(key, 0) for key in after},
        "consistency": {
            "status": consistency["status"],
            "violation_count": consistency["violation_count"],
            "warning_count": consistency["warning_count"],
        },
    }


def _prepare_memory(conn: sqlite3.Connection, job_id: str, workspace: Path) -> None:
    event_id = rk._event(
        conn,
        job_id,
        "anti_stuck_recovery_succeeded",
        {"summary": "contract verifier pattern recovered phase4g runtime verification"},
    )
    candidate = rm.write_runtime_memory_candidate(
        conn,
        job_id,
        event_id,
        "anti_stuck_recovery",
        lesson="Insert focused verification before claiming runtime implementation completion.",
        applies_when="runtime verification gap remains open after provider or worker recovery",
    )
    memory_root = workspace / "docs" / "runtime-memory"
    topic_path = memory_root / "recovery-patterns.md"
    rm.promote_runtime_memory_candidate(candidate, topic_path)
    (memory_root / "MEMORY.md").write_text(
        "# Runtime Memory Index\n\n"
        "- recovery-patterns.md\n"
        "  - scope: workspace\n"
        "  - keywords: runtime, verification, recovery, provider, worker\n",
        encoding="utf-8",
    )


def _append_old_segment_sentinel(conn: sqlite3.Connection, job_id: str) -> None:
    rk.append_decision_segment_entry(
        conn,
        job_id,
        "phase4g_old_segment_marker",
        {"marker": OLD_SEGMENT_SENTINEL},
        payload_text=OLD_SEGMENT_SENTINEL,
    )


def _complete_node(conn: sqlite3.Connection, job_id: str, node_key: str, evidence: dict[str, Any]) -> None:
    node = conn.execute(
        "SELECT * FROM execution_nodes WHERE job_id = ? AND node_key = ?",
        (job_id, node_key),
    ).fetchone()
    if node is None:
        raise ValueError(f"unknown node {node_key}")
    if not node["latest_task_id"]:
        raise ValueError(f"node {node_key} has no latest task")
    ok = kb.complete_task(
        conn,
        node["latest_task_id"],
        result=evidence.get("summary") or "done",
        summary=evidence.get("summary") or "done",
        metadata=evidence,
    )
    if not ok:
        raise RuntimeError(f"failed to complete task for node {node_key}")


def _mark_latest_run_crashed(conn: sqlite3.Connection, job_id: str, node_key: str) -> None:
    node = conn.execute(
        "SELECT * FROM execution_nodes WHERE job_id = ? AND node_key = ?",
        (job_id, node_key),
    ).fetchone()
    if node is None or not node["latest_task_id"]:
        raise ValueError(f"node {node_key} is not materialized")
    now = rk._now()
    conn.execute("DELETE FROM task_runs WHERE task_id = ?", (node["latest_task_id"],))
    cursor = conn.execute(
        """
        INSERT INTO task_runs (
            task_id, profile, step_key, status, claim_lock, claim_expires,
            worker_pid, max_runtime_seconds, last_heartbeat_at, started_at,
            ended_at, outcome, summary, metadata, error
        ) VALUES (?, NULL, NULL, 'crashed', 'phase4g-crash', NULL,
                  NULL, NULL, NULL, ?, ?, 'crashed', 'synthetic crash', '{}', NULL)
        """,
        (node["latest_task_id"], now - 5, now - 1),
    )
    run_id = int(cursor.lastrowid)
    conn.execute(
        """
        UPDATE tasks
           SET status = 'running', current_run_id = ?, claim_lock = 'phase4g-crash',
               claim_expires = NULL, last_heartbeat_at = NULL
         WHERE id = ?
        """,
        (run_id, node["latest_task_id"]),
    )
    conn.execute("UPDATE node_materializations SET run_id = ? WHERE node_id = ? AND task_id = ?", (run_id, node["id"], node["latest_task_id"]))


def _counts(conn: sqlite3.Connection, job_id: str) -> dict[str, int]:
    counts = {
        "events": "SELECT COUNT(*) FROM execution_events WHERE job_id = ?",
        "patches": "SELECT COUNT(*) FROM graph_patches WHERE job_id = ?",
        "decisions": "SELECT COUNT(*) FROM kernel_decisions WHERE job_id = ?",
        "materializations": "SELECT COUNT(*) FROM node_materializations WHERE job_id = ?",
        "ledger": "SELECT COUNT(*) FROM progress_ledger WHERE job_id = ?",
    }
    return {key: int(conn.execute(sql, (job_id,)).fetchone()[0] or 0) for key, sql in counts.items()}


def _event_counts(conn: sqlite3.Connection, job_id: str) -> dict[str, int]:
    return {
        row["event_type"]: int(row["count"] or 0)
        for row in conn.execute(
            """
            SELECT event_type, COUNT(*) AS count
              FROM execution_events
             WHERE job_id = ?
             GROUP BY event_type
            """,
            (job_id,),
        ).fetchall()
    }


def _patch_counts(conn: sqlite3.Connection, job_id: str) -> dict[str, int]:
    return {
        row["status"]: int(row["count"] or 0)
        for row in conn.execute(
            """
            SELECT status, COUNT(*) AS count
              FROM graph_patches
             WHERE job_id = ?
             GROUP BY status
            """,
            (job_id,),
        ).fetchall()
    }


def _latest_provider_input_text(conn: sqlite3.Connection, job_id: str) -> str:
    row = conn.execute(
        """
        SELECT payload_text
          FROM decision_segment_entries
         WHERE job_id = ? AND entry_type = 'provider_input'
         ORDER BY id DESC
         LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    return str(row["payload_text"] or "") if row else ""


def _build_report(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    scenario: str,
    ticks: list[dict[str, Any]],
    provider: Phase4GScriptedDecisionProvider,
    first_lock: dict[str, Any],
    first_compaction: dict[str, Any],
    second_compaction: dict[str, Any],
    authorization: dict[str, Any],
    consistency: dict[str, Any],
    status: dict[str, Any],
) -> dict[str, Any]:
    event_counts = _event_counts(conn, job_id)
    patch_counts = _patch_counts(conn, job_id)
    memory = rm.summarize_runtime_memory(conn, job_id)
    compaction_count = int(
        conn.execute("SELECT COUNT(*) FROM decision_checkpoints WHERE job_id = ?", (job_id,)).fetchone()[0] or 0
    )
    selected_memory_hints = sum(
        len((request.get("memory") or {}).get("selected_hints") or [])
        for request in provider.requests
    )
    provider_input_after_compaction = _latest_provider_input_text(conn, job_id)
    required_goals = [
        {"item_key": item["item_key"], "state": item["state"], "required": bool(item["required"])}
        for item in status["goal_items"]
        if item["required"]
    ]
    return {
        "scenario": scenario,
        "job_id": job_id,
        "ticks": len(ticks),
        "final_state": status["job"]["state"],
        "goal_completion": status["job"]["state"] == "done",
        "required_goals": required_goals,
        "decision_count": int(conn.execute("SELECT COUNT(*) FROM kernel_decisions WHERE job_id = ?", (job_id,)).fetchone()[0] or 0),
        "patch_applied": patch_counts.get("applied", 0),
        "patch_rejected": patch_counts.get("rejected", 0),
        "stale_patch_rejected": event_counts.get("decision_stale_revision", 0),
        "worker_recoveries": event_counts.get("worker_run_crashed", 0)
        + event_counts.get("worker_run_stale", 0)
        + event_counts.get("worker_run_timeout", 0)
        + event_counts.get("materialization_lost", 0),
        "materialization_attempts": int(
            conn.execute("SELECT COUNT(*) FROM node_materializations WHERE job_id = ?", (job_id,)).fetchone()[0] or 0
        ),
        "compactions": compaction_count,
        "compaction_statuses": [first_compaction.get("status"), second_compaction.get("status")],
        "memory_hints_used": selected_memory_hints,
        "memory_usage_events": len(memory["recent_usage"]),
        "capability_blocks": event_counts.get("capability_policy_blocked", 0),
        "human_decisions": event_counts.get("capability_authorized", 0),
        "liveness_violations": event_counts.get("liveness_violation", 0),
        "lease_takeovers": 1 if first_lock.get("acquired") else 0,
        "authorization_id": authorization["id"],
        "old_segment_excluded_from_provider_input": OLD_SEGMENT_SENTINEL not in provider_input_after_compaction,
        "checkpoint_memory_hint_leak": _checkpoint_contains_memory_hint(conn, job_id),
        "ticks_detail": ticks[:50],
        "consistency": {
            "status": consistency["status"],
            "violation_count": consistency["violation_count"],
            "warning_count": consistency["warning_count"],
            "violations": consistency["violations"][:20],
            "warnings": consistency["warnings"][:20],
        },
    }


def _checkpoint_contains_memory_hint(conn: sqlite3.Connection, job_id: str) -> bool:
    for row in conn.execute("SELECT payload_json, checkpoint_json FROM decision_checkpoints WHERE job_id = ?", (job_id,)).fetchall():
        text = json.dumps(
            {
                "payload": json.loads(row["payload_json"] or "{}"),
                "checkpoint": json.loads(row["checkpoint_json"] or "{}"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        if "selected_hints" in text or "non-authoritative memory" in text.lower():
            return True
    return False
