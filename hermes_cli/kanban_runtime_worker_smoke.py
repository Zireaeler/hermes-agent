"""Bounded Phase 4G3 worker-lane smoke orchestration.

This helper deliberately composes the production decision, materialization,
dispatcher, Codex worker wrapper, and runtime ingest APIs. It never writes a
synthetic receipt or starts a worker directly.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_decision as rd
from hermes_cli import kanban_runtime_kernel as rk
from hermes_cli.worker_lanes import register_configured_worker_lanes


def run_real_worker_lane_smoke(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    provider_source: dict[str, Any],
    lane_name: str,
    max_decision_ticks: int = 3,
    max_steps: int = 24,
    worker_wait_seconds: float = 120.0,
    poll_interval_seconds: float = 0.5,
    profile_name: str = "graph_patch_decision",
    timeout_seconds: Optional[float] = None,
    max_retries: int = 1,
) -> dict[str, Any]:
    """Run a bounded real-provider/real-worker path for an existing job.

    Nodes must already be assigned to ``lane_name`` by the job setup or an
    accepted provider patch. The helper will not repair an unassigned model
    proposal because that would turn the runner into a hidden graph authority.
    """
    rk.ensure_runtime_schema(conn)
    db_path = _connection_db_path(conn)
    active_conn = conn
    refreshed_conn: Optional[sqlite3.Connection] = None
    if not provider_source.get("provider_name") or not provider_source.get("model"):
        raise ValueError("real worker smoke requires an explicit model source")
    if not lane_name or not lane_name.strip():
        raise ValueError("real worker smoke requires an explicit worker lane")
    lane_name = lane_name.strip()
    # The decision request needs the configured lane directory before its
    # stable prefix is rendered; dispatcher registration alone is too late.
    register_configured_worker_lanes()
    decision_limit = max(1, min(int(max_decision_ticks), 5))
    # Real workers can run for minutes. Keep the loop bounded, but poll often
    # enough that one terminal checkpoint can affect siblings still running.
    step_limit = max(2, min(int(max_steps), 256))
    worker_wait = max(1.0, min(float(worker_wait_seconds), 900.0))
    provider = rd.RuntimeDecisionProvider(
        provider_name=provider_source["provider_name"],
        model=provider_source["model"],
        profile_name=profile_name,
        max_retries=max_retries,
        timeout_seconds=timeout_seconds,
        explicit_base_url=provider_source.get("explicit_base_url"),
        explicit_api_key=provider_source.get("explicit_api_key"),
    )

    decisions: list[dict[str, Any]] = []
    dispatches: list[dict[str, Any]] = []
    terminal_receipts: list[dict[str, Any]] = []
    reason = "step_limit"
    for index in range(step_limit):
        job = rk._job(active_conn, job_id)
        if job["state"] in {"done", "cancelled", "failed"}:
            reason = job["state"]
            break

        advanced = rk.advance_runtime_job(
            active_conn,
            job_id,
            create_tasks=False,
            auto_compact=False,
        )
        ready = _ready_nodes(active_conn, job_id)
        materialized: list[str] = []
        unassigned: list[str] = []
        for node in ready:
            # An absent assignee may be filled by the job's local default lane
            # inside materialize_runtime_node(). Only an explicit different
            # lane is outside this smoke run's ownership.
            if node.get("assignee") and node["assignee"] != lane_name:
                unassigned.append(node["node_key"])
                continue
            if rk.materialize_runtime_node(active_conn, node):
                materialized.append(node["node_key"])
        if materialized:
            task_ids = _active_task_ids(active_conn, job_id, materialized)
            dispatch = kb.dispatch_once(
                active_conn,
                max_spawn=max(1, len(task_ids)),
                only_task_ids=task_ids,
            )
            dispatches.append(
                {
                    "step": index + 1,
                    "materialized_nodes": materialized,
                    "spawned_task_ids": [item[0] for item in dispatch.spawned],
                }
            )
            _wait_for_terminal_tasks(
                active_conn,
                task_ids,
                worker_wait,
                poll_interval_seconds,
            )
            # A worker process may checkpoint and replace WAL sidecars while
            # this supervisor connection is idle. Never ingest terminal facts
            # through the pre-worker connection: refresh the control-plane
            # connection at every completed process boundary.
            if db_path is not None:
                if refreshed_conn is not None:
                    refreshed_conn.close()
                refreshed_conn = kb.connect(db_path=db_path)
                active_conn = refreshed_conn

        ingested = rk.advance_runtime_job(
            active_conn,
            job_id,
            create_tasks=False,
            auto_compact=False,
        )
        terminal_receipts.extend(_terminal_receipt_summaries(active_conn, job_id))
        reduction = rk.reduce_runtime_job(active_conn, job_id)
        decision = None
        if reduction["state"] == "waiting_decision" and len(decisions) < decision_limit:
            before = _counts(active_conn, job_id)
            decision_result = rk.advance_runtime_job(
                active_conn,
                job_id,
                create_tasks=False,
                decision_provider=provider,
                max_patches=1,
                auto_compact=False,
            )
            after = _counts(active_conn, job_id)
            decision = {
                "decision_index": len(decisions) + 1,
                "patch_status": decision_result.patch_status,
                "graph_revision_before": before["graph_revision"],
                "graph_revision_after": after["graph_revision"],
            }
            decisions.append(decision)

        consistency = rk.check_runtime_consistency(
            active_conn,
            job_id,
            write_events=False,
        )
        if consistency["status"] != "passed":
            reason = "consistency_failed"
            break
        if unassigned:
            reason = "ready_unassigned"
            break
        if not materialized and not decision and not advanced.ingested_nodes and not ingested.ingested_nodes:
            reason = rk.runtime_legal_waiting_reason(active_conn, job_id)
            if reason != "waiting_worker":
                break

    final = rk.status_runtime_job(active_conn, job_id)
    consistency = rk.check_runtime_consistency(
        active_conn,
        job_id,
        write_events=False,
    )
    attempts = _materialization_attempts(active_conn, job_id)
    materialized_node_keys = sorted({item["node_key"] for item in attempts})
    report = {
        "job_id": job_id,
        "provider": _source_summary(provider_source),
        "worker_lane": lane_name,
        "decision_tick_count": len(decisions),
        "accepted_patch_count": len([item for item in decisions if item["patch_status"] == "applied"]),
        "rejected_patch_count": len([item for item in decisions if item["patch_status"] == "rejected"]),
        "dispatches": dispatches,
        "materialization_attempts": attempts,
        "materialization_attempt_count": len(attempts),
        "materialized_node_keys": materialized_node_keys,
        "single_primary_node": len(materialized_node_keys) == 1,
        "single_worker_attempt": len(attempts) == 1,
        "terminal_receipts": _dedupe_receipts(terminal_receipts),
        "final_state": final["job"]["state"],
        "goal_items": [{"item_key": item["item_key"], "state": item["state"]} for item in final["goal_items"]],
        "reason": reason,
        "consistency": {
            "status": consistency["status"],
            "violation_count": consistency["violation_count"],
            "warning_count": consistency["warning_count"],
        },
        "secrets_leaked": False,
    }
    report["secrets_leaked"] = _secrets_leaked(report, provider_source)
    rk._event(
        active_conn,
        job_id,
        "real_worker_lane_smoke_completed",
        {
            "decision_tick_count": report["decision_tick_count"],
            "accepted_patch_count": report["accepted_patch_count"],
            "rejected_patch_count": report["rejected_patch_count"],
            "receipt_count": len(report["terminal_receipts"]),
            "final_state": report["final_state"],
            "reason": reason,
            "consistency_status": report["consistency"]["status"],
            "secrets_leaked": report["secrets_leaked"],
        },
    )
    if refreshed_conn is not None:
        refreshed_conn.close()
    return report


def _ready_nodes(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(
        "SELECT * FROM execution_nodes WHERE job_id = ? AND state = 'ready' ORDER BY created_at, node_key",
        (job_id,),
    ).fetchall()]


def _active_task_ids(conn: sqlite3.Connection, job_id: str, keys: list[str]) -> list[str]:
    if not keys:
        return []
    placeholders = ",".join("?" for _ in keys)
    rows = conn.execute(
        f"SELECT latest_task_id FROM execution_nodes WHERE job_id = ? AND node_key IN ({placeholders})",
        (job_id, *keys),
    ).fetchall()
    return [str(row["latest_task_id"]) for row in rows if row["latest_task_id"]]


def _wait_for_terminal_tasks(conn: sqlite3.Connection, task_ids: list[str], timeout: float, interval: float) -> None:
    db_path = _connection_db_path(conn)
    deadline = time.monotonic() + timeout
    while task_ids and time.monotonic() < deadline:
        placeholders = ",".join("?" for _ in task_ids)
        if db_path is None:
            rows = conn.execute(
                f"SELECT status FROM tasks WHERE id IN ({placeholders})",
                tuple(task_ids),
            ).fetchall()
        else:
            uri = db_path.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=30) as poll_conn:
                rows = poll_conn.execute(
                    f"SELECT status FROM tasks WHERE id IN ({placeholders})",
                    tuple(task_ids),
                ).fetchall()
        states = [str(row[0]) for row in rows]
        if states and all(state in {"done", "blocked"} for state in states):
            return
        time.sleep(max(0.05, interval))


def _connection_db_path(conn: sqlite3.Connection) -> Optional[Path]:
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None or not row[2] or str(row[2]) == ":memory:":
        return None
    return Path(str(row[2]))


def _terminal_receipt_summaries(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT node_key, latest_task_id FROM execution_nodes WHERE job_id = ? AND state IN ('succeeded', 'failed', 'blocked', 'waiting_human')",
        (job_id,),
    ).fetchall()
    result = []
    for row in rows:
        if not row["latest_task_id"]:
            continue
        snapshot = kb.task_progress_snapshot(conn, row["latest_task_id"])
        evidence = dict(snapshot.evidence or {}) if snapshot else {}
        receipt = evidence.get("runtime_receipt") if isinstance(evidence.get("runtime_receipt"), dict) else {}
        result.append({"node_key": row["node_key"], "task_id": row["latest_task_id"], "verdict": receipt.get("verdict")})
    return result


def _counts(conn: sqlite3.Connection, job_id: str) -> dict[str, int]:
    job = rk._job(conn, job_id)
    return {"graph_revision": int(job["graph_revision"])}


def _materialization_attempts(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT n.node_key, m.attempt, m.task_id, m.run_id, m.worker_lane, m.status
          FROM node_materializations m
          JOIN execution_nodes n ON n.id = m.node_id
         WHERE m.job_id = ?
         ORDER BY m.created_at, m.attempt
        """,
        (job_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _dedupe_receipts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    result = []
    for item in items:
        key = (str(item["node_key"]), str(item["task_id"]))
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _source_summary(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": source.get("source") or "unspecified",
        "model_provider": source.get("display_provider") or source.get("provider_name"),
        "model": source.get("model") or None,
    }


def _secrets_leaked(report: dict[str, Any], source: dict[str, Any]) -> bool:
    secret = source.get("explicit_api_key")
    return bool(secret and str(secret) in json.dumps(report, ensure_ascii=False, sort_keys=True))
