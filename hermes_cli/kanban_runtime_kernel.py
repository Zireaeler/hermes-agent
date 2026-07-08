"""Goal-driven runtime kernel over Hermes Kanban.

Phase 1 keeps the implementation intentionally local to this module.  The
runtime state is authoritative in SQLite rows; decision providers only propose
graph patches and never own state or scheduling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import sqlite3
import time
import uuid
from typing import Any, Callable, Iterable, Optional

from hermes_cli import kanban_db as kb


PATCH_SCHEMA = "runtime_graph_patch_v1"

NODE_STATES = {
    "planned",
    "waiting_dependency",
    "ready",
    "running",
    "succeeded",
    "failed",
    "blocked",
    "waiting_human",
    "cancelled",
    "superseded",
}
NODE_TYPES = {
    "analysis",
    "implementation",
    "verification",
    "review",
    "debug",
    "research",
    "human_gate",
    "artifact_transform",
}
DEPENDENCY_TYPES = {"depends_on", "artifact_input"}
RELATION_TYPES = {"verifies", "blocks", "supersedes", "explains", "replaces_attempt"}
PATCH_OPS = {
    "create_node",
    "add_dependency",
    "insert_verifier",
    "request_human",
    "propose_blocked",
}
BLOCKER_TYPES = {
    "missing_secret",
    "external_cost",
    "external_permission",
    "permission_required",
    "destructive_change_needs_approval",
    "destructive_change",
    "unavailable_dependency",
    "system_error",
    "policy_violation",
    "legal_or_policy",
}
HUMAN_DECISION_TYPES = {
    "external_cost",
    "credential",
    "missing_secret",
    "permission",
    "permission_required",
    "destructive_change",
    "product_preference",
    "architecture_choice",
    "policy_exception",
    "legal_or_policy",
}
TERMINAL_NODE_STATES = {"succeeded", "failed", "blocked", "cancelled", "superseded"}
OPEN_NODE_STATES = {"planned", "waiting_dependency", "ready", "running", "waiting_human"}


@dataclass
class AdvanceResult:
    job_id: str
    job_state: str
    materialized_nodes: list[str] = field(default_factory=list)
    ingested_nodes: list[str] = field(default_factory=list)
    decision_requested: bool = False
    patch_status: Optional[str] = None
    events: list[str] = field(default_factory=list)


class PatchValidationError(ValueError):
    """Raised when a graph patch violates runtime kernel invariants."""


def _now() -> int:
    return int(time.time())


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _json(data: Any) -> str:
    return json.dumps(data if data is not None else {}, ensure_ascii=False, sort_keys=True)


def _loads(raw: Any, default: Any = None) -> Any:
    if raw is None or raw == "":
        return {} if default is None else default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {} if default is None else default


def _row_to_dict(row: sqlite3.Row | None) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    data = dict(row)
    for key in list(data):
        if key.endswith("_json"):
            data[key[:-5]] = _loads(data[key])
    return data


def _rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    return [_row_to_dict(row) or {} for row in conn.execute(sql, tuple(params)).fetchall()]


def ensure_runtime_schema(conn: sqlite3.Connection) -> None:
    """Create Phase 1 runtime kernel tables and indexes."""

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runtime_jobs (
            id TEXT PRIMARY KEY,
            root_task_id TEXT,
            board TEXT,
            state TEXT NOT NULL,
            objective TEXT NOT NULL,
            workspace_path TEXT,
            decision_profile TEXT,
            active_milestone_key TEXT,
            graph_revision INTEGER NOT NULL DEFAULT 0,
            advance_lock TEXT,
            claim_expires_at INTEGER,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS goal_contracts (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            objective TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL,
            constraints_json TEXT NOT NULL DEFAULT '{}',
            defaults_policy_json TEXT NOT NULL DEFAULT '{}',
            human_required_conditions_json TEXT NOT NULL DEFAULT '{}',
            completion_policy_json TEXT NOT NULL DEFAULT '{}',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS goal_items (
            id TEXT PRIMARY KEY,
            contract_id TEXT NOT NULL,
            item_key TEXT NOT NULL,
            description TEXT NOT NULL,
            required INTEGER NOT NULL DEFAULT 1,
            acceptance_criteria_json TEXT NOT NULL DEFAULT '{}',
            evidence_requirements_json TEXT NOT NULL DEFAULT '{}',
            verifier_required INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            UNIQUE(contract_id, item_key)
        );

        CREATE TABLE IF NOT EXISTS execution_nodes (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            node_key TEXT NOT NULL,
            node_type TEXT NOT NULL,
            state TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            assignee TEXT,
            latest_task_id TEXT,
            latest_run_id INTEGER,
            input_summary TEXT,
            output_summary TEXT,
            assumptions_json TEXT NOT NULL DEFAULT '{}',
            constraints_json TEXT NOT NULL DEFAULT '{}',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            UNIQUE(job_id, node_key)
        );

        CREATE TABLE IF NOT EXISTS execution_dependencies (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            from_node_id TEXT NOT NULL,
            to_node_id TEXT NOT NULL,
            dependency_type TEXT NOT NULL,
            required INTEGER NOT NULL DEFAULT 1,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL,
            UNIQUE(job_id, from_node_id, to_node_id, dependency_type)
        );

        CREATE TABLE IF NOT EXISTS node_relations (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            from_node_id TEXT NOT NULL,
            to_node_id TEXT NOT NULL,
            relation_type TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL,
            UNIQUE(job_id, from_node_id, to_node_id, relation_type)
        );

        CREATE TABLE IF NOT EXISTS node_materializations (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            task_id TEXT NOT NULL,
            run_id INTEGER,
            worker_lane TEXT,
            status TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            terminal_event_id INTEGER,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(node_id, attempt),
            UNIQUE(task_id)
        );

        CREATE TABLE IF NOT EXISTS progress_ledger (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            contract_id TEXT NOT NULL,
            goal_item_id TEXT NOT NULL,
            node_id TEXT,
            artifact_id TEXT,
            verifier_node_id TEXT,
            evidence_ref TEXT,
            satisfaction TEXT NOT NULL,
            verification_state TEXT NOT NULL,
            confidence REAL,
            summary TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS goal_gaps (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            goal_item_id TEXT,
            gap_key TEXT NOT NULL,
            gap_type TEXT NOT NULL,
            state TEXT NOT NULL,
            summary TEXT NOT NULL,
            evidence_ref TEXT,
            last_attempt_node_id TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            UNIQUE(job_id, gap_key)
        );

        CREATE TABLE IF NOT EXISTS execution_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            node_id TEXT,
            task_id TEXT,
            run_id INTEGER,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            source TEXT NOT NULL,
            source_event_id INTEGER,
            graph_revision INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS graph_patches (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            decision_id TEXT,
            base_revision INTEGER NOT NULL,
            applied_revision INTEGER,
            patch_json TEXT NOT NULL,
            status TEXT NOT NULL,
            reject_reason TEXT,
            created_at INTEGER NOT NULL,
            applied_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS kernel_decisions (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            trigger_event_id INTEGER,
            db_revision INTEGER NOT NULL,
            decision_session_id TEXT,
            delta_json TEXT NOT NULL,
            decision_json TEXT,
            model TEXT,
            status TEXT NOT NULL,
            validator_result_json TEXT NOT NULL DEFAULT '{}',
            error TEXT,
            created_at INTEGER NOT NULL,
            completed_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS decision_sessions (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            profile TEXT,
            provider TEXT,
            model TEXT,
            state TEXT NOT NULL,
            stable_prefix_hash TEXT,
            session_ref TEXT,
            transcript_ref TEXT,
            last_appended_event_id INTEGER,
            last_checkpoint_revision INTEGER,
            context_state_json TEXT NOT NULL DEFAULT '{}',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS decision_session_segments (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            decision_session_id TEXT NOT NULL,
            segment_index INTEGER NOT NULL,
            state TEXT NOT NULL,
            started_at INTEGER NOT NULL,
            closed_at INTEGER,
            start_decision_id TEXT,
            end_decision_id TEXT,
            covered_event_start INTEGER,
            covered_event_end INTEGER,
            covered_graph_revision_start INTEGER,
            covered_graph_revision_end INTEGER,
            estimated_input_tokens INTEGER NOT NULL DEFAULT 0,
            estimated_output_tokens INTEGER NOT NULL DEFAULT 0,
            active_segment_tokens INTEGER NOT NULL DEFAULT 0,
            compacted_checkpoint_id TEXT,
            archive_ref TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(decision_session_id, segment_index)
        );

        CREATE TABLE IF NOT EXISTS decision_segment_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            segment_id TEXT NOT NULL,
            job_id TEXT NOT NULL,
            entry_index INTEGER NOT NULL,
            entry_type TEXT NOT NULL,
            ref_type TEXT,
            ref_id TEXT,
            decision_id TEXT,
            event_id INTEGER,
            patch_id TEXT,
            graph_revision INTEGER,
            payload_json TEXT NOT NULL DEFAULT '{}',
            payload_text TEXT,
            estimated_tokens INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            UNIQUE(segment_id, entry_index)
        );

        CREATE TABLE IF NOT EXISTS decision_checkpoints (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            decision_session_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            checkpoint_json TEXT NOT NULL,
            reason TEXT NOT NULL,
            transcript_ref TEXT,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS node_artifacts (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            node_id TEXT,
            artifact_type TEXT NOT NULL,
            path_or_ref TEXT NOT NULL,
            summary TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_runtime_jobs_state ON runtime_jobs(state);
        CREATE INDEX IF NOT EXISTS idx_runtime_nodes_job_state ON execution_nodes(job_id, state);
        CREATE INDEX IF NOT EXISTS idx_runtime_events_job ON execution_events(job_id, id);
        CREATE INDEX IF NOT EXISTS idx_runtime_gaps_job_state ON goal_gaps(job_id, state);
        CREATE INDEX IF NOT EXISTS idx_decision_checkpoints_job_revision ON decision_checkpoints(job_id, revision);
        CREATE INDEX IF NOT EXISTS idx_decision_segments_job_state ON decision_session_segments(job_id, state);
        CREATE INDEX IF NOT EXISTS idx_decision_entries_segment_order ON decision_segment_entries(segment_id, entry_index);
        CREATE INDEX IF NOT EXISTS idx_decision_entries_job_order ON decision_segment_entries(job_id, id);
        """
    )
    _ensure_column(conn, "decision_sessions", "active_segment_id", "TEXT")
    _ensure_column(conn, "decision_sessions", "latest_checkpoint_id", "TEXT")
    _ensure_column(conn, "decision_sessions", "last_compaction_at", "INTEGER")
    _ensure_column(conn, "decision_sessions", "last_compaction_status", "TEXT")
    _ensure_column(conn, "decision_sessions", "last_compaction_profile", "TEXT")
    _ensure_column(conn, "decision_checkpoints", "source_segment_id", "TEXT")
    _ensure_column(conn, "decision_checkpoints", "profile_name", "TEXT")
    _ensure_column(conn, "decision_checkpoints", "profile_version", "TEXT")
    _ensure_column(conn, "decision_checkpoints", "profile_hash", "TEXT")
    _ensure_column(conn, "decision_checkpoints", "profile_path", "TEXT")
    _ensure_column(conn, "decision_checkpoints", "checkpoint_revision", "INTEGER")
    _ensure_column(conn, "decision_checkpoints", "db_revision", "INTEGER")
    _ensure_column(conn, "decision_checkpoints", "graph_revision", "INTEGER")
    _ensure_column(conn, "decision_checkpoints", "ledger_revision", "INTEGER")
    _ensure_column(conn, "decision_checkpoints", "covered_event_start", "INTEGER")
    _ensure_column(conn, "decision_checkpoints", "covered_event_end", "INTEGER")
    _ensure_column(conn, "decision_checkpoints", "covered_decision_start", "TEXT")
    _ensure_column(conn, "decision_checkpoints", "covered_decision_end", "TEXT")
    _ensure_column(conn, "decision_checkpoints", "covered_entry_start", "INTEGER")
    _ensure_column(conn, "decision_checkpoints", "covered_entry_end", "INTEGER")
    _ensure_column(conn, "decision_checkpoints", "payload_json", "TEXT")
    _ensure_column(conn, "decision_checkpoints", "payload_text", "TEXT")
    _ensure_column(conn, "decision_checkpoints", "validator_status", "TEXT")
    _ensure_column(conn, "decision_checkpoints", "reject_reason", "TEXT")
    _ensure_column(conn, "decision_checkpoints", "supersedes_checkpoint_id", "TEXT")
    _ensure_column(conn, "decision_checkpoints", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _event(
    conn: sqlite3.Connection,
    job_id: str,
    event_type: str,
    payload: Optional[dict[str, Any]] = None,
    *,
    node_id: Optional[str] = None,
    task_id: Optional[str] = None,
    run_id: Optional[int] = None,
    source: str = "runtime_kernel",
    source_event_id: Optional[int] = None,
) -> int:
    job = _job(conn, job_id)
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO execution_events (
            job_id, node_id, task_id, run_id, event_type, payload_json,
            source, source_event_id, graph_revision, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            node_id,
            task_id,
            run_id,
            event_type,
            _json(payload or {}),
            source,
            source_event_id,
            int(job["graph_revision"]),
            now,
        ),
    )
    return int(cur.lastrowid)


def _event_once(
    conn: sqlite3.Connection,
    job_id: str,
    event_type: str,
    key: str,
    payload: Optional[dict[str, Any]] = None,
    *,
    node_id: Optional[str] = None,
    source: str = "runtime_kernel",
) -> Optional[int]:
    """Record a synthetic event once per graph revision and stable key."""

    job = _job(conn, job_id)
    payload_data = dict(payload or {})
    payload_data.setdefault("key", key)
    rows = conn.execute(
        """
        SELECT id, payload_json FROM execution_events
         WHERE job_id = ? AND event_type = ? AND graph_revision = ?
         ORDER BY id DESC
        """,
        (job_id, event_type, int(job["graph_revision"])),
    ).fetchall()
    for row in rows:
        if _loads(row["payload_json"]).get("key") == key:
            return None
    return _event(conn, job_id, event_type, payload_data, node_id=node_id, source=source)


def _job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM runtime_jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown runtime job {job_id}")
    return dict(row)


def _contract(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM goal_contracts WHERE job_id = ? AND state = 'active' ORDER BY version DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"job {job_id} has no active goal contract")
    return dict(row)


def _node_by_key(conn: sqlite3.Connection, job_id: str, node_key: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM execution_nodes WHERE job_id = ? AND node_key = ?",
        (job_id, node_key),
    ).fetchone()
    if row is None:
        raise PatchValidationError(f"unknown node_key {node_key!r}")
    return dict(row)


def _node_optional(conn: sqlite3.Connection, job_id: str, node_key: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM execution_nodes WHERE job_id = ? AND node_key = ?",
        (job_id, node_key),
    ).fetchone()
    return dict(row) if row else None


def _goal_item_by_key(conn: sqlite3.Connection, job_id: str, item_key: str) -> dict[str, Any]:
    contract = _contract(conn, job_id)
    row = conn.execute(
        "SELECT * FROM goal_items WHERE contract_id = ? AND item_key = ?",
        (contract["id"], item_key),
    ).fetchone()
    if row is None:
        raise PatchValidationError(f"unknown goal_item_key {item_key!r}")
    return dict(row)


def _goal_item_optional(conn: sqlite3.Connection, job_id: str, item_key: str) -> Optional[dict[str, Any]]:
    contract = _contract(conn, job_id)
    row = conn.execute(
        "SELECT * FROM goal_items WHERE contract_id = ? AND item_key = ?",
        (contract["id"], item_key),
    ).fetchone()
    return dict(row) if row else None


def _touch_job(conn: sqlite3.Connection, job_id: str, *, state: Optional[str] = None, bump_revision: bool = False) -> None:
    if bump_revision:
        conn.execute(
            """
            UPDATE runtime_jobs
               SET updated_at = ?, graph_revision = graph_revision + 1
             WHERE id = ?
            """,
            (_now(), job_id),
        )
    elif state is not None:
        conn.execute(
            "UPDATE runtime_jobs SET state = ?, updated_at = ? WHERE id = ?",
            (state, _now(), job_id),
        )
    else:
        conn.execute("UPDATE runtime_jobs SET updated_at = ? WHERE id = ?", (_now(), job_id))


def create_runtime_job(
    conn: sqlite3.Connection,
    root_task_id: Optional[str],
    objective: str,
    board: Optional[str] = None,
    workspace_path: Optional[str] = None,
    goal_items: Optional[list[dict[str, Any]]] = None,
    initial_assignee: Optional[str] = None,
) -> str:
    """Create a runtime job, goal contract, decision session, and first node."""

    ensure_runtime_schema(conn)
    if not objective or not objective.strip():
        raise ValueError("objective is required")

    now = _now()
    job_id = _id("rjob")
    contract_id = _id("gcon")
    session_id = _id("dses")
    segment_id = _id("dseg")
    node_id = _id("rnode")
    initial_goal_items = goal_items or [
        {
            "item_key": "initial-runtime-result",
            "description": "produce verified evidence for the requested objective",
            "required": True,
            "acceptance_criteria": {"kind": "phase1-fixture"},
            "evidence_requirements": {"requires_verification": True},
            "verifier_required": True,
        }
    ]
    prefix_hash = hashlib.sha256(
        f"{PATCH_SCHEMA}:{objective.strip()}:{json.dumps(initial_goal_items, sort_keys=True)}".encode("utf-8")
    ).hexdigest()

    conn.execute(
        """
        INSERT INTO runtime_jobs (
            id, root_task_id, board, state, objective, workspace_path,
            decision_profile, active_milestone_key, graph_revision,
            metadata_json, created_at, updated_at
        ) VALUES (?, ?, ?, 'active', ?, ?, 'fixture', NULL, 0, '{}', ?, ?)
        """,
        (job_id, root_task_id, board, objective.strip(), workspace_path, now, now),
    )
    conn.execute(
        """
        INSERT INTO goal_contracts (
            id, job_id, objective, version, state, constraints_json,
            defaults_policy_json, human_required_conditions_json,
            completion_policy_json, metadata_json, created_at, updated_at
        ) VALUES (?, ?, ?, 1, 'active', '{}', '{}', '{}', ?, '{}', ?, ?)
        """,
        (
            contract_id,
            job_id,
            objective.strip(),
            _json({"requires_required_goal_items": True}),
            now,
            now,
        ),
    )
    for item in initial_goal_items:
        key = str(item.get("item_key") or "").strip()
        if not key:
            raise ValueError("goal item item_key is required")
        conn.execute(
            """
            INSERT INTO goal_items (
                id, contract_id, item_key, description, required,
                acceptance_criteria_json, evidence_requirements_json,
                verifier_required, state, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', '{}', ?, ?)
            """,
            (
                _id("gitem"),
                contract_id,
                key,
                str(item.get("description") or key),
                1 if item.get("required", True) else 0,
                _json(item.get("acceptance_criteria") or {}),
                _json(item.get("evidence_requirements") or {}),
                1 if item.get("verifier_required", False) else 0,
                now,
                now,
            ),
        )
    conn.execute(
        """
        INSERT INTO decision_sessions (
            id, job_id, profile, provider, model, state, stable_prefix_hash,
            active_segment_id, context_state_json, metadata_json, created_at, updated_at
        ) VALUES (?, ?, 'fixture', 'local', 'deterministic', 'active', ?, ?, ?, '{}', ?, ?)
        """,
        (
            session_id,
            job_id,
            prefix_hash,
            segment_id,
            _json(
                {
                    "stable_prefix": {"schema": PATCH_SCHEMA, "objective": objective.strip()},
                    "active_segment_id": segment_id,
                }
            ),
            now,
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO decision_session_segments (
            id, job_id, decision_session_id, segment_index, state,
            started_at, covered_graph_revision_start, metadata_json
        ) VALUES (?, ?, ?, 0, 'active', ?, 0, '{}')
        """,
        (segment_id, job_id, session_id, now),
    )
    conn.execute(
        """
        INSERT INTO execution_nodes (
            id, job_id, node_key, node_type, state, title, description,
            assignee, input_summary, assumptions_json, constraints_json, metadata_json,
            created_at, updated_at
        ) VALUES (?, ?, 'understand-scope', 'analysis', 'ready', ?, ?, ?, ?, '{}', '{}', ?, ?, ?)
        """,
        (
            node_id,
            job_id,
            "Establish executable understanding",
            "Analyze the objective enough to produce structured evidence for the first runtime gap.",
            initial_assignee,
            objective.strip(),
            _json({"goal_item_keys": [initial_goal_items[0]["item_key"]], "gap_keys": []}),
            now,
            now,
        ),
    )
    _event(conn, job_id, "job_created", {"objective": objective.strip(), "root_task_id": root_task_id})
    _event(conn, job_id, "goal_contract_created", {"contract_id": contract_id})
    _event(conn, job_id, "node_created", {"node_key": "understand-scope", "node_type": "analysis"}, node_id=node_id)
    detect_goal_gaps(conn, job_id)
    return job_id


def create_runtime_job_from_objective(
    conn: sqlite3.Connection,
    objective: str,
    *,
    board: Optional[str] = None,
    workspace_kind: str = "scratch",
    workspace_path: Optional[str] = None,
    assignee: Optional[str] = None,
    created_by: str = "runtime",
    goal_items: Optional[list[dict[str, Any]]] = None,
    idempotency_key: Optional[str] = None,
) -> str:
    """Create a root Kanban task and promote it into a runtime job."""

    ensure_runtime_schema(conn)
    root_task_id = kb.create_task(
        conn,
        title=objective.strip(),
        body=objective.strip(),
        assignee=None,
        created_by=created_by,
        workspace_kind=workspace_kind,
        workspace_path=workspace_path,
        tenant="runtime",
        idempotency_key=idempotency_key,
        initial_status="running",
        board=board,
    )
    return create_runtime_job(
        conn,
        root_task_id,
        objective,
        board=board,
        workspace_path=workspace_path,
        goal_items=goal_items,
        initial_assignee=assignee,
    )


def promote_runtime_job(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    objective: Optional[str] = None,
    board: Optional[str] = None,
    workspace_path: Optional[str] = None,
    goal_items: Optional[list[dict[str, Any]]] = None,
    initial_assignee: Optional[str] = None,
) -> str:
    """Create a runtime job rooted at an existing Kanban task."""

    ensure_runtime_schema(conn)
    task = kb.get_task(conn, task_id)
    if task is None:
        raise ValueError(f"unknown root task {task_id}")
    resolved_objective = (objective or task.body or task.title or "").strip()
    if not resolved_objective:
        raise ValueError("objective is required")
    return create_runtime_job(
        conn,
        task_id,
        resolved_objective,
        board=board,
        workspace_path=workspace_path or task.workspace_path,
        goal_items=goal_items,
        initial_assignee=initial_assignee,
    )


def list_runtime_jobs(
    conn: sqlite3.Connection,
    *,
    state: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return recent runtime jobs for control-plane list views."""

    ensure_runtime_schema(conn)
    sql = "SELECT * FROM runtime_jobs"
    params: list[Any] = []
    if state:
        sql += " WHERE state = ?"
        params.append(state)
    sql += " ORDER BY updated_at DESC, created_at DESC LIMIT ?"
    params.append(max(1, int(limit)))
    return [_row_to_dict(row) or {} for row in conn.execute(sql, params).fetchall()]


def status_runtime_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    ensure_runtime_schema(conn)
    job = _row_to_dict(conn.execute("SELECT * FROM runtime_jobs WHERE id = ?", (job_id,)).fetchone())
    if job is None:
        raise ValueError(f"unknown runtime job {job_id}")
    frontier = summarize_active_frontier(conn, job_id)
    liveness = summarize_liveness(conn, job_id, frontier)
    return {
        "job": job,
        "goal_contract": _row_to_dict(
            conn.execute("SELECT * FROM goal_contracts WHERE job_id = ? ORDER BY version DESC LIMIT 1", (job_id,)).fetchone()
        ),
        "goal_items": _rows(conn, "SELECT * FROM goal_items WHERE contract_id IN (SELECT id FROM goal_contracts WHERE job_id = ?) ORDER BY item_key", (job_id,)),
        "progress_ledger": _rows(conn, "SELECT * FROM progress_ledger WHERE job_id = ? ORDER BY created_at, id", (job_id,)),
        "goal_gaps": _rows(conn, "SELECT * FROM goal_gaps WHERE job_id = ? ORDER BY gap_key", (job_id,)),
        "nodes": _rows(conn, "SELECT * FROM execution_nodes WHERE job_id = ? ORDER BY created_at, node_key", (job_id,)),
        "dependencies": _rows(conn, "SELECT * FROM execution_dependencies WHERE job_id = ? ORDER BY created_at, id", (job_id,)),
        "relations": _rows(conn, "SELECT * FROM node_relations WHERE job_id = ? ORDER BY created_at, id", (job_id,)),
        "materializations": _rows(conn, "SELECT * FROM node_materializations WHERE job_id = ? ORDER BY created_at, attempt", (job_id,)),
        "recent_events": _rows(conn, "SELECT * FROM execution_events WHERE job_id = ? ORDER BY id DESC LIMIT 50", (job_id,)),
        "decisions": _rows(conn, "SELECT * FROM kernel_decisions WHERE job_id = ? ORDER BY created_at, id", (job_id,)),
        "patches": _rows(conn, "SELECT * FROM graph_patches WHERE job_id = ? ORDER BY created_at, id", (job_id,)),
        "ledger_summary": summarize_progress_ledger(conn, job_id),
        "frontier_summary": frontier,
        "liveness": liveness,
    }


def _patch_reject(conn: sqlite3.Connection, job_id: str, patch: dict[str, Any], reason: str, decision_id: Optional[str]) -> str:
    now = _now()
    patch_id = _id("gpatch")
    conn.execute(
        """
        INSERT INTO graph_patches (
            id, job_id, decision_id, base_revision, patch_json, status,
            reject_reason, created_at
        ) VALUES (?, ?, ?, ?, ?, 'rejected', ?, ?)
        """,
        (patch_id, job_id, decision_id, int(patch.get("expected_revision") or -1), _json(patch), reason, now),
    )
    _event(conn, job_id, "patch_rejected", {"reason": reason})
    return patch_id


def _validate_goal_linkage(op: dict[str, Any]) -> None:
    if op.get("goal_item_keys") or op.get("gap_keys") or op.get("human_gate_reason"):
        return
    raise PatchValidationError("create_node requires goal_item_keys, gap_keys, or human_gate_reason")


def _validate_patch(conn: sqlite3.Connection, job_id: str, patch: dict[str, Any]) -> None:
    job = _job(conn, job_id)
    if not isinstance(patch, dict):
        raise PatchValidationError("patch must be a JSON object")
    if patch.get("schema") != PATCH_SCHEMA:
        raise PatchValidationError(f"patch schema must be {PATCH_SCHEMA!r}")
    if int(patch.get("expected_revision", -1)) != int(job["graph_revision"]):
        raise PatchValidationError("expected_revision does not match current graph revision")
    ops = patch.get("ops")
    if not isinstance(ops, list):
        raise PatchValidationError("patch ops must be a list")
    for op in ops:
        if not isinstance(op, dict):
            raise PatchValidationError("patch op must be an object")
        name = op.get("op")
        if name not in PATCH_OPS:
            raise PatchValidationError(f"unsupported patch op {name!r}")
        if name == "create_node":
            _validate_goal_linkage(op)
            node_key = str(op.get("node_key") or "").strip()
            node_type = str(op.get("node_type") or "").strip()
            if not node_key or not str(op.get("title") or "").strip() or not str(op.get("description") or "").strip():
                raise PatchValidationError("create_node requires node_key, title, and description")
            if node_type not in NODE_TYPES:
                raise PatchValidationError(f"unknown node_type {node_type!r}")
            existing = _node_optional(conn, job_id, node_key)
            if existing is not None:
                raise PatchValidationError(f"duplicate node_key {node_key!r}")
            for key in op.get("goal_item_keys") or []:
                _goal_item_by_key(conn, job_id, key)
            for dep_key in op.get("depends_on") or []:
                _node_by_key(conn, job_id, dep_key)
        elif name == "add_dependency":
            from_node = _node_by_key(conn, job_id, str(op.get("from_node_key") or ""))
            to_node = _node_by_key(conn, job_id, str(op.get("to_node_key") or ""))
            dep_type = str(op.get("dependency_type") or "depends_on")
            if dep_type not in DEPENDENCY_TYPES:
                raise PatchValidationError(f"unknown dependency_type {dep_type!r}")
            if from_node["id"] == to_node["id"]:
                raise PatchValidationError("dependency cannot point to itself")
            if _would_create_dependency_cycle(conn, job_id, from_node["id"], to_node["id"]):
                raise PatchValidationError("dependency would create a cycle")
        elif name == "insert_verifier":
            target_key = op.get("target_node_key")
            if target_key:
                _node_by_key(conn, job_id, str(target_key))
            goal_key = op.get("target_goal_item_key")
            if goal_key:
                _goal_item_by_key(conn, job_id, str(goal_key))
            if not target_key and not goal_key:
                raise PatchValidationError("insert_verifier requires target_node_key or target_goal_item_key")
            verifier_key = str(op.get("verifier_node_key") or "").strip()
            if not verifier_key or not str(op.get("title") or "").strip():
                raise PatchValidationError("insert_verifier requires verifier_node_key and title")
            if _node_optional(conn, job_id, verifier_key) is not None:
                raise PatchValidationError(f"duplicate node_key {verifier_key!r}")
            if not (op.get("goal_item_keys") or op.get("gap_keys")):
                raise PatchValidationError("insert_verifier requires goal_item_keys or gap_keys")
            for key in op.get("goal_item_keys") or []:
                _goal_item_by_key(conn, job_id, key)
        elif name == "request_human":
            _validate_goal_linkage(op)
            if str(op.get("decision_type") or "") not in HUMAN_DECISION_TYPES:
                raise PatchValidationError("request_human requires a supported decision_type")
            for field_name in ("node_key", "question", "why_user_required", "default_recommendation"):
                if not str(op.get(field_name) or "").strip():
                    raise PatchValidationError(f"request_human requires {field_name}")
        elif name == "propose_blocked":
            if str(op.get("blocker_type") or "") not in BLOCKER_TYPES:
                raise PatchValidationError("propose_blocked requires a supported blocker_type")
            for field_name in ("target", "reason", "evidence_ref"):
                if not str(op.get(field_name) or "").strip():
                    raise PatchValidationError(f"propose_blocked requires {field_name}")


def _would_create_dependency_cycle(conn: sqlite3.Connection, job_id: str, from_id: str, to_id: str) -> bool:
    if from_id == to_id:
        return True
    stack = [to_id]
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if current == from_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        rows = conn.execute(
            """
            SELECT to_node_id FROM execution_dependencies
             WHERE job_id = ? AND from_node_id = ? AND required = 1
            """,
            (job_id, current),
        ).fetchall()
        stack.extend(str(row["to_node_id"]) for row in rows)
    return False


def apply_graph_patch(conn: sqlite3.Connection, job_id: str, patch: dict[str, Any], decision_id: Optional[str] = None) -> dict[str, Any]:
    ensure_runtime_schema(conn)
    now = _now()
    try:
        _validate_patch(conn, job_id, patch)
    except PatchValidationError as exc:
        patch_id = _patch_reject(conn, job_id, patch if isinstance(patch, dict) else {}, str(exc), decision_id)
        return {"status": "rejected", "reason": str(exc), "patch_id": patch_id}

    base_revision = int(patch["expected_revision"])
    patch_id = _id("gpatch")
    conn.execute(
        """
        INSERT INTO graph_patches (
            id, job_id, decision_id, base_revision, patch_json, status, created_at
        ) VALUES (?, ?, ?, ?, ?, 'proposed', ?)
        """,
        (patch_id, job_id, decision_id, base_revision, _json(patch), now),
    )
    for op in patch["ops"]:
        _apply_op(conn, job_id, op)
    _touch_job(conn, job_id, bump_revision=True)
    applied_revision = int(_job(conn, job_id)["graph_revision"])
    conn.execute(
        "UPDATE graph_patches SET status = 'applied', applied_revision = ?, applied_at = ? WHERE id = ?",
        (applied_revision, now, patch_id),
    )
    _event(conn, job_id, "patch_applied", {"patch_id": patch_id, "ops": len(patch["ops"])})
    reduce_runtime_job(conn, job_id)
    return {"status": "applied", "patch_id": patch_id, "applied_revision": applied_revision}


def _apply_op(conn: sqlite3.Connection, job_id: str, op: dict[str, Any]) -> None:
    name = op["op"]
    now = _now()
    if name == "create_node":
        node_id = _id("rnode")
        metadata = {
            "goal_item_keys": op.get("goal_item_keys") or [],
            "gap_keys": op.get("gap_keys") or [],
            "human_gate_reason": op.get("human_gate_reason"),
        }
        depends_on = op.get("depends_on") or []
        state = "waiting_dependency" if depends_on else "planned"
        conn.execute(
            """
            INSERT INTO execution_nodes (
                id, job_id, node_key, node_type, state, title, description,
                assignee, input_summary, assumptions_json, constraints_json,
                metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, ?, ?)
            """,
            (
                node_id,
                job_id,
                str(op["node_key"]).strip(),
                str(op["node_type"]).strip(),
                state,
                str(op["title"]).strip(),
                str(op["description"]).strip(),
                op.get("assignee"),
                str(op.get("input_summary") or op["description"]).strip(),
                _json(op.get("constraints") or {}),
                _json(metadata),
                now,
                now,
            ),
        )
        _event(conn, job_id, "node_created", {"node_key": op["node_key"], "node_type": op["node_type"]}, node_id=node_id)
        for dep_key in depends_on:
            from_node = _node_by_key(conn, job_id, dep_key)
            _insert_dependency(conn, job_id, from_node["id"], node_id, "depends_on")
    elif name == "add_dependency":
        from_node = _node_by_key(conn, job_id, str(op["from_node_key"]))
        to_node = _node_by_key(conn, job_id, str(op["to_node_key"]))
        _insert_dependency(conn, job_id, from_node["id"], to_node["id"], str(op.get("dependency_type") or "depends_on"))
    elif name == "insert_verifier":
        goal_keys = op.get("goal_item_keys") or ([op["target_goal_item_key"]] if op.get("target_goal_item_key") else [])
        verifier_op = {
            "op": "create_node",
            "node_key": op["verifier_node_key"],
            "node_type": "verification",
            "title": op["title"],
            "description": op.get("description") or f"Verify {op.get('target_node_key') or op.get('target_goal_item_key')}",
            "goal_item_keys": goal_keys,
            "gap_keys": op.get("gap_keys") or [],
            "assignee": op.get("assignee"),
            "depends_on": [op["target_node_key"]] if op.get("target_node_key") else [],
        }
        _apply_op(conn, job_id, verifier_op)
        verifier = _node_by_key(conn, job_id, str(op["verifier_node_key"]))
        if op.get("target_node_key"):
            target = _node_by_key(conn, job_id, str(op["target_node_key"]))
            _insert_relation(conn, job_id, verifier["id"], target["id"], "verifies")
    elif name == "request_human":
        node_id = _id("rnode")
        conn.execute(
            """
            INSERT INTO execution_nodes (
                id, job_id, node_key, node_type, state, title, description,
                metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, 'human_gate', 'waiting_human', ?, ?, ?, ?, ?)
            """,
            (
                node_id,
                job_id,
                str(op["node_key"]).strip(),
                str(op["question"]).strip(),
                str(op["why_user_required"]).strip(),
                _json(op),
                now,
                now,
            ),
        )
        _event(conn, job_id, "human_required", op, node_id=node_id)
    elif name == "propose_blocked":
        _event(conn, job_id, "blocked_proposed", op)


def _insert_dependency(conn: sqlite3.Connection, job_id: str, from_node_id: str, to_node_id: str, dep_type: str) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO execution_dependencies (
            id, job_id, from_node_id, to_node_id, dependency_type, required,
            metadata_json, created_at
        ) VALUES (?, ?, ?, ?, ?, 1, '{}', ?)
        """,
        (_id("rdep"), job_id, from_node_id, to_node_id, dep_type, _now()),
    )


def _insert_relation(conn: sqlite3.Connection, job_id: str, from_node_id: str, to_node_id: str, relation_type: str) -> None:
    if relation_type not in RELATION_TYPES:
        raise PatchValidationError(f"unknown relation_type {relation_type!r}")
    conn.execute(
        """
        INSERT OR IGNORE INTO node_relations (
            id, job_id, from_node_id, to_node_id, relation_type,
            metadata_json, created_at
        ) VALUES (?, ?, ?, ?, ?, '{}', ?)
        """,
        (_id("nrel"), job_id, from_node_id, to_node_id, relation_type, _now()),
    )


def _nodes_linked_to_goal_item(conn: sqlite3.Connection, job_id: str, item_key: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM execution_nodes WHERE job_id = ? ORDER BY created_at, node_key",
        (job_id,),
    ).fetchall()
    linked: list[dict[str, Any]] = []
    for row in rows:
        node = dict(row)
        metadata = _loads(node.get("metadata_json"))
        if item_key in (metadata.get("goal_item_keys") or []):
            linked.append(node)
    return linked


def _has_pending_decision(conn: sqlite3.Connection, job_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM kernel_decisions WHERE job_id = ? AND status = 'started' LIMIT 1",
        (job_id,),
    ).fetchone()
    return row is not None


def summarize_active_frontier(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT node_key, node_type, state, latest_task_id, metadata_json FROM execution_nodes WHERE job_id = ?",
        (job_id,),
    ).fetchall()
    buckets: dict[str, list[dict[str, Any]]] = {
        "ready": [],
        "running": [],
        "waiting_human": [],
        "waiting_dependency": [],
        "planned": [],
        "failed": [],
        "succeeded": [],
    }
    for row in rows:
        state = str(row["state"])
        if state in buckets:
            buckets[state].append(
                {
                    "node_key": row["node_key"],
                    "node_type": row["node_type"],
                    "task_id": row["latest_task_id"],
                    "goal_item_keys": _loads(row["metadata_json"]).get("goal_item_keys") or [],
                }
            )
    for items in buckets.values():
        items.sort(key=lambda item: item["node_key"])
    return {
        **buckets,
        "has_runnable": bool(buckets["ready"] or buckets["running"]),
        "has_legal_wait": bool(buckets["running"] or buckets["waiting_human"] or _has_pending_decision(conn, job_id)),
    }


def summarize_progress_ledger(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT gi.item_key, pl.satisfaction, pl.verification_state, COUNT(*) AS count
          FROM progress_ledger pl
          JOIN goal_items gi ON gi.id = pl.goal_item_id
         WHERE pl.job_id = ?
         GROUP BY gi.item_key, pl.satisfaction, pl.verification_state
         ORDER BY gi.item_key, pl.satisfaction, pl.verification_state
        """,
        (job_id,),
    ).fetchall()
    return [
        {
            "goal_item_key": row["item_key"],
            "satisfaction": row["satisfaction"],
            "verification_state": row["verification_state"],
            "count": int(row["count"]),
        }
        for row in rows
    ]


def summarize_liveness(conn: sqlite3.Connection, job_id: str, frontier: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    frontier = frontier or summarize_active_frontier(conn, job_id)
    open_gaps = conn.execute(
        "SELECT gap_key, gap_type FROM goal_gaps WHERE job_id = ? AND state = 'open' ORDER BY gap_key",
        (job_id,),
    ).fetchall()
    job = _job(conn, job_id)
    legal_wait = bool(frontier["running"] or frontier["waiting_human"] or _has_pending_decision(conn, job_id))
    illegal_idle = (
        job["state"] != "done"
        and bool(open_gaps)
        and not frontier["ready"]
        and not legal_wait
        and job["state"] != "blocked"
    )
    return {
        "legal_wait": legal_wait,
        "illegal_idle": illegal_idle,
        "open_gap_count": len(open_gaps),
        "ready_count": len(frontier["ready"]),
        "running_count": len(frontier["running"]),
        "waiting_human_count": len(frontier["waiting_human"]),
        "pending_decision": _has_pending_decision(conn, job_id),
    }


def _stale_gap_candidates(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM goal_gaps
         WHERE job_id = ? AND state = 'open' AND gap_type != 'stale_or_no_progress'
           AND attempt_count >= 3
         ORDER BY gap_key
        """,
        (job_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def detect_stagnation(conn: sqlite3.Connection, job_id: str, gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    stale = [gap for gap in gaps if gap.get("gap_type") == "stale_or_no_progress"]
    for gap in stale:
        events.append(
            {
                "event_type": "structure_audit_requested",
                "key": f"stale:{gap['gap_key']}",
                "payload": {
                    "gap_key": gap["gap_key"],
                    "gap_type": gap["gap_type"],
                    "reason": "open gap repeated without new progress",
                },
            }
        )
    rejected_count = conn.execute(
        """
        SELECT COUNT(*) AS count
          FROM graph_patches
         WHERE job_id = ? AND status = 'rejected'
           AND created_at >= COALESCE((SELECT MAX(applied_at) FROM graph_patches WHERE job_id = ? AND status = 'applied'), 0)
        """,
        (job_id, job_id),
    ).fetchone()
    if int(rejected_count["count"] or 0) >= 2:
        events.append(
            {
                "event_type": "structure_audit_requested",
                "key": "repeated_patch_rejections",
                "payload": {
                    "reason": "multiple rejected patches without an applied graph change",
                    "rejected_count": int(rejected_count["count"] or 0),
                },
            }
        )
    return events


def reduce_runtime_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    ensure_runtime_schema(conn)
    now = _now()
    changed_ready: list[str] = []
    for node in conn.execute(
        "SELECT * FROM execution_nodes WHERE job_id = ? AND state IN ('planned', 'waiting_dependency')",
        (job_id,),
    ).fetchall():
        deps = conn.execute(
            """
            SELECT n.state
              FROM execution_dependencies d
              JOIN execution_nodes n ON n.id = d.from_node_id
             WHERE d.job_id = ? AND d.to_node_id = ? AND d.required = 1
            """,
            (job_id, node["id"]),
        ).fetchall()
        if all(dep["state"] == "succeeded" for dep in deps):
            conn.execute(
                "UPDATE execution_nodes SET state = 'ready', updated_at = ? WHERE id = ?",
                (now, node["id"]),
            )
            changed_ready.append(node["node_key"])
            _event_once(conn, job_id, "dependency_satisfied", f"ready:{node['id']}", {"node_key": node["node_key"]}, node_id=node["id"])

    gaps = detect_goal_gaps(conn, job_id)
    frontier = summarize_active_frontier(conn, job_id)
    stagnation_events = detect_stagnation(conn, job_id, gaps)
    active_nodes = conn.execute(
        """
        SELECT state, COUNT(*) AS count FROM execution_nodes
         WHERE job_id = ? GROUP BY state
        """,
        (job_id,),
    ).fetchall()
    counts = {row["state"]: int(row["count"]) for row in active_nodes}
    has_human = counts.get("waiting_human", 0) > 0
    has_running = counts.get("running", 0) > 0
    has_ready = counts.get("ready", 0) > 0
    has_pending_decision = _has_pending_decision(conn, job_id)
    complete = _completion_satisfied(conn, job_id)
    if complete:
        state = "done"
    elif has_human:
        state = "waiting_human"
    elif has_running:
        state = "waiting_worker"
    elif has_ready:
        state = "active"
    elif gaps:
        state = "waiting_decision"
        _event_once(
            conn,
            job_id,
            "decision_requested",
            "open_goal_gaps",
            {"gap_count": len(gaps), "gap_keys": [gap["gap_key"] for gap in gaps]},
        )
        if not has_pending_decision and any(gap["gap_type"] == "no_runnable_for_open_goal" for gap in gaps):
            _event_once(
                conn,
                job_id,
                "liveness_violation",
                "no_runnable_for_open_goal",
                {"reason": "no runnable node while goal gaps remain", "frontier": frontier},
            )
    else:
        state = "active"
    for event in stagnation_events:
        _event_once(conn, job_id, event["event_type"], event["key"], event["payload"])
    _touch_job(conn, job_id, state=state)
    return {"state": state, "ready": changed_ready, "gaps": gaps, "complete": complete, "frontier": frontier}


def _completion_satisfied(conn: sqlite3.Connection, job_id: str) -> bool:
    contract = _contract(conn, job_id)
    required = conn.execute(
        "SELECT * FROM goal_items WHERE contract_id = ? AND required = 1",
        (contract["id"],),
    ).fetchall()
    if not required:
        return False
    for item in required:
        if item["state"] != "satisfied":
            return False
    running = conn.execute(
        "SELECT 1 FROM execution_nodes WHERE job_id = ? AND state IN ('running', 'waiting_human') LIMIT 1",
        (job_id,),
    ).fetchone()
    contradicted = conn.execute(
        "SELECT 1 FROM progress_ledger WHERE job_id = ? AND satisfaction = 'contradicted' LIMIT 1",
        (job_id,),
    ).fetchone()
    failed_required_verifier = conn.execute(
        """
        SELECT 1
          FROM execution_nodes n
         WHERE n.job_id = ?
           AND n.node_type = 'verification'
           AND n.state = 'failed'
           AND n.metadata_json LIKE '%goal_item_keys%'
         LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    return running is None and contradicted is None and failed_required_verifier is None


def detect_goal_gaps(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    ensure_runtime_schema(conn)
    now = _now()
    contract = _contract(conn, job_id)
    active_gap_keys: set[str] = set()
    gaps: list[dict[str, Any]] = []
    items = conn.execute(
        "SELECT * FROM goal_items WHERE contract_id = ? AND required = 1 ORDER BY item_key",
        (contract["id"],),
    ).fetchall()
    for item in items:
        ledger = conn.execute(
            "SELECT * FROM progress_ledger WHERE goal_item_id = ? ORDER BY created_at DESC",
            (item["id"],),
        ).fetchall()
        gap_type: Optional[str] = None
        if item["state"] == "satisfied":
            continue
        linked_nodes = _nodes_linked_to_goal_item(conn, job_id, item["item_key"])
        has_human_gate = any(node["state"] == "waiting_human" for node in linked_nodes)
        has_open_path = any(node["state"] in OPEN_NODE_STATES for node in linked_nodes)
        failed_required = any(node["state"] in {"failed", "blocked"} for node in linked_nodes)
        if has_human_gate:
            gap_type = "blocked_by_human_gate"
        elif any(row["satisfaction"] == "contradicted" for row in ledger):
            gap_type = "contradicted_evidence"
        elif any(row["verification_state"] in {"failed", "failed_verification"} for row in ledger):
            gap_type = "verification_failed"
        elif failed_required and not has_open_path:
            gap_type = "failed_required_node"
        elif not ledger:
            gap_type = "missing_evidence"
        elif any(row["satisfaction"] == "partial" for row in ledger):
            gap_type = "partial_evidence"
        elif any(
            row["satisfaction"] == "full" and row["verification_state"] in {"unverified", "self_reported"}
            for row in ledger
        ):
            gap_type = "needs_verification" if item["verifier_required"] else "partial_evidence"
        else:
            gap_type = "missing_evidence"
        gap_key = f"{item['item_key']}:{gap_type}"
        active_gap_keys.add(gap_key)
        summary = f"{item['item_key']} has gap {gap_type}"
        _upsert_gap(conn, job_id, item["id"], gap_key, gap_type, summary, now)
    runnable = conn.execute(
        """
        SELECT 1 FROM execution_nodes
         WHERE job_id = ? AND state IN ('ready', 'running', 'waiting_human')
         LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    if runnable is None and not _completion_satisfied(conn, job_id):
        gap_key = "runtime:no_runnable_for_open_goal"
        active_gap_keys.add(gap_key)
        _upsert_gap(conn, job_id, None, gap_key, "no_runnable_for_open_goal", "goal remains unmet but graph has no runnable node", now)
    for stale in _stale_gap_candidates(conn, job_id):
        stale_key = f"runtime:stale:{stale['gap_key']}"
        active_gap_keys.add(stale_key)
        _upsert_gap(
            conn,
            job_id,
            stale.get("goal_item_id"),
            stale_key,
            "stale_or_no_progress",
            f"{stale['gap_key']} has not produced new progress",
            now,
        )
    conn.execute(
        "UPDATE goal_gaps SET state = 'resolved', updated_at = ? WHERE job_id = ? AND state = 'open' AND gap_key NOT IN (%s)"
        % ",".join("?" for _ in active_gap_keys) if active_gap_keys else
        "UPDATE goal_gaps SET state = 'resolved', updated_at = ? WHERE job_id = ? AND state = 'open'",
        (now, job_id, *sorted(active_gap_keys)) if active_gap_keys else (now, job_id),
    )
    for row in conn.execute(
        "SELECT * FROM goal_gaps WHERE job_id = ? AND state = 'open' ORDER BY gap_key",
        (job_id,),
    ).fetchall():
        gap = _row_to_dict(row) or {}
        gaps.append(gap)
    for gap in gaps:
        _event_once(
            conn,
            job_id,
            "goal_gap_detected",
            str(gap["gap_key"]),
            {"gap_key": gap["gap_key"], "gap_type": gap["gap_type"]},
        )
    return gaps


def _upsert_gap(conn: sqlite3.Connection, job_id: str, goal_item_id: Optional[str], gap_key: str, gap_type: str, summary: str, now: int) -> None:
    existing = conn.execute(
        "SELECT id, attempt_count, metadata_json FROM goal_gaps WHERE job_id = ? AND gap_key = ?",
        (job_id, gap_key),
    ).fetchone()
    if existing:
        metadata = _loads(existing["metadata_json"])
        metadata["last_detected_at"] = now
        metadata["last_gap_type"] = gap_type
        conn.execute(
            """
            UPDATE goal_gaps
               SET goal_item_id = ?, gap_type = ?, state = 'open', summary = ?,
                   attempt_count = attempt_count + 1, metadata_json = ?, updated_at = ?
             WHERE id = ?
            """,
            (goal_item_id, gap_type, summary, _json(metadata), now, existing["id"]),
        )
    else:
        metadata = {"first_detected_at": now, "last_detected_at": now, "last_gap_type": gap_type}
        conn.execute(
            """
            INSERT INTO goal_gaps (
                id, job_id, goal_item_id, gap_key, gap_type, state, summary,
                attempt_count, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'open', ?, 1, ?, ?, ?)
            """,
            (_id("gap"), job_id, goal_item_id, gap_key, gap_type, summary, _json(metadata), now, now),
        )


def build_decision_delta(conn: sqlite3.Connection, job_id: str, trigger_event_id: Optional[int] = None) -> dict[str, Any]:
    reduce_runtime_job(conn, job_id)
    status = status_runtime_job(conn, job_id)
    return {
        "job": {
            "id": job_id,
            "state": status["job"]["state"],
            "objective": status["job"]["objective"],
            "graph_revision": status["job"]["graph_revision"],
        },
        "trigger_event_id": trigger_event_id,
        "goal_items": [
            {
                "item_key": item["item_key"],
                "state": item["state"],
                "required": bool(item["required"]),
            }
            for item in status["goal_items"]
        ],
        "goal_gaps": [
            {
                "gap_key": gap["gap_key"],
                "gap_type": gap["gap_type"],
                "summary": gap["summary"],
            }
            for gap in status["goal_gaps"]
            if gap["state"] == "open"
        ],
        "frontier": [
            {
                "node_key": node["node_key"],
                "node_type": node["node_type"],
                "state": node["state"],
                "summary": node.get("output_summary") or node.get("input_summary"),
            }
            for node in status["nodes"]
            if node["state"] in {"ready", "running", "succeeded", "failed", "waiting_human"}
        ],
        "available_actions": sorted(PATCH_OPS),
        "policy": {
            "no_release_node": True,
            "no_direct_complete": True,
            "new_node_requires_goal_or_gap_linkage": True,
        },
    }


def append_decision_delta(conn: sqlite3.Connection, decision_session_id: str, delta: dict[str, Any], event_id: Optional[int] = None) -> None:
    row = conn.execute(
        "SELECT job_id, context_state_json FROM decision_sessions WHERE id = ?",
        (decision_session_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown decision session {decision_session_id}")
    state = _loads(row["context_state_json"])
    appended = state.setdefault("deltas", [])
    appended.append(delta)
    state["last_delta"] = delta
    conn.execute(
        """
        UPDATE decision_sessions
           SET context_state_json = ?, last_appended_event_id = ?, updated_at = ?
         WHERE id = ?
        """,
        (_json(state), event_id, _now(), decision_session_id),
    )
    append_decision_segment_entry(
        conn,
        str(row["job_id"]),
        "delta_appended",
        delta,
        event_id=event_id,
        ref_type="kernel_decision_delta",
    )


def _current_session(conn: sqlite3.Connection, job_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM decision_sessions WHERE job_id = ? AND state = 'active' ORDER BY created_at DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    return dict(row) if row else None


def _estimate_tokens(payload: Any, payload_text: Optional[str] = None) -> int:
    if payload_text is not None:
        text = payload_text
    else:
        text = _json(payload)
    return max(1, (len(text) + 3) // 4)


def ensure_decision_segment(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    """Ensure the job has exactly one active decision-session segment."""

    ensure_runtime_schema(conn)
    session = _current_session(conn, job_id)
    if session is None:
        raise ValueError(f"job {job_id} has no active decision session")
    active_id = session.get("active_segment_id")
    if active_id:
        row = conn.execute(
            "SELECT * FROM decision_session_segments WHERE id = ? AND state = 'active'",
            (active_id,),
        ).fetchone()
        if row is not None:
            return dict(row)
    row = conn.execute(
        """
        SELECT * FROM decision_session_segments
         WHERE job_id = ? AND decision_session_id = ? AND state = 'active'
         ORDER BY segment_index DESC LIMIT 1
        """,
        (job_id, session["id"]),
    ).fetchone()
    if row is not None:
        segment = dict(row)
        conn.execute(
            "UPDATE decision_sessions SET active_segment_id = ?, updated_at = ? WHERE id = ?",
            (segment["id"], _now(), session["id"]),
        )
        return segment
    next_index = conn.execute(
        "SELECT COALESCE(MAX(segment_index), -1) + 1 FROM decision_session_segments WHERE decision_session_id = ?",
        (session["id"],),
    ).fetchone()[0]
    segment_id = _id("dseg")
    now = _now()
    conn.execute(
        """
        INSERT INTO decision_session_segments (
            id, job_id, decision_session_id, segment_index, state,
            started_at, covered_graph_revision_start, metadata_json
        ) VALUES (?, ?, ?, ?, 'active', ?, ?, '{}')
        """,
        (segment_id, job_id, session["id"], int(next_index), now, int(_job(conn, job_id)["graph_revision"])),
    )
    conn.execute(
        "UPDATE decision_sessions SET active_segment_id = ?, updated_at = ? WHERE id = ?",
        (segment_id, now, session["id"]),
    )
    return dict(
        conn.execute("SELECT * FROM decision_session_segments WHERE id = ?", (segment_id,)).fetchone()
    )


def append_decision_segment_entry(
    conn: sqlite3.Connection,
    job_id: str,
    entry_type: str,
    payload: Any,
    *,
    decision_id: Optional[str] = None,
    event_id: Optional[int] = None,
    patch_id: Optional[str] = None,
    ref_type: Optional[str] = None,
    ref_id: Optional[str] = None,
    payload_text: Optional[str] = None,
    graph_revision: Optional[int] = None,
) -> dict[str, Any]:
    """Append an ordered transcript entry to the active decision segment."""

    segment = ensure_decision_segment(conn, job_id)
    next_index = conn.execute(
        "SELECT COALESCE(MAX(entry_index), -1) + 1 FROM decision_segment_entries WHERE segment_id = ?",
        (segment["id"],),
    ).fetchone()[0]
    estimated = _estimate_tokens(payload, payload_text)
    now = _now()
    conn.execute(
        """
        INSERT INTO decision_segment_entries (
            segment_id, job_id, entry_index, entry_type, ref_type, ref_id,
            decision_id, event_id, patch_id, graph_revision, payload_json,
            payload_text, estimated_tokens, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            segment["id"],
            job_id,
            int(next_index),
            entry_type,
            ref_type,
            ref_id,
            decision_id,
            event_id,
            patch_id,
            int(graph_revision if graph_revision is not None else _job(conn, job_id)["graph_revision"]),
            _json(payload),
            payload_text,
            estimated,
            now,
        ),
    )
    entry_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        """
        UPDATE decision_session_segments
           SET active_segment_tokens = COALESCE(active_segment_tokens, 0) + ?,
               estimated_input_tokens = COALESCE(estimated_input_tokens, 0) + ?
         WHERE id = ?
        """,
        (estimated, estimated, segment["id"]),
    )
    return dict(conn.execute("SELECT * FROM decision_segment_entries WHERE id = ?", (entry_id,)).fetchone())


def advance_runtime_job(
    conn: sqlite3.Connection,
    job_id: str,
    board: Optional[str] = None,
    create_tasks: bool = True,
    decision_provider: Optional[Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]] = None,
    max_patches: int = 1,
    auto_compact: bool = True,
    compaction_policy: Optional[dict[str, Any]] = None,
) -> AdvanceResult:
    ensure_runtime_schema(conn)
    ingested: list[str] = []
    for node in conn.execute(
        "SELECT * FROM execution_nodes WHERE job_id = ? AND state = 'running'",
        (job_id,),
    ).fetchall():
        if ingest_runtime_node_evidence(conn, node["id"], board=board):
            ingested.append(node["node_key"])
    reduction = reduce_runtime_job(conn, job_id)
    materialized: list[str] = []
    if create_tasks:
        for node in conn.execute(
            "SELECT * FROM execution_nodes WHERE job_id = ? AND state = 'ready' ORDER BY created_at, node_key",
            (job_id,),
        ).fetchall():
            task_id = materialize_runtime_node(conn, dict(node), board=board)
            if task_id:
                materialized.append(node["node_key"])
    reduction = reduce_runtime_job(conn, job_id)
    patch_status = None
    decision_requested = reduction["state"] == "waiting_decision"
    if decision_provider and decision_requested and max_patches > 0:
        from hermes_cli import kanban_runtime_decision as rd

        session = _current_session(conn, job_id)
        if session is None:
            raise ValueError(f"job {job_id} has no active decision session")
        delta = build_decision_delta(conn, job_id)
        append_decision_delta(conn, session["id"], delta)
        decision_id = _id("kdec")
        now = _now()
        db_revision = int(_job(conn, job_id)["graph_revision"])
        conn.execute(
            """
            INSERT INTO kernel_decisions (
                id, job_id, db_revision, decision_session_id, delta_json,
                status, validator_result_json, created_at
            ) VALUES (?, ?, ?, ?, ?, 'started', '{}', ?)
            """,
            (decision_id, job_id, db_revision, session["id"], _json(delta), now),
        )
        try:
            raw_output = decision_provider(session, delta)
            append_decision_segment_entry(
                conn,
                job_id,
                "provider_output",
                {"raw_output": raw_output},
                decision_id=decision_id,
                ref_type="kernel_decision",
                ref_id=decision_id,
            )
            patch = rd.parse_provider_patch(raw_output, db_revision)
            append_decision_segment_entry(
                conn,
                job_id,
                "patch_parsed",
                patch,
                decision_id=decision_id,
                ref_type="kernel_decision",
                ref_id=decision_id,
            )
            conn.execute(
                "UPDATE kernel_decisions SET decision_json = ?, status = 'completed', completed_at = ? WHERE id = ?",
                (_json({"raw_output": raw_output, "patch": patch, "parse_status": "parsed"}), _now(), decision_id),
            )
        except rd.ProviderPatchParseError as exc:
            result = {"status": "parse_failed", "reason": str(exc)}
            conn.execute(
                """
                UPDATE kernel_decisions
                   SET decision_json = ?, status = 'parse_failed',
                       validator_result_json = ?, error = ?, completed_at = ?
                 WHERE id = ?
                """,
                (
                    _json({"raw_output": locals().get("raw_output"), "parse_status": "failed"}),
                    _json(result),
                    str(exc),
                    _now(),
                    decision_id,
                ),
            )
            _event(conn, job_id, "decision_parse_failed", {"reason": str(exc)})
            append_decision_segment_entry(
                conn,
                job_id,
                "validator_result",
                result,
                decision_id=decision_id,
                ref_type="kernel_decision",
                ref_id=decision_id,
            )
            append_decision_segment_entry(
                conn,
                job_id,
                "patch_rejected",
                {"reason": str(exc), "stage": "parse"},
                decision_id=decision_id,
                ref_type="kernel_decision",
                ref_id=decision_id,
            )
            patch_status = "parse_failed"
        except Exception as exc:  # pragma: no cover - defensive path covered by status assertions later.
            conn.execute(
                "UPDATE kernel_decisions SET status = 'failed', error = ?, completed_at = ? WHERE id = ?",
                (str(exc), _now(), decision_id),
            )
            _event(conn, job_id, "decision_failed", {"error": str(exc)})
            append_decision_segment_entry(
                conn,
                job_id,
                "validator_result",
                {"status": "failed", "error": str(exc)},
                decision_id=decision_id,
                ref_type="kernel_decision",
                ref_id=decision_id,
            )
            raise
        else:
            result = apply_graph_patch(conn, job_id, patch, decision_id=decision_id)
            patch_status = result["status"]
            conn.execute(
                "UPDATE kernel_decisions SET validator_result_json = ? WHERE id = ?",
                (_json(result), decision_id),
            )
            append_decision_segment_entry(
                conn,
                job_id,
                "validator_result",
                result,
                decision_id=decision_id,
                patch_id=result.get("patch_id"),
                ref_type="kernel_decision",
                ref_id=decision_id,
            )
            append_decision_segment_entry(
                conn,
                job_id,
                "patch_applied" if result["status"] == "applied" else "patch_rejected",
                result,
                decision_id=decision_id,
                patch_id=result.get("patch_id"),
                ref_type="graph_patch",
                ref_id=result.get("patch_id"),
            )
    if auto_compact:
        from hermes_cli import kanban_runtime_decision as rd

        policy_result = rd.should_compact_decision_session(conn, job_id, compaction_policy)
        if policy_result["should_compact"]:
            rd.compact_decision_session(
                conn,
                job_id,
                profile_name=policy_result["profile_name"],
                reason=policy_result["reason"],
            )
    final_state = _job(conn, job_id)["state"]
    events = [row["event_type"] for row in conn.execute("SELECT event_type FROM execution_events WHERE job_id = ? ORDER BY id", (job_id,))]
    return AdvanceResult(
        job_id=job_id,
        job_state=final_state,
        materialized_nodes=materialized,
        ingested_nodes=ingested,
        decision_requested=decision_requested,
        patch_status=patch_status,
        events=events,
    )


def advance_runtime_job_until_idle(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    board: Optional[str] = None,
    create_tasks: bool = True,
    decision_provider: Optional[Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]] = None,
    max_steps: int = 8,
) -> dict[str, Any]:
    """Run a bounded runtime supervisor loop and stop at a recoverable edge."""

    steps: list[dict[str, Any]] = []
    reason = "max_steps"
    last_signature: Optional[tuple[Any, ...]] = None
    for _ in range(max(1, int(max_steps))):
        before_revision = int(_job(conn, job_id)["graph_revision"])
        result = advance_runtime_job(
            conn,
            job_id,
            board=board,
            create_tasks=create_tasks,
            decision_provider=decision_provider,
        )
        after = _job(conn, job_id)
        signature = (
            after["state"],
            int(after["graph_revision"]),
            tuple(result.materialized_nodes),
            tuple(result.ingested_nodes),
            result.patch_status,
        )
        steps.append(
            {
                "job_state": result.job_state,
                "materialized_nodes": result.materialized_nodes,
                "ingested_nodes": result.ingested_nodes,
                "decision_requested": result.decision_requested,
                "patch_status": result.patch_status,
                "graph_revision_before": before_revision,
                "graph_revision_after": int(after["graph_revision"]),
            }
        )
        if after["state"] == "done":
            reason = "done"
            break
        if after["state"] in {"waiting_worker", "waiting_human", "blocked"}:
            reason = after["state"]
            break
        if (
            after["state"] == "waiting_decision"
            and not decision_provider
        ):
            reason = "waiting_decision"
            break
        if signature == last_signature:
            reason = "no_progress"
            break
        last_signature = signature
    return {
        "job_id": job_id,
        "state": _job(conn, job_id)["state"],
        "reason": reason,
        "steps": steps,
    }


def materialize_runtime_node(conn: sqlite3.Connection, node: dict[str, Any], board: Optional[str] = None) -> Optional[str]:
    if node["state"] != "ready":
        return None
    existing = conn.execute(
        "SELECT * FROM node_materializations WHERE node_id = ? AND status IN ('created', 'running') ORDER BY attempt DESC LIMIT 1",
        (node["id"],),
    ).fetchone()
    if existing:
        return str(existing["task_id"])
    job = _job(conn, node["job_id"])
    attempts = conn.execute(
        "SELECT COALESCE(MAX(attempt), 0) AS max_attempt FROM node_materializations WHERE node_id = ?",
        (node["id"],),
    ).fetchone()
    attempt = int(attempts["max_attempt"] or 0) + 1
    materialization_id = _id("mat")
    body = _worker_context(conn, job, node, materialization_id)
    task_id = kb.create_task(
        conn,
        title=f"[runtime] {node['title']}",
        body=body,
        assignee=node.get("assignee"),
        created_by="runtime_kernel",
        workspace_kind="worktree" if job.get("workspace_path") else "scratch",
        workspace_path=job.get("workspace_path"),
        tenant=f"runtime:{job['id']}",
        idempotency_key=f"runtime:{job['id']}:{node['id']}",
        initial_status="running",
        board=board or job.get("board"),
    )
    run = kb.latest_run(conn, task_id)
    run_id = run.id if run else None
    now = _now()
    conn.execute(
        """
        INSERT OR IGNORE INTO node_materializations (
            id, job_id, node_id, attempt, task_id, run_id, worker_lane,
            status, created_at, started_at, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, '{}')
        """,
        (materialization_id, job["id"], node["id"], attempt, task_id, run_id, node.get("assignee"), now, now),
    )
    conn.execute(
        """
        UPDATE execution_nodes
           SET state = 'running', latest_task_id = ?, latest_run_id = ?,
               started_at = COALESCE(started_at, ?), updated_at = ?
         WHERE id = ?
        """,
        (task_id, run_id, now, now, node["id"]),
    )
    _event(conn, job["id"], "node_materialized", {"node_key": node["node_key"], "task_id": task_id}, node_id=node["id"], task_id=task_id, run_id=run_id)
    _touch_job(conn, job["id"], state="waiting_worker")
    return task_id


def _worker_context(conn: sqlite3.Connection, job: dict[str, Any], node: dict[str, Any], materialization_id: str) -> str:
    metadata = _loads(node.get("metadata_json"))
    dep_rows = conn.execute(
        """
        SELECT n.node_key, n.output_summary
          FROM execution_dependencies d
          JOIN execution_nodes n ON n.id = d.from_node_id
         WHERE d.to_node_id = ?
         ORDER BY n.node_key
        """,
        (node["id"],),
    ).fetchall()
    deps = "\n".join(f"- {row['node_key']}: {row['output_summary'] or ''}" for row in dep_rows) or "- none"
    footer = {
        "runtime_job_id": job["id"],
        "execution_node_id": node["id"],
        "node_key": node["node_key"],
        "node_type": node["node_type"],
        "node_materialization_id": materialization_id,
    }
    return (
        f"# Runtime node\n\n"
        f"Objective: {job['objective']}\n\n"
        f"Node: {node['title']}\n\n"
        f"{node['description']}\n\n"
        f"Goal items: {', '.join(metadata.get('goal_item_keys') or []) or '-'}\n"
        f"Gaps: {', '.join(metadata.get('gap_keys') or []) or '-'}\n\n"
        f"Dependencies:\n{deps}\n\n"
        "Expected receipt fields: verdict, summary, claimed_goal_items, "
        "partial_goal_items, unmet_goal_items, verification, artifacts, "
        "active_assumptions, rejected_approaches, known_failure_boundaries.\n\n"
        f"Runtime footer: {json.dumps(footer, sort_keys=True)}"
    )


def ingest_runtime_node_evidence(conn: sqlite3.Connection, node_id: str, board: Optional[str] = None) -> bool:
    node = conn.execute("SELECT * FROM execution_nodes WHERE id = ?", (node_id,)).fetchone()
    if node is None:
        raise ValueError(f"unknown node {node_id}")
    if not node["latest_task_id"]:
        return False
    snapshot = kb.task_progress_snapshot(conn, node["latest_task_id"], board=board)
    if snapshot is None or snapshot.task.status not in {"done", "blocked"}:
        return False
    metadata = dict(snapshot.evidence or {})
    snapshot_run_id = snapshot.run.id if snapshot.run else node["latest_run_id"]
    verdict = _normalize_verdict(metadata.get("verdict") or snapshot.task.status)
    if node["state"] in TERMINAL_NODE_STATES:
        return False
    now = _now()
    if verdict == "succeeded":
        state = "succeeded"
        event_type = "node_completed"
    elif verdict == "waiting_human":
        state = "waiting_human"
        event_type = "human_required"
    elif verdict == "blocked":
        state = "blocked"
        event_type = "node_blocked"
    elif verdict == "uncertain":
        state = "failed"
        event_type = "node_uncertain"
    else:
        state = "failed"
        event_type = "node_failed"
    assumptions = {
        key: metadata.get(key)
        for key in (
            "active_assumptions",
            "decisions_made",
            "rejected_approaches",
            "known_failure_boundaries",
            "open_questions",
            "risk_notes",
        )
        if metadata.get(key)
    }
    conn.execute(
        """
        UPDATE execution_nodes
           SET state = ?, output_summary = ?, assumptions_json = ?,
               latest_run_id = COALESCE(?, latest_run_id),
               updated_at = ?, completed_at = ?
         WHERE id = ?
        """,
        (
            state,
            metadata.get("summary") or snapshot.task.result or "",
            _json(assumptions),
            snapshot_run_id,
            now,
            now,
            node_id,
        ),
    )
    conn.execute(
        """
        UPDATE node_materializations
           SET status = ?, run_id = COALESCE(?, run_id), completed_at = ?,
               terminal_event_id = COALESCE(terminal_event_id, ?)
         WHERE node_id = ? AND task_id = ?
        """,
        (
            state,
            snapshot_run_id,
            now,
            snapshot.last_event.id if snapshot.last_event else None,
            node_id,
            node["latest_task_id"],
        ),
    )
    update_progress_ledger(conn, node_id, metadata)
    for artifact in metadata.get("artifacts") or []:
        if isinstance(artifact, dict) and artifact.get("path_or_ref"):
            conn.execute(
                """
                INSERT INTO node_artifacts (
                    id, job_id, node_id, artifact_type, path_or_ref, summary,
                    metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _id("art"),
                    node["job_id"],
                    node_id,
                    artifact.get("artifact_type") or "generic",
                    artifact["path_or_ref"],
                    artifact.get("summary") or "",
                    _json(artifact),
                    now,
                ),
            )
    _event(conn, node["job_id"], event_type, {"node_key": node["node_key"], "verdict": verdict}, node_id=node_id, task_id=node["latest_task_id"], run_id=snapshot_run_id, source="kanban_task")
    reduce_runtime_job(conn, node["job_id"])
    return True


def _normalize_verdict(verdict: Any) -> str:
    value = str(verdict or "").strip().lower()
    if value in {"pass", "approved", "success", "succeeded", "done", "completed"}:
        return "succeeded"
    if value in {"blocked"}:
        return "blocked"
    if value in {"human_required", "requires_human", "waiting_human"}:
        return "waiting_human"
    if value in {"uncertain", "unknown"}:
        return "uncertain"
    return "failed"


def update_progress_ledger(conn: sqlite3.Connection, node_id: str, evidence: dict[str, Any]) -> None:
    node = conn.execute("SELECT * FROM execution_nodes WHERE id = ?", (node_id,)).fetchone()
    if node is None:
        raise ValueError(f"unknown node {node_id}")
    job_id = node["job_id"]
    contract = _contract(conn, job_id)
    summary = str(evidence.get("summary") or "")
    verification = evidence.get("verification") or {}
    verification_passed = bool(verification.get("passed")) if isinstance(verification, dict) else False
    explicit_verification_state = str(evidence.get("verification_state") or "").strip()
    default_verification_state = _default_verification_state(dict(node), evidence, verification_passed)
    metadata = _ledger_metadata(evidence)
    claimed_items = evidence.get("claimed_goal_item_keys") or evidence.get("claimed_goal_items") or []
    partial_items = evidence.get("partial_goal_item_keys") or evidence.get("partial_goal_items") or []
    unmet_items = evidence.get("unmet_goal_item_keys") or evidence.get("unmet_goal_items") or []
    contradicted_items = evidence.get("contradicted_goal_item_keys") or evidence.get("contradicted_goal_items") or []
    for key in claimed_items:
        item = _goal_item_optional(conn, job_id, str(key))
        if item:
            _insert_ledger(
                conn,
                job_id,
                contract["id"],
                item["id"],
                node_id,
                str(evidence.get("satisfaction") or "full"),
                explicit_verification_state or default_verification_state,
                summary,
                metadata,
            )
    for key in partial_items:
        item = _goal_item_optional(conn, job_id, str(key))
        if item:
            _insert_ledger(conn, job_id, contract["id"], item["id"], node_id, "partial", "unverified", summary, metadata)
    for key in unmet_items:
        item = _goal_item_optional(conn, job_id, str(key))
        if item:
            _insert_ledger(conn, job_id, contract["id"], item["id"], node_id, "none", "unverified", summary, metadata)
    for key in contradicted_items:
        item = _goal_item_optional(conn, job_id, str(key))
        if item:
            _insert_ledger(conn, job_id, contract["id"], item["id"], node_id, "contradicted", "failed", summary, metadata)
    _refresh_goal_item_states(conn, contract["id"])


def _default_verification_state(node: dict[str, Any], evidence: dict[str, Any], verification_passed: bool) -> str:
    verification = evidence.get("verification")
    verdict = _normalize_verdict(evidence.get("verdict") or "")
    if verification_passed:
        return "verified"
    if node.get("node_type") == "verification" and (verification or verdict == "failed"):
        return "failed"
    if isinstance(verification, dict) and verification.get("passed") is False and node.get("node_type") == "verification":
        return "failed"
    return "self_reported"


def _ledger_metadata(evidence: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "claimed_goal_item_keys",
        "claimed_goal_items",
        "partial_goal_item_keys",
        "partial_goal_items",
        "unmet_goal_item_keys",
        "unmet_goal_items",
        "contradicted_goal_item_keys",
        "contradicted_goal_items",
        "artifacts",
        "artifact_refs",
        "verification",
        "verification_refs",
        "remaining_gaps",
        "new_constraints",
        "active_assumptions",
        "rejected_approaches",
        "known_failure_boundaries",
        "open_questions",
        "risk_notes",
    )
    return {key: evidence.get(key) for key in keys if evidence.get(key)}


def _insert_ledger(
    conn: sqlite3.Connection,
    job_id: str,
    contract_id: str,
    goal_item_id: str,
    node_id: str,
    satisfaction: str,
    verification_state: str,
    summary: str,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO progress_ledger (
            id, job_id, contract_id, goal_item_id, node_id, evidence_ref,
            satisfaction, verification_state, confidence, summary,
            metadata_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _id("pledger"),
            job_id,
            contract_id,
            goal_item_id,
            node_id,
            f"node:{node_id}",
            satisfaction,
            verification_state,
            1.0 if satisfaction == "full" else 0.5,
            summary,
            _json(metadata or {}),
            _now(),
        ),
    )


def _refresh_goal_item_states(conn: sqlite3.Connection, contract_id: str) -> None:
    now = _now()
    for item in conn.execute("SELECT * FROM goal_items WHERE contract_id = ?", (contract_id,)).fetchall():
        ledgers = conn.execute(
            "SELECT satisfaction, verification_state FROM progress_ledger WHERE goal_item_id = ?",
            (item["id"],),
        ).fetchall()
        state = "open"
        if any(row["satisfaction"] == "contradicted" for row in ledgers):
            state = "contradicted"
        elif any(
            row["satisfaction"] == "full"
            and (
                row["verification_state"] == "verified"
                or (not item["verifier_required"] and row["verification_state"] not in {"failed", "failed_verification"})
            )
            for row in ledgers
        ):
            state = "satisfied"
        elif any(row["satisfaction"] in {"full", "partial"} for row in ledgers):
            state = "partial"
        conn.execute(
            "UPDATE goal_items SET state = ?, updated_at = ? WHERE id = ?",
            (state, now, item["id"]),
        )


def fixture_decision_provider(session: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    """Deterministic provider used by tests; not a default workflow."""

    revision = int(delta["job"]["graph_revision"])
    gaps = delta.get("goal_gaps") or []
    if not gaps:
        return {"schema": PATCH_SCHEMA, "expected_revision": revision, "rationale_summary": "no structural gap", "ops": []}
    goal_gap = next((item for item in gaps if item.get("gap_type") not in {"no_runnable_graph", "no_runnable_for_open_goal"}), None)
    gap = goal_gap or gaps[0]
    goal_items = [item["item_key"] for item in delta.get("goal_items") or [] if item["state"] != "satisfied"]
    goal_key = goal_items[0] if goal_items else "initial-runtime-result"
    gap_key = gap["gap_key"]
    if gap["gap_type"] in {"missing_evidence", "no_runnable_graph", "no_runnable_for_open_goal", "failed_required_node"}:
        node_key = f"implement-{goal_key}".replace(":", "-")
        return {
            "schema": PATCH_SCHEMA,
            "expected_revision": revision,
            "rationale_summary": f"create implementation node for {gap_key}",
            "ops": [
                {
                    "op": "create_node",
                    "node_key": node_key,
                    "node_type": "implementation",
                    "title": f"Produce evidence for {goal_key}",
                    "description": f"Execute work that can satisfy goal item {goal_key}.",
                    "goal_item_keys": [goal_key],
                    "gap_keys": [gap_key],
                }
            ],
        }
    if gap["gap_type"] in {"unverified_evidence", "partial_satisfaction", "needs_verification", "partial_evidence"}:
        target = None
        for node in delta.get("frontier") or []:
            if node["state"] == "succeeded" and node["node_type"] == "implementation":
                target = node["node_key"]
                break
        for node in delta.get("frontier") or []:
            if target is not None:
                break
            if node["state"] in {"succeeded", "failed", "ready", "running"}:
                target = node["node_key"]
                break
        target = target or f"implement-{goal_key}".replace(":", "-")
        return {
            "schema": PATCH_SCHEMA,
            "expected_revision": revision,
            "rationale_summary": f"insert verifier for {gap_key}",
            "ops": [
                {
                    "op": "insert_verifier",
                    "target_node_key": target,
                    "verifier_node_key": f"verify-{goal_key}".replace(":", "-"),
                    "title": f"Verify {goal_key}",
                    "goal_item_keys": [goal_key],
                    "gap_keys": [gap_key],
                }
            ],
        }
    return {
        "schema": PATCH_SCHEMA,
        "expected_revision": revision,
        "rationale_summary": f"debug failed gap {gap_key}",
        "ops": [
            {
                "op": "create_node",
                "node_key": f"debug-{goal_key}".replace(":", "-"),
                "node_type": "debug",
                "title": f"Debug {goal_key}",
                "description": f"Investigate failed verifier for {goal_key}.",
                "goal_item_keys": [goal_key],
                "gap_keys": [gap_key],
            }
        ],
    }
