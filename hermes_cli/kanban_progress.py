"""Read-only helpers for Kanban progress/status surfaces."""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable, Optional

from hermes_cli import kanban_db as kb


def _diagnostics_config() -> dict[str, Any]:
    try:
        from hermes_cli import kanban_diagnostics as kd
        from hermes_cli.config import load_config

        return kd.config_from_runtime_config(load_config())
    except Exception:
        return {}


def _warnings_summary(diagnostics: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not diagnostics:
        return None
    try:
        from hermes_cli.kanban_diagnostics import SEVERITY_ORDER
    except Exception:
        SEVERITY_ORDER = ("warning", "error", "critical")  # type: ignore[assignment]

    kinds: dict[str, int] = {}
    latest = 0
    highest_idx = -1
    highest_severity: Optional[str] = None
    count = 0
    for item in diagnostics:
        kind = str(item.get("kind") or "")
        if kind:
            kinds[kind] = kinds.get(kind, 0) + int(item.get("count") or 1)
        count += int(item.get("count") or 1)
        latest = max(latest, int(item.get("last_seen_at") or 0))
        severity = item.get("severity")
        if severity in SEVERITY_ORDER:
            idx = SEVERITY_ORDER.index(severity)
            if idx > highest_idx:
                highest_idx = idx
                highest_severity = str(severity)
    return {
        "count": count,
        "highest_severity": highest_severity,
        "kinds": kinds,
        "latest_at": latest,
    }


def _task_diagnostics(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    config: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None:
        return []
    try:
        from hermes_cli import kanban_diagnostics as kd

        diags = kd.compute_task_diagnostics(
            task,
            kb.list_events(conn, task_id),
            kb.list_runs(conn, task_id),
            config=config if config is not None else _diagnostics_config(),
        )
        return [diag.to_dict() for diag in diags]
    except Exception:
        return []


def _iter_payload_task_ids(payload: dict[str, Any]) -> Iterable[str]:
    task = payload.get("task")
    if isinstance(task, dict) and task.get("id"):
        yield str(task["id"])
    for child in payload.get("children") or []:
        if not isinstance(child, dict):
            continue
        child_task = child.get("task")
        if isinstance(child_task, dict) and child_task.get("id"):
            yield str(child_task["id"])


def attach_progress_diagnostics(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Attach active diagnostics to a progress snapshot payload.

    This is intentionally read-only and surface-layer only: callers already
    have a progress snapshot, and diagnostics explain blocked/stuck states
    without claiming, reclaiming, dispatching, or interrupting workers.
    """
    config = _diagnostics_config()
    diagnostics_by_id: dict[str, list[dict[str, Any]]] = {}
    for task_id in _iter_payload_task_ids(payload):
        if task_id in diagnostics_by_id:
            continue
        diagnostics_by_id[task_id] = _task_diagnostics(conn, task_id, config=config)

    root_task = payload.get("task")
    if isinstance(root_task, dict) and root_task.get("id"):
        root_diags = diagnostics_by_id.get(str(root_task["id"])) or []
        if root_diags:
            payload["diagnostics"] = root_diags
            payload["warnings"] = _warnings_summary(root_diags)

    for child in payload.get("children") or []:
        if not isinstance(child, dict):
            continue
        child_task = child.get("task")
        if not (isinstance(child_task, dict) and child_task.get("id")):
            continue
        child_diags = diagnostics_by_id.get(str(child_task["id"])) or []
        if child_diags:
            child["diagnostics"] = child_diags
            child["warnings"] = _warnings_summary(child_diags)
    return payload


def diagnostic_kinds(payload: dict[str, Any]) -> list[str]:
    kinds: list[str] = []
    seen: set[str] = set()

    def add_from(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "").strip()
            if kind and kind not in seen:
                seen.add(kind)
                kinds.append(kind)

    add_from(payload.get("diagnostics"))
    for child in payload.get("children") or []:
        if isinstance(child, dict):
            add_from(child.get("diagnostics"))
    return kinds
