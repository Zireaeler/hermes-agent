"""Bounded real-decision-provider loops with synthetic worker evidence.

Phase 4G2 uses a real decision provider but deliberately keeps worker evidence
synthetic.  The helper drives existing runtime APIs and reports the resulting
audit surface; it does not introduce a second scheduler or workflow engine.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_decision as rd
from hermes_cli import kanban_runtime_kernel as rk


def run_real_provider_bounded_loop(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    provider_source: dict[str, Any],
    max_decision_ticks: int = 3,
    max_steps: int = 16,
    profile_name: str = "graph_patch_decision",
    timeout_seconds: Optional[float] = None,
    max_retries: int = 1,
) -> dict[str, Any]:
    """Run up to N real decision calls while using deterministic synthetic receipts.

    Synthetic failures keep a goal gap open until the requested decision budget
    has been consumed.  The next materialized node then receives a successful
    receipt, so completion remains controlled by the normal progress ledger.
    """

    rk.ensure_runtime_schema(conn)
    if not provider_source.get("provider_name") or not provider_source.get("model"):
        raise ValueError("real provider bounded loop requires an explicit model source")
    decision_limit = max(1, min(int(max_decision_ticks), 5))
    step_limit = max(decision_limit + 2, min(int(max_steps), 32))
    provider = rd.RuntimeDecisionProvider(
        provider_name=provider_source["provider_name"],
        model=provider_source["model"],
        profile_name=profile_name,
        max_retries=max_retries,
        timeout_seconds=timeout_seconds,
        explicit_base_url=provider_source.get("explicit_base_url"),
        explicit_api_key=provider_source.get("explicit_api_key"),
    )

    steps: list[dict[str, Any]] = []
    decision_ticks: list[dict[str, Any]] = []
    synthetic_receipts: list[dict[str, Any]] = []
    reason = "step_limit"
    for index in range(step_limit):
        job = rk._job(conn, job_id)
        if job["state"] in {"done", "cancelled", "failed"}:
            reason = job["state"]
            break

        materialized = _materialize_ready_nodes(conn, job_id)
        completed = None
        running = _next_running_node(conn, job_id)
        if running:
            should_succeed = len(decision_ticks) >= decision_limit
            receipt = _synthetic_receipt(running, succeed=should_succeed)
            _complete_node(conn, running, receipt)
            ingested = rk.advance_runtime_job(conn, job_id, create_tasks=False, auto_compact=False)
            completed = {
                "node_key": running["node_key"],
                "verdict": receipt["verdict"],
                "ingested_nodes": ingested.ingested_nodes,
            }
            synthetic_receipts.append({"node_key": running["node_key"], "verdict": receipt["verdict"]})

        reduction = rk.reduce_runtime_job(conn, job_id)
        decision = None
        if reduction["state"] == "waiting_decision" and len(decision_ticks) < decision_limit:
            before = _counts(conn, job_id)
            advanced = rk.advance_runtime_job(
                conn,
                job_id,
                create_tasks=False,
                decision_provider=provider,
                max_patches=1,
                auto_compact=False,
            )
            after = _counts(conn, job_id)
            decision = {
                "decision_index": len(decision_ticks) + 1,
                "patch_status": advanced.patch_status,
                "job_state": advanced.job_state,
                "graph_revision_before": before["graph_revision"],
                "graph_revision_after": after["graph_revision"],
                "kernel_decisions_before": before["kernel_decisions"],
                "kernel_decisions_after": after["kernel_decisions"],
                "graph_patches_before": before["graph_patches"],
                "graph_patches_after": after["graph_patches"],
            }
            decision_ticks.append(decision)

        consistency = rk.check_runtime_consistency(conn, job_id, write_events=False)
        steps.append(
            {
                "step": index + 1,
                "materialized_nodes": materialized,
                "synthetic_receipt": completed,
                "decision": decision,
                "job_state": rk._job(conn, job_id)["state"],
                "legal_waiting_reason": rk.runtime_legal_waiting_reason(conn, job_id),
                "consistency": {
                    "status": consistency["status"],
                    "violation_count": consistency["violation_count"],
                    "warning_count": consistency["warning_count"],
                },
            }
        )
        if consistency["status"] != "passed":
            reason = "consistency_failed"
            break
        if len(decision_ticks) >= decision_limit and not _next_running_node(conn, job_id):
            # Give a newly created ready node one more step to materialize and
            # receive the final success receipt before exiting.
            if not _ready_nodes(conn, job_id):
                reason = "decision_limit_reached"
                break

    final = rk.status_runtime_job(conn, job_id)
    consistency = rk.check_runtime_consistency(conn, job_id, write_events=False)
    report = {
        "job_id": job_id,
        "provider": _source_summary(provider_source),
        "max_decision_ticks": decision_limit,
        "decision_tick_count": len(decision_ticks),
        "accepted_patch_count": len([item for item in decision_ticks if item["patch_status"] == "applied"]),
        "rejected_patch_count": len([item for item in decision_ticks if item["patch_status"] == "rejected"]),
        "synthetic_receipt_count": len(synthetic_receipts),
        "synthetic_receipts": synthetic_receipts[:20],
        "final_state": final["job"]["state"],
        "goal_items": [
            {"item_key": item["item_key"], "state": item["state"], "required": bool(item["required"])}
            for item in final["goal_items"]
        ],
        "reason": reason,
        "steps": steps[:32],
        "consistency": {
            "status": consistency["status"],
            "violation_count": consistency["violation_count"],
            "warning_count": consistency["warning_count"],
            "violations": consistency["violations"][:20],
            "warnings": consistency["warnings"][:20],
        },
        "secrets_leaked": False,
    }
    report["secrets_leaked"] = _secrets_leaked(report, provider_source)
    rk._event(
        conn,
        job_id,
        "real_provider_bounded_loop_completed",
        {
            "decision_tick_count": report["decision_tick_count"],
            "accepted_patch_count": report["accepted_patch_count"],
            "rejected_patch_count": report["rejected_patch_count"],
            "synthetic_receipt_count": report["synthetic_receipt_count"],
            "final_state": report["final_state"],
            "reason": reason,
            "consistency_status": report["consistency"]["status"],
            "secrets_leaked": report["secrets_leaked"],
        },
    )
    return report


def _materialize_ready_nodes(conn: sqlite3.Connection, job_id: str) -> list[str]:
    materialized: list[str] = []
    for node in _ready_nodes(conn, job_id):
        if rk.materialize_runtime_node(conn, node):
            materialized.append(node["node_key"])
    return materialized


def _ready_nodes(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM execution_nodes WHERE job_id = ? AND state = 'ready' ORDER BY created_at, node_key",
        (job_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _next_running_node(conn: sqlite3.Connection, job_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM execution_nodes WHERE job_id = ? AND state = 'running' ORDER BY started_at, created_at, node_key LIMIT 1",
        (job_id,),
    ).fetchone()
    return dict(row) if row else None


def _synthetic_receipt(node: dict[str, Any], *, succeed: bool) -> dict[str, Any]:
    metadata = _loads(node.get("metadata_json"))
    goal_keys = [str(key) for key in metadata.get("goal_item_keys") or [] if str(key).strip()]
    if succeed:
        return {
            "verdict": "succeeded",
            "summary": f"synthetic verified evidence for {node['node_key']}",
            "claimed_goal_items": goal_keys,
            "verification": {"passed": True, "summary": "synthetic bounded-loop verification"},
        }
    return {
        "verdict": "failed",
        "summary": f"synthetic bounded-loop failure for {node['node_key']}",
        "unmet_goal_items": goal_keys,
        "verification": {"passed": False, "summary": "synthetic failure keeps the goal gap open"},
    }


def _complete_node(conn: sqlite3.Connection, node: dict[str, Any], receipt: dict[str, Any]) -> None:
    task_id = node.get("latest_task_id")
    if not task_id:
        raise ValueError(f"running node {node['node_key']} has no materialized task")
    if not kb.complete_task(
        conn,
        str(task_id),
        result=receipt["summary"],
        summary=receipt["summary"],
        metadata=receipt,
    ):
        raise RuntimeError(f"failed to complete synthetic task for node {node['node_key']}")


def _counts(conn: sqlite3.Connection, job_id: str) -> dict[str, int]:
    return {
        "graph_revision": int(rk._job(conn, job_id)["graph_revision"]),
        "kernel_decisions": _count(conn, "kernel_decisions", job_id),
        "graph_patches": _count(conn, "graph_patches", job_id),
    }


def _count(conn: sqlite3.Connection, table: str, job_id: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE job_id = ?", (job_id,)).fetchone()[0] or 0)


def _loads(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def _source_summary(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": source.get("source") or "unspecified",
        "model_provider": source.get("display_provider") or source.get("provider_name"),
        "model": source.get("model") or None,
        "explicit_base_url": bool(source.get("explicit_base_url")),
        "explicit_api_key": bool(source.get("explicit_api_key")),
    }


def _secrets_leaked(report: dict[str, Any], source: dict[str, Any]) -> bool:
    secret = source.get("explicit_api_key")
    return bool(secret and str(secret) in json.dumps(report, ensure_ascii=False, sort_keys=True))
