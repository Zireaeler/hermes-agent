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
PHASE4G6_SCENARIO = "phase4g6-active-long-run"
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


class Phase4G6ActiveDecisionProvider:
    """Create one goal-linked primary node per active long-run cycle."""

    profile_name = "graph_patch_decision"

    def __init__(self, target_nodes: int = 25) -> None:
        self.target_nodes = max(25, int(target_nodes))
        self.requests: list[dict[str, Any]] = []
        self.call_count = 0
        self.node_count = 0

    def decide(self, request: rd.DecisionProviderRequest) -> rd.DecisionProviderResult:
        self.requests.append(request.to_dict())
        self.call_count += 1
        if self.call_count == 1:
            patch = {
                "schema": rk.PATCH_SCHEMA,
                "expected_revision": request.db_revision,
                "rationale_summary": "exercise unlinked node validator rejection",
                "ops": [
                    {
                        "op": "create_node",
                        "node_key": "phase4g6-invalid-unlinked",
                        "node_type": "implementation",
                        "title": "Invalid unlinked node",
                        "description": "Intentionally missing goal linkage.",
                    }
                ],
            }
            step = "unlinked-rejection"
        elif self.call_count == 2:
            patch = {
                "schema": rk.PATCH_SCHEMA,
                "expected_revision": request.db_revision - 1,
                "rationale_summary": "exercise stale revision rejection",
                "ops": [],
            }
            step = "stale-revision"
        elif self.node_count >= self.target_nodes:
            patch = {
                "schema": rk.PATCH_SCHEMA,
                "expected_revision": request.db_revision,
                "rationale_summary": "long-run node budget exhausted",
                "ops": [],
            }
            step = "node-budget-exhausted"
        else:
            self.node_count += 1
            node_key = f"phase4g6-cycle-{self.node_count:02d}"
            gaps = [
                item
                for item in request.delta.get("goal_gaps") or []
                if item.get("gap_type") not in {"no_runnable_graph", "no_runnable_for_open_goal"}
            ]
            gap_keys = [gaps[0]["gap_key"]] if gaps else []
            op = {
                "op": "create_node",
                "node_key": node_key,
                "node_type": "implementation",
                "title": f"Long-run coherent delivery {self.node_count}",
                "description": "Produce the next bounded evidence increment for the long-run goal.",
                "goal_item_keys": ["long-run-result"],
                "gap_keys": gap_keys,
                "contract": {
                    "outcome": f"Produce evidence increment {self.node_count} for the long-run result.",
                    "acceptance_criteria": [
                        "The evidence increment is explicit",
                        "The worker receipt remains linked to long-run-result",
                    ],
                    "success_evidence": ["worker_summary", "verification"],
                    "declared_write_scope": [],
                    "prohibited_actions": ["production_deployment"],
                },
            }
            if self.node_count == 5:
                op["requested_capabilities"] = ["secret_access"]
            patch = {
                "schema": rk.PATCH_SCHEMA,
                "expected_revision": request.db_revision,
                "rationale_summary": f"create coherent long-run node {self.node_count}",
                "ops": [op],
            }
            step = node_key
        return rd.DecisionProviderResult(
            patch=patch,
            raw_output={"scripted_step": step},
            provider_name="phase4g6-active-scripted",
            model="deterministic",
            profile_name=self.profile_name,
            parse_status="parsed",
        )


class _FailingCompactionProvider:
    provider_name = "phase4g6-failing-compactor"
    model = "deterministic-failure"

    def compact(self, request: rd.CompactionProviderRequest) -> rd.CompactionProviderResult:
        return rd.CompactionProviderResult(
            checkpoint=None,
            raw_output="synthetic compaction provider failure",
            provider_name=self.provider_name,
            model=self.model,
            profile_name=request.profile["profile_name"],
            profile_version=request.profile["profile_version"],
            profile_hash=request.profile["profile_hash"],
            parse_status="provider_error",
            error="synthetic compaction provider failure",
        )


class _AcceptingCompactionProvider:
    provider_name = "phase4g6-accepting-compactor"
    model = "db-derived-candidate"

    def __init__(self, conn: sqlite3.Connection, *, invalid_provenance: bool = False) -> None:
        self.conn = conn
        self.invalid_provenance = invalid_provenance

    def compact(self, request: rd.CompactionProviderRequest) -> rd.CompactionProviderResult:
        checkpoint = rd.build_deterministic_checkpoint(
            self.conn,
            request.job_id,
            request.source_segment["id"],
            profile_name=request.profile["profile_name"],
        )
        if self.invalid_provenance and checkpoint["open_goal_gaps"]:
            checkpoint["open_goal_gaps"][0].pop("source_refs", None)
        return rd.CompactionProviderResult(
            checkpoint=checkpoint,
            raw_output=checkpoint,
            provider_name=self.provider_name,
            model=self.model,
            profile_name=request.profile["profile_name"],
            profile_version=request.profile["profile_version"],
            profile_hash=request.profile["profile_hash"],
            parse_status="parsed",
        )


def run_runtime_soak(
    conn: sqlite3.Connection,
    scenario: str = PHASE4G_SCENARIO,
    *,
    max_ticks: Optional[int] = None,
    workspace_path: Optional[str] = None,
) -> dict[str, Any]:
    """Run the Phase 4G deterministic soak and return a bounded report."""

    if scenario == PHASE4G6_SCENARIO:
        return run_active_long_run_soak(
            conn,
            max_ticks=max_ticks,
            workspace_path=workspace_path,
        )
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
                "verifier_required": False,
            },
            {
                "item_key": "runtime-verified",
                "description": "runtime implementation evidence is verified after recovery",
                "required": True,
                "verifier_required": False,
            },
        ],
        initialization_mode="fixture",
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


def run_active_long_run_soak(
    conn: sqlite3.Connection,
    *,
    max_ticks: Optional[int] = None,
    workspace_path: Optional[str] = None,
) -> dict[str, Any]:
    """Run a production-initialized long soak with meaningful active ticks."""

    rk.ensure_runtime_schema(conn)
    active_tick_target = max(50, int(max_ticks or 50))
    workspace = Path(workspace_path) if workspace_path else _default_workspace() / PHASE4G6_SCENARIO
    workspace.mkdir(parents=True, exist_ok=True)
    root_task_id = kb.create_task(
        conn,
        title="phase4g6 active long-run soak",
        body="phase4g6 active long-run soak",
        created_by="runtime_soak",
        workspace_kind="worktree",
        workspace_path=str(workspace),
        tenant="runtime-soak",
        initial_status="running",
    )
    job_id = rk.create_runtime_job(
        conn,
        root_task_id,
        "phase4g6 production initialized active long-run reliability soak",
        workspace_path=str(workspace),
        goal_items=[
            {
                "item_key": "long-run-result",
                "description": "long-run runtime result has full verified evidence",
                "required": True,
                "verifier_required": False,
            }
        ],
        initialization_mode="provider_first",
    )
    provider = Phase4G6ActiveDecisionProvider(target_nodes=25)
    ticks: list[dict[str, Any]] = []
    sentinels: list[str] = []
    compaction_results: list[dict[str, Any]] = []
    compaction_schedule = {4, 8, 12, 16, 20, 21, 24}
    completed_at_compaction: set[int] = set()
    crashed_node_key = "phase4g6-cycle-09"
    crash_injected = False
    authorized = False
    gap_reopened = False
    max_iterations = max(active_tick_target * 8, 500)

    stale_lock = rk.acquire_runtime_advance_lock(conn, job_id, owner="phase4g6-stale-owner", ttl_seconds=60)
    conn.execute("UPDATE runtime_jobs SET claim_expires_at = 0 WHERE id = ?", (job_id,))

    for iteration in range(max_iterations):
        tick = _tick(
            conn,
            job_id,
            provider,
            label=f"active-supervisor-{iteration + 1}",
            owner="phase4g6-takeover" if iteration == 0 else None,
        )
        ticks.append(tick)

        waiting_capability = conn.execute(
            """
            SELECT node_key FROM execution_nodes
             WHERE job_id = ? AND state = 'waiting_human'
             ORDER BY created_at, node_key LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        if waiting_capability and not authorized:
            before = _counts(conn, job_id)
            rk.authorize_runtime_capability(
                conn,
                job_id,
                ["secret_access"],
                reason="phase4g6 bounded human authorization",
            )
            ticks.append(_action_tick(conn, job_id, "capability-authorization", before, {"status": "authorized"}))
            authorized = True

        running_nodes = conn.execute(
            """
            SELECT * FROM execution_nodes
             WHERE job_id = ? AND state = 'running' AND latest_task_id IS NOT NULL
             ORDER BY created_at, node_key
            """,
            (job_id,),
        ).fetchall()
        for node in running_nodes:
            task = kb.get_task(conn, node["latest_task_id"])
            if task is None or task.status in {"done", "blocked", "archived"}:
                continue
            if node["node_key"] == crashed_node_key and not crash_injected:
                before = _counts(conn, job_id)
                _mark_latest_run_crashed(conn, job_id, node["node_key"])
                ticks.append(_action_tick(conn, job_id, "worker-crash-injected", before, {"status": "crashed"}))
                crash_injected = True
                continue
            node_number = int(str(node["node_key"]).rsplit("-", 1)[-1])
            final = node_number == provider.target_nodes
            temporary_satisfaction = node_number == 14
            evidence = {
                "verdict": "succeeded",
                "summary": f"phase4g6 evidence increment {node_number}",
                "verification": {
                    "passed": final or temporary_satisfaction,
                    "summary": "verification passed" if final or temporary_satisfaction else "partial evidence recorded",
                },
            }
            if final or temporary_satisfaction:
                evidence["claimed_goal_items"] = ["long-run-result"]
            else:
                evidence["partial_goal_items"] = ["long-run-result"]
            _complete_node(conn, job_id, node["node_key"], evidence)

        completed_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM execution_nodes WHERE job_id = ? AND state = 'succeeded' AND node_key LIKE 'phase4g6-cycle-%'",
                (job_id,),
            ).fetchone()[0]
            or 0
        )
        goal_state = conn.execute(
            """
            SELECT gi.state
              FROM goal_items gi
              JOIN goal_contracts gc ON gc.id = gi.contract_id
             WHERE gc.job_id = ? AND gi.item_key = 'long-run-result'
            """,
            (job_id,),
        ).fetchone()[0]
        if completed_count == 14 and goal_state == "satisfied" and not gap_reopened:
            before = _counts(conn, job_id)
            reopening_node = conn.execute(
                "SELECT id FROM execution_nodes WHERE job_id = ? AND node_key = 'phase4g6-cycle-14'",
                (job_id,),
            ).fetchone()
            rk.update_progress_ledger(
                conn,
                reopening_node["id"],
                {
                    "summary": "later verification invalidated the temporary result",
                    "contradicted_goal_items": ["long-run-result"],
                    "verification": {"passed": False, "summary": "later verification failed"},
                },
            )
            rk.reduce_runtime_job(conn, job_id)
            ticks.append(_action_tick(conn, job_id, "goal-gap-reopened", before, {"status": "reopened"}))
            gap_reopened = True
        if completed_count in compaction_schedule and completed_count not in completed_at_compaction:
            completed_at_compaction.add(completed_count)
            profile_name = {
                4: "token_budget_compaction",
                8: "validator_boundary_compaction",
                12: "anti_stuck_compaction",
                16: "token_budget_compaction",
                20: "validator_boundary_compaction",
                21: "token_budget_compaction",
                24: "anti_stuck_compaction",
            }[completed_count]
            sentinel = f"SEGMENT_SENTINEL_{completed_count}_{len(sentinels) + 1}"
            sentinels.append(sentinel)
            before = _counts(conn, job_id)
            rk.append_decision_segment_entry(
                conn,
                job_id,
                "phase4g6_segment_marker",
                {"marker": sentinel},
                payload_text=sentinel,
            )
            if completed_count in {4, 8}:
                result = rd.compact_decision_session(
                    conn,
                    job_id,
                    profile_name=profile_name,
                    reason=f"phase4g6-cycle-{completed_count}-fallback",
                    compaction_provider=_FailingCompactionProvider(),
                )
            elif completed_count == 20:
                result = rd.compact_decision_session(
                    conn,
                    job_id,
                    profile_name=profile_name,
                    reason="phase4g6-no-fallback-rejection",
                    compaction_provider=_AcceptingCompactionProvider(conn, invalid_provenance=True),
                    fallback_to_deterministic=False,
                )
            elif completed_count in {12, 24}:
                result = rd.compact_decision_session(
                    conn,
                    job_id,
                    profile_name=profile_name,
                    reason=f"phase4g6-cycle-{completed_count}-provider-recovery",
                    compaction_provider=_AcceptingCompactionProvider(conn),
                    fallback_to_deterministic=False,
                )
            else:
                result = rd.compact_decision_session(
                    conn,
                    job_id,
                    profile_name=profile_name,
                    reason=f"phase4g6-cycle-{completed_count}-deterministic",
                )
            compaction_results.append(result)
            ticks.append(_action_tick(conn, job_id, f"compaction-{completed_count}", before, result))

        state = rk.reduce_runtime_job(conn, job_id)["state"]
        active_count = sum(bool(item.get("active")) for item in ticks)
        if state == "done" and active_count >= active_tick_target:
            break
    else:
        raise RuntimeError("phase4g6 active long-run soak exceeded iteration budget")

    rendered = rd.render_decision_prompt(
        rd.build_decision_provider_request(conn, job_id, rk.build_decision_delta(conn, job_id))
    )
    rendered_text = json.dumps(rendered, ensure_ascii=False, sort_keys=True)
    context_chain = rd.validate_decision_context_chain(conn, job_id)
    consistency = rk.check_runtime_consistency(conn, job_id, write_events=True)
    status = rk.status_runtime_job(conn, job_id)
    active_ticks = [item for item in ticks if item.get("active")]
    noop_ticks = [item for item in ticks if not item.get("active")]
    patch_counts = _patch_counts(conn, job_id)
    event_counts = _event_counts(conn, job_id)
    health = rd.summarize_compaction_health(conn, job_id)
    report = {
        "scenario": PHASE4G6_SCENARIO,
        "job_id": job_id,
        "ticks": len(ticks),
        "active_tick_count": len(active_ticks),
        "noop_tick_count": len(noop_ticks),
        "terminal_noop_padding_count": sum(
            not item.get("active") and item.get("job_state") == "done" for item in ticks
        ),
        "final_state": status["job"]["state"],
        "goal_completion": status["job"]["state"] == "done",
        "graph_revision_delta": int(status["job"]["graph_revision"]),
        "decision_count": int(
            conn.execute("SELECT COUNT(*) FROM kernel_decisions WHERE job_id = ?", (job_id,)).fetchone()[0] or 0
        ),
        "patch_applied": patch_counts.get("applied", 0),
        "patch_rejected": patch_counts.get("rejected", 0),
        "materialization_attempts": int(
            conn.execute("SELECT COUNT(*) FROM node_materializations WHERE job_id = ?", (job_id,)).fetchone()[0] or 0
        ),
        "worker_recoveries": event_counts.get("worker_run_crashed", 0),
        "lease_takeovers": 1 if stale_lock.get("acquired") else 0,
        "compactions": int(
            conn.execute("SELECT COUNT(*) FROM decision_checkpoints WHERE job_id = ?", (job_id,)).fetchone()[0] or 0
        ),
        "compaction_attempts": len(compaction_results),
        "compaction_statuses": [item.get("status") for item in compaction_results],
        "compaction_profiles": [item.get("profile_name") for item in compaction_results],
        "compaction_health": health,
        "quality_degraded_events": event_counts.get("compaction_quality_degraded", 0),
        "quality_recovered_events": event_counts.get("compaction_quality_recovered", 0),
        "goal_gap_reopened_events": event_counts.get("goal_gap_reopened", 0),
        "historical_segment_sentinels": len(sentinels),
        "historical_sentinels_excluded": all(sentinel not in rendered_text for sentinel in sentinels),
        "context_chain_validation": {
            key: context_chain.get(key)
            for key in (
                "status",
                "selection_mode",
                "latest_checkpoint_id",
                "selected_checkpoint_id",
                "checked_checkpoint_count",
                "errors",
            )
        },
        "required_goals": [
            {"item_key": item["item_key"], "state": item["state"], "required": bool(item["required"])}
            for item in status["goal_items"]
            if item["required"]
        ],
        "liveness_violations": event_counts.get("liveness_violation", 0),
        "ticks_detail": ticks[:100],
        "consistency": {
            "status": consistency["status"],
            "violation_count": consistency["violation_count"],
            "warning_count": consistency["warning_count"],
            "violations": consistency["violations"][:20],
            "warnings": consistency["warnings"][:20],
        },
    }
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
    event_delta = {key: after[key] - before.get(key, 0) for key in after}
    return {
        "label": label,
        "status": result.get("status"),
        "reason": result.get("reason"),
        "job_state": rk._job(conn, job_id)["state"],
        "legal_waiting_reason": rk.runtime_legal_waiting_reason(conn, job_id),
        "result": result.get("result"),
        "active": any(value != 0 for value in event_delta.values()),
        "event_delta": event_delta,
        "consistency": {
            "status": consistency["status"],
            "violation_count": consistency["violation_count"],
            "warning_count": consistency["warning_count"],
        },
    }


def _action_tick(
    conn: sqlite3.Connection,
    job_id: str,
    label: str,
    before: dict[str, int],
    result: dict[str, Any],
) -> dict[str, Any]:
    after = _counts(conn, job_id)
    event_delta = {key: after[key] - before.get(key, 0) for key in after}
    return {
        "label": label,
        "status": result.get("status"),
        "job_state": rk._job(conn, job_id)["state"],
        "active": any(value != 0 for value in event_delta.values()),
        "event_delta": event_delta,
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
        "nodes": "SELECT COUNT(*) FROM execution_nodes WHERE job_id = ?",
        "materializations": "SELECT COUNT(*) FROM node_materializations WHERE job_id = ?",
        "ledger": "SELECT COUNT(*) FROM progress_ledger WHERE job_id = ?",
        "checkpoints": "SELECT COUNT(*) FROM decision_checkpoints WHERE job_id = ?",
        "segments": "SELECT COUNT(*) FROM decision_session_segments WHERE job_id = ?",
    }
    values = {key: int(conn.execute(sql, (job_id,)).fetchone()[0] or 0) for key, sql in counts.items()}
    values["graph_revision"] = int(rk._job(conn, job_id)["graph_revision"])
    return values


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
