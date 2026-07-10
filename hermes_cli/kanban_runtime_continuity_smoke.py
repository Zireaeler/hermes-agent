"""Bounded Phase 4G4 worker execution continuity smoke."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import time
from typing import Any

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_kernel as rk


DEFAULT_CONTINUATION_SIGNAL = ".git/hermes-runtime-resume-ready"


def run_worker_continuity_smoke(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    lane_name: str,
    worker_wait_seconds: float = 180.0,
    poll_interval_seconds: float = 0.25,
    continuation_signal: str = DEFAULT_CONTINUATION_SIGNAL,
    resume_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Run one interrupted attempt followed by one backend-session resume.

    The caller owns job/node creation and lane registration. This runner never
    creates graph structure, completes a task synthetically, or signals a PID.
    The lane timeout/crash policy must end attempt-1 through the normal worker
    lifecycle.
    """

    rk.ensure_runtime_schema(conn)
    job = rk._job(conn, job_id)
    workspace = Path(str(job.get("workspace_path") or "")).expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError("continuity smoke requires an existing workspace_path")
    if not lane_name or not lane_name.strip():
        raise ValueError("continuity smoke requires an explicit worker lane")
    signal_name = str(continuation_signal or "").strip()
    if not signal_name or os.path.isabs(signal_name) or ".." in Path(signal_name).parts:
        raise ValueError("continuation signal must be a workspace-relative path")
    signal_path = workspace / signal_name
    if signal_path.exists():
        signal_path.unlink()

    existing_attempts = _attempts(conn, job_id)
    ready = _ready_nodes(conn, job_id)
    if existing_attempts or len(ready) != 1:
        raise ValueError("continuity smoke requires exactly one fresh ready node")
    node = ready[0]
    if node.get("assignee") and node["assignee"] != lane_name:
        raise ValueError("ready node assignee does not match continuity smoke lane")

    wait = max(1.0, min(float(worker_wait_seconds), 900.0))
    interval = max(0.05, min(float(poll_interval_seconds), 5.0))
    dispatches: list[dict[str, Any]] = []

    first_task = rk.materialize_runtime_node(conn, node)
    if not first_task:
        raise RuntimeError("attempt-1 was not materialized")
    dispatches.append(_dispatch_and_wait(conn, first_task, wait=wait, interval=interval))
    first_recovery = rk.reconcile_runtime_materializations(conn, job_id)
    first_attempt = _attempts(conn, job_id)[0]
    first_sessions = conn.execute(
        "SELECT * FROM backend_worker_sessions WHERE job_id = ? ORDER BY created_at",
        (job_id,),
    ).fetchall()
    if len(first_sessions) != 1 or first_sessions[0]["status"] != "interrupted":
        return _report(
            conn,
            job_id,
            dispatches=dispatches,
            first_recovery=first_recovery,
            reason="attempt_1_not_resumable",
        )
    signal_path.parent.mkdir(parents=True, exist_ok=True)
    signal_path.write_text("resume\n", encoding="utf-8")
    if resume_timeout_seconds is not None:
        _replace_codex_lane_timeout(lane_name, float(resume_timeout_seconds))

    ready = _ready_nodes(conn, job_id)
    if len(ready) != 1:
        return _report(
            conn,
            job_id,
            dispatches=dispatches,
            first_recovery=first_recovery,
            reason="resume_node_not_ready",
        )
    second_task = rk.materialize_runtime_node(conn, ready[0])
    if not second_task:
        raise RuntimeError("attempt-2 was not materialized")
    second_attempt = _attempts(conn, job_id)[-1]
    continuity = second_attempt["execution_continuity"]
    if continuity.get("mode") != "resume":
        return _report(
            conn,
            job_id,
            dispatches=dispatches,
            first_recovery=first_recovery,
            reason="resume_not_scheduled",
        )
    dispatches.append(_dispatch_and_wait(conn, second_task, wait=wait, interval=interval))
    rk.advance_runtime_job(conn, job_id, create_tasks=False, auto_compact=False)
    rk.sync_runtime_backend_sessions(conn, job_id)
    rk.reduce_runtime_job(conn, job_id)
    report = _report(
        conn,
        job_id,
        dispatches=dispatches,
        first_recovery=first_recovery,
        reason="completed" if rk._job(conn, job_id)["state"] == "done" else "resume_incomplete",
    )
    report["first_attempt_id"] = first_attempt["id"]
    report["second_attempt_id"] = second_attempt["id"]
    return report


def _replace_codex_lane_timeout(lane_name: str, timeout_seconds: float) -> None:
    from hermes_cli.codex_worker import make_codex_worker_lane
    from hermes_cli.worker_lanes import get_worker_lane, register_worker_lane

    lane = get_worker_lane(lane_name)
    if lane is None or lane.kind != "codex_cli":
        raise ValueError("resume timeout override requires a registered codex_cli lane")
    config = dict(lane.config)
    config.update(
        {
            "name": lane.name,
            "type": "codex_cli",
            "success_policy": lane.success_policy,
            "max_concurrency": lane.max_concurrency,
            "timeout_seconds": max(1, int(timeout_seconds)),
        }
    )
    register_worker_lane(
        make_codex_worker_lane(config, source=lane.source or "continuity-smoke"),
        replace=True,
    )


def _ready_nodes(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM execution_nodes WHERE job_id = ? AND state = 'ready' ORDER BY created_at, node_key",
            (job_id,),
        ).fetchall()
    ]


def _dispatch_and_wait(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    wait: float,
    interval: float,
) -> dict[str, Any]:
    dispatched = kb.dispatch_once(conn, max_spawn=1, only_task_ids=[task_id])
    spawned = [item for item in dispatched.spawned if item[0] == task_id]
    if not spawned:
        raise RuntimeError(f"continuity smoke dispatcher did not spawn {task_id}")
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        task = kb.get_task(conn, task_id)
        if task is not None and task.status in {"done", "blocked"}:
            return {"task_id": task_id, "worker_ref": spawned[0][1], "terminal_status": task.status}
        time.sleep(interval)
    return {"task_id": task_id, "worker_ref": spawned[0][1], "terminal_status": "wait_timeout"}


def _attempts(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM node_materializations WHERE job_id = ? ORDER BY attempt, created_at",
        (job_id,),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        metadata = rk._loads(item.get("metadata_json"))
        item["execution_continuity"] = metadata.get("execution_continuity") or {}
        result.append(item)
    return result


def _report(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    dispatches: list[dict[str, Any]],
    first_recovery: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    attempts = _attempts(conn, job_id)
    continuity = rk.summarize_worker_execution_continuity(conn, job_id)
    consistency = rk.check_runtime_consistency(conn, job_id, write_events=False)
    final = rk._job(conn, job_id)
    report = {
        "job_id": job_id,
        "reason": reason,
        "final_state": final["state"],
        "dispatches": dispatches,
        "attempts": [
            {
                "id": item["id"],
                "attempt": int(item["attempt"]),
                "task_id": item["task_id"],
                "status": item["status"],
                "mode": item["execution_continuity"].get("mode") or "legacy",
                "backend_session_record_id": item["execution_continuity"].get("backend_session_record_id"),
            }
            for item in attempts
        ],
        "first_recovery_events": first_recovery.get("events") or [],
        "backend_session_count": continuity["session_count"],
        "materialization_modes": continuity["materialization_modes"],
        "context_reacquisition_count": continuity["context_reacquisition_count"],
        "resumed": any(
            attempt["execution_continuity"].get("mode") == "resume"
            for attempt in attempts
        ),
        "consistency": {
            "status": consistency["status"],
            "violation_count": consistency["violation_count"],
            "warning_count": consistency["warning_count"],
        },
    }
    rk._event(
        conn,
        job_id,
        "worker_continuity_smoke_completed",
        {
            "reason": reason,
            "final_state": report["final_state"],
            "attempt_count": len(attempts),
            "resumed": report["resumed"],
            "context_reacquisition_count": report["context_reacquisition_count"],
            "consistency_status": report["consistency"]["status"],
        },
    )
    return report
