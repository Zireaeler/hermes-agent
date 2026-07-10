"""Goal-driven runtime kernel over Hermes Kanban.

Phase 1 keeps the implementation intentionally local to this module.  The
runtime state is authoritative in SQLite rows; decision providers only propose
graph patches and never own state or scheduling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
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
    "strategy_update",
}
DEPENDENCY_TYPES = {"depends_on", "artifact_input"}
RELATION_TYPES = {"verifies", "blocks", "supersedes", "explains", "replaces_attempt"}
PATCH_OPS = {
    "create_node",
    "add_dependency",
    "insert_verifier",
    "request_human",
    "propose_blocked",
    "strategy_update",
}
DECOMPOSITION_REASON_TYPES = {
    "independent_verification",
    "capability_boundary",
    "human_authority_boundary",
    "workspace_isolation",
    "durable_parallelism",
    "context_or_runtime_limit",
    "execution_discovered_gap",
}
DECOMPOSITION_EVIDENCE_REQUIRED = {
    "context_or_runtime_limit",
    "execution_discovered_gap",
}
EXECUTION_NODE_OPS = {"create_node", "insert_verifier", "strategy_update"}
NONTERMINAL_EXECUTION_STATES = {"planned", "waiting_dependency", "ready", "running"}
RUNTIME_INITIALIZATION_MODES = {"provider_first", "fixture"}
VERIFIER_TARGET_FIELDS = {
    "target_evidence_ref",
    "target_materialization_attempt",
    "target_artifact_ref",
    "target_workspace_revision",
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
    recovery: dict[str, Any] = field(default_factory=dict)


class PatchValidationError(ValueError):
    """Raised when a graph patch violates runtime kernel invariants."""


RECOVERY_EVENT_TYPES = {
    "materialization_lost",
    "worker_run_stale",
    "worker_run_timeout",
    "worker_run_crashed",
    "receipt_missing",
    "receipt_invalid",
    "receipt_recovery_requested",
    "node_recovery_retry_scheduled",
    "node_recovery_rerun_scheduled",
    "node_recovery_not_retryable",
    "materialization_reconciled",
    "terminal_fact_preserved",
    "task_node_state_mismatch",
    "ledger_reference_missing",
    "checkpoint_reference_missing",
    "consistency_violation",
    "consistency_check_passed",
    "legal_waiting_reason_updated",
    "worker_session_discovered",
    "worker_session_interrupted",
    "worker_session_resume_scheduled",
    "worker_session_resumed",
    "worker_session_resume_failed",
    "worker_session_fallback_fresh",
    "worker_context_reacquired",
    "worker_session_identity_conflict",
}

WORKER_SESSION_EVENT_TYPES = {
    "worker_session_discovered",
    "worker_session_interrupted",
    "worker_session_resume_scheduled",
    "worker_session_resumed",
    "worker_session_resume_failed",
    "worker_session_fallback_fresh",
    "worker_context_reacquired",
    "worker_session_identity_conflict",
}

WORKER_SESSION_RESUME_LIMIT = 2

RUNTIME_CAPABILITIES = {
    "filesystem_read",
    "filesystem_write",
    "workspace_write",
    "workspace_escape",
    "network_access",
    "secret_access",
    "external_cost",
    "destructive_action",
    "git_read",
    "git_write",
    "db_read",
    "db_migration",
    "process_spawn",
    "long_running_process",
}

POLICY_RESOLUTION_ORDER = [
    "lane/backend physical incapability",
    "hard deny",
    "unresolved require_human",
    "valid human authorization",
    "job policy",
    "workspace/lane policy",
    "global default",
]

CAPABILITY_EVENT_TYPES = {
    "capability_policy_created",
    "capability_policy_updated",
    "capability_request_evaluated",
    "capability_denied",
    "capability_requires_human",
    "capability_authorized",
    "capability_policy_blocked",
}

RECOVERY_FAILURE_STATUSES = {
    "lost",
    "stale",
    "timed_out",
    "crashed",
    "receipt_missing",
    "receipt_invalid",
}

DEFAULT_RUNTIME_RECOVERY_POLICY = {
    "infra_retry_limit": 1,
    "receipt_recovery_limit": 1,
    "business_failure_auto_retry": False,
    "uncertain_auto_retry": False,
    "run_stale_after_seconds": kb.DEFAULT_CLAIM_TTL_SECONDS,
    "retryable_failure_types": [
        "worker_run_stale",
        "worker_run_timeout",
        "worker_run_crashed",
        "materialization_lost",
        "receipt_missing",
        "receipt_invalid",
    ],
    "non_retryable_failure_types": [
        "business_failed",
        "verification_failed",
        "policy_blocked",
        "missing_secret",
        "external_permission",
    ],
}

DEFAULT_RUNTIME_CAPABILITY_POLICY = {
    "policy_revision": 1,
    "allow_by_default": [
        "filesystem_read",
        "workspace_write",
        "git_read",
        "process_spawn",
    ],
    "require_human": [
        "workspace_escape",
        "secret_access",
        "external_cost",
        "destructive_action",
        "git_write",
        "db_migration",
        "long_running_process",
    ],
    "deny_by_default": [
        "network_access",
        "db_read",
    ],
}


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


def _loads_list(raw: Any) -> list[Any]:
    value = _loads(raw, default=[])
    return value if isinstance(value, list) else []


def _normalize_capability_list(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = str(item or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append(key)
    return normalized


def _validate_capabilities(capabilities: Any, *, field_name: str = "requested_capabilities") -> list[str]:
    normalized = _normalize_capability_list(capabilities)
    unknown = [key for key in normalized if key not in RUNTIME_CAPABILITIES]
    if unknown:
        raise PatchValidationError(f"{field_name} contains unknown capability {unknown[0]!r}")
    return normalized


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

        CREATE TABLE IF NOT EXISTS backend_worker_sessions (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            backend_kind TEXT NOT NULL,
            backend_session_key TEXT NOT NULL,
            status TEXT NOT NULL,
            initial_materialization_id TEXT NOT NULL,
            latest_materialization_id TEXT NOT NULL,
            worker_lane TEXT,
            workspace_path TEXT,
            workspace_revision TEXT,
            capability_fingerprint TEXT NOT NULL,
            node_contract_fingerprint TEXT NOT NULL,
            checkpoint_json TEXT NOT NULL DEFAULT '{}',
            resume_count INTEGER NOT NULL DEFAULT 0,
            last_heartbeat_at INTEGER,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            completed_at INTEGER,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(backend_kind, backend_session_key)
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

        CREATE TABLE IF NOT EXISTS runtime_capability_policies (
            id TEXT PRIMARY KEY,
            job_id TEXT,
            scope_type TEXT NOT NULL,
            scope_ref TEXT,
            policy_revision INTEGER NOT NULL DEFAULT 1,
            allow_json TEXT NOT NULL DEFAULT '[]',
            deny_json TEXT NOT NULL DEFAULT '[]',
            require_human_json TEXT NOT NULL DEFAULT '[]',
            defaults_json TEXT NOT NULL DEFAULT '{}',
            source TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS runtime_capability_authorizations (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            scope_type TEXT NOT NULL,
            scope_ref TEXT,
            capabilities_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL,
            expires_at INTEGER,
            revoked_at INTEGER,
            source_event_id INTEGER,
            source_human_decision_id TEXT,
            reason TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_runtime_jobs_state ON runtime_jobs(state);
        CREATE INDEX IF NOT EXISTS idx_runtime_nodes_job_state ON execution_nodes(job_id, state);
        CREATE INDEX IF NOT EXISTS idx_runtime_events_job ON execution_events(job_id, id);
        CREATE INDEX IF NOT EXISTS idx_runtime_gaps_job_state ON goal_gaps(job_id, state);
        CREATE INDEX IF NOT EXISTS idx_decision_checkpoints_job_revision ON decision_checkpoints(job_id, revision);
        CREATE INDEX IF NOT EXISTS idx_decision_segments_job_state ON decision_session_segments(job_id, state);
        CREATE INDEX IF NOT EXISTS idx_decision_entries_segment_order ON decision_segment_entries(segment_id, entry_index);
        CREATE INDEX IF NOT EXISTS idx_decision_entries_job_order ON decision_segment_entries(job_id, id);
        CREATE INDEX IF NOT EXISTS idx_runtime_capability_policies_job ON runtime_capability_policies(job_id, scope_type, scope_ref);
        CREATE INDEX IF NOT EXISTS idx_runtime_capability_authorizations_job ON runtime_capability_authorizations(job_id, status, scope_type, scope_ref);
        CREATE INDEX IF NOT EXISTS idx_backend_worker_sessions_job ON backend_worker_sessions(job_id, node_id, updated_at);
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


def _node_capability_metadata(op: dict[str, Any]) -> dict[str, Any]:
    metadata = op.get("metadata")
    if isinstance(metadata, dict) and metadata.get("capability_policy") is not None:
        raise PatchValidationError("LLM patch must not write capability_policy")
    if op.get("capability_policy") is not None:
        raise PatchValidationError("LLM patch must not write capability_policy")
    requested = _validate_capabilities(op.get("requested_capabilities") or [], field_name="requested_capabilities")
    return {"requested_capabilities": requested}


def _merge_capability_overrides(policy: dict[str, Any], override: dict[str, Any]) -> None:
    for target, *aliases in (
        ("allow_by_default", "allow_by_default", "allowed", "allow"),
        ("require_human", "require_human", "requires_human"),
        ("deny_by_default", "deny_by_default", "denied", "deny"),
    ):
        for alias in aliases:
            if alias in override:
                policy[target] = _validate_capabilities(override.get(alias), field_name=f"runtime_capability_policy.{alias}")
                break
    if override.get("policy_revision") is not None:
        try:
            policy["policy_revision"] = max(int(policy.get("policy_revision") or 1), int(override["policy_revision"]))
        except (TypeError, ValueError):
            pass


def _capability_policy_rows(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM runtime_capability_policies
             WHERE job_id IS NULL OR job_id = ?
             ORDER BY
               CASE scope_type
                 WHEN 'global_default' THEN 0
                 WHEN 'workspace' THEN 1
                 WHEN 'lane' THEN 2
                 WHEN 'job' THEN 3
                 WHEN 'node_type' THEN 4
                 ELSE 5
               END,
               policy_revision ASC,
               created_at ASC
            """,
            (job_id,),
        ).fetchall()
    ]


def _active_capability_authorizations(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    now: Optional[int] = None,
) -> list[dict[str, Any]]:
    current = _now() if now is None else int(now)
    rows = conn.execute(
        """
        SELECT * FROM runtime_capability_authorizations
         WHERE job_id = ?
         ORDER BY created_at ASC, id ASC
        """,
        (job_id,),
    ).fetchall()
    active: list[dict[str, Any]] = []
    for row in rows:
        auth = dict(row)
        if str(auth.get("status") or "") != "active":
            continue
        expires_at = auth.get("expires_at")
        if expires_at is not None and int(expires_at) <= current:
            continue
        if auth.get("revoked_at") is not None:
            continue
        auth["capabilities"] = _normalize_capability_list(_loads_list(auth.get("capabilities_json")))
        active.append(auth)
    return active


def build_runtime_capability_policy(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    """Build the local runtime capability policy for a job.

    This is a fact-layer policy view. Decision providers may request
    capabilities, but this function decides what the runtime can execute.
    """

    ensure_runtime_schema(conn)
    job = _job(conn, job_id)
    policy: dict[str, Any] = {
        "policy_revision": int(DEFAULT_RUNTIME_CAPABILITY_POLICY["policy_revision"]),
        "allow_by_default": list(DEFAULT_RUNTIME_CAPABILITY_POLICY["allow_by_default"]),
        "require_human": list(DEFAULT_RUNTIME_CAPABILITY_POLICY["require_human"]),
        "deny_by_default": list(DEFAULT_RUNTIME_CAPABILITY_POLICY["deny_by_default"]),
        "policy_resolution_order": list(POLICY_RESOLUTION_ORDER),
        "source": "default",
    }
    metadata = _loads(job.get("metadata_json"))
    job_override = metadata.get("runtime_capability_policy")
    if isinstance(job_override, dict):
        _merge_capability_overrides(policy, job_override)
        policy["source"] = "job_metadata"
    for row in _capability_policy_rows(conn, job_id):
        override = {
            "policy_revision": row.get("policy_revision"),
            "allow_by_default": _loads_list(row.get("allow_json")),
            "deny_by_default": _loads_list(row.get("deny_json")),
            "require_human": _loads_list(row.get("require_human_json")),
        }
        _merge_capability_overrides(policy, override)
        policy["source"] = str(row.get("source") or "runtime_capability_policies")
    policy["allow_by_default"] = sorted(set(policy["allow_by_default"]))
    policy["require_human"] = sorted(set(policy["require_human"]))
    policy["deny_by_default"] = sorted(set(policy["deny_by_default"]))
    policy["active_authorizations"] = [
        {
            "id": auth["id"],
            "scope_type": auth["scope_type"],
            "scope_ref": auth["scope_ref"],
            "capabilities": auth["capabilities"],
            "expires_at": auth["expires_at"],
            "source_event_id": auth["source_event_id"],
            "source_human_decision_id": auth["source_human_decision_id"],
            "reason": auth["reason"],
        }
        for auth in _active_capability_authorizations(conn, job_id)
    ]
    return policy


def _authorization_matches_node(auth: dict[str, Any], node: dict[str, Any], capability: str) -> bool:
    if capability not in set(auth.get("capabilities") or []):
        return False
    scope_type = str(auth.get("scope_type") or "")
    scope_ref = auth.get("scope_ref")
    if scope_type == "job":
        return scope_ref in {None, "", node["job_id"]}
    if scope_type == "node":
        return scope_ref in {node["id"], node["node_key"]}
    return False


def _node_lane_incapable_capabilities(node: dict[str, Any]) -> list[str]:
    metadata = _loads(node.get("metadata_json"))
    policy = metadata.get("capability_policy")
    if not isinstance(policy, dict):
        return []
    return _normalize_capability_list(
        policy.get("lane_incapable")
        or policy.get("backend_incapable")
        or policy.get("physical_incapability")
        or []
    )


def evaluate_node_capability_policy(
    conn: sqlite3.Connection,
    job_id: str,
    node: dict[str, Any],
    *,
    now: Optional[int] = None,
) -> dict[str, Any]:
    ensure_runtime_schema(conn)
    policy = build_runtime_capability_policy(conn, job_id)
    metadata = _loads(node.get("metadata_json"))
    requested = _validate_capabilities(metadata.get("requested_capabilities") or [], field_name="node.requested_capabilities")
    active_authorizations = _active_capability_authorizations(conn, job_id, now=now)
    lane_incapable = set(_node_lane_incapable_capabilities(node))
    allowed: list[str] = []
    denied: list[str] = []
    requires_human: list[str] = []
    authorized: list[str] = []
    lane_blocked: list[str] = []
    deny_set = set(policy["deny_by_default"])
    require_set = set(policy["require_human"])
    allow_set = set(policy["allow_by_default"])
    for capability in requested:
        if capability in lane_incapable:
            lane_blocked.append(capability)
            denied.append(capability)
            continue
        if capability in deny_set:
            denied.append(capability)
            continue
        matching_auth = any(_authorization_matches_node(auth, node, capability) for auth in active_authorizations)
        if capability in require_set and not matching_auth:
            requires_human.append(capability)
            continue
        if matching_auth:
            authorized.append(capability)
        if capability in allow_set or matching_auth:
            allowed.append(capability)
        else:
            denied.append(capability)
    if lane_blocked:
        status = "lane_incapable"
        reason = "lane/backend physical incapability"
    elif denied:
        status = "denied"
        reason = "hard deny"
    elif requires_human:
        status = "requires_human"
        reason = "human authorization required"
    else:
        status = "allowed"
        reason = "allowed"
    return {
        "status": status,
        "reason": reason,
        "policy_revision": policy["policy_revision"],
        "requested": requested,
        "allowed": sorted(set(allowed)),
        "denied": sorted(set(denied)),
        "requires_human": sorted(set(requires_human)),
        "authorized": sorted(set(authorized)),
        "lane_incapable": sorted(set(lane_blocked)),
        "defaults": {
            "allowed_by_default": policy["allow_by_default"],
            "denied_by_default": policy["deny_by_default"],
            "require_human": policy["require_human"],
        },
        "policy_resolution_order": policy["policy_resolution_order"],
    }


def _store_node_capability_evaluation(
    conn: sqlite3.Connection,
    node: dict[str, Any],
    evaluation: dict[str, Any],
) -> None:
    metadata = _loads(node.get("metadata_json"))
    metadata["capability_policy"] = {
        "status": evaluation["status"],
        "reason": evaluation["reason"],
        "policy_revision": evaluation["policy_revision"],
        "requested": evaluation["requested"],
        "allowed": evaluation["allowed"],
        "denied": evaluation["denied"],
        "requires_human": evaluation["requires_human"],
        "authorized": evaluation["authorized"],
        "lane_incapable": evaluation["lane_incapable"],
        "defaults": evaluation["defaults"],
    }
    conn.execute(
        "UPDATE execution_nodes SET metadata_json = ?, updated_at = ? WHERE id = ?",
        (_json(metadata), _now(), node["id"]),
    )


def authorize_runtime_capability(
    conn: sqlite3.Connection,
    job_id: str,
    capabilities: list[str],
    *,
    scope: str = "job",
    scope_ref: Optional[str] = None,
    reason: str,
    expires_at: Optional[int] = None,
    source_event_id: Optional[int] = None,
    source_human_decision_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    ensure_runtime_schema(conn)
    if scope not in {"job", "node"}:
        raise ValueError("capability authorization scope must be job or node")
    caps = _validate_capabilities(capabilities, field_name="capabilities")
    if not caps:
        raise ValueError("capabilities are required")
    if not str(reason or "").strip():
        raise ValueError("authorization reason is required")
    now = _now()
    auth_id = _id("cpauth")
    conn.execute(
        """
        INSERT INTO runtime_capability_authorizations (
            id, job_id, scope_type, scope_ref, capabilities_json, status,
            expires_at, revoked_at, source_event_id, source_human_decision_id,
            reason, created_at, updated_at, metadata_json
        ) VALUES (?, ?, ?, ?, ?, 'active', ?, NULL, ?, ?, ?, ?, ?, ?)
        """,
        (
            auth_id,
            job_id,
            scope,
            scope_ref,
            _json(caps),
            expires_at,
            source_event_id,
            source_human_decision_id,
            str(reason).strip(),
            now,
            now,
            _json(metadata or {}),
        ),
    )
    event_id = _event(
        conn,
        job_id,
        "capability_authorized",
        {
            "authorization_id": auth_id,
            "capabilities": caps,
            "scope": scope,
            "scope_ref": scope_ref,
            "expires_at": expires_at,
            "status": "active",
            "reason": str(reason).strip(),
        },
        source_event_id=source_event_id,
    )
    reenabled: list[str] = []
    for row in conn.execute(
        "SELECT * FROM execution_nodes WHERE job_id = ? AND state = 'waiting_human'",
        (job_id,),
    ).fetchall():
        node = dict(row)
        metadata_json = _loads(node.get("metadata_json"))
        cap_policy = metadata_json.get("capability_policy")
        if not isinstance(cap_policy, dict) or cap_policy.get("status") != "requires_human":
            continue
        evaluation = evaluate_node_capability_policy(conn, job_id, node)
        _store_node_capability_evaluation(conn, node, evaluation)
        if evaluation["status"] == "allowed":
            conn.execute(
                "UPDATE execution_nodes SET state = 'ready', updated_at = ? WHERE id = ?",
                (now, node["id"]),
            )
            reenabled.append(node["node_key"])
    reduce_runtime_job(conn, job_id)
    return {
        "id": auth_id,
        "job_id": job_id,
        "capabilities": caps,
        "scope": scope,
        "scope_ref": scope_ref,
        "expires_at": expires_at,
        "event_id": event_id,
        "reenabled_nodes": reenabled,
    }


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
    initialization_mode: str = "provider_first",
) -> str:
    """Create a runtime job and its authoritative goal/decision state."""

    ensure_runtime_schema(conn)
    if not objective or not objective.strip():
        raise ValueError("objective is required")
    initialization_mode = str(initialization_mode or "").strip()
    if initialization_mode not in RUNTIME_INITIALIZATION_MODES:
        raise ValueError(f"unknown runtime initialization mode {initialization_mode!r}")

    now = _now()
    job_id = _id("rjob")
    contract_id = _id("gcon")
    session_id = _id("dses")
    segment_id = _id("dseg")
    node_id = _id("rnode") if initialization_mode == "fixture" else None
    initial_goal_items = goal_items or [
        {
            "item_key": "initial-runtime-result",
            "description": "produce verified evidence for the requested objective",
            "required": True,
            "acceptance_criteria": {
                "kind": "phase1-fixture" if initialization_mode == "fixture" else "runtime-objective"
            },
            "evidence_requirements": {"requires_verification": True},
            "verifier_required": True,
        }
    ]
    prefix_hash = hashlib.sha256(
        f"{PATCH_SCHEMA}:{objective.strip()}:{json.dumps(initial_goal_items, sort_keys=True)}".encode("utf-8")
    ).hexdigest()

    initial_state = "active" if initialization_mode == "fixture" else "waiting_decision"
    decision_profile = "fixture" if initialization_mode == "fixture" else "graph_patch_decision"
    job_metadata = {"initialization_mode": initialization_mode}
    if initial_assignee:
        job_metadata["default_worker_lane"] = initial_assignee
    conn.execute(
        """
        INSERT INTO runtime_jobs (
            id, root_task_id, board, state, objective, workspace_path,
            decision_profile, active_milestone_key, graph_revision,
            metadata_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?, ?)
        """,
        (
            job_id,
            root_task_id,
            board,
            initial_state,
            objective.strip(),
            workspace_path,
            decision_profile,
            _json(job_metadata),
            now,
            now,
        ),
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
        ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            job_id,
            decision_profile,
            "local" if initialization_mode == "fixture" else "unconfigured",
            "deterministic" if initialization_mode == "fixture" else "unconfigured",
            prefix_hash,
            segment_id,
            _json(
                {
                    "stable_prefix": {"schema": PATCH_SCHEMA, "objective": objective.strip()},
                    "active_segment_id": segment_id,
                }
            ),
            _json({"initialization_mode": initialization_mode}),
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
    if initialization_mode == "fixture":
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
    _event(
        conn,
        job_id,
        "job_created",
        {
            "objective": objective.strip(),
            "root_task_id": root_task_id,
            "initialization_mode": initialization_mode,
        },
    )
    _event(conn, job_id, "goal_contract_created", {"contract_id": contract_id})
    if initialization_mode == "fixture":
        _event(conn, job_id, "node_created", {"node_key": "understand-scope", "node_type": "analysis"}, node_id=node_id)
    detect_goal_gaps(conn, job_id)
    if initialization_mode == "provider_first":
        _event_once(
            conn,
            job_id,
            "decision_requested",
            "initial_graph_required",
            {
                "reason": "initial_graph_required",
                "goal_item_keys": [item["item_key"] for item in initial_goal_items],
                "graph_revision": 0,
            },
        )
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
        initialization_mode="provider_first",
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
        initialization_mode="provider_first",
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


def acquire_runtime_advance_lock(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    owner: Optional[str] = None,
    ttl_seconds: int = 60,
) -> dict[str, Any]:
    """Acquire a DB lease for one supervisor advance tick."""

    ensure_runtime_schema(conn)
    owner_id = owner or f"runtime-supervisor-{uuid.uuid4().hex[:8]}"
    ttl = max(1, int(ttl_seconds))
    now = _now()
    expires_at = now + ttl
    row = conn.execute(
        "SELECT id, state, advance_lock, claim_expires_at, metadata_json FROM runtime_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown runtime job {job_id}")
    if row["state"] in {"done", "cancelled", "failed"}:
        return {
            "acquired": False,
            "job_id": job_id,
            "owner": owner_id,
            "reason": f"terminal_state:{row['state']}",
        }
    metadata = _loads(row["metadata_json"])
    if metadata.get("paused"):
        return {"acquired": False, "job_id": job_id, "owner": owner_id, "reason": "paused"}
    current_owner = row["advance_lock"]
    current_expiry = int(row["claim_expires_at"] or 0)
    if current_owner and current_expiry > now and current_owner != owner_id:
        return {
            "acquired": False,
            "job_id": job_id,
            "owner": owner_id,
            "held_by": current_owner,
            "claim_expires_at": current_expiry,
            "reason": "locked",
        }
    cursor = conn.execute(
        """
        UPDATE runtime_jobs
           SET advance_lock = ?, claim_expires_at = ?, updated_at = ?
         WHERE id = ?
           AND (
                advance_lock IS NULL
                OR claim_expires_at IS NULL
                OR claim_expires_at <= ?
                OR advance_lock = ?
           )
        """,
        (owner_id, expires_at, now, job_id, now, owner_id),
    )
    if cursor.rowcount != 1:
        row = conn.execute(
            "SELECT advance_lock, claim_expires_at FROM runtime_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        return {
            "acquired": False,
            "job_id": job_id,
            "owner": owner_id,
            "held_by": row["advance_lock"] if row else None,
            "claim_expires_at": row["claim_expires_at"] if row else None,
            "reason": "locked",
        }
    _event(conn, job_id, "advance_lock_acquired", {"owner": owner_id, "claim_expires_at": expires_at})
    return {
        "acquired": True,
        "job_id": job_id,
        "owner": owner_id,
        "claim_expires_at": expires_at,
        "ttl_seconds": ttl,
    }


def release_runtime_advance_lock(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    owner: str,
    force: bool = False,
) -> dict[str, Any]:
    """Release a supervisor lease if still held by this owner."""

    ensure_runtime_schema(conn)
    row = conn.execute(
        "SELECT advance_lock, claim_expires_at FROM runtime_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown runtime job {job_id}")
    if not force and row["advance_lock"] != owner:
        return {
            "released": False,
            "job_id": job_id,
            "owner": owner,
            "held_by": row["advance_lock"],
            "reason": "not_owner",
        }
    conn.execute(
        "UPDATE runtime_jobs SET advance_lock = NULL, claim_expires_at = NULL, updated_at = ? WHERE id = ?",
        (_now(), job_id),
    )
    _event(conn, job_id, "advance_lock_released", {"owner": owner, "force": bool(force)})
    return {"released": True, "job_id": job_id, "owner": owner, "force": bool(force)}


def status_runtime_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    ensure_runtime_schema(conn)
    job = _row_to_dict(conn.execute("SELECT * FROM runtime_jobs WHERE id = ?", (job_id,)).fetchone())
    if job is None:
        raise ValueError(f"unknown runtime job {job_id}")
    frontier = summarize_active_frontier(conn, job_id)
    liveness = summarize_liveness(conn, job_id, frontier)
    capabilities = summarize_runtime_capabilities(conn, job_id)
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
        "backend_worker_sessions": _rows(conn, "SELECT * FROM backend_worker_sessions WHERE job_id = ? ORDER BY created_at, id", (job_id,)),
        "recent_events": _rows(conn, "SELECT * FROM execution_events WHERE job_id = ? ORDER BY id DESC LIMIT 50", (job_id,)),
        "decisions": _rows(conn, "SELECT * FROM kernel_decisions WHERE job_id = ? ORDER BY created_at, id", (job_id,)),
        "patches": _rows(conn, "SELECT * FROM graph_patches WHERE job_id = ? ORDER BY created_at, id", (job_id,)),
        "ledger_summary": summarize_progress_ledger(conn, job_id),
        "frontier_summary": frontier,
        "liveness": liveness,
        "capabilities": capabilities,
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


def _validate_goal_or_gap_linkage(op: dict[str, Any], op_name: str) -> None:
    if op.get("goal_item_keys") or op.get("gap_keys") or op.get("human_gate_reason"):
        return
    raise PatchValidationError(f"{op_name} requires goal_item_keys, gap_keys, or human_gate_reason")


def _validate_node_contract(op: dict[str, Any], *, required: bool = False) -> None:
    contract = op.get("contract")
    if contract is None:
        if required:
            raise PatchValidationError(f"{op.get('op')} requires typed contract for provider-first jobs")
        return
    if not isinstance(contract, dict):
        raise PatchValidationError("create_node contract must be an object")
    if not str(contract.get("outcome") or "").strip():
        raise PatchValidationError("create_node contract requires outcome")
    for key in ("acceptance_criteria", "success_evidence"):
        values = contract.get(key)
        if not isinstance(values, list) or not [str(value).strip() for value in values if str(value).strip()]:
            raise PatchValidationError(f"create_node contract requires non-empty {key}")
    for key in ("declared_write_scope", "prohibited_actions"):
        values = contract.get(key, [])
        if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
            raise PatchValidationError(f"create_node contract {key} must be a string list")
    _validate_declared_write_scopes(
        contract.get("declared_write_scope") or [],
        field_name="create_node contract declared_write_scope",
    )


def _validate_declared_write_scopes(scopes: list[str], *, field_name: str) -> None:
    for scope in scopes:
        clean = scope.strip().replace("\\", "/")
        parts = clean.split("/")
        if scope != clean or clean.startswith("/") or clean.startswith("./") or ".." in parts:
            raise PatchValidationError(f"{field_name} must use canonical workspace-relative globs")
        if clean in {"repository", "workspace"} or clean.startswith(("repository/", "workspace/")):
            raise PatchValidationError(f"{field_name} must use '**' for the whole workspace")


def _scope_prefix(scope: str) -> str:
    clean = str(scope).strip().replace("\\", "/")
    wildcard = min([index for index in (clean.find("*"), clean.find("?")) if index >= 0] or [len(clean)])
    return clean[:wildcard].rstrip("/")


def _scopes_obviously_overlap(left: list[str], right: list[str]) -> bool:
    for first in left:
        for second in right:
            a = _scope_prefix(first)
            b = _scope_prefix(second)
            if not a or not b or a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                return True
    return False


def _validate_evidence_ref(conn: sqlite3.Connection, job_id: str, ref: str) -> None:
    value = str(ref or "").strip()
    if not value:
        raise PatchValidationError("decomposition evidence_refs must not be empty")
    if value.startswith("event:"):
        try:
            event_id = int(value.split(":", 1)[1])
        except ValueError as exc:
            raise PatchValidationError(f"invalid event evidence_ref {value!r}") from exc
        if conn.execute("SELECT 1 FROM execution_events WHERE job_id = ? AND id = ?", (job_id, event_id)).fetchone() is None:
            raise PatchValidationError(f"unknown evidence_ref {value!r}")
        return
    if value.startswith("artifact:"):
        artifact_ref = value.split(":", 1)[1]
        if conn.execute("SELECT 1 FROM node_artifacts WHERE job_id = ? AND (id = ? OR path_or_ref = ?)", (job_id, artifact_ref, artifact_ref)).fetchone() is None:
            raise PatchValidationError(f"unknown evidence_ref {value!r}")
        return
    if value.startswith("receipt:"):
        parts = value.split(":")
        if len(parts) != 3 or not parts[2].startswith("attempt-"):
            raise PatchValidationError(f"invalid receipt evidence_ref {value!r}")
        node = _node_by_key(conn, job_id, parts[1])
        try:
            attempt = int(parts[2][len("attempt-"):])
        except ValueError as exc:
            raise PatchValidationError(f"invalid receipt evidence_ref {value!r}") from exc
        if conn.execute("SELECT 1 FROM node_materializations WHERE node_id = ? AND attempt = ? AND status IN ('succeeded', 'failed', 'blocked', 'waiting_human')", (node["id"], attempt)).fetchone() is None:
            raise PatchValidationError(f"unknown evidence_ref {value!r}")
        return
    raise PatchValidationError(f"unsupported evidence_ref {value!r}")


def _patch_requires_decomposition(conn: sqlite3.Connection, job_id: str, ops: list[dict[str, Any]]) -> bool:
    execution_ops = [op for op in ops if op.get("op") in EXECUTION_NODE_OPS]
    if len(execution_ops) >= 2 or any(op.get("op") == "insert_verifier" for op in execution_ops):
        return True
    immediate = [op for op in execution_ops if op.get("op") in {"create_node", "strategy_update"} and not (op.get("depends_on") or [])]
    initialization_mode = _loads(_job(conn, job_id).get("metadata_json")).get("initialization_mode")
    states = NONTERMINAL_EXECUTION_STATES if initialization_mode == "provider_first" else {"running"}
    placeholders = ",".join("?" for _ in states)
    if immediate and conn.execute(
        f"SELECT 1 FROM execution_nodes WHERE job_id = ? AND state IN ({placeholders}) LIMIT 1",
        (job_id, *sorted(states)),
    ).fetchone() is not None:
        return True
    return False


def _validate_decomposition(conn: sqlite3.Connection, job_id: str, patch: dict[str, Any], ops: list[dict[str, Any]]) -> None:
    decomposition = patch.get("decomposition")
    required = _patch_requires_decomposition(conn, job_id, ops)
    if decomposition is None:
        if required:
            raise PatchValidationError("graph expansion requires decomposition")
        if len([op for op in ops if op.get("op") in EXECUTION_NODE_OPS and not (op.get("depends_on") or [])]) > 1:
            raise PatchValidationError("patch without decomposition may create at most one runnable worker node")
        return
    if not isinstance(decomposition, dict):
        raise PatchValidationError("decomposition must be an object")
    if str(decomposition.get("policy_version") or "") != "1":
        raise PatchValidationError("decomposition policy_version must be '1'")
    if decomposition.get("mode") != "multiple_runtime_nodes":
        raise PatchValidationError("decomposition mode must be 'multiple_runtime_nodes'")
    justifications = decomposition.get("justifications")
    if not isinstance(justifications, list) or not justifications:
        raise PatchValidationError("decomposition requires justifications")
    patch_node_keys = {
        str(op.get("node_key") or op.get("verifier_node_key") or "").strip()
        for op in ops if op.get("op") in EXECUTION_NODE_OPS
    }
    known_node_keys = {row["node_key"] for row in conn.execute("SELECT node_key FROM execution_nodes WHERE job_id = ?", (job_id,))}
    covered: set[str] = set()
    for item in justifications:
        if not isinstance(item, dict):
            raise PatchValidationError("decomposition justification must be an object")
        reason = str(item.get("type") or "")
        if reason not in DECOMPOSITION_REASON_TYPES:
            raise PatchValidationError(f"unsupported decomposition reason {reason!r}")
        nodes = item.get("nodes")
        if not isinstance(nodes, list) or not nodes or any(not str(node).strip() for node in nodes):
            raise PatchValidationError("decomposition justification requires nodes")
        nodes = [str(node).strip() for node in nodes]
        unknown = set(nodes) - patch_node_keys - known_node_keys
        if unknown:
            raise PatchValidationError(f"decomposition references unknown nodes {sorted(unknown)!r}")
        if not str(item.get("explanation") or "").strip():
            raise PatchValidationError("decomposition justification requires explanation")
        refs = item.get("evidence_refs") or []
        if not isinstance(refs, list):
            raise PatchValidationError("decomposition evidence_refs must be a list")
        if reason in DECOMPOSITION_EVIDENCE_REQUIRED and not refs:
            raise PatchValidationError(f"decomposition reason {reason!r} requires evidence_refs")
        for ref in refs:
            _validate_evidence_ref(conn, job_id, str(ref))
        if reason == "durable_parallelism":
            scopes = item.get("declared_write_scopes")
            if not isinstance(scopes, dict) or any(node not in scopes for node in nodes):
                raise PatchValidationError("durable_parallelism requires declared_write_scopes for every node")
            if not str(item.get("integration_owner_node_key") or "").strip():
                raise PatchValidationError("durable_parallelism requires integration_owner_node_key")
            for index, node_key in enumerate(nodes):
                node_scopes = scopes.get(node_key)
                if not isinstance(node_scopes, list):
                    raise PatchValidationError("declared_write_scopes values must be lists")
                _validate_declared_write_scopes(
                    node_scopes,
                    field_name="decomposition declared_write_scopes",
                )
                for other_key in nodes[index + 1:]:
                    if _scopes_obviously_overlap(node_scopes, scopes.get(other_key) or []):
                        raise PatchValidationError(f"durable_parallelism write scopes overlap for {node_key!r} and {other_key!r}")
        covered.update(set(nodes) & patch_node_keys)
    if required and not patch_node_keys.issubset(covered):
        raise PatchValidationError("decomposition must justify every new execution node")


def _validate_patch(conn: sqlite3.Connection, job_id: str, patch: dict[str, Any]) -> None:
    job = _job(conn, job_id)
    typed_contract_required = (
        _loads(job.get("metadata_json")).get("initialization_mode") == "provider_first"
    )
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
            _node_capability_metadata(op)
            _validate_node_contract(op, required=typed_contract_required)
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
            from_key = str(op.get("from_node_key") or "").strip()
            to_key = str(op.get("to_node_key") or "").strip()
            if not from_key or not to_key:
                raise PatchValidationError("add_dependency requires from_node_key and to_node_key")
            from_node = _node_by_key(conn, job_id, from_key)
            to_node = _node_by_key(conn, job_id, to_key)
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
            if not any(op.get(field) not in {None, ""} for field in VERIFIER_TARGET_FIELDS):
                raise PatchValidationError("insert_verifier requires a fixed target evidence/materialization/artifact/workspace reference")
            if op.get("target_evidence_ref"):
                _validate_evidence_ref(conn, job_id, str(op["target_evidence_ref"]))
            if op.get("target_materialization_attempt") is not None:
                if not target_key:
                    raise PatchValidationError("target_materialization_attempt requires target_node_key")
                try:
                    attempt = int(op["target_materialization_attempt"])
                except (TypeError, ValueError) as exc:
                    raise PatchValidationError("target_materialization_attempt must be an integer") from exc
                target = _node_by_key(conn, job_id, str(target_key))
                if conn.execute("SELECT 1 FROM node_materializations WHERE node_id = ? AND attempt = ?", (target["id"], attempt)).fetchone() is None:
                    raise PatchValidationError("insert_verifier target materialization does not exist")
            if op.get("target_artifact_ref"):
                artifact_ref = str(op["target_artifact_ref"])
                if conn.execute("SELECT 1 FROM node_artifacts WHERE job_id = ? AND (id = ? OR path_or_ref = ?)", (job_id, artifact_ref, artifact_ref)).fetchone() is None:
                    raise PatchValidationError("insert_verifier target artifact does not exist")
            verifier_key = str(op.get("verifier_node_key") or "").strip()
            if not verifier_key or not str(op.get("title") or "").strip():
                raise PatchValidationError("insert_verifier requires verifier_node_key and title")
            if _node_optional(conn, job_id, verifier_key) is not None:
                raise PatchValidationError(f"duplicate node_key {verifier_key!r}")
            if not (op.get("goal_item_keys") or op.get("gap_keys")):
                raise PatchValidationError("insert_verifier requires goal_item_keys or gap_keys")
            _node_capability_metadata(op)
            _validate_node_contract(op, required=typed_contract_required)
            for key in op.get("goal_item_keys") or []:
                _goal_item_by_key(conn, job_id, key)

        elif name == "request_human":
            _validate_goal_linkage(op)
            if str(op.get("decision_type") or "") not in HUMAN_DECISION_TYPES:
                raise PatchValidationError("request_human requires a supported decision_type")
            for field_name in ("node_key", "question", "why_user_required", "default_recommendation"):
                if not str(op.get(field_name) or "").strip():
                    raise PatchValidationError(f"request_human requires {field_name}")
            cap_request = op.get("capability_request")
            if cap_request is not None:
                if not isinstance(cap_request, dict):
                    raise PatchValidationError("capability_request must be an object")
                _validate_capabilities(cap_request.get("capabilities") or [], field_name="capability_request.capabilities")
                if not str(cap_request.get("reason") or "").strip():
                    raise PatchValidationError("capability_request requires reason")
                if not str(op.get("risk_if_defaulted") or "").strip():
                    raise PatchValidationError("request_human capability authorization requires risk_if_defaulted")
        elif name == "propose_blocked":
            if str(op.get("blocker_type") or "") not in BLOCKER_TYPES:
                raise PatchValidationError("propose_blocked requires a supported blocker_type")
            for field_name in ("target", "reason", "evidence_ref"):
                if not str(op.get(field_name) or "").strip():
                    raise PatchValidationError(f"propose_blocked requires {field_name}")
        elif name == "strategy_update":
            _validate_goal_or_gap_linkage(op, "strategy_update")
            _node_capability_metadata(op)
            _validate_node_contract(op, required=typed_contract_required)
            node_key = str(op.get("node_key") or "").strip()
            if not node_key or not str(op.get("title") or "").strip() or not str(op.get("description") or "").strip():
                raise PatchValidationError("strategy_update requires node_key, title, and description")
            if _node_optional(conn, job_id, node_key) is not None:
                raise PatchValidationError(f"duplicate node_key {node_key!r}")
            if not str(op.get("strategy_summary") or "").strip():
                raise PatchValidationError("strategy_update requires strategy_summary")
            changes = op.get("changes_from_previous_attempts")
            if not isinstance(changes, list) or not [str(item).strip() for item in changes if str(item).strip()]:
                raise PatchValidationError("strategy_update requires changes_from_previous_attempts")
            for key in op.get("goal_item_keys") or []:
                _goal_item_by_key(conn, job_id, key)

    _validate_decomposition(conn, job_id, patch, ops)


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


def validate_graph_patch(conn: sqlite3.Connection, job_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Validate a graph patch without inserting graph_patches or applying ops."""

    ensure_runtime_schema(conn)
    current_revision = int(_job(conn, job_id)["graph_revision"])
    try:
        _validate_patch(conn, job_id, patch)
    except PatchValidationError as exc:
        reason = str(exc)
        status = "stale" if "expected_revision" in reason else "rejected"
        return {
            "status": status,
            "would_apply": False,
            "reason": reason,
            "current_revision": current_revision,
            "expected_revision": int(patch.get("expected_revision") or -1) if isinstance(patch, dict) else None,
        }
    return {
        "status": "accepted",
        "would_apply": True,
        "current_revision": current_revision,
        "expected_revision": int(patch.get("expected_revision") or current_revision),
        "ops": len(patch.get("ops") or []),
    }


def _apply_op(conn: sqlite3.Connection, job_id: str, op: dict[str, Any]) -> None:
    name = op["op"]
    now = _now()
    if name == "create_node":
        node_id = _id("rnode")
        metadata = {
            "goal_item_keys": op.get("goal_item_keys") or [],
            "gap_keys": op.get("gap_keys") or [],
            "human_gate_reason": op.get("human_gate_reason"),
            **_node_capability_metadata(op),
        }
        depends_on = op.get("depends_on") or []
        state = "waiting_dependency" if depends_on else "planned"
        constraints = dict(op.get("constraints") or {})
        if op.get("contract") is not None:
            constraints["contract"] = op["contract"]
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
                _json(constraints),
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
            "requested_capabilities": op.get("requested_capabilities") or [],
            "contract": op.get("contract"),
        }
        _apply_op(conn, job_id, verifier_op)
        verifier = _node_by_key(conn, job_id, str(op["verifier_node_key"]))
        if op.get("target_node_key"):
            target = _node_by_key(conn, job_id, str(op["target_node_key"]))
            _insert_relation(
                conn,
                job_id,
                verifier["id"],
                target["id"],
                "verifies",
                metadata={field: op.get(field) for field in VERIFIER_TARGET_FIELDS if op.get(field) is not None},
            )
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
    elif name == "strategy_update":
        node_id = _id("rnode")
        metadata = {
            "goal_item_keys": op.get("goal_item_keys") or [],
            "gap_keys": op.get("gap_keys") or [],
            "human_gate_reason": op.get("human_gate_reason"),
            "strategy_summary": op.get("strategy_summary"),
            "changes_from_previous_attempts": op.get("changes_from_previous_attempts") or [],
            **_node_capability_metadata(op),
        }
        constraints = dict(op.get("constraints") or {})
        if op.get("contract") is not None:
            constraints["contract"] = op["contract"]
        conn.execute(
            """
            INSERT INTO execution_nodes (
                id, job_id, node_key, node_type, state, title, description,
                assignee, input_summary, assumptions_json, constraints_json,
                metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, 'strategy_update', 'planned', ?, ?, ?, ?, '{}', ?, ?, ?, ?)
            """,
            (
                node_id,
                job_id,
                str(op["node_key"]).strip(),
                str(op["title"]).strip(),
                str(op["description"]).strip(),
                op.get("assignee"),
                str(op.get("input_summary") or op["description"]).strip(),
                _json(constraints),
                _json(metadata),
                now,
                now,
            ),
        )
        _event(conn, job_id, "strategy_update_requested", metadata, node_id=node_id)


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


def _insert_relation(
    conn: sqlite3.Connection,
    job_id: str,
    from_node_id: str,
    to_node_id: str,
    relation_type: str,
    *,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    if relation_type not in RELATION_TYPES:
        raise PatchValidationError(f"unknown relation_type {relation_type!r}")
    conn.execute(
        """
        INSERT OR IGNORE INTO node_relations (
            id, job_id, from_node_id, to_node_id, relation_type,
            metadata_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (_id("nrel"), job_id, from_node_id, to_node_id, relation_type, _json(metadata or {}), _now()),
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
    waiting_decision = _job(conn, job_id)["state"] == "waiting_decision"
    return {
        **buckets,
        "has_runnable": bool(buckets["ready"] or buckets["running"]),
        "has_legal_wait": bool(
            buckets["running"]
            or buckets["waiting_human"]
            or _has_pending_decision(conn, job_id)
            or waiting_decision
        ),
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
    pending_decision = _has_pending_decision(conn, job_id)
    decision_requested = job["state"] == "waiting_decision"
    legal_wait = bool(
        frontier["running"]
        or frontier["waiting_human"]
        or pending_decision
        or decision_requested
    )
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
        "pending_decision": pending_decision,
        "decision_requested": decision_requested,
    }


def summarize_runtime_capabilities(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    limit: int = 20,
) -> dict[str, Any]:
    ensure_runtime_schema(conn)
    policy = build_runtime_capability_policy(conn, job_id)
    blocked_nodes: list[dict[str, Any]] = []
    pending_authorizations: list[dict[str, Any]] = []
    for row in conn.execute(
        """
        SELECT * FROM execution_nodes
         WHERE job_id = ?
           AND state IN ('ready', 'waiting_human', 'blocked', 'running')
         ORDER BY created_at, node_key
        """,
        (job_id,),
    ).fetchall():
        node = dict(row)
        metadata = _loads(node.get("metadata_json"))
        requested = _normalize_capability_list(metadata.get("requested_capabilities") or [])
        cap_policy = metadata.get("capability_policy")
        if requested and not isinstance(cap_policy, dict):
            cap_policy = evaluate_node_capability_policy(conn, job_id, node)
        if not isinstance(cap_policy, dict):
            continue
        status = str(cap_policy.get("status") or "")
        if status in {"denied", "lane_incapable", "requires_human"}:
            entry = {
                "node_key": node["node_key"],
                "node_id": node["id"],
                "state": node["state"],
                "status": status,
                "requested_capabilities": cap_policy.get("requested") or requested,
                "allowed": cap_policy.get("allowed") or [],
                "denied": cap_policy.get("denied") or [],
                "requires_human": cap_policy.get("requires_human") or [],
                "lane_incapable": cap_policy.get("lane_incapable") or [],
                "reason": cap_policy.get("reason"),
                "policy_revision": cap_policy.get("policy_revision"),
            }
            blocked_nodes.append(entry)
            if status == "requires_human":
                pending_authorizations.append(entry)
    rows = conn.execute(
        """
        SELECT id, event_type, payload_json, created_at
          FROM execution_events
         WHERE job_id = ?
           AND event_type IN (%s)
         ORDER BY id DESC
         LIMIT ?
        """ % ",".join("?" for _ in CAPABILITY_EVENT_TYPES),
        (job_id, *sorted(CAPABILITY_EVENT_TYPES), max(1, int(limit))),
    ).fetchall()
    recent = [
        {
            "id": int(row["id"]),
            "event_type": row["event_type"],
            "created_at": int(row["created_at"]),
            "payload": _loads(row["payload_json"]),
        }
        for row in rows
    ]
    return {
        "policy_revision": policy["policy_revision"],
        "allowed_by_default": policy["allow_by_default"],
        "require_human": policy["require_human"],
        "denied_by_default": policy["deny_by_default"],
        "policy_resolution_order": policy["policy_resolution_order"],
        "blocked_nodes": blocked_nodes,
        "pending_authorizations": pending_authorizations,
        "active_authorizations": policy["active_authorizations"],
        "recent_policy_events": recent,
    }


def _runtime_recovery_policy(policy: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    merged = dict(DEFAULT_RUNTIME_RECOVERY_POLICY)
    if policy:
        merged.update({key: value for key, value in policy.items() if value is not None})
    for key in ("infra_retry_limit", "receipt_recovery_limit", "run_stale_after_seconds"):
        try:
            merged[key] = max(0, int(merged.get(key) or 0))
        except (TypeError, ValueError):
            merged[key] = int(DEFAULT_RUNTIME_RECOVERY_POLICY[key])
    return merged


def _recovery_event_once(
    conn: sqlite3.Connection,
    job_id: str,
    event_type: str,
    key: str,
    payload: Optional[dict[str, Any]] = None,
    *,
    node_id: Optional[str] = None,
    task_id: Optional[str] = None,
    run_id: Optional[int] = None,
) -> tuple[Optional[int], bool]:
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
            return int(row["id"]), False
    event_id = _event(
        conn,
        job_id,
        event_type,
        payload_data,
        node_id=node_id,
        task_id=task_id,
        run_id=run_id,
    )
    return event_id, True


def _latest_materialization(conn: sqlite3.Connection, node_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM node_materializations WHERE node_id = ? ORDER BY attempt DESC, created_at DESC LIMIT 1",
        (node_id,),
    ).fetchone()
    return dict(row) if row else None


def _active_materialization(conn: sqlite3.Connection, node_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        """
        SELECT * FROM node_materializations
         WHERE node_id = ? AND status IN ('created', 'running')
         ORDER BY attempt DESC, created_at DESC LIMIT 1
        """,
        (node_id,),
    ).fetchone()
    return dict(row) if row else None


def _stable_fingerprint(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _canonical_workspace_path(path: Optional[str]) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path or ""))) if path else ""


def _workspace_revision(path: Optional[str]) -> str:
    workspace = _canonical_workspace_path(path)
    if not workspace or not os.path.isdir(workspace):
        return "missing"
    try:
        head = subprocess.run(
            ["git", "-C", workspace, "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
        if head.returncode == 0 and head.stdout.strip():
            status = subprocess.run(
                ["git", "-C", workspace, "status", "--porcelain=v1", "-z"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            dirty = hashlib.sha256(status.stdout or b"").hexdigest()
            return f"git:{head.stdout.strip()}:dirty:{dirty}"
    except (OSError, subprocess.SubprocessError):
        pass

    marker = hashlib.sha256()
    seen = 0
    for root, dirs, files in os.walk(workspace):
        dirs[:] = sorted(name for name in dirs if name != ".git")
        for name in sorted(files):
            full = os.path.join(root, name)
            try:
                stat = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, workspace)
            marker.update(f"{rel}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode("utf-8", errors="replace"))
            seen += 1
            if seen >= 500:
                return f"fs:{marker.hexdigest()}:truncated"
    return f"fs:{marker.hexdigest()}"


def _node_contract_fingerprint(node: dict[str, Any]) -> str:
    constraints = _loads(node.get("constraints_json"))
    return _stable_fingerprint(constraints.get("contract") or {})


def _node_capability_fingerprint(node: dict[str, Any]) -> str:
    metadata = _loads(node.get("metadata_json"))
    policy = metadata.get("capability_policy")
    return _stable_fingerprint(policy if isinstance(policy, dict) else {})


def _task_event_rows(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, run_id, kind, payload, created_at FROM task_events "
        "WHERE task_id = ? ORDER BY created_at, id",
        (task_id,),
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "run_id": int(row["run_id"]) if row["run_id"] is not None else None,
            "kind": row["kind"],
            "payload": _loads(row["payload"]),
            "created_at": int(row["created_at"]),
        }
        for row in rows
    ]


def _backend_session_event(events: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    for event in reversed(events):
        payload = event["payload"] if isinstance(event.get("payload"), dict) else {}
        session_id = payload.get("backend_session_id") or payload.get("thread_id")
        if session_id and event["kind"] in {
            "worker_backend_session_started",
            "worker_backend_session_resumed",
            "worker_codex_event",
        }:
            return {**event, "backend_session_id": str(session_id)}
    return None


def sync_runtime_backend_sessions(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    board: Optional[str] = None,
) -> dict[str, Any]:
    """Project backend worker session task events into runtime-owned facts."""

    del board  # Runtime and Kanban tables share the selected connection.
    ensure_runtime_schema(conn)
    summary = {
        "job_id": job_id,
        "discovered": [],
        "updated": [],
        "resumed": [],
        "resume_failed": [],
        "identity_conflicts": [],
    }
    rows = conn.execute(
        """
        SELECT m.*, n.node_key, n.constraints_json, n.metadata_json AS node_metadata_json,
               j.workspace_path
          FROM node_materializations m
          JOIN execution_nodes n ON n.id = m.node_id
          JOIN runtime_jobs j ON j.id = m.job_id
         WHERE m.job_id = ?
         ORDER BY m.attempt, m.created_at
        """,
        (job_id,),
    ).fetchall()
    for raw in rows:
        materialization = dict(raw)
        events = _task_event_rows(conn, materialization["task_id"])
        continuity = _loads(materialization.get("metadata_json")).get("execution_continuity") or {}
        session_event = _backend_session_event(events)
        referenced_session = None
        if continuity.get("backend_session_record_id"):
            referenced_session = conn.execute(
                "SELECT * FROM backend_worker_sessions WHERE id = ? AND node_id = ?",
                (continuity["backend_session_record_id"], materialization["node_id"]),
            ).fetchone()
        if session_event is None and referenced_session is not None:
            resume_failed_event = next(
                (
                    event
                    for event in reversed(events)
                    if event["kind"] == "worker_backend_session_resume_failed"
                ),
                None,
            )
            if resume_failed_event is not None:
                session_event = {
                    **resume_failed_event,
                    "backend_session_id": referenced_session["backend_session_key"],
                }
        if session_event is None:
            continue
        session_key = session_event["backend_session_id"]
        node = {
            "constraints_json": materialization.get("constraints_json"),
            "metadata_json": materialization.get("node_metadata_json"),
        }
        latest_progress = next(
            (event for event in reversed(events) if event["kind"] == "worker_progress"),
            None,
        )
        latest_heartbeat = next(
            (event for event in reversed(events) if event["kind"] == "worker_heartbeat"),
            None,
        )
        latest_codex = next(
            (event for event in reversed(events) if event["kind"] == "worker_codex_event"),
            None,
        )
        failure_event = next(
            (
                event
                for event in reversed(events)
                if event["kind"]
                in {
                    "worker_timed_out",
                    "worker_failed",
                    "crashed",
                    "timed_out",
                    "worker_backend_session_resume_failed",
                }
            ),
            None,
        )
        completed_event = next(
            (event for event in reversed(events) if event["kind"] == "worker_review_required"),
            None,
        )
        if failure_event and failure_event["kind"] == "worker_backend_session_resume_failed":
            status = "resume_failed"
        elif failure_event:
            status = "interrupted"
        elif materialization["status"] in RECOVERY_FAILURE_STATUSES:
            status = "interrupted"
        elif materialization["status"] in {"succeeded", "failed", "blocked", "waiting_human"} or completed_event:
            status = "completed"
        else:
            status = "active"
        checkpoint = {
            "materialization_id": materialization["id"],
            "attempt": int(materialization["attempt"]),
            "task_id": materialization["task_id"],
            "run_id": materialization.get("run_id"),
            "latest_progress": latest_progress["payload"] if latest_progress else None,
            "latest_codex_event_type": (
                latest_codex["payload"].get("event_type") if latest_codex else None
            ),
            "failure_event": failure_event["kind"] if failure_event else None,
        }
        now = _now()
        existing = conn.execute(
            "SELECT * FROM backend_worker_sessions WHERE backend_kind = 'codex_cli' AND backend_session_key = ?",
            (session_key,),
        ).fetchone()
        if existing is not None and existing["node_id"] != materialization["node_id"]:
            _, created = _recovery_event_once(
                conn,
                job_id,
                "worker_session_identity_conflict",
                f"worker-session:{existing['id']}:node-conflict:{materialization['node_id']}",
                {
                    "backend_session_record_id": existing["id"],
                    "existing_node_id": existing["node_id"],
                    "observed_node_id": materialization["node_id"],
                    "materialization_id": materialization["id"],
                },
                node_id=materialization["node_id"],
                task_id=materialization["task_id"],
                run_id=materialization.get("run_id"),
            )
            if created:
                summary["identity_conflicts"].append(existing["id"])
            continue
        stale_projection = bool(
            existing is not None
            and existing["latest_materialization_id"] != materialization["id"]
        )
        if stale_projection:
            status = existing["status"]
        preserve_interruption_revision = bool(
            existing is not None
            and existing["status"] in {"interrupted", "resume_failed"}
            and existing["latest_materialization_id"] == materialization["id"]
        )
        workspace_revision = (
            existing["workspace_revision"]
            if preserve_interruption_revision
            else _workspace_revision(materialization.get("workspace_path"))
            if status in {"interrupted", "resume_failed", "completed"}
            else (existing["workspace_revision"] if existing is not None else None)
        )
        if existing is None:
            session_record_id = _id("bws")
            conn.execute(
                """
                INSERT INTO backend_worker_sessions (
                    id, job_id, node_id, backend_kind, backend_session_key, status,
                    initial_materialization_id, latest_materialization_id, worker_lane,
                    workspace_path, workspace_revision, capability_fingerprint,
                    node_contract_fingerprint, checkpoint_json, resume_count,
                    last_heartbeat_at, created_at, updated_at, completed_at, metadata_json
                ) VALUES (?, ?, ?, 'codex_cli', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                """,
                (
                    session_record_id,
                    job_id,
                    materialization["node_id"],
                    session_key,
                    status,
                    materialization["id"],
                    materialization["id"],
                    materialization.get("worker_lane"),
                    _canonical_workspace_path(materialization.get("workspace_path")),
                    workspace_revision,
                    _node_capability_fingerprint(node),
                    _node_contract_fingerprint(node),
                    _json(checkpoint),
                    latest_heartbeat["created_at"] if latest_heartbeat else None,
                    session_event["created_at"],
                    now,
                    now if status == "completed" else None,
                    _json({"latest_task_event_id": session_event["id"]}),
                ),
            )
            summary["discovered"].append(session_record_id)
            _recovery_event_once(
                conn,
                job_id,
                "worker_session_discovered",
                f"worker-session:{session_record_id}:discovered",
                {
                    "backend_session_record_id": session_record_id,
                    "node_key": materialization["node_key"],
                    "materialization_id": materialization["id"],
                    "attempt": int(materialization["attempt"]),
                    "execution_mode": continuity.get("mode") or "fresh",
                },
                node_id=materialization["node_id"],
                task_id=materialization["task_id"],
                run_id=materialization.get("run_id"),
            )
        else:
            session_record_id = existing["id"]
            conn.execute(
                """
                UPDATE backend_worker_sessions
                   SET status = ?, latest_materialization_id = ?, worker_lane = ?,
                       workspace_revision = COALESCE(?, workspace_revision),
                       checkpoint_json = ?, last_heartbeat_at = COALESCE(?, last_heartbeat_at),
                       updated_at = ?, completed_at = CASE WHEN ? = 'completed' THEN COALESCE(completed_at, ?) ELSE completed_at END
                 WHERE id = ?
                """,
                (
                    status,
                    existing["latest_materialization_id"] if stale_projection else materialization["id"],
                    materialization.get("worker_lane"),
                    workspace_revision,
                    _json(checkpoint),
                    latest_heartbeat["created_at"] if latest_heartbeat else None,
                    now,
                    status,
                    now,
                    session_record_id,
                ),
            )
            summary["updated"].append(session_record_id)

        mat_metadata = _loads(materialization.get("metadata_json"))
        mat_continuity = mat_metadata.setdefault("execution_continuity", {})
        mat_continuity["backend_session_record_id"] = session_record_id
        mat_continuity["observed_session_id"] = session_key
        mat_continuity["session_status"] = status
        conn.execute(
            "UPDATE node_materializations SET metadata_json = ? WHERE id = ?",
            (_json(mat_metadata), materialization["id"]),
        )
        resumed_event = next(
            (event for event in events if event["kind"] == "worker_backend_session_resumed"),
            None,
        )
        if resumed_event:
            _, created = _recovery_event_once(
                conn,
                job_id,
                "worker_session_resumed",
                f"worker-session:{session_record_id}:resumed:{materialization['id']}",
                {
                    "backend_session_record_id": session_record_id,
                    "materialization_id": materialization["id"],
                    "attempt": int(materialization["attempt"]),
                },
                node_id=materialization["node_id"],
                task_id=materialization["task_id"],
                run_id=materialization.get("run_id"),
            )
            if created:
                summary["resumed"].append(session_record_id)
        if failure_event and failure_event["kind"] == "worker_backend_session_resume_failed":
            _, created = _recovery_event_once(
                conn,
                job_id,
                "worker_session_resume_failed",
                f"worker-session:{session_record_id}:resume-failed:{materialization['id']}",
                {
                    "backend_session_record_id": session_record_id,
                    "materialization_id": materialization["id"],
                    "attempt": int(materialization["attempt"]),
                    "reason": failure_event["payload"].get("reason"),
                },
                node_id=materialization["node_id"],
                task_id=materialization["task_id"],
                run_id=materialization.get("run_id"),
            )
            if created:
                summary["resume_failed"].append(session_record_id)
    return summary


def _latest_backend_worker_session(conn: sqlite3.Connection, node_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM backend_worker_sessions WHERE node_id = ? ORDER BY updated_at DESC, created_at DESC LIMIT 1",
        (node_id,),
    ).fetchone()
    return dict(row) if row else None


def _mark_backend_worker_session_interrupted(
    conn: sqlite3.Connection,
    node: dict[str, Any],
    materialization: Optional[dict[str, Any]],
    failure_type: str,
    *,
    now: int,
) -> None:
    session = _latest_backend_worker_session(conn, node["id"])
    if session is None or session["status"] in {"completed", "resume_failed"}:
        return
    job = _job(conn, node["job_id"])
    checkpoint = _loads(session.get("checkpoint_json"))
    checkpoint["failure_type"] = failure_type
    checkpoint["interrupted_materialization_id"] = materialization["id"] if materialization else None
    revision = _workspace_revision(job.get("workspace_path"))
    conn.execute(
        """
        UPDATE backend_worker_sessions
           SET status = 'interrupted', workspace_revision = ?, checkpoint_json = ?, updated_at = ?
         WHERE id = ?
        """,
        (revision, _json(checkpoint), now, session["id"]),
    )
    _recovery_event_once(
        conn,
        node["job_id"],
        "worker_session_interrupted",
        f"worker-session:{session['id']}:interrupted:{(materialization or {}).get('id')}:{failure_type}",
        {
            "backend_session_record_id": session["id"],
            "materialization_id": (materialization or {}).get("id"),
            "attempt": (materialization or {}).get("attempt"),
            "failure_type": failure_type,
            "workspace_revision": revision,
        },
        node_id=node["id"],
        task_id=(materialization or {}).get("task_id"),
        run_id=(materialization or {}).get("run_id"),
    )


def _plan_worker_execution_continuity(
    conn: sqlite3.Connection,
    job: dict[str, Any],
    node: dict[str, Any],
    *,
    assignee: Optional[str],
) -> dict[str, Any]:
    session = _latest_backend_worker_session(conn, node["id"])
    if session is None:
        return {"mode": "fresh", "eligibility": "no_prior_session", "context_reacquisition": False}

    reasons: list[str] = []
    current_workspace = _canonical_workspace_path(job.get("workspace_path"))
    current_revision = _workspace_revision(job.get("workspace_path"))
    if session["status"] != "interrupted":
        reasons.append(f"session_status_{session['status']}")
    if session["backend_kind"] != "codex_cli":
        reasons.append("backend_resume_unsupported")
    if _canonical_workspace_path(session.get("workspace_path")) != current_workspace:
        reasons.append("workspace_path_mismatch")
    if session.get("workspace_revision") != current_revision:
        reasons.append("workspace_revision_mismatch")
    if (session.get("worker_lane") or "") != (assignee or ""):
        reasons.append("worker_lane_mismatch")
    if session.get("capability_fingerprint") != _node_capability_fingerprint(node):
        reasons.append("capability_fingerprint_mismatch")
    if session.get("node_contract_fingerprint") != _node_contract_fingerprint(node):
        reasons.append("node_contract_fingerprint_mismatch")
    if int(session.get("resume_count") or 0) >= WORKER_SESSION_RESUME_LIMIT:
        reasons.append("resume_limit_exhausted")

    common = {
        "backend_session_record_id": session["id"],
        "resume_session_id": session["backend_session_key"],
        "resume_from_materialization_id": session["latest_materialization_id"],
        "workspace_revision": current_revision,
        "workspace_path": current_workspace,
        "worker_lane": assignee,
        "capability_fingerprint": _node_capability_fingerprint(node),
        "node_contract_fingerprint": _node_contract_fingerprint(node),
    }
    if not reasons:
        return {
            "mode": "resume",
            "eligibility": "accepted",
            "context_reacquisition": False,
            **common,
        }
    return {
        "mode": "fallback_fresh",
        "eligibility": "rejected",
        "rejection_reasons": reasons,
        "context_reacquisition": True,
        **common,
    }


def runtime_worker_continuity_for_task(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    """Return the bounded, read-only continuity decision for a worker task."""

    ensure_runtime_schema(conn)
    row = conn.execute(
        "SELECT metadata_json FROM node_materializations WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return {"mode": "fresh", "eligibility": "not_runtime_materialization"}
    continuity = _loads(row["metadata_json"]).get("execution_continuity")
    return dict(continuity) if isinstance(continuity, dict) else {"mode": "fresh", "eligibility": "legacy"}


def summarize_worker_execution_continuity(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    limit: int = 50,
) -> dict[str, Any]:
    ensure_runtime_schema(conn)
    sessions = _rows(
        conn,
        "SELECT * FROM backend_worker_sessions WHERE job_id = ? ORDER BY updated_at DESC LIMIT ?",
        (job_id, max(1, int(limit))),
    )
    materializations = _rows(
        conn,
        "SELECT id, node_id, attempt, task_id, status, metadata_json FROM node_materializations WHERE job_id = ? ORDER BY created_at, attempt",
        (job_id,),
    )
    modes: dict[str, int] = {}
    reacquisitions = 0
    for materialization in materializations:
        continuity = materialization.get("metadata") or {}
        continuity = continuity.get("execution_continuity") if isinstance(continuity, dict) else {}
        mode = str((continuity or {}).get("mode") or "legacy")
        modes[mode] = modes.get(mode, 0) + 1
        if (continuity or {}).get("context_reacquisition"):
            reacquisitions += 1
    recent_events = _rows(
        conn,
        "SELECT * FROM execution_events WHERE job_id = ? AND event_type IN (%s) ORDER BY id DESC LIMIT ?"
        % ",".join("?" for _ in WORKER_SESSION_EVENT_TYPES),
        (job_id, *sorted(WORKER_SESSION_EVENT_TYPES), max(1, int(limit))),
    )
    return {
        "sessions": sessions,
        "session_count": len(sessions),
        "materialization_modes": modes,
        "context_reacquisition_count": reacquisitions,
        "recent_events": recent_events,
    }


def _update_materialization_recovery_status(
    conn: sqlite3.Connection,
    materialization: Optional[dict[str, Any]],
    status: str,
    *,
    now: int,
    recovery_reason: str,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    if not materialization:
        return
    metadata = _loads(materialization.get("metadata_json"))
    recovery = metadata.setdefault("recovery", {})
    recovery.update(
        {
            "status": status,
            "recovery_reason": recovery_reason,
            "updated_at": now,
            **(payload or {}),
        }
    )
    conn.execute(
        """
        UPDATE node_materializations
           SET status = ?, completed_at = COALESCE(completed_at, ?),
               metadata_json = ?
         WHERE id = ?
        """,
        (status, now, _json(metadata), materialization["id"]),
    )


def _recovery_failure_count(conn: sqlite3.Connection, node_id: str) -> int:
    placeholders = ",".join("?" for _ in RECOVERY_FAILURE_STATUSES)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count
          FROM node_materializations
         WHERE node_id = ? AND status IN ({placeholders})
        """,
        (node_id, *sorted(RECOVERY_FAILURE_STATUSES)),
    ).fetchone()
    return int(row["count"] or 0)


def _is_codex_lane_evidence(evidence: Any) -> bool:
    lane = evidence.get("worker_lane") if isinstance(evidence, dict) else None
    return isinstance(lane, dict) and lane.get("kind") == "codex_cli"


def _structure_request_valid(structure_request: Any) -> bool:
    if not isinstance(structure_request, dict):
        return False
    if structure_request.get("required") is not True or not isinstance(structure_request.get("blocking"), bool):
        return False
    if structure_request.get("reason_type") not in DECOMPOSITION_REASON_TYPES:
        return False
    completed_scope = structure_request.get("completed_scope") or []
    if not isinstance(completed_scope, list) or any(not isinstance(value, str) or not value.strip() for value in completed_scope):
        return False
    gaps = structure_request.get("discovered_gaps") or []
    if not isinstance(gaps, list):
        return False
    for gap in gaps:
        if not isinstance(gap, dict) or not str(gap.get("description") or "").strip():
            return False
        refs = gap.get("evidence_refs") or []
        if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref.strip() for ref in refs):
            return False
    suggested = structure_request.get("suggested_nodes") or []
    return isinstance(suggested, list) and not any(
        not isinstance(item, dict) or not str(item.get("objective") or "").strip()
        for item in suggested
    )


def _runtime_receipt_from_evidence(evidence: Any, node: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    """Validate a Codex runtime receipt before allowing it into the ledger."""
    if not isinstance(evidence, dict):
        return None
    receipt = evidence.get("runtime_receipt")
    if not isinstance(receipt, dict) or receipt.get("schema") != "runtime_worker_receipt_v1":
        return None
    if not isinstance(receipt.get("summary"), str) or not receipt["summary"].strip():
        return None
    if not isinstance(receipt.get("verification"), dict) or not isinstance(receipt["verification"].get("passed"), bool):
        return None
    verdict = str(receipt.get("verdict") or "").strip().lower()
    if verdict not in {"pass", "passed", "success", "succeeded", "failed", "fail", "blocked", "human_required", "uncertain"}:
        return None
    result = dict(receipt)
    keys = ("claimed_goal_items", "partial_goal_items", "unmet_goal_items", "contradicted_goal_items")
    for key in keys:
        values = result.get(key, [])
        if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
            return None
        result[key] = [value.strip() for value in values]
    changed_files = result.get("changed_files", [])
    if not isinstance(changed_files, list) or any(not isinstance(value, str) or not value.strip() for value in changed_files):
        return None
    result["changed_files"] = [value.strip().replace("\\", "/") for value in changed_files]
    structure_request = result.get("structure_request")
    if structure_request is not None and not _structure_request_valid(structure_request):
        return None
    if node is not None:
        metadata = _loads(node.get("metadata_json"))
        allowed = {str(value) for value in metadata.get("goal_item_keys") or []}
        referenced = set().union(*(set(result[key]) for key in keys))
        if not referenced.issubset(allowed):
            return None
    result["worker_lane"] = evidence.get("worker_lane")
    result["worker_receipt"] = evidence.get("worker_receipt")
    return result


def _receipt_evidence_valid(evidence: Any, *, node: Optional[dict[str, Any]] = None) -> bool:
    if _is_codex_lane_evidence(evidence):
        return _runtime_receipt_from_evidence(evidence, node) is not None
    if not isinstance(evidence, dict) or not evidence:
        return False
    return any(
        evidence.get(key) is not None
        for key in (
            "verdict",
            "summary",
            "claimed_goal_items",
            "claimed_goal_item_keys",
            "partial_goal_items",
            "partial_goal_item_keys",
            "unmet_goal_items",
            "unmet_goal_item_keys",
            "verification",
            "artifacts",
            "artifact_refs",
        )
    )


def _schedule_recovery_retry_or_fail(
    conn: sqlite3.Connection,
    node: dict[str, Any],
    materialization: Optional[dict[str, Any]],
    failure_type: str,
    *,
    now: int,
    policy: dict[str, Any],
    summary: dict[str, Any],
    task_id: Optional[str] = None,
    run_id: Optional[int] = None,
) -> None:
    _mark_backend_worker_session_interrupted(
        conn,
        node,
        materialization,
        failure_type,
        now=now,
    )
    retryable_types = {str(item) for item in policy.get("retryable_failure_types") or []}
    retry_limit_key = "receipt_recovery_limit" if failure_type in {"receipt_missing", "receipt_invalid"} else "infra_retry_limit"
    retry_limit = int(policy.get(retry_limit_key) or 0)
    retryable = failure_type in retryable_types and _recovery_failure_count(conn, node["id"]) <= retry_limit
    payload = {
        "node_key": node["node_key"],
        "materialization_id": materialization["id"] if materialization else None,
        "attempt": int(materialization["attempt"]) if materialization else None,
        "task_id": task_id,
        "run_id": run_id,
        "recovery_reason": failure_type,
        "retryable": retryable,
        "policy_decision": "retry" if retryable else "mark_failed",
        "retry_limit": retry_limit,
    }
    if retryable:
        conn.execute(
            """
            UPDATE execution_nodes
               SET state = 'ready', latest_task_id = NULL, latest_run_id = NULL,
                   updated_at = ?, completed_at = NULL
             WHERE id = ? AND state = 'running'
            """,
            (now, node["id"]),
        )
        _recovery_event_once(
            conn,
            node["job_id"],
            "receipt_recovery_requested" if failure_type in {"receipt_missing", "receipt_invalid"} else "node_recovery_retry_scheduled",
            f"{failure_type}:{node['id']}:{payload['attempt']}:retry",
            payload,
            node_id=node["id"],
            task_id=task_id,
            run_id=run_id,
        )
        if failure_type in {"receipt_missing", "receipt_invalid"}:
            _recovery_event_once(
                conn,
                node["job_id"],
                "node_recovery_retry_scheduled",
                f"{failure_type}:{node['id']}:{payload['attempt']}:retry-scheduled",
                payload,
                node_id=node["id"],
                task_id=task_id,
                run_id=run_id,
            )
        summary["scheduled_retries"].append(node["node_key"])
    else:
        conn.execute(
            """
            UPDATE execution_nodes
               SET state = 'failed', output_summary = ?, updated_at = ?, completed_at = ?
             WHERE id = ? AND state = 'running'
            """,
            (f"Runtime recovery marked node failed: {failure_type}", now, now, node["id"]),
        )
        _recovery_event_once(
            conn,
            node["job_id"],
            "node_recovery_not_retryable",
            f"{failure_type}:{node['id']}:{payload['attempt']}:not-retryable",
            payload,
            node_id=node["id"],
            task_id=task_id,
            run_id=run_id,
        )
        summary["failed_nodes"].append(node["node_key"])


def _run_failure_type(snapshot: kb.TaskProgressSnapshot, *, now: int, policy: dict[str, Any]) -> Optional[str]:
    task = snapshot.task
    run = snapshot.run
    run_status = str(run.status if run else "").strip().lower()
    outcome = str(run.outcome if run and run.outcome else "").strip().lower()
    evidence = snapshot.evidence if isinstance(snapshot.evidence, dict) else {}
    worker_lane = evidence.get("worker_lane") if isinstance(evidence.get("worker_lane"), dict) else {}
    if worker_lane.get("timed_out") is True:
        return "worker_run_timeout"
    if worker_lane.get("binary_missing") is True:
        return "worker_run_crashed"
    if run_status == "timed_out" or outcome == "timed_out":
        return "worker_run_timeout"
    if run_status == "crashed" or outcome == "crashed":
        return "worker_run_crashed"
    if run_status in {"failed", "released"} or outcome in {"spawn_failed", "gave_up"}:
        return "worker_run_crashed"
    task_claim_expires = task.claim_expires
    run_claim_expires = run.claim_expires if run else None
    claim_expires = run_claim_expires if run_claim_expires is not None else task_claim_expires
    if task.status == "running" and claim_expires is not None and int(claim_expires) < now:
        return "worker_run_timeout"
    last_heartbeat = None
    if run and run.last_heartbeat_at is not None:
        last_heartbeat = int(run.last_heartbeat_at)
    elif task.last_heartbeat_at is not None:
        last_heartbeat = int(task.last_heartbeat_at)
    stale_after = int(policy.get("run_stale_after_seconds") or 0)
    if task.status == "running" and stale_after > 0 and last_heartbeat is not None and now - last_heartbeat > stale_after:
        return "worker_run_stale"
    active_started = run.started_at if run else task.started_at
    if task.status == "running" and stale_after > 0 and last_heartbeat is None and active_started is not None and now - int(active_started) > stale_after:
        return "worker_run_stale"
    if task.status == "ready" and run and outcome == "reclaimed":
        return "worker_run_stale"
    return None


def _recovery_status_for_failure(failure_type: str) -> str:
    return {
        "materialization_lost": "lost",
        "worker_run_stale": "stale",
        "worker_run_timeout": "timed_out",
        "worker_run_crashed": "crashed",
        "receipt_missing": "receipt_missing",
        "receipt_invalid": "receipt_invalid",
    }.get(failure_type, "failed")


def reconcile_runtime_materializations(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    board: Optional[str] = None,
    now: Optional[int] = None,
    policy: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Normalize worker/materialization mismatches into runtime facts."""

    ensure_runtime_schema(conn)
    current = int(now if now is not None else _now())
    effective_policy = _runtime_recovery_policy(policy)
    worker_sessions = sync_runtime_backend_sessions(conn, job_id, board=board)
    summary: dict[str, Any] = {
        "job_id": job_id,
        "checked_nodes": 0,
        "events": [],
        "scheduled_retries": [],
        "failed_nodes": [],
        "materializations_updated": [],
        "worker_sessions": worker_sessions,
        "policy": {
            "infra_retry_limit": effective_policy["infra_retry_limit"],
            "receipt_recovery_limit": effective_policy["receipt_recovery_limit"],
            "run_stale_after_seconds": effective_policy["run_stale_after_seconds"],
        },
    }
    nodes = conn.execute(
        """
        SELECT * FROM execution_nodes
         WHERE job_id = ? AND state = 'running'
         ORDER BY updated_at, created_at, node_key
        """,
        (job_id,),
    ).fetchall()
    for row in nodes:
        node = dict(row)
        summary["checked_nodes"] += 1
        materialization = _active_materialization(conn, node["id"]) or _latest_materialization(conn, node["id"])
        task_id = node.get("latest_task_id") or (materialization or {}).get("task_id")
        run_id = node.get("latest_run_id") or (materialization or {}).get("run_id")
        if not task_id:
            failure_type = "materialization_lost"
            _update_materialization_recovery_status(
                conn,
                materialization,
                _recovery_status_for_failure(failure_type),
                now=current,
                recovery_reason=failure_type,
                payload={"task_id": None, "run_id": run_id},
            )
            event_id, created = _recovery_event_once(
                conn,
                job_id,
                failure_type,
                f"{failure_type}:{node['id']}:{(materialization or {}).get('attempt')}:missing-task-id",
                {
                    "node_key": node["node_key"],
                    "materialization_id": (materialization or {}).get("id"),
                    "attempt": (materialization or {}).get("attempt"),
                    "task_id": None,
                    "run_id": run_id,
                    "recovery_reason": failure_type,
                    "retryable": True,
                    "policy_decision": "evaluate_retry",
                },
                node_id=node["id"],
                run_id=run_id,
            )
            if created:
                summary["events"].append(failure_type)
            _schedule_recovery_retry_or_fail(
                conn,
                node,
                materialization,
                failure_type,
                now=current,
                policy=effective_policy,
                summary=summary,
                task_id=None,
                run_id=run_id,
            )
            continue
        snapshot = kb.task_progress_snapshot(conn, str(task_id), board=board)
        if snapshot is None:
            failure_type = "materialization_lost"
            _update_materialization_recovery_status(
                conn,
                materialization,
                _recovery_status_for_failure(failure_type),
                now=current,
                recovery_reason=failure_type,
                payload={"task_id": task_id, "run_id": run_id},
            )
            if materialization:
                summary["materializations_updated"].append(materialization["id"])
            _, created = _recovery_event_once(
                conn,
                job_id,
                failure_type,
                f"{failure_type}:{node['id']}:{(materialization or {}).get('attempt')}:{task_id}",
                {
                    "node_key": node["node_key"],
                    "materialization_id": (materialization or {}).get("id"),
                    "attempt": (materialization or {}).get("attempt"),
                    "task_id": task_id,
                    "run_id": run_id,
                    "recovery_reason": failure_type,
                    "retryable": True,
                    "policy_decision": "evaluate_retry",
                },
                node_id=node["id"],
                task_id=str(task_id),
                run_id=run_id,
            )
            if created:
                summary["events"].append(failure_type)
            _schedule_recovery_retry_or_fail(
                conn,
                node,
                materialization,
                failure_type,
                now=current,
                policy=effective_policy,
                summary=summary,
                task_id=str(task_id),
                run_id=run_id,
            )
            continue
        snapshot_run_id = snapshot.run.id if snapshot.run else run_id
        failure_type = _run_failure_type(snapshot, now=current, policy=effective_policy)
        if failure_type is not None:
            _update_materialization_recovery_status(
                conn,
                materialization,
                _recovery_status_for_failure(failure_type),
                now=current,
                recovery_reason=failure_type,
                payload={"task_id": task_id, "run_id": snapshot_run_id},
            )
            if materialization:
                summary["materializations_updated"].append(materialization["id"])
            event_type = failure_type
            _, created = _recovery_event_once(
                conn,
                job_id,
                event_type,
                f"{event_type}:{node['id']}:{(materialization or {}).get('attempt')}:{task_id}:{snapshot_run_id}",
                {
                    "node_key": node["node_key"],
                    "materialization_id": (materialization or {}).get("id"),
                    "attempt": (materialization or {}).get("attempt"),
                    "task_id": task_id,
                    "run_id": snapshot_run_id,
                    "recovery_reason": failure_type,
                    "retryable": True,
                    "policy_decision": "evaluate_retry",
                    "task_status": snapshot.task.status,
                    "run_status": snapshot.run.status if snapshot.run else None,
                    "run_outcome": snapshot.run.outcome if snapshot.run else None,
                },
                node_id=node["id"],
                task_id=str(task_id),
                run_id=snapshot_run_id,
            )
            if created:
                summary["events"].append(event_type)
            _schedule_recovery_retry_or_fail(
                conn,
                node,
                materialization,
                failure_type,
                now=current,
                policy=effective_policy,
                summary=summary,
                task_id=str(task_id),
                run_id=snapshot_run_id,
            )
            continue
        if snapshot.task.status in {"done", "blocked"} and not _receipt_evidence_valid(snapshot.evidence, node=node):
            failure_type = "receipt_missing" if not snapshot.evidence else "receipt_invalid"
            _update_materialization_recovery_status(
                conn,
                materialization,
                _recovery_status_for_failure(failure_type),
                now=current,
                recovery_reason=failure_type,
                payload={"task_id": task_id, "run_id": snapshot_run_id},
            )
            if materialization:
                summary["materializations_updated"].append(materialization["id"])
            _, created = _recovery_event_once(
                conn,
                job_id,
                failure_type,
                f"{failure_type}:{node['id']}:{(materialization or {}).get('attempt')}:{task_id}:{snapshot_run_id}",
                {
                    "node_key": node["node_key"],
                    "materialization_id": (materialization or {}).get("id"),
                    "attempt": (materialization or {}).get("attempt"),
                    "task_id": task_id,
                    "run_id": snapshot_run_id,
                    "recovery_reason": failure_type,
                    "retryable": True,
                    "policy_decision": "evaluate_retry",
                    "task_status": snapshot.task.status,
                },
                node_id=node["id"],
                task_id=str(task_id),
                run_id=snapshot_run_id,
            )
            if created:
                summary["events"].append(failure_type)
            _schedule_recovery_retry_or_fail(
                conn,
                node,
                materialization,
                failure_type,
                now=current,
                policy=effective_policy,
                summary=summary,
                task_id=str(task_id),
                run_id=snapshot_run_id,
            )
            continue
        if materialization and snapshot.task.status in {"done", "blocked"}:
            conn.execute(
                """
                UPDATE node_materializations
                   SET run_id = COALESCE(?, run_id)
                 WHERE id = ?
                """,
                (snapshot_run_id, materialization["id"]),
            )
    if summary["events"] or summary["scheduled_retries"] or summary["failed_nodes"]:
        _recovery_event_once(
            conn,
            job_id,
            "materialization_reconciled",
            f"reconcile:{current}",
            {
                "checked_nodes": summary["checked_nodes"],
                "event_types": summary["events"],
                "scheduled_retries": summary["scheduled_retries"],
                "failed_nodes": summary["failed_nodes"],
                "recovery_reason": "reconcile_runtime_materializations",
                "retryable": bool(summary["scheduled_retries"]),
                "policy_decision": "reconciled",
            },
        )
    reduce_runtime_job(conn, job_id)
    return summary


def runtime_legal_waiting_reason(conn: sqlite3.Connection, job_id: str) -> str:
    ensure_runtime_schema(conn)
    job = _job(conn, job_id)
    if job["state"] in {"done", "cancelled", "failed"}:
        return str(job["state"])
    frontier = summarize_active_frontier(conn, job_id)
    if frontier["running"]:
        return "waiting_worker"
    capability_summary = summarize_runtime_capabilities(conn, job_id, limit=5)
    if capability_summary["pending_authorizations"]:
        return "waiting_capability_authorization"
    if any(item.get("status") in {"denied", "lane_incapable"} for item in capability_summary["blocked_nodes"]):
        return "blocked_by_policy"
    if frontier["waiting_human"]:
        return "waiting_human"
    if _has_pending_decision(conn, job_id) or job["state"] == "waiting_decision":
        return "waiting_decision"
    if frontier["ready"]:
        return "ready_to_materialize"
    if job["state"] == "blocked":
        return "blocked_by_policy"
    liveness = summarize_liveness(conn, job_id, frontier)
    if liveness["illegal_idle"]:
        return "liveness_violation"
    open_gaps = conn.execute(
        "SELECT 1 FROM goal_gaps WHERE job_id = ? AND state = 'open' LIMIT 1",
        (job_id,),
    ).fetchone()
    if open_gaps:
        return "waiting_decision"
    return "active"


def summarize_runtime_recovery(conn: sqlite3.Connection, job_id: str, *, limit: int = 20) -> dict[str, Any]:
    ensure_runtime_schema(conn)
    rows = conn.execute(
        """
        SELECT id, event_type, payload_json, created_at
          FROM execution_events
         WHERE job_id = ?
           AND event_type IN (%s)
         ORDER BY id DESC
         LIMIT ?
        """ % ",".join("?" for _ in RECOVERY_EVENT_TYPES),
        (job_id, *sorted(RECOVERY_EVENT_TYPES), max(1, int(limit))),
    ).fetchall()
    events = []
    retryable_count = 0
    non_retryable_count = 0
    latest_reconcile_at = None
    for row in rows:
        payload = _loads(row["payload_json"])
        if payload.get("retryable") is True:
            retryable_count += 1
        elif payload.get("retryable") is False:
            non_retryable_count += 1
        if row["event_type"] == "materialization_reconciled" and latest_reconcile_at is None:
            latest_reconcile_at = int(row["created_at"])
        events.append(
            {
                "id": int(row["id"]),
                "event_type": row["event_type"],
                "created_at": int(row["created_at"]),
                "payload": payload,
            }
        )
    return {
        "open_recovery_events": events,
        "retryable_count": retryable_count,
        "non_retryable_count": non_retryable_count,
        "latest_reconcile_at": latest_reconcile_at,
    }


def _checkpoint_source_refs(payload: Any) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            source_refs = value.get("source_refs")
            if isinstance(source_refs, list):
                refs.extend(ref for ref in source_refs if isinstance(ref, dict))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return refs


def check_runtime_consistency(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    write_events: bool = True,
) -> dict[str, Any]:
    """Run a deterministic consistency check over runtime DB facts."""

    ensure_runtime_schema(conn)
    _job(conn, job_id)
    violations: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    for node in conn.execute("SELECT * FROM execution_nodes WHERE job_id = ?", (job_id,)).fetchall():
        metadata = _loads(node["metadata_json"])
        if not (
            metadata.get("goal_item_keys")
            or metadata.get("gap_keys")
            or metadata.get("human_gate_reason")
            or node["node_type"] == "human_gate"
        ):
            violations.append({"type": "node_without_goal_gap_or_human_linkage", "node_key": node["node_key"]})
        if node["latest_task_id"]:
            materialization = conn.execute(
                """
                SELECT * FROM node_materializations
                 WHERE node_id = ? AND task_id = ?
                 ORDER BY attempt DESC LIMIT 1
                """,
                (node["id"], node["latest_task_id"]),
            ).fetchone()
            if materialization is None:
                violations.append(
                    {
                        "type": "latest_task_without_materialization",
                        "node_key": node["node_key"],
                        "task_id": node["latest_task_id"],
                    }
                )
        if node["state"] in TERMINAL_NODE_STATES:
            active = _active_materialization(conn, node["id"])
            if active is not None:
                warnings.append(
                    {
                        "type": "terminal_node_has_active_materialization",
                        "node_key": node["node_key"],
                        "materialization_id": active["id"],
                    }
                )
        active_materializations = conn.execute(
            """
            SELECT * FROM node_materializations
             WHERE node_id = ? AND status IN ('created', 'running')
             ORDER BY attempt
            """,
            (node["id"],),
        ).fetchall()
        if node["state"] == "ready" and active_materializations:
            violations.append(
                {
                    "type": "ready_node_has_active_materialization",
                    "node_key": node["node_key"],
                    "materialization_ids": [row["id"] for row in active_materializations],
                }
            )
        if node["state"] == "running" and not active_materializations:
            violations.append({"type": "running_node_without_active_materialization", "node_key": node["node_key"]})
        if len(active_materializations) > 1:
            violations.append(
                {
                    "type": "duplicate_active_materialization",
                    "node_key": node["node_key"],
                    "materialization_ids": [row["id"] for row in active_materializations],
                }
            )
        cap_policy = metadata.get("capability_policy") if isinstance(metadata, dict) else None
        if isinstance(cap_policy, dict) and cap_policy.get("status") in {"denied", "lane_incapable", "requires_human"}:
            if active_materializations:
                violations.append(
                    {
                        "type": "capability_blocked_node_materialized",
                        "node_key": node["node_key"],
                        "capability_status": cap_policy.get("status"),
                        "materialization_ids": [row["id"] for row in active_materializations],
                    }
                )
    for mat in conn.execute("SELECT * FROM node_materializations WHERE job_id = ?", (job_id,)).fetchall():
        node = conn.execute("SELECT id, node_key FROM execution_nodes WHERE id = ?", (mat["node_id"],)).fetchone()
        if node is None:
            violations.append({"type": "materialization_node_missing", "materialization_id": mat["id"], "node_id": mat["node_id"]})
        if kb.get_task(conn, mat["task_id"]) is None:
            violations.append({"type": "materialization_task_missing", "materialization_id": mat["id"], "task_id": mat["task_id"]})
        continuity = _loads(mat["metadata_json"]).get("execution_continuity") or {}
        mode = continuity.get("mode")
        if mode == "resume":
            session_id = continuity.get("backend_session_record_id")
            session = conn.execute(
                "SELECT * FROM backend_worker_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                violations.append(
                    {
                        "type": "resume_materialization_session_missing",
                        "materialization_id": mat["id"],
                        "backend_session_record_id": session_id,
                    }
                )
            else:
                fingerprint_fields = {
                    "workspace_path": session["workspace_path"],
                    "worker_lane": session["worker_lane"],
                    "capability_fingerprint": session["capability_fingerprint"],
                    "node_contract_fingerprint": session["node_contract_fingerprint"],
                }
                mismatched = sorted(
                    field
                    for field, expected in fingerprint_fields.items()
                    if continuity.get(field) != expected
                )
                if mismatched:
                    violations.append(
                        {
                            "type": "resume_materialization_fingerprint_mismatch",
                            "materialization_id": mat["id"],
                            "backend_session_record_id": session_id,
                            "fields": mismatched,
                        }
                    )
            prior_id = continuity.get("resume_from_materialization_id")
            if not prior_id or conn.execute(
                "SELECT 1 FROM node_materializations WHERE id = ? AND node_id = ?",
                (prior_id, mat["node_id"]),
            ).fetchone() is None:
                violations.append(
                    {
                        "type": "resume_materialization_prior_attempt_missing",
                        "materialization_id": mat["id"],
                        "resume_from_materialization_id": prior_id,
                    }
                )
            if continuity.get("context_reacquisition") is True:
                violations.append(
                    {
                        "type": "resume_materialization_reacquires_context",
                        "materialization_id": mat["id"],
                    }
                )
        elif continuity.get("context_reacquisition") is False and mode not in {"fresh", None}:
            violations.append(
                {
                    "type": "non_resume_materialization_without_reacquisition",
                    "materialization_id": mat["id"],
                    "mode": mode,
                }
            )
    for session in conn.execute("SELECT * FROM backend_worker_sessions WHERE job_id = ?", (job_id,)).fetchall():
        node = conn.execute("SELECT * FROM execution_nodes WHERE id = ?", (session["node_id"],)).fetchone()
        if node is None:
            violations.append(
                {
                    "type": "backend_session_node_missing",
                    "backend_session_record_id": session["id"],
                    "node_id": session["node_id"],
                }
            )
            continue
        for field in ("initial_materialization_id", "latest_materialization_id"):
            if conn.execute(
                "SELECT 1 FROM node_materializations WHERE id = ? AND node_id = ?",
                (session[field], session["node_id"]),
            ).fetchone() is None:
                violations.append(
                    {
                        "type": "backend_session_materialization_missing",
                        "backend_session_record_id": session["id"],
                        "field": field,
                        "materialization_id": session[field],
                    }
                )
        if node["state"] in TERMINAL_NODE_STATES and session["status"] in {
            "active",
            "resume_pending",
            "resuming",
        }:
            violations.append(
                {
                    "type": "terminal_node_has_active_backend_session",
                    "node_key": node["node_key"],
                    "backend_session_record_id": session["id"],
                    "session_status": session["status"],
                }
            )
    for conflict in conn.execute(
        "SELECT id, payload_json FROM execution_events WHERE job_id = ? AND event_type = 'worker_session_identity_conflict'",
        (job_id,),
    ).fetchall():
        payload = _loads(conflict["payload_json"])
        violations.append(
            {
                "type": "backend_session_identity_conflict",
                "event_id": int(conflict["id"]),
                "backend_session_record_id": payload.get("backend_session_record_id"),
                "existing_node_id": payload.get("existing_node_id"),
                "observed_node_id": payload.get("observed_node_id"),
            }
        )
    for row in conn.execute("SELECT * FROM progress_ledger WHERE job_id = ?", (job_id,)).fetchall():
        if row["node_id"] and conn.execute("SELECT 1 FROM execution_nodes WHERE id = ?", (row["node_id"],)).fetchone() is None:
            violations.append({"type": "ledger_node_missing", "ledger_id": row["id"], "node_id": row["node_id"]})
        if conn.execute("SELECT 1 FROM goal_items WHERE id = ?", (row["goal_item_id"],)).fetchone() is None:
            violations.append({"type": "ledger_goal_item_missing", "ledger_id": row["id"], "goal_item_id": row["goal_item_id"]})
        if row["artifact_id"] and conn.execute("SELECT 1 FROM node_artifacts WHERE id = ?", (row["artifact_id"],)).fetchone() is None:
            violations.append({"type": "ledger_artifact_missing", "ledger_id": row["id"], "artifact_id": row["artifact_id"]})
    for checkpoint in conn.execute("SELECT * FROM decision_checkpoints WHERE job_id = ?", (job_id,)).fetchall():
        if checkpoint["source_segment_id"] and conn.execute(
            "SELECT 1 FROM decision_session_segments WHERE id = ?",
            (checkpoint["source_segment_id"],),
        ).fetchone() is None:
            violations.append(
                {
                    "type": "checkpoint_source_segment_missing",
                    "checkpoint_id": checkpoint["id"],
                    "source_segment_id": checkpoint["source_segment_id"],
                }
            )
        if checkpoint["graph_revision"] is not None and int(checkpoint["graph_revision"]) > int(_job(conn, job_id)["graph_revision"]):
            violations.append(
                {
                    "type": "checkpoint_future_graph_revision",
                    "checkpoint_id": checkpoint["id"],
                    "checkpoint_graph_revision": int(checkpoint["graph_revision"]),
                }
            )
        payload = _loads(checkpoint["payload_json"])
        for ref in _checkpoint_source_refs(payload):
            if ref.get("node_key") and conn.execute(
                "SELECT 1 FROM execution_nodes WHERE job_id = ? AND node_key = ?",
                (job_id, ref["node_key"]),
            ).fetchone() is None:
                violations.append({"type": "checkpoint_node_missing", "checkpoint_id": checkpoint["id"], "node_key": ref["node_key"]})
            if ref.get("goal_item_key"):
                contract = _contract(conn, job_id)
                if conn.execute(
                    "SELECT 1 FROM goal_items WHERE contract_id = ? AND item_key = ?",
                    (contract["id"], ref["goal_item_key"]),
                ).fetchone() is None:
                    violations.append({"type": "checkpoint_goal_item_missing", "checkpoint_id": checkpoint["id"], "goal_item_key": ref["goal_item_key"]})
            if ref.get("event_id") and conn.execute(
                "SELECT 1 FROM execution_events WHERE job_id = ? AND id = ?",
                (job_id, int(ref["event_id"])),
            ).fetchone() is None:
                violations.append({"type": "checkpoint_event_missing", "checkpoint_id": checkpoint["id"], "event_id": ref["event_id"]})
            if ref.get("decision_id") and conn.execute(
                "SELECT 1 FROM kernel_decisions WHERE job_id = ? AND id = ?",
                (job_id, ref["decision_id"]),
            ).fetchone() is None:
                violations.append({"type": "checkpoint_decision_missing", "checkpoint_id": checkpoint["id"], "decision_id": ref["decision_id"]})
            if ref.get("patch_id") and conn.execute(
                "SELECT 1 FROM graph_patches WHERE job_id = ? AND id = ?",
                (job_id, ref["patch_id"]),
            ).fetchone() is None:
                violations.append({"type": "checkpoint_patch_missing", "checkpoint_id": checkpoint["id"], "patch_id": ref["patch_id"]})
            if ref.get("artifact_ref") and conn.execute(
                "SELECT 1 FROM node_artifacts WHERE job_id = ? AND path_or_ref = ?",
                (job_id, ref["artifact_ref"]),
            ).fetchone() is None:
                violations.append({"type": "checkpoint_artifact_missing", "checkpoint_id": checkpoint["id"], "artifact_ref": ref["artifact_ref"]})
        checkpoint_text = json.dumps(
            {
                "payload": _loads(checkpoint["payload_json"]),
                "checkpoint": _loads(checkpoint["checkpoint_json"]),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        if "selected_hints" in checkpoint_text or "non_authoritative_notice" in checkpoint_text:
            violations.append({"type": "memory_hint_leaked_into_checkpoint", "checkpoint_id": checkpoint["id"]})
    active_segments = conn.execute(
        "SELECT COUNT(*) AS count FROM decision_session_segments WHERE job_id = ? AND state = 'active'",
        (job_id,),
    ).fetchone()
    if int(active_segments["count"] or 0) != 1:
        violations.append({"type": "active_segment_count_invalid", "count": int(active_segments["count"] or 0)})
    for entry in conn.execute(
        """
        SELECT * FROM decision_segment_entries
         WHERE job_id = ? AND entry_type IN ('memory_hint_used', 'memory_hint_outcome_recorded')
        """,
        (job_id,),
    ).fetchall():
        payload = _loads(entry["payload_json"])
        if entry["decision_id"] and conn.execute(
            "SELECT 1 FROM kernel_decisions WHERE job_id = ? AND id = ?",
            (job_id, entry["decision_id"]),
        ).fetchone() is None:
            violations.append({"type": "memory_usage_decision_missing", "entry_id": entry["id"], "decision_id": entry["decision_id"]})
        if entry["entry_type"] == "memory_hint_used" and not payload.get("provider_request_ref"):
            warnings.append({"type": "memory_usage_missing_provider_request_ref", "entry_id": entry["id"]})
        for hint in payload.get("hints") or []:
            if not isinstance(hint, dict):
                violations.append({"type": "memory_usage_hint_invalid", "entry_id": entry["id"]})
                continue
            if hint.get("status") != "accepted":
                violations.append({"type": "memory_usage_non_accepted_hint", "entry_id": entry["id"], "entry_id_ref": hint.get("entry_id")})
            if hint.get("non_authoritative") is not True:
                violations.append({"type": "memory_usage_hint_not_non_authoritative", "entry_id": entry["id"], "entry_id_ref": hint.get("entry_id")})
    job = _job(conn, job_id)
    if job.get("advance_lock") and job.get("claim_expires_at") and int(job["claim_expires_at"]) <= _now():
        warnings.append({"type": "expired_supervisor_lease", "owner": job["advance_lock"], "claim_expires_at": int(job["claim_expires_at"])})
    if job["state"] == "done":
        contract = _contract(conn, job_id)
        incomplete = conn.execute(
            """
            SELECT item_key, state FROM goal_items
             WHERE contract_id = ? AND required = 1 AND state NOT IN ('satisfied', 'waived')
             ORDER BY item_key
            """,
            (contract["id"],),
        ).fetchall()
        for item in incomplete:
            violations.append({"type": "done_job_has_incomplete_goal", "goal_item_key": item["item_key"], "state": item["state"]})
    liveness = summarize_liveness(conn, job_id)
    if liveness["illegal_idle"]:
        warnings.append({"type": "illegal_idle", "liveness": liveness})

    result = {
        "job_id": job_id,
        "status": "failed" if violations else "passed",
        "violation_count": len(violations),
        "warning_count": len(warnings),
        "violations": violations,
        "warnings": warnings,
    }
    if write_events:
        event_type = "consistency_violation" if violations else "consistency_check_passed"
        _event(
            conn,
            job_id,
            event_type,
            {
                "status": result["status"],
                "violation_count": len(violations),
                "warning_count": len(warnings),
                "violations": violations[:20],
                "warnings": warnings[:20],
                "recovery_reason": "check_runtime_consistency",
                "retryable": False,
                "policy_decision": "operator_review" if violations else "none",
            },
        )
        for violation in violations:
            if str(violation.get("type") or "").startswith("ledger_"):
                _recovery_event_once(
                    conn,
                    job_id,
                    "ledger_reference_missing",
                    f"ledger:{violation}",
                    {
                        "recovery_reason": violation["type"],
                        "retryable": False,
                        "policy_decision": "operator_review",
                        "violation": violation,
                    },
                )
            elif str(violation.get("type") or "").startswith("checkpoint_"):
                _recovery_event_once(
                    conn,
                    job_id,
                    "checkpoint_reference_missing",
                    f"checkpoint:{violation}",
                    {
                        "recovery_reason": violation["type"],
                        "retryable": False,
                        "policy_decision": "operator_review",
                        "violation": violation,
                    },
                )
    return result


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
    complete = _completion_satisfied(conn, job_id)
    capability_summary = summarize_runtime_capabilities(conn, job_id, limit=5)
    has_pending_capability_authorization = bool(capability_summary["pending_authorizations"])
    has_policy_block = any(
        item.get("status") in {"denied", "lane_incapable"}
        for item in capability_summary["blocked_nodes"]
    )
    if complete:
        state = "done"
    elif has_human:
        state = "waiting_human"
    elif has_running:
        state = "waiting_worker"
    elif has_ready:
        state = "active"
    elif has_pending_capability_authorization:
        state = "waiting_human"
    elif has_policy_block:
        state = "blocked"
    elif gaps:
        state = "waiting_decision"
        _event_once(
            conn,
            job_id,
            "decision_requested",
            "open_goal_gaps",
            {"gap_count": len(gaps), "gap_keys": [gap["gap_key"] for gap in gaps]},
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
        if item["state"] not in {"satisfied", "waived"}:
            return False
    running = conn.execute(
        "SELECT 1 FROM execution_nodes WHERE job_id = ? AND state IN ('running', 'waiting_human') LIMIT 1",
        (job_id,),
    ).fetchone()
    return running is None


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
            "SELECT * FROM progress_ledger WHERE goal_item_id = ? ORDER BY created_at DESC, rowid DESC",
            (item["id"],),
        ).fetchall()
        latest_ledger = ledger[0] if ledger else None
        gap_type: Optional[str] = None
        if item["state"] in {"satisfied", "waived"}:
            continue
        linked_nodes = _nodes_linked_to_goal_item(conn, job_id, item["item_key"])
        has_human_gate = any(node["state"] == "waiting_human" for node in linked_nodes)
        has_open_path = any(node["state"] in OPEN_NODE_STATES for node in linked_nodes)
        failed_required = any(node["state"] in {"failed", "blocked"} for node in linked_nodes)
        if has_human_gate:
            gap_type = "blocked_by_human_gate"
        elif latest_ledger and latest_ledger["satisfaction"] == "contradicted":
            gap_type = "contradicted_evidence"
        elif latest_ledger and latest_ledger["verification_state"] in {"failed", "failed_verification"}:
            gap_type = "verification_failed"
        elif failed_required and not has_open_path:
            gap_type = "failed_required_node"
        elif not ledger:
            gap_type = "missing_evidence"
        elif latest_ledger and latest_ledger["satisfaction"] == "partial":
            gap_type = "partial_evidence"
        elif latest_ledger and latest_ledger["satisfaction"] == "full" and latest_ledger[
            "verification_state"
        ] in {"unverified", "self_reported"}:
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
    structure_requests = [
        {
            "event_id": row["id"],
            "node_key": (_loads(row["payload_json"]).get("node_key")),
            "structure_request": (_loads(row["payload_json"]).get("structure_request")),
        }
        for row in conn.execute(
            "SELECT id, payload_json FROM execution_events WHERE job_id = ? AND event_type = 'worker_structure_requested' ORDER BY id DESC LIMIT 10",
            (job_id,),
        ).fetchall()
    ]
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
        "structure_requests": structure_requests,
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
    recovery = reconcile_runtime_materializations(conn, job_id, board=board)
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
        from hermes_cli import kanban_runtime_memory as rm

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
            if hasattr(decision_provider, "decide"):
                request = rd.build_decision_provider_request(conn, job_id, delta)
                profile_name = getattr(decision_provider, "profile_name", "graph_patch_decision")
                messages, rendered, profile = rd.render_decision_messages(request, profile_name=profile_name)
                provider_request_ref = hashlib.sha256(
                    _json({"messages": messages, "rendered": rendered}).encode("utf-8")
                ).hexdigest()
                rm.record_memory_hint_usage(
                    conn,
                    job_id,
                    decision_id,
                    request.memory,
                    provider_request_ref=provider_request_ref,
                )
                append_decision_segment_entry(
                    conn,
                    job_id,
                    "provider_input",
                    {
                        "request": request.to_dict(),
                        "rendered": rendered,
                        "profile": {
                            "profile_name": profile["profile_name"],
                            "profile_version": profile["profile_version"],
                            "profile_hash": profile["profile_hash"],
                            "profile_path": profile["profile_path"],
                        },
                        "no_tools": True,
                    },
                    payload_text=_json(messages),
                    decision_id=decision_id,
                    ref_type="kernel_decision",
                    ref_id=decision_id,
                )
                provider_result = decision_provider.decide(request)
                if not isinstance(provider_result, rd.DecisionProviderResult):
                    raise rd.ProviderPatchParseError("RuntimeDecisionProvider.decide() must return DecisionProviderResult")
                raw_output = provider_result.raw_output
                append_decision_segment_entry(
                    conn,
                    job_id,
                    "provider_output",
                    provider_result.to_dict(),
                    decision_id=decision_id,
                    ref_type="kernel_decision",
                    ref_id=decision_id,
                )
                if provider_result.retry_count:
                    append_decision_segment_entry(
                        conn,
                        job_id,
                        "parse_retry",
                        {
                            "retry_count": provider_result.retry_count,
                            "parse_status": provider_result.parse_status,
                            "error": provider_result.error,
                            "request_ref": provider_result.request_ref,
                            "response_ref": provider_result.response_ref,
                        },
                        decision_id=decision_id,
                        ref_type="kernel_decision",
                        ref_id=decision_id,
                    )
                if provider_result.patch is None:
                    event_type = (
                        "decision_provider_error"
                        if provider_result.parse_status == "provider_error"
                        else "decision_parse_failed"
                    )
                    result = {
                        "status": provider_result.parse_status,
                        "reason": provider_result.error or provider_result.parse_status,
                    }
                    conn.execute(
                        """
                        UPDATE kernel_decisions
                           SET decision_json = ?, status = ?,
                               validator_result_json = ?, error = ?, completed_at = ?, model = ?
                         WHERE id = ?
                        """,
                        (
                            _json(provider_result.to_dict()),
                            provider_result.parse_status,
                            _json(result),
                            provider_result.error,
                            _now(),
                            provider_result.model,
                            decision_id,
                        ),
                    )
                    _event(conn, job_id, event_type, {"reason": result["reason"], "status": provider_result.parse_status})
                    append_decision_segment_entry(
                        conn,
                        job_id,
                        "provider_error" if provider_result.parse_status == "provider_error" else "validator_result",
                        result,
                        decision_id=decision_id,
                        ref_type="kernel_decision",
                        ref_id=decision_id,
                    )
                    append_decision_segment_entry(
                        conn,
                        job_id,
                        "patch_rejected",
                        {"reason": result["reason"], "stage": provider_result.parse_status},
                        decision_id=decision_id,
                        ref_type="kernel_decision",
                        ref_id=decision_id,
                    )
                    patch_status = provider_result.parse_status
                    rm.record_memory_hint_usage(
                        conn,
                        job_id,
                        decision_id,
                        locals().get("request").memory if "request" in locals() else {},
                        outcome=result,
                    )
                    patch = None
                else:
                    patch = provider_result.patch
                    conn.execute(
                        "UPDATE kernel_decisions SET model = ? WHERE id = ?",
                        (provider_result.model, decision_id),
                    )
            else:
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
            if patch is not None:
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
                    (_json(provider_result.to_dict() if "provider_result" in locals() else {"raw_output": raw_output, "patch": patch, "parse_status": "parsed"}), _now(), decision_id),
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
            parse_event = "decision_stale_revision" if "expected_revision" in str(exc) else "decision_parse_failed"
            _event(conn, job_id, parse_event, {"reason": str(exc), "stage": "parse"})
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
            rm.record_memory_hint_usage(
                conn,
                job_id,
                decision_id,
                locals().get("request").memory if "request" in locals() else {},
                outcome=result,
            )
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
            if patch is None:
                reduction = reduce_runtime_job(conn, job_id)
                if auto_compact:
                    policy_result = rd.should_compact_decision_session(conn, job_id, compaction_policy)
                    if policy_result["should_compact"]:
                        rd.compact_decision_session(
                            conn,
                            job_id,
                            profile_name=policy_result["profile_name"],
                            reason=policy_result["reason"],
                        )
                final = status_runtime_job(conn, job_id)["job"]["state"]
                return AdvanceResult(
                    job_id=job_id,
                    job_state=final,
                    materialized_nodes=materialized,
                    ingested_nodes=ingested,
                    decision_requested=decision_requested,
                    patch_status=patch_status,
                    recovery=recovery,
                )
            result = apply_graph_patch(conn, job_id, patch, decision_id=decision_id)
            patch_status = result["status"]
            rm.record_memory_hint_usage(
                conn,
                job_id,
                decision_id,
                locals().get("request").memory if "request" in locals() else {},
                outcome=result,
            )
            if result["status"] != "applied":
                reason = str(result.get("reason") or "")
                event_type = "decision_stale_revision" if "expected_revision" in reason else "decision_patch_rejected"
                _event(
                    conn,
                    job_id,
                    event_type,
                    {
                        "decision_id": decision_id,
                        "patch_id": result.get("patch_id"),
                        "reason": reason,
                    },
                )
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
        recovery=recovery,
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
            tuple(result.recovery.get("events") or []),
            tuple(result.recovery.get("scheduled_retries") or []),
        )
        steps.append(
            {
                "job_state": result.job_state,
                "materialized_nodes": result.materialized_nodes,
                "ingested_nodes": result.ingested_nodes,
                "decision_requested": result.decision_requested,
                "patch_status": result.patch_status,
                "recovery": result.recovery,
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


def supervisor_runtime_tick(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    owner: Optional[str] = None,
    lock_ttl_seconds: int = 60,
    board: Optional[str] = None,
    create_tasks: bool = True,
    decision_provider: Optional[Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]] = None,
    max_patches: int = 1,
    auto_compact: bool = True,
    compaction_policy: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Run one production supervisor tick under a resumable DB lease."""

    lock = acquire_runtime_advance_lock(
        conn,
        job_id,
        owner=owner,
        ttl_seconds=lock_ttl_seconds,
    )
    if not lock.get("acquired"):
        return {
            "job_id": job_id,
            "status": "skipped",
            "reason": lock.get("reason"),
            "lock": lock,
        }
    owner_id = str(lock["owner"])
    try:
        result = advance_runtime_job(
            conn,
            job_id,
            board=board,
            create_tasks=create_tasks,
            decision_provider=decision_provider,
            max_patches=max_patches,
            auto_compact=auto_compact,
            compaction_policy=compaction_policy,
        )
        return {
            "job_id": job_id,
            "status": "advanced",
            "lock": lock,
            "result": {
                "job_state": result.job_state,
                "materialized_nodes": result.materialized_nodes,
                "ingested_nodes": result.ingested_nodes,
                "decision_requested": result.decision_requested,
                "patch_status": result.patch_status,
                "recovery": result.recovery,
                "events": result.events,
            },
        }
    finally:
        release_runtime_advance_lock(conn, job_id, owner=owner_id)


def supervise_runtime_jobs_once(
    conn: sqlite3.Connection,
    *,
    owner: Optional[str] = None,
    limit: int = 10,
    board: Optional[str] = None,
    create_tasks: bool = True,
    decision_provider: Optional[Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]] = None,
    lock_ttl_seconds: int = 60,
) -> dict[str, Any]:
    """Poll resumable runtime jobs and run at most one leased tick per job."""

    ensure_runtime_schema(conn)
    rows = conn.execute(
        """
        SELECT id
          FROM runtime_jobs
         WHERE state NOT IN ('done', 'cancelled', 'failed')
         ORDER BY updated_at ASC, created_at ASC
         LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    ticks = [
        supervisor_runtime_tick(
            conn,
            row["id"],
            owner=owner,
            lock_ttl_seconds=lock_ttl_seconds,
            board=board,
            create_tasks=create_tasks,
            decision_provider=decision_provider,
        )
        for row in rows
    ]
    return {
        "owner": owner,
        "job_count": len(rows),
        "advanced_count": len([tick for tick in ticks if tick.get("status") == "advanced"]),
        "ticks": ticks,
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
    job_metadata = _loads(job.get("metadata_json"))
    assignee = node.get("assignee") or job_metadata.get("default_worker_lane")
    if assignee and not node.get("assignee"):
        conn.execute(
            "UPDATE execution_nodes SET assignee = ?, updated_at = ? WHERE id = ?",
            (assignee, _now(), node["id"]),
        )
        node["assignee"] = assignee
    evaluation = evaluate_node_capability_policy(conn, job["id"], node)
    _store_node_capability_evaluation(conn, node, evaluation)
    metadata = _loads(node.get("metadata_json"))
    metadata["capability_policy"] = {
        "status": evaluation["status"],
        "reason": evaluation["reason"],
        "policy_revision": evaluation["policy_revision"],
        "requested": evaluation["requested"],
        "allowed": evaluation["allowed"],
        "denied": evaluation["denied"],
        "requires_human": evaluation["requires_human"],
        "authorized": evaluation["authorized"],
        "lane_incapable": evaluation["lane_incapable"],
        "defaults": evaluation["defaults"],
    }
    node["metadata_json"] = _json(metadata)
    if evaluation["requested"]:
        _event(
            conn,
            job["id"],
            "capability_request_evaluated",
            {
                "node_key": node["node_key"],
                "requested_capabilities": evaluation["requested"],
                "allowed": evaluation["allowed"],
                "denied": evaluation["denied"],
                "requires_human": evaluation["requires_human"],
                "lane_incapable": evaluation["lane_incapable"],
                "policy_revision": evaluation["policy_revision"],
                "status": evaluation["status"],
                "reason": evaluation["reason"],
            },
            node_id=node["id"],
        )
    if evaluation["status"] in {"denied", "lane_incapable", "requires_human"}:
        next_state = "waiting_human" if evaluation["status"] == "requires_human" else "blocked"
        event_type = "capability_requires_human" if evaluation["status"] == "requires_human" else "capability_denied"
        now = _now()
        conn.execute(
            "UPDATE execution_nodes SET state = ?, updated_at = ? WHERE id = ?",
            (next_state, now, node["id"]),
        )
        event_id = _event(
            conn,
            job["id"],
            event_type,
            {
                "node_key": node["node_key"],
                "requested_capabilities": evaluation["requested"],
                "allowed": evaluation["allowed"],
                "denied": evaluation["denied"],
                "requires_human": evaluation["requires_human"],
                "lane_incapable": evaluation["lane_incapable"],
                "policy_revision": evaluation["policy_revision"],
                "reason": evaluation["reason"],
            },
            node_id=node["id"],
        )
        _event(
            conn,
            job["id"],
            "capability_policy_blocked",
            {
                "node_key": node["node_key"],
                "status": evaluation["status"],
                "event_id": event_id,
                "legal_waiting_reason": "waiting_capability_authorization"
                if evaluation["status"] == "requires_human"
                else "blocked_by_policy",
            },
            node_id=node["id"],
            source_event_id=event_id,
        )
        _touch_job(
            conn,
            job["id"],
            state="waiting_human" if evaluation["status"] == "requires_human" else "blocked",
        )
        return None
    attempts = conn.execute(
        "SELECT COALESCE(MAX(attempt), 0) AS max_attempt FROM node_materializations WHERE node_id = ?",
        (node["id"],),
    ).fetchone()
    attempt = int(attempts["max_attempt"] or 0) + 1
    materialization_id = _id("mat")
    continuity = _plan_worker_execution_continuity(
        conn,
        job,
        node,
        assignee=assignee,
    )
    body = _worker_context(conn, job, node, materialization_id, continuity=continuity)
    task_id = kb.create_task(
        conn,
        title=f"[runtime] {node['title']}",
        body=body,
        assignee=assignee,
        created_by="runtime_kernel",
        workspace_kind="worktree" if job.get("workspace_path") else "scratch",
        workspace_path=job.get("workspace_path"),
        tenant=f"runtime:{job['id']}",
        idempotency_key=f"runtime:{job['id']}:{node['id']}:{attempt}",
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
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)
        """,
        (
            materialization_id,
            job["id"],
            node["id"],
            attempt,
            task_id,
            run_id,
            assignee,
            now,
            now,
            _json({"execution_continuity": continuity}),
        ),
    )
    if continuity["mode"] == "resume":
        conn.execute(
            """
            UPDATE backend_worker_sessions
               SET status = 'resume_pending', latest_materialization_id = ?,
                   resume_count = resume_count + 1, updated_at = ?
             WHERE id = ? AND status = 'interrupted'
            """,
            (materialization_id, now, continuity["backend_session_record_id"]),
        )
        _event(
            conn,
            job["id"],
            "worker_session_resume_scheduled",
            {
                "node_key": node["node_key"],
                "backend_session_record_id": continuity["backend_session_record_id"],
                "materialization_id": materialization_id,
                "resume_from_materialization_id": continuity["resume_from_materialization_id"],
                "attempt": attempt,
            },
            node_id=node["id"],
            task_id=task_id,
            run_id=run_id,
        )
    elif continuity["mode"] == "fallback_fresh":
        _event(
            conn,
            job["id"],
            "worker_session_fallback_fresh",
            {
                "node_key": node["node_key"],
                "backend_session_record_id": continuity.get("backend_session_record_id"),
                "materialization_id": materialization_id,
                "attempt": attempt,
                "rejection_reasons": continuity.get("rejection_reasons") or [],
            },
            node_id=node["id"],
            task_id=task_id,
            run_id=run_id,
        )
        _event(
            conn,
            job["id"],
            "worker_context_reacquired",
            {
                "node_key": node["node_key"],
                "materialization_id": materialization_id,
                "attempt": attempt,
                "reason": "resume_ineligible",
            },
            node_id=node["id"],
            task_id=task_id,
            run_id=run_id,
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


def _worker_context(
    conn: sqlite3.Connection,
    job: dict[str, Any],
    node: dict[str, Any],
    materialization_id: str,
    *,
    continuity: Optional[dict[str, Any]] = None,
) -> str:
    metadata = _loads(node.get("metadata_json"))
    constraints = _loads(node.get("constraints_json"))
    capability_policy = metadata.get("capability_policy")
    if not isinstance(capability_policy, dict):
        capability_policy = evaluate_node_capability_policy(conn, job["id"], node)
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
        "runtime_capability_policy": {
            "policy_revision": capability_policy.get("policy_revision"),
            "requested": capability_policy.get("requested") or [],
            "allowed": sorted(
                set((capability_policy.get("defaults") or {}).get("allowed_by_default") or [])
                | set(capability_policy.get("allowed") or [])
            ),
            "denied": sorted(
                set((capability_policy.get("defaults") or {}).get("denied_by_default") or [])
                | set(capability_policy.get("denied") or [])
            ),
            "requires_human": sorted(
                set((capability_policy.get("defaults") or {}).get("require_human") or [])
                | set(capability_policy.get("requires_human") or [])
            ),
            "on_denied": "return receipt with verdict=blocked and blocked_reason=policy_blocked",
            "on_requires_human": "return receipt with human_required=true",
        },
        "worker_execution_continuity": {
            "mode": (continuity or {}).get("mode") or "fresh",
            "eligibility": (continuity or {}).get("eligibility") or "not_evaluated",
            "resume_from_materialization_id": (continuity or {}).get("resume_from_materialization_id"),
            "context_reacquisition": bool((continuity or {}).get("context_reacquisition")),
        },
    }
    return (
        f"# Runtime node\n\n"
        f"Objective: {job['objective']}\n\n"
        f"Node: {node['title']}\n\n"
        f"{node['description']}\n\n"
        f"Goal items: {', '.join(metadata.get('goal_item_keys') or []) or '-'}\n"
        f"Gaps: {', '.join(metadata.get('gap_keys') or []) or '-'}\n\n"
        f"Node contract: {json.dumps(constraints.get('contract') or {}, sort_keys=True)}\n\n"
        f"Dependencies:\n{deps}\n\n"
        "Expected receipt fields: verdict, summary, claimed_goal_items, "
        "partial_goal_items, unmet_goal_items, verification, artifacts, "
        "active_assumptions, rejected_approaches, known_failure_boundaries.\n\n"
        "Capability policy: obey Runtime footer.runtime_capability_policy. "
        "Do not perform denied actions; return blocked or human_required instead.\n\n"
        f"Runtime footer: {json.dumps(footer, sort_keys=True)}"
    )


def _apply_declared_write_scope_check(node: dict[str, Any], evidence: dict[str, Any]) -> tuple[dict[str, Any], list[str], bool]:
    constraints = _loads(node.get("constraints_json"))
    contract = constraints.get("contract") if isinstance(constraints.get("contract"), dict) else {}
    scopes = contract.get("declared_write_scope") or []
    if not scopes:
        return evidence, [], False
    changed_files = evidence.get("changed_files")
    if not isinstance(changed_files, list):
        return evidence, [], True
    violations = [
        path
        for path in changed_files
        if not any(
            fnmatch.fnmatch(path, scope)
            or (scope.endswith("/**") and (path == scope[:-3] or path.startswith(scope[:-3] + "/")))
            for scope in scopes
        )
    ]
    if not violations:
        return evidence, [], False
    result = dict(evidence)
    claimed = list(result.get("claimed_goal_items") or [])
    result["claimed_goal_items"] = []
    result["unmet_goal_items"] = sorted(set(result.get("unmet_goal_items") or []) | set(claimed))
    result["verdict"] = "failed"
    verification = dict(result.get("verification") or {})
    verification["passed"] = False
    verification["summary"] = "declared write scope violated: " + ", ".join(violations)
    result["verification"] = verification
    return result, violations, False


def ingest_runtime_node_evidence(conn: sqlite3.Connection, node_id: str, board: Optional[str] = None) -> bool:
    node = conn.execute("SELECT * FROM execution_nodes WHERE id = ?", (node_id,)).fetchone()
    if node is None:
        raise ValueError(f"unknown node {node_id}")
    if not node["latest_task_id"]:
        return False
    snapshot = kb.task_progress_snapshot(conn, node["latest_task_id"], board=board)
    if snapshot is None or snapshot.task.status not in {"done", "blocked"}:
        return False
    raw_evidence = dict(snapshot.evidence or {})
    metadata = (
        _runtime_receipt_from_evidence(raw_evidence, dict(node))
        if _is_codex_lane_evidence(raw_evidence)
        else raw_evidence
    )
    if metadata is None:
        return False
    if metadata.get("structure_request") is not None and not _structure_request_valid(metadata["structure_request"]):
        return False
    metadata, scope_violations, scope_unverified = _apply_declared_write_scope_check(dict(node), metadata)
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
    if scope_violations:
        _event(
            conn,
            node["job_id"],
            "write_scope_violation",
            {"node_key": node["node_key"], "changed_files": scope_violations},
            node_id=node_id,
            task_id=node["latest_task_id"],
            run_id=snapshot_run_id,
            source="kanban_task",
        )
    elif scope_unverified:
        _event(
            conn,
            node["job_id"],
            "write_scope_unverified",
            {"node_key": node["node_key"], "reason": "receipt missing structured changed_files"},
            node_id=node_id,
            task_id=node["latest_task_id"],
            run_id=snapshot_run_id,
            source="kanban_task",
        )
    if metadata.get("structure_request") is not None:
        _event(
            conn,
            node["job_id"],
            "worker_structure_requested",
            {"node_key": node["node_key"], "verdict": verdict, "structure_request": metadata["structure_request"]},
            node_id=node_id,
            task_id=node["latest_task_id"],
            run_id=snapshot_run_id,
            source="kanban_task",
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


def waive_goal_item(
    conn: sqlite3.Connection,
    job_id: str,
    item_key: str,
    *,
    reason: str,
    source: str = "user",
) -> dict[str, Any]:
    """Record an explicit operator/user waiver for a goal item.

    This is a fact-layer goal mutation. It writes ledger/event evidence and lets
    the reducer decide whether the job is now done.
    """

    ensure_runtime_schema(conn)
    if not str(reason or "").strip():
        raise ValueError("waiver reason is required")
    item = _goal_item_by_key(conn, job_id, item_key)
    contract = _contract(conn, job_id)
    now = _now()
    metadata = {
        "reason": str(reason).strip(),
        "source": str(source or "user").strip() or "user",
        "goal_item_key": item_key,
    }
    ledger_id = _id("pledger")
    conn.execute(
        """
        INSERT INTO progress_ledger (
            id, job_id, contract_id, goal_item_id, node_id, evidence_ref,
            satisfaction, verification_state, confidence, summary,
            metadata_json, created_at
        ) VALUES (?, ?, ?, ?, NULL, ?, 'waived', 'waived', 1.0, ?, ?, ?)
        """,
        (
            ledger_id,
            job_id,
            contract["id"],
            item["id"],
            f"waiver:{metadata['source']}:{item_key}",
            str(reason).strip(),
            _json(metadata),
            now,
        ),
    )
    _refresh_goal_item_states(conn, contract["id"])
    _event(conn, job_id, "goal_item_waived", {"goal_item_key": item_key, "reason": reason, "source": metadata["source"]})
    _event(conn, job_id, "human_decision_received", {"decision_type": "goal_waiver", "goal_item_key": item_key, "reason": reason, "source": metadata["source"]})
    reduction = reduce_runtime_job(conn, job_id)
    return {
        "job_id": job_id,
        "goal_item_key": item_key,
        "ledger_id": ledger_id,
        "state": _goal_item_by_key(conn, job_id, item_key)["state"],
        "job_state": _job(conn, job_id)["state"],
        "reduction": reduction,
    }


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
    contract = conn.execute("SELECT job_id FROM goal_contracts WHERE id = ?", (contract_id,)).fetchone()
    for item in conn.execute("SELECT * FROM goal_items WHERE contract_id = ?", (contract_id,)).fetchall():
        latest = conn.execute(
            """
            SELECT id, satisfaction, verification_state
              FROM progress_ledger
             WHERE goal_item_id = ?
             ORDER BY created_at DESC, rowid DESC
             LIMIT 1
            """,
            (item["id"],),
        ).fetchone()
        state = "open"
        if latest and latest["satisfaction"] == "contradicted":
            state = "contradicted"
        elif latest and latest["satisfaction"] == "waived":
            state = "waived"
        elif latest and latest["satisfaction"] == "full" and (
            latest["verification_state"] == "verified"
            or (not item["verifier_required"] and latest["verification_state"] not in {"failed", "failed_verification"})
        ):
            state = "satisfied"
        elif latest and latest["satisfaction"] in {"full", "partial"}:
            state = "partial"
        conn.execute(
            "UPDATE goal_items SET state = ?, updated_at = ? WHERE id = ?",
            (state, now, item["id"]),
        )
        if contract and item["state"] in {"satisfied", "waived"} and state not in {"satisfied", "waived"}:
            _event(
                conn,
                contract["job_id"],
                "goal_gap_reopened",
                {
                    "goal_item_key": item["item_key"],
                    "previous_state": item["state"],
                    "new_state": state,
                    "latest_ledger_id": latest["id"] if latest else None,
                },
            )


def fixture_decision_provider(session: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    """Deterministic provider used by tests; not a default workflow."""

    revision = int(delta["job"]["graph_revision"])
    contract = {
        "outcome": "Produce verified evidence for the linked runtime goal item.",
        "acceptance_criteria": ["The linked goal outcome exists", "Required verification passes"],
        "success_evidence": ["changed_files", "verification", "worker_summary"],
        "declared_write_scope": [],
        "prohibited_actions": ["production_deployment"],
    }
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
                    "contract": contract,
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
            "decomposition": {
                "policy_version": "1",
                "mode": "multiple_runtime_nodes",
                "justifications": [
                    {
                        "type": "independent_verification",
                        "nodes": [target, f"verify-{goal_key}".replace(":", "-")],
                        "explanation": "Verification must use an independent worker responsibility.",
                        "evidence_refs": [],
                    }
                ],
            },
            "ops": [
                {
                    "op": "insert_verifier",
                    "target_node_key": target,
                    "target_workspace_revision": f"node:{target}:latest-terminal",
                    "verifier_node_key": f"verify-{goal_key}".replace(":", "-"),
                    "title": f"Verify {goal_key}",
                    "goal_item_keys": [goal_key],
                    "gap_keys": [gap_key],
                    "contract": contract,
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
                "contract": contract,
            }
        ],
    }
