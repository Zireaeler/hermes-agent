"""Decision-provider support for the Kanban runtime kernel.

This module is intentionally non-authoritative: it renders DB-derived context,
creates DB-derived checkpoints, and parses provider output.  Graph changes still
go through ``kanban_runtime_kernel.apply_graph_patch`` and its validator.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid
from typing import Any, Callable, Optional


PATCH_SCHEMA = "runtime_graph_patch_v1"
PATCH_OPS = {
    "create_node",
    "add_dependency",
    "insert_verifier",
    "request_human",
    "propose_blocked",
    "strategy_update",
    "continue_node",
}
DEFAULT_COMPACTION_POLICY = {
    "mode": "auto",
    "max_active_segment_tokens": None,
    "compaction_trigger_ratio": 0.65,
    "max_context_window_ratio": 0.65,
    "context_window_tokens": 128000,
    "reserved_output_tokens": 8192,
    "reserved_reasoning_tokens": 32768,
    "estimation_safety_tokens": 32768,
    "max_compaction_input_ratio": 0.55,
    "max_compaction_input_chars": 1000000,
    "max_single_entry_chars": 16000,
    "max_segment_entries": 200,
    "rejected_patch_threshold": 5,
    "noop_threshold": 5,
    "max_tail_entries": 8,
    "max_tail_tokens": 2000,
    "default_profile": "token_budget_compaction",
    "fallback_degraded_threshold": 2,
}
COMPACTION_AUDIT_ENTRY_TYPES = frozenset({
    "compaction_requested",
    "compaction_provider_input",
    "compaction_provider_output",
    "compaction_fallback",
    "compaction_event",
    "checkpoint_created",
    "checkpoint_rejected",
})
COMPACTION_NON_CONTEXT_ENTRY_TYPES = COMPACTION_AUDIT_ENTRY_TYPES | {"provider_input"}
CHECKPOINT_FACT_SOURCE_REF_TYPES = {
    "satisfied_goal_items": {"goal_item_key", "evidence_ref"},
    "open_goal_gaps": {"gap_key"},
    "open_blockers": {"gap_key", "node_key", "event_id"},
    "key_decisions": {"decision_id", "event_id", "patch_id"},
    "rejected_approaches": {"decision_id", "event_id", "patch_id", "patch_base_revision"},
    "known_failure_boundaries": {"node_key", "event_id", "patch_id"},
    "validator_rejection_lessons": {"patch_id", "patch_base_revision"},
    "human_decisions": {"decision_id", "event_id"},
    "artifact_index": {"artifact_ref"},
    "graph_frontier": {"node_key"},
    "do_not_repeat": {"patch_id", "patch_base_revision"},
    "next_strategy_constraints": {"goal_item_key", "gap_key", "node_key", "decision_id", "event_id"},
}


class ProviderPatchParseError(ValueError):
    """Raised when provider output is not a strict runtime graph patch."""


class CompactionInputBudgetError(ValueError):
    """Raised before provider invocation when compaction input exceeds its hard budget."""


@dataclass(frozen=True)
class DecisionProviderRequest:
    job_id: str
    db_revision: int
    session: dict[str, Any]
    stable_prefix: dict[str, Any]
    goal_contract: dict[str, Any]
    checkpoint: Optional[dict[str, Any]]
    memory: dict[str, Any]
    short_tail: list[dict[str, Any]]
    delta: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "db_revision": self.db_revision,
            "session": self.session,
            "stable_prefix": self.stable_prefix,
            "goal_contract": self.goal_contract,
            "checkpoint": self.checkpoint,
            "memory": self.memory,
            "short_tail": self.short_tail,
            "delta": self.delta,
        }


@dataclass(frozen=True)
class DecisionProviderResult:
    patch: Optional[dict[str, Any]]
    raw_output: Any
    provider_name: str
    model: Optional[str] = None
    profile_name: Optional[str] = None
    profile_version: Optional[str] = None
    profile_hash: Optional[str] = None
    request_ref: Optional[str] = None
    response_ref: Optional[str] = None
    parse_status: str = "parsed"
    retry_count: int = 0
    provider_latency_ms: Optional[int] = None
    input_token_estimate: Optional[int] = None
    output_token_estimate: Optional[int] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "patch": self.patch,
            "raw_output": self.raw_output,
            "provider_name": self.provider_name,
            "model": self.model,
            "profile_name": self.profile_name,
            "profile_version": self.profile_version,
            "profile_hash": self.profile_hash,
            "request_ref": self.request_ref,
            "response_ref": self.response_ref,
            "parse_status": self.parse_status,
            "retry_count": self.retry_count,
            "provider_latency_ms": self.provider_latency_ms,
            "input_token_estimate": self.input_token_estimate,
            "output_token_estimate": self.output_token_estimate,
            "error": self.error,
        }


@dataclass(frozen=True)
class CompactionProviderRequest:
    job_id: str
    source_segment: dict[str, Any]
    profile: dict[str, Any]
    budget: dict[str, Any]
    db_state: dict[str, Any]
    provenance_catalog: dict[str, Any]
    checkpoint_fact_schema: dict[str, Any]
    segment_entries: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "source_segment": self.source_segment,
            "profile": {key: self.profile.get(key) for key in ("profile_name", "profile_version", "profile_hash", "profile_path")},
            "budget": self.budget,
            "db_state": self.db_state,
            "provenance_catalog": self.provenance_catalog,
            "checkpoint_fact_schema": self.checkpoint_fact_schema,
            "segment_entries": self.segment_entries,
        }


@dataclass(frozen=True)
class CompactionProviderResult:
    checkpoint: Optional[dict[str, Any]]
    raw_output: Any
    provider_name: str
    model: Optional[str] = None
    profile_name: Optional[str] = None
    profile_version: Optional[str] = None
    profile_hash: Optional[str] = None
    request_ref: Optional[str] = None
    response_ref: Optional[str] = None
    parse_status: str = "parsed"
    retry_count: int = 0
    provider_latency_ms: Optional[int] = None
    input_token_estimate: Optional[int] = None
    output_token_estimate: Optional[int] = None
    error: Optional[str] = None
    fallback_used: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint": self.checkpoint,
            "raw_output": self.raw_output,
            "provider_name": self.provider_name,
            "model": self.model,
            "profile_name": self.profile_name,
            "profile_version": self.profile_version,
            "profile_hash": self.profile_hash,
            "request_ref": self.request_ref,
            "response_ref": self.response_ref,
            "parse_status": self.parse_status,
            "retry_count": self.retry_count,
            "provider_latency_ms": self.provider_latency_ms,
            "input_token_estimate": self.input_token_estimate,
            "output_token_estimate": self.output_token_estimate,
            "error": self.error,
            "fallback_used": self.fallback_used,
        }


def _now() -> int:
    return int(time.time())


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _json(data: Any) -> str:
    return json.dumps(data if data is not None else {}, ensure_ascii=False, sort_keys=True)


def _estimate_tokens(payload: Any) -> int:
    text = payload if isinstance(payload, str) else _json(payload)
    return max(1, (len(text) + 3) // 4)


def estimate_decision_input_tokens(rendered: dict[str, Any], profile_content: str = "") -> int:
    """Rough token estimate for rendered decision input plus profile text."""

    return _estimate_tokens(rendered) + _estimate_tokens(profile_content or "")


def _loads(raw: Any, default: Any = None) -> Any:
    if raw is None or raw == "":
        return {} if default is None else default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {} if default is None else default


def _row(row: sqlite3.Row | None) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    data = dict(row)
    for key in list(data):
        if key.endswith("_json"):
            data[key[:-5]] = _loads(data[key])
    return data


def ensure_decision_schema(conn: sqlite3.Connection) -> None:
    from hermes_cli import kanban_runtime_kernel as rk

    rk.ensure_runtime_schema(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS decision_checkpoints (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            decision_session_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            checkpoint_json TEXT NOT NULL,
            reason TEXT NOT NULL,
            transcript_ref TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_decision_checkpoints_job_revision "
        "ON decision_checkpoints(job_id, revision)"
    )


def latest_decision_session(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM decision_sessions WHERE job_id = ? AND state = 'active' "
        "ORDER BY created_at DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"job {job_id} has no active decision session")
    return _row(row) or {}


def latest_decision_checkpoint(conn: sqlite3.Connection, job_id: str) -> Optional[dict[str, Any]]:
    ensure_decision_schema(conn)
    row = conn.execute(
        "SELECT * FROM decision_checkpoints WHERE job_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    data = _row(row)
    if data is not None:
        data["checkpoint"] = _loads(data.get("payload_json") or data.get("checkpoint_json") or data.get("checkpoint"))
    return data


def _decision_checkpoint_rows(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM decision_checkpoints WHERE job_id = ? ORDER BY created_at DESC, rowid DESC",
        (job_id,),
    ).fetchall()
    checkpoints: list[dict[str, Any]] = []
    for row in rows:
        data = _row(row) or {}
        data["checkpoint"] = _loads(data.get("payload_json") or data.get("checkpoint_json"))
        checkpoints.append(data)
    return checkpoints


def _validate_context_checkpoint_row(
    conn: sqlite3.Connection,
    job_id: str,
    checkpoint: dict[str, Any],
    active_segment: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    checkpoint_id = checkpoint.get("id")
    payload = checkpoint.get("checkpoint")
    if checkpoint.get("validator_status") != "accepted":
        errors.append("checkpoint validator status is not accepted")
    if not isinstance(payload, dict):
        return [*errors, "checkpoint payload is not an object"]
    required = {
        "objective_summary",
        "goal_contract_revision",
        "satisfied_goal_items",
        "open_goal_gaps",
        "open_blockers",
        "graph_frontier",
        "metadata",
    }
    missing = sorted(key for key in required if key not in payload)
    if missing:
        errors.append(f"checkpoint payload missing fields: {', '.join(missing)}")
    else:
        facts = _validate_checkpoint_fact_payload(conn, job_id, payload)
        if facts["status"] != "accepted":
            errors.append(str(facts["reason"]))

    source_segment_id = checkpoint.get("source_segment_id")
    source_segment = _row(
        conn.execute(
            "SELECT * FROM decision_session_segments WHERE job_id = ? AND id = ?",
            (job_id, source_segment_id),
        ).fetchone()
    )
    if source_segment is None:
        errors.append("checkpoint source segment is missing")
    else:
        if source_segment.get("decision_session_id") != checkpoint.get("decision_session_id"):
            errors.append("checkpoint and source segment decision sessions do not match")
        if source_segment.get("state") != "compacted":
            errors.append("checkpoint source segment is not compacted")
        if source_segment.get("compacted_checkpoint_id") != checkpoint_id:
            errors.append("source segment compacted checkpoint does not match")
        if int(active_segment.get("segment_index") or 0) <= int(source_segment.get("segment_index") or 0):
            errors.append("active segment does not follow checkpoint source segment")

    current_revision = int(
        conn.execute("SELECT graph_revision FROM runtime_jobs WHERE id = ?", (job_id,)).fetchone()[0]
    )
    checkpoint_revision = checkpoint.get("graph_revision")
    if checkpoint_revision is not None and int(checkpoint_revision) > current_revision:
        errors.append("checkpoint graph revision is in the future")

    covered_start = checkpoint.get("covered_entry_start")
    covered_end = checkpoint.get("covered_entry_end")
    if covered_start is None or covered_end is None or int(covered_start) > int(covered_end):
        errors.append("checkpoint covered entry range is invalid")
    elif source_segment is not None:
        covered_endpoints = conn.execute(
            """
            SELECT COUNT(*) FROM decision_segment_entries
             WHERE segment_id = ? AND id IN (?, ?)
            """,
            (source_segment_id, int(covered_start), int(covered_end)),
        ).fetchone()[0]
        expected_endpoints = 1 if int(covered_start) == int(covered_end) else 2
        if int(covered_endpoints) != expected_endpoints:
            errors.append("checkpoint covered entry range endpoints are not in source segment")

    supersedes = checkpoint.get("supersedes_checkpoint_id")
    if supersedes and conn.execute(
        "SELECT 1 FROM decision_checkpoints WHERE job_id = ? AND id = ?",
        (job_id, supersedes),
    ).fetchone() is None:
        errors.append("checkpoint supersedes reference is missing")
    checkpoint_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if "selected_hints" in checkpoint_text or "non_authoritative_notice" in checkpoint_text:
        errors.append("checkpoint contains runtime memory hint content")
    return errors


def validate_decision_context_chain(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    emit_event: bool = False,
) -> dict[str, Any]:
    """Validate and select checkpoint context without restoring runtime truth."""

    from hermes_cli import kanban_runtime_kernel as rk

    rk.ensure_runtime_schema(conn)
    session = latest_decision_session(conn, job_id)
    active_rows = [
        _row(row) or {}
        for row in conn.execute(
            "SELECT * FROM decision_session_segments WHERE job_id = ? AND state = 'active' ORDER BY segment_index",
            (job_id,),
        ).fetchall()
    ]
    chain_errors: list[str] = []
    if len(active_rows) != 1:
        chain_errors.append(f"expected one active decision segment, found {len(active_rows)}")
    if not active_rows:
        return {
            "status": "invalid",
            "selection_mode": "db_derived",
            "latest_checkpoint_id": session.get("latest_checkpoint_id"),
            "selected_checkpoint_id": None,
            "checked_checkpoint_count": 0,
            "errors": chain_errors,
        }
    active = active_rows[-1]
    if session.get("active_segment_id") != active.get("id"):
        chain_errors.append("decision session active segment pointer does not match")
    if active.get("decision_session_id") != session.get("id"):
        chain_errors.append("active segment belongs to a different decision session")

    checkpoints = _decision_checkpoint_rows(conn, job_id)
    if not checkpoints:
        if session.get("latest_checkpoint_id"):
            chain_errors.append("decision session points to a missing latest checkpoint")
        return {
            "status": "valid" if not chain_errors else "invalid",
            "selection_mode": "db_derived",
            "latest_checkpoint_id": None,
            "selected_checkpoint_id": None,
            "checked_checkpoint_count": 0,
            "errors": chain_errors,
        }

    latest_id = checkpoints[0]["id"]
    if session.get("latest_checkpoint_id") != latest_id:
        chain_errors.append("decision session latest checkpoint pointer does not match")
    checked: list[dict[str, Any]] = []
    selected: Optional[dict[str, Any]] = None
    for checkpoint in checkpoints:
        errors = _validate_context_checkpoint_row(conn, job_id, checkpoint, active)
        if checkpoint.get("decision_session_id") != session.get("id"):
            errors.append("checkpoint belongs to a different decision session")
        checked.append({"checkpoint_id": checkpoint["id"], "errors": errors})
        if not errors and selected is None:
            selected = checkpoint

    latest_errors = checked[0]["errors"]
    if chain_errors:
        selected = None
        status = "invalid"
        selection_mode = "db_derived"
    elif selected is None:
        status = "invalid"
        selection_mode = "db_derived"
    elif selected["id"] == latest_id and not chain_errors:
        status = "valid"
        selection_mode = "latest_checkpoint"
    else:
        status = "degraded"
        selection_mode = "prior_checkpoint" if selected["id"] != latest_id else "latest_checkpoint"

    errors = [*chain_errors, *latest_errors]
    if emit_event and status != "valid":
        key = f"{latest_id}:{status}:{selection_mode}"
        existing = conn.execute(
            "SELECT payload_json FROM execution_events WHERE job_id = ? AND event_type = 'decision_context_checkpoint_invalid'",
            (job_id,),
        ).fetchall()
        if not any(_loads(row["payload_json"]).get("key") == key for row in existing):
            rk._event(
                conn,
                job_id,
                "decision_context_checkpoint_invalid",
                {
                    "key": key,
                    "latest_checkpoint_id": latest_id,
                    "selected_checkpoint_id": (selected or {}).get("id"),
                    "selection_mode": selection_mode,
                    "errors": errors[:20],
                },
                source="runtime_compaction",
            )
    return {
        "status": status,
        "selection_mode": selection_mode,
        "latest_checkpoint_id": latest_id,
        "selected_checkpoint_id": (selected or {}).get("id"),
        "selected_checkpoint": selected,
        "active_segment_id": active.get("id"),
        "checked_checkpoint_count": len(checked),
        "errors": errors,
        "checks": checked[:20],
    }


def select_decision_context_checkpoint(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    emit_event: bool = True,
) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    validation = validate_decision_context_chain(conn, job_id, emit_event=emit_event)
    return validation.get("selected_checkpoint"), validation


def load_compaction_profile(profile_name: str) -> dict[str, Any]:
    profile_dir = Path(__file__).resolve().parents[1] / "docs" / "kanban-runtime-kernel-compaction-profiles"
    profile_path = profile_dir / f"{profile_name}.md"
    if profile_path.exists():
        content = profile_path.read_text(encoding="utf-8")
        match = re.search(r"^Profile-Version:\s*(.+?)\s*$", content, flags=re.MULTILINE)
        version = match.group(1).strip() if match else "file"
        path_text = str(profile_path.relative_to(Path(__file__).resolve().parents[1]))
    else:
        content = profile_name
        version = "builtin"
        path_text = ""
    return {
        "profile_name": profile_name,
        "profile_version": version,
        "profile_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "profile_path": path_text,
        "content": content,
    }


def load_decision_profile(profile_name: str) -> dict[str, Any]:
    """Load a runtime decision-provider profile and record its audit identity."""

    profile_dir = Path(__file__).resolve().parents[1] / "docs" / "kanban-runtime-kernel-decision-profiles"
    profile_path = profile_dir / f"{profile_name}.md"
    if profile_path.exists():
        content = profile_path.read_text(encoding="utf-8")
        match = re.search(r"^Profile-Version:\s*(.+?)\s*$", content, flags=re.MULTILINE)
        version = match.group(1).strip() if match else "file"
        path_text = str(profile_path.relative_to(Path(__file__).resolve().parents[1]))
    else:
        content = profile_name
        version = "builtin"
        path_text = ""
    return {
        "profile_name": profile_name,
        "profile_version": version,
        "profile_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "profile_path": path_text,
        "content": content,
    }


def _profile_metadata(profile_name: str) -> dict[str, Any]:
    profile = load_compaction_profile(profile_name)
    return {key: profile[key] for key in ("profile_name", "profile_version", "profile_hash", "profile_path")}


def _profile_public(profile: dict[str, Any]) -> dict[str, Any]:
    return {key: profile.get(key) for key in ("profile_name", "profile_version", "profile_hash", "profile_path")}


def _source_refs(**refs: Any) -> list[dict[str, Any]]:
    return [{key: value} for key, value in refs.items() if value is not None]


def build_deterministic_checkpoint(
    conn: sqlite3.Connection,
    job_id: str,
    source_segment_id: str,
    profile_name: str = "token_budget_compaction",
) -> dict[str, Any]:
    """Build a DB-derived checkpoint candidate without reading old transcript text."""

    base = build_checkpoint_payload(conn, job_id)
    goal_items = []
    for item in base["goal_items"]:
        entries = [
            row
            for row in base["progress_ledger"]
            if row["item_key"] == item["item_key"]
        ]
        verified = [
            entry for entry in entries
            if entry["satisfaction"] == "full" and entry["verification_state"] in {"independently_verified", "waived"}
        ]
        if verified:
            goal_items.append(
                {
                    "goal_item_key": item["item_key"],
                    "state": "satisfied",
                    "summary": verified[0]["summary"],
                    "verification_state": verified[0]["verification_state"],
                    "source_refs": _source_refs(goal_item_key=item["item_key"], evidence_ref=verified[0].get("evidence_ref")),
                }
            )
    open_gaps = [
        {
            "gap_key": gap["gap_key"],
            "gap_type": gap["gap_type"],
            "summary": gap["summary"],
            "source_refs": _source_refs(gap_key=gap["gap_key"]),
        }
        for gap in base["open_gaps"]
    ]
    frontier = [
        {
            "node_key": node["node_key"],
            "node_type": node["node_type"],
            "state": node["state"],
            "summary": node["summary"],
            "source_refs": _source_refs(node_key=node["node_key"]),
        }
        for node in base["frontier_nodes"]
    ]
    rejections = [
        {
            "summary": item["reject_reason"],
            "base_revision": item["base_revision"],
            "source_refs": _source_refs(patch_base_revision=item["base_revision"]),
        }
        for item in base["recent_validator_rejections"]
    ]
    artifacts = [
        {
            "artifact_type": item["artifact_type"],
            "path_or_ref": item["path_or_ref"],
            "summary": item["summary"],
            "source_refs": _source_refs(artifact_ref=item["path_or_ref"]),
        }
        for item in base["artifact_index"]
    ]
    return {
        "objective_summary": base["job"]["objective"],
        "goal_contract": base["goal_contract"],
        "goal_items": base["goal_items"],
        "goal_contract_revision": base["goal_contract"]["version"],
        "active_milestone": None,
        "satisfied_goal_items": goal_items,
        "open_goal_gaps": open_gaps,
        "open_blockers": [
            gap for gap in open_gaps
            if gap["gap_type"] in {"human_required", "blocked_constraint"}
        ],
        "key_decisions": [],
        "rejected_approaches": [],
        "known_failure_boundaries": [],
        "validator_rejection_lessons": rejections,
        "human_decisions": [],
        "artifact_index": artifacts,
        "graph_frontier": frontier,
        "do_not_repeat": [
            {
                "summary": item["reject_reason"],
                "source_refs": _source_refs(patch_base_revision=item["base_revision"]),
            }
            for item in base["recent_validator_rejections"]
        ],
        "next_strategy_constraints": [],
        "metadata": {
            "source_segment_id": source_segment_id,
            "profile_name": profile_name,
            "deterministic": True,
            "db_revision": base["job"]["graph_revision"],
            "graph_revision": base["job"]["graph_revision"],
            "ledger_revision": base["job"]["graph_revision"],
        },
    }


def _checkpoint_fact_lists(payload: dict[str, Any]) -> list[tuple[str, list[dict[str, Any]]]]:
    keys = [
        "satisfied_goal_items",
        "open_goal_gaps",
        "open_blockers",
        "key_decisions",
        "rejected_approaches",
        "known_failure_boundaries",
        "validator_rejection_lessons",
        "human_decisions",
        "artifact_index",
        "graph_frontier",
        "do_not_repeat",
        "next_strategy_constraints",
    ]
    return [(key, payload.get(key) or []) for key in keys]


def validate_decision_checkpoint(conn: sqlite3.Connection, job_id: str, checkpoint_payload: dict[str, Any]) -> dict[str, Any]:
    required = {
        "objective_summary",
        "goal_contract_revision",
        "satisfied_goal_items",
        "open_goal_gaps",
        "open_blockers",
        "graph_frontier",
        "metadata",
    }
    missing = sorted(key for key in required if key not in checkpoint_payload)
    if missing:
        return {"status": "rejected", "reason": f"checkpoint missing required fields: {', '.join(missing)}"}

    current_revision = int(
        conn.execute("SELECT graph_revision FROM runtime_jobs WHERE id = ?", (job_id,)).fetchone()["graph_revision"]
    )
    metadata = checkpoint_payload.get("metadata") or {}
    for rev_key in ("db_revision", "graph_revision", "ledger_revision"):
        if int(metadata.get(rev_key, current_revision)) != current_revision:
            return {"status": "rejected", "reason": f"checkpoint {rev_key} conflicts with current revision"}

    fact_validation = _validate_checkpoint_fact_payload(conn, job_id, checkpoint_payload)
    if fact_validation["status"] != "accepted":
        return fact_validation

    current_open_human = conn.execute(
        "SELECT COUNT(*) FROM execution_nodes WHERE job_id = ? AND state = 'waiting_human'",
        (job_id,),
    ).fetchone()[0]
    if current_open_human and not checkpoint_payload.get("open_blockers"):
        return {"status": "rejected", "reason": "checkpoint omits active human gate blocker"}
    return {"status": "accepted"}


def _validate_checkpoint_fact_payload(
    conn: sqlite3.Connection,
    job_id: str,
    checkpoint_payload: dict[str, Any],
) -> dict[str, Any]:
    goal_keys = {
        row["item_key"]
        for row in conn.execute(
            """
            SELECT gi.item_key
              FROM goal_items gi
              JOIN goal_contracts gc ON gc.id = gi.contract_id
             WHERE gc.job_id = ? AND gc.state = 'active'
            """,
            (job_id,),
        ).fetchall()
    }
    node_keys = {
        row["node_key"]
        for row in conn.execute("SELECT node_key FROM execution_nodes WHERE job_id = ?", (job_id,)).fetchall()
    }
    artifacts = {
        row["path_or_ref"]
        for row in conn.execute("SELECT path_or_ref FROM node_artifacts WHERE job_id = ?", (job_id,)).fetchall()
    }
    gap_keys = {
        row["gap_key"]
        for row in conn.execute("SELECT gap_key FROM goal_gaps WHERE job_id = ?", (job_id,)).fetchall()
    }
    evidence_refs = {
        row["evidence_ref"]
        for row in conn.execute(
            "SELECT evidence_ref FROM progress_ledger WHERE job_id = ? AND evidence_ref IS NOT NULL",
            (job_id,),
        ).fetchall()
    }
    patch_base_revisions = {
        int(row["base_revision"])
        for row in conn.execute("SELECT base_revision FROM graph_patches WHERE job_id = ?", (job_id,)).fetchall()
    }
    event_ids = {
        int(row["id"])
        for row in conn.execute("SELECT id FROM execution_events WHERE job_id = ?", (job_id,)).fetchall()
    }
    decision_ids = {
        row["id"]
        for row in conn.execute("SELECT id FROM kernel_decisions WHERE job_id = ?", (job_id,)).fetchall()
    }
    patch_ids = {
        row["id"]
        for row in conn.execute("SELECT id FROM graph_patches WHERE job_id = ?", (job_id,)).fetchall()
    }
    reference_sets = {
        "node_key": node_keys,
        "goal_item_key": goal_keys,
        "gap_key": gap_keys,
        "evidence_ref": evidence_refs,
        "artifact_ref": artifacts,
        "patch_base_revision": patch_base_revisions,
        "event_id": event_ids,
        "decision_id": decision_ids,
        "patch_id": patch_ids,
    }
    fact_schemas = _compaction_checkpoint_fact_schema()["fact_lists"]

    for key, items in _checkpoint_fact_lists(checkpoint_payload):
        for item in items:
            if not isinstance(item, dict):
                return {"status": "rejected", "reason": f"{key} item must be an object"}
            missing_fields = [
                field
                for field in fact_schemas[key]["required_fields"]
                if field != "source_refs" and field not in item
            ]
            if missing_fields:
                return {
                    "status": "rejected",
                    "reason": f"{key} item missing required fields: {', '.join(missing_fields)}",
                }
            refs = item.get("source_refs") or []
            if not isinstance(refs, list) or not refs:
                return {"status": "rejected", "reason": f"{key} item lacks provenance"}
            for ref in refs:
                if not isinstance(ref, dict) or len(ref) != 1:
                    return {"status": "rejected", "reason": "checkpoint source_ref must contain exactly one reference"}
                ref_type, ref_value = next(iter(ref.items()))
                if ref_type not in reference_sets:
                    return {"status": "rejected", "reason": f"unsupported checkpoint source_ref {ref_type!r}"}
                if ref_type not in CHECKPOINT_FACT_SOURCE_REF_TYPES[key]:
                    return {"status": "rejected", "reason": f"{key} does not allow {ref_type} provenance"}
                comparable = ref_value
                if ref_type in {"patch_base_revision", "event_id"}:
                    try:
                        comparable = int(ref_value)
                    except (TypeError, ValueError):
                        return {"status": "rejected", "reason": f"invalid {ref_type} {ref_value!r}"}
                if comparable not in reference_sets[ref_type]:
                    return {"status": "rejected", "reason": f"unknown {ref_type} {ref_value!r}"}
            identity_ref = {
                "satisfied_goal_items": ("goal_item_key", "goal_item_key"),
                "open_goal_gaps": ("gap_key", "gap_key"),
                "graph_frontier": ("node_key", "node_key"),
                "artifact_index": ("path_or_ref", "artifact_ref"),
                "validator_rejection_lessons": ("base_revision", "patch_base_revision"),
            }.get(key)
            if identity_ref and item.get(identity_ref[0]) is not None:
                expected_value = item[identity_ref[0]]
                if identity_ref[1] in {"patch_base_revision", "event_id"}:
                    try:
                        expected_value = int(expected_value)
                    except (TypeError, ValueError):
                        return {"status": "rejected", "reason": f"invalid {identity_ref[0]} {expected_value!r}"}
                if not any(ref.get(identity_ref[1]) == expected_value for ref in refs):
                    return {
                        "status": "rejected",
                        "reason": f"{key} {identity_ref[0]} does not match its provenance",
                    }
            if key == "satisfied_goal_items":
                if item.get("verification_state") not in {"independently_verified", "waived"}:
                    return {"status": "rejected", "reason": "satisfied goal item must be independently verified or waived"}

    return {"status": "accepted"}


def build_checkpoint_payload(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    job = _row(conn.execute("SELECT * FROM runtime_jobs WHERE id = ?", (job_id,)).fetchone())
    if job is None:
        raise ValueError(f"unknown runtime job {job_id}")
    contract = _row(
        conn.execute(
            "SELECT * FROM goal_contracts WHERE job_id = ? AND state = 'active' "
            "ORDER BY version DESC LIMIT 1",
            (job_id,),
        ).fetchone()
    )
    goal_items = [
        _row(row) or {}
        for row in conn.execute(
            "SELECT * FROM goal_items WHERE contract_id = ? ORDER BY item_key",
            ((contract or {}).get("id"),),
        ).fetchall()
    ]
    gaps = [
        _row(row) or {}
        for row in conn.execute(
            "SELECT * FROM goal_gaps WHERE job_id = ? AND state = 'open' ORDER BY gap_key",
            (job_id,),
        ).fetchall()
    ]
    ledger = [
        _row(row) or {}
        for row in conn.execute(
            """
            SELECT gi.item_key, pl.satisfaction, pl.verification_state, pl.summary,
                   pl.evidence_ref, pl.created_at
              FROM progress_ledger pl
              JOIN goal_items gi ON gi.id = pl.goal_item_id
             WHERE pl.job_id = ?
             ORDER BY gi.item_key, pl.created_at DESC, pl.id
            """,
            (job_id,),
        ).fetchall()
    ]
    frontier = [
        _row(row) or {}
        for row in conn.execute(
            """
            SELECT node_key, node_type, state, title, output_summary,
                   input_summary, assumptions_json, metadata_json
              FROM execution_nodes
             WHERE job_id = ?
               AND state IN ('ready', 'running', 'succeeded', 'failed', 'waiting_human')
             ORDER BY node_key
            """,
            (job_id,),
        ).fetchall()
    ]
    artifacts = [
        _row(row) or {}
        for row in conn.execute(
            """
            SELECT artifact_type, path_or_ref, summary, metadata_json
              FROM node_artifacts
             WHERE job_id = ?
             ORDER BY path_or_ref, id
            """,
            (job_id,),
        ).fetchall()
    ]
    rejected = [
        _row(row) or {}
        for row in conn.execute(
            """
            SELECT base_revision, reject_reason, patch_json, created_at
              FROM graph_patches
             WHERE job_id = ? AND status = 'rejected'
             ORDER BY created_at DESC, id DESC
             LIMIT 10
            """,
            (job_id,),
        ).fetchall()
    ]
    return {
        "job": {
            "id": job["id"],
            "objective": job["objective"],
            "state": job["state"],
            "graph_revision": job["graph_revision"],
        },
        "goal_contract": {
            "id": (contract or {}).get("id"),
            "objective": (contract or {}).get("objective"),
            "version": (contract or {}).get("version"),
            "constraints": (contract or {}).get("constraints", {}),
            "defaults_policy": (contract or {}).get("defaults_policy", {}),
            "human_required_conditions": (contract or {}).get("human_required_conditions", {}),
            "completion_policy": (contract or {}).get("completion_policy", {}),
        },
        "goal_items": [
            {
                "item_key": item["item_key"],
                "description": item["description"],
                "required": bool(item["required"]),
                "verifier_required": bool(item["verifier_required"]),
                "state": item["state"],
            }
            for item in goal_items
        ],
        "open_gaps": [
            {
                "gap_key": gap["gap_key"],
                "gap_type": gap["gap_type"],
                "summary": gap["summary"],
            }
            for gap in gaps
        ],
        "progress_ledger": [
            {
                "item_key": item["item_key"],
                "satisfaction": item["satisfaction"],
                "verification_state": item["verification_state"],
                "summary": item["summary"],
                "evidence_ref": item["evidence_ref"],
            }
            for item in ledger
        ],
        "frontier_nodes": [
            {
                "node_key": node["node_key"],
                "node_type": node["node_type"],
                "state": node["state"],
                "title": node["title"],
                "summary": node.get("output_summary") or node.get("input_summary") or "",
                "assumptions": node.get("assumptions", {}),
            }
            for node in frontier
        ],
        "artifact_index": [
            {
                "artifact_type": item["artifact_type"],
                "path_or_ref": item["path_or_ref"],
                "summary": item["summary"],
            }
            for item in artifacts
        ],
        "recent_validator_rejections": [
            {
                "base_revision": item["base_revision"],
                "reject_reason": item["reject_reason"],
                "patch": item.get("patch", _loads(item.get("patch_json"))),
            }
            for item in rejected
        ],
    }


def create_decision_checkpoint(
    conn: sqlite3.Connection,
    job_id: str,
    reason: str = "manual",
    *,
    transcript_ref: Optional[str] = None,
) -> dict[str, Any]:
    ensure_decision_schema(conn)
    session = latest_decision_session(conn, job_id)
    payload = build_checkpoint_payload(conn, job_id)
    checkpoint_id = _id("dchk")
    now = _now()
    conn.execute(
        """
        INSERT INTO decision_checkpoints (
            id, job_id, decision_session_id, revision, checkpoint_json,
            reason, transcript_ref, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            checkpoint_id,
            job_id,
            session["id"],
            int(payload["job"]["graph_revision"]),
            _json(payload),
            reason,
            transcript_ref,
            now,
        ),
    )
    conn.execute(
        """
        UPDATE decision_sessions
           SET last_checkpoint_revision = ?, updated_at = ?
         WHERE id = ?
        """,
        (int(payload["job"]["graph_revision"]), now, session["id"]),
    )
    return {
        "id": checkpoint_id,
        "job_id": job_id,
        "decision_session_id": session["id"],
        "revision": int(payload["job"]["graph_revision"]),
        "reason": reason,
        "checkpoint": payload,
        "created_at": now,
    }


def _segment_range(conn: sqlite3.Connection, segment_id: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT MIN(id) AS first_entry, MAX(id) AS last_entry FROM decision_segment_entries WHERE segment_id = ?",
        (segment_id,),
    ).fetchone()
    events = conn.execute(
        "SELECT MIN(event_id) AS first_event, MAX(event_id) AS last_event FROM decision_segment_entries WHERE segment_id = ? AND event_id IS NOT NULL",
        (segment_id,),
    ).fetchone()
    decisions = conn.execute(
        "SELECT MIN(decision_id) AS first_decision, MAX(decision_id) AS last_decision FROM decision_segment_entries WHERE segment_id = ? AND decision_id IS NOT NULL",
        (segment_id,),
    ).fetchone()
    return {
        "covered_entry_start": rows["first_entry"],
        "covered_entry_end": rows["last_entry"],
        "covered_event_start": events["first_event"],
        "covered_event_end": events["last_event"],
        "covered_decision_start": decisions["first_decision"],
        "covered_decision_end": decisions["last_decision"],
    }


def _entry_type_exclusion_sql(entry_types: set[str] | frozenset[str]) -> tuple[str, list[str]]:
    ordered = sorted(entry_types)
    return ", ".join("?" for _ in ordered), ordered


def _project_compaction_entry(row: sqlite3.Row, *, max_chars: int) -> dict[str, Any]:
    item = _row(row) or {}
    raw = str(item.pop("payload_json", "") or "")
    item.pop("payload_text", None)
    item["source_estimated_tokens"] = int(item.pop("estimated_tokens", 0) or 0)
    if len(raw) <= max_chars:
        item["payload"] = _loads(raw)
    else:
        payload = _loads(raw)
        scalar_summary = {}
        if isinstance(payload, dict):
            for key in (
                "status", "state", "outcome", "verdict", "summary", "reason", "error",
                "key", "gap_key", "node_key", "profile_name", "parse_status",
            ):
                value = payload.get(key)
                if isinstance(value, (str, int, float, bool)) or value is None:
                    scalar_summary[key] = (
                        value[:1000] + "[truncated]"
                        if isinstance(value, str) and len(value) > 1000
                        else value
                    )
        item["payload"] = {
            "truncated": True,
            "payload_chars": len(raw),
            "payload_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "scalar_summary": scalar_summary,
        }
    item["projected_tokens"] = _estimate_tokens(item)
    return item


def _segment_entries_for_compaction(
    conn: sqlite3.Connection,
    segment_id: str,
    *,
    max_entries: int = 200,
    max_single_entry_chars: int = 16000,
) -> list[dict[str, Any]]:
    placeholders, excluded = _entry_type_exclusion_sql(COMPACTION_NON_CONTEXT_ENTRY_TYPES)
    rows = conn.execute(
        f"""
        SELECT id, entry_index, entry_type, decision_id, event_id, patch_id,
               graph_revision, ref_type, ref_id, payload_json, payload_text,
               estimated_tokens, created_at
          FROM decision_segment_entries
         WHERE segment_id = ? AND entry_type NOT IN ({placeholders})
         ORDER BY entry_index DESC, id DESC
         LIMIT ?
        """,
        (segment_id, *excluded, max(1, int(max_entries))),
    ).fetchall()
    return list(reversed([
        _project_compaction_entry(row, max_chars=max(256, int(max_single_entry_chars)))
        for row in rows
    ]))


def _compaction_source_fingerprint(
    conn: sqlite3.Connection,
    job_id: str,
    segment_id: str,
) -> str:
    placeholders, excluded = _entry_type_exclusion_sql(COMPACTION_NON_CONTEXT_ENTRY_TYPES)
    digest = hashlib.sha256()
    digest.update(_json(build_checkpoint_payload(conn, job_id)).encode("utf-8"))
    rows = conn.execute(
        f"""
        SELECT id, entry_type, decision_id, event_id, patch_id, graph_revision, payload_json
          FROM decision_segment_entries
         WHERE segment_id = ? AND entry_type NOT IN ({placeholders})
         ORDER BY entry_index, id
        """,
        (segment_id, *excluded),
    ).fetchall()
    for row in rows:
        digest.update(str(row["id"]).encode("ascii"))
        digest.update(str(row["entry_type"]).encode("utf-8"))
        digest.update(str(row["decision_id"] or "").encode("utf-8"))
        digest.update(str(row["event_id"] or "").encode("utf-8"))
        digest.update(str(row["patch_id"] or "").encode("utf-8"))
        digest.update(str(row["graph_revision"] or "").encode("ascii"))
        digest.update(hashlib.sha256(str(row["payload_json"] or "").encode("utf-8")).digest())
    return digest.hexdigest()


def estimate_segment_tokens(conn: sqlite3.Connection, segment_id: str) -> dict[str, int]:
    """Estimate token usage for one decision session segment from entries."""

    placeholders, excluded = _entry_type_exclusion_sql(COMPACTION_NON_CONTEXT_ENTRY_TYPES)
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(CASE WHEN entry_type NOT IN ({placeholders})
                                 THEN estimated_tokens ELSE 0 END), 0) AS active_segment_tokens,
               COALESCE(SUM(estimated_tokens), 0) AS persisted_segment_tokens,
               COALESCE(SUM(CASE WHEN entry_type IN ({placeholders})
                                 THEN estimated_tokens ELSE 0 END), 0) AS audit_segment_tokens,
               COALESCE(SUM(CASE WHEN entry_type IN ('provider_input', 'compaction_provider_input', 'delta_appended')
                                 THEN estimated_tokens ELSE 0 END), 0) AS estimated_input_tokens,
               COALESCE(SUM(CASE WHEN entry_type IN ('provider_output', 'compaction_provider_output')
                                 THEN estimated_tokens ELSE 0 END), 0) AS estimated_output_tokens
          FROM decision_segment_entries
         WHERE segment_id = ?
        """,
        (*excluded, *excluded, segment_id),
    ).fetchone()
    return {
        "active_segment_tokens": int(row["active_segment_tokens"] or 0),
        "persisted_segment_tokens": int(row["persisted_segment_tokens"] or 0),
        "audit_segment_tokens": int(row["audit_segment_tokens"] or 0),
        "estimated_input_tokens": int(row["estimated_input_tokens"] or 0),
        "estimated_output_tokens": int(row["estimated_output_tokens"] or 0),
    }


def build_compaction_provider_request(
    conn: sqlite3.Connection,
    job_id: str,
    source_segment: Optional[dict[str, Any]] = None,
    *,
    profile_name: str = "token_budget_compaction",
    budget: Optional[dict[str, Any]] = None,
) -> CompactionProviderRequest:
    from hermes_cli import kanban_runtime_kernel as rk

    rk.ensure_runtime_schema(conn)
    source_segment = source_segment or rk.ensure_decision_segment(conn, job_id)
    profile = load_compaction_profile(profile_name)
    metrics = estimate_segment_tokens(conn, source_segment["id"])
    policy = _job_compaction_policy(conn, job_id)
    context_window_tokens = max(1, int(policy["context_window_tokens"]))
    max_input_ratio = float(policy.get("max_compaction_input_ratio") or 0.55)
    resolved_budget = {
        "max_checkpoint_tokens": 3000,
        "max_segment_entries": int(policy.get("max_segment_entries") or 200),
        "max_single_entry_chars": int(policy.get("max_single_entry_chars") or 16000),
        "max_compaction_input_tokens": max(1, int(context_window_tokens * max_input_ratio)),
        "max_compaction_input_chars": int(policy.get("max_compaction_input_chars") or 1000000),
        **(budget or {}),
        "active_segment_tokens": metrics["active_segment_tokens"],
        "persisted_segment_tokens": metrics["persisted_segment_tokens"],
        "audit_segment_tokens": metrics["audit_segment_tokens"],
    }
    db_state = build_checkpoint_payload(conn, job_id)
    candidates = _segment_entries_for_compaction(
        conn,
        source_segment["id"],
        max_entries=int(resolved_budget["max_segment_entries"]),
        max_single_entry_chars=int(resolved_budget["max_single_entry_chars"]),
    )
    source_summary = {
        "id": source_segment["id"],
        "segment_index": source_segment["segment_index"],
        "state": source_segment["state"],
        "covered_graph_revision_start": source_segment.get("covered_graph_revision_start"),
    }

    def _request(entries: list[dict[str, Any]], resolved: dict[str, Any]) -> CompactionProviderRequest:
        return CompactionProviderRequest(
            job_id=job_id,
            source_segment=source_summary,
            profile=profile,
            budget=resolved,
            db_state=db_state,
            provenance_catalog=_build_compaction_provenance_catalog(db_state, entries),
            checkpoint_fact_schema=_compaction_checkpoint_fact_schema(),
            segment_entries=entries,
        )

    max_tokens = max(1, int(resolved_budget["max_compaction_input_tokens"]))
    max_chars = max(1024, int(resolved_budget["max_compaction_input_chars"]))
    selected: list[dict[str, Any]] = []
    for entry in reversed(candidates):
        candidate_entries = [entry, *selected]
        candidate = _request(candidate_entries, resolved_budget)
        messages, rendered, _ = render_compaction_messages(candidate)
        rendered_chars = sum(len(str(message.get("content") or "")) for message in messages)
        rendered_tokens = estimate_decision_input_tokens(rendered, profile["content"])
        if rendered_tokens <= max_tokens and rendered_chars <= max_chars:
            selected = candidate_entries

    resolved_budget = {
        **resolved_budget,
        "eligible_entry_count": len(candidates),
        "included_entry_count": len(selected),
        "omitted_entry_count": len(candidates) - len(selected),
        "included_entry_ids": [int(entry["id"]) for entry in selected],
        "source_fingerprint": _compaction_source_fingerprint(conn, job_id, source_segment["id"]),
    }
    request = _request(selected, resolved_budget)
    messages, rendered, _ = render_compaction_messages(request)
    rendered_chars = sum(len(str(message.get("content") or "")) for message in messages)
    rendered_tokens = estimate_decision_input_tokens(rendered, profile["content"])
    if rendered_tokens > max_tokens or rendered_chars > max_chars:
        raise CompactionInputBudgetError(
            "compaction_input_budget_exceeded: "
            f"tokens={rendered_tokens}/{max_tokens}, chars={rendered_chars}/{max_chars}"
        )
    request.budget["rendered_input_tokens"] = rendered_tokens
    request.budget["rendered_input_chars"] = rendered_chars
    return request


def _build_compaction_provenance_catalog(
    db_state: dict[str, Any],
    segment_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    verified_evidence: dict[str, list[str]] = {}
    for entry in db_state.get("progress_ledger") or []:
        if entry.get("satisfaction") != "full" or entry.get("verification_state") not in {"independently_verified", "waived"}:
            continue
        evidence_ref = entry.get("evidence_ref")
        if evidence_ref:
            verified_evidence.setdefault(str(entry.get("item_key")), []).append(str(evidence_ref))

    def _entry_refs(key: str) -> list[Any]:
        values: list[Any] = []
        for entry in segment_entries:
            value = entry.get(key)
            if value is not None and value not in values:
                values.append(value)
        return values

    return {
        "goal_items": [
            {
                "goal_item_key": item["item_key"],
                "verified_evidence_refs": sorted(set(verified_evidence.get(str(item["item_key"]), []))),
            }
            for item in db_state.get("goal_items") or []
        ],
        "goal_gaps": [
            {"gap_key": item["gap_key"]}
            for item in db_state.get("open_gaps") or []
        ],
        "execution_nodes": [
            {"node_key": item["node_key"]}
            for item in db_state.get("frontier_nodes") or []
        ],
        "artifacts": [
            {"artifact_ref": item["path_or_ref"]}
            for item in db_state.get("artifact_index") or []
        ],
        "validator_revisions": [
            {"patch_base_revision": int(item["base_revision"])}
            for item in db_state.get("recent_validator_rejections") or []
        ],
        "segment_events": [{"event_id": value} for value in _entry_refs("event_id")],
        "segment_decisions": [{"decision_id": value} for value in _entry_refs("decision_id")],
        "segment_patches": [{"patch_id": value} for value in _entry_refs("patch_id")],
    }


def _compaction_checkpoint_fact_schema() -> dict[str, Any]:
    return {
        "source_ref_contract": {
            "required_for_every_non_empty_fact_item": True,
            "one_reference_key_per_source_ref_object": True,
            "copy_values_exactly_from_provenance_catalog": True,
            "invented_references_forbidden": True,
            "supported_reference_keys": [
                "goal_item_key",
                "evidence_ref",
                "gap_key",
                "node_key",
                "artifact_ref",
                "patch_base_revision",
                "event_id",
                "decision_id",
                "patch_id",
            ],
            "example": {"source_refs": [{"gap_key": "copy-exact-gap-key-from-catalog"}]},
        },
        "fact_lists": {
            "satisfied_goal_items": {
                "required_fields": ["goal_item_key", "state", "summary", "verification_state", "source_refs"],
                "allowed_source_refs": ["goal_item_key", "evidence_ref"],
                "verification_state": ["independently_verified", "waived"],
            },
            "open_goal_gaps": {
                "required_fields": ["gap_key", "gap_type", "summary", "source_refs"],
                "allowed_source_refs": ["gap_key"],
            },
            "open_blockers": {
                "required_fields": ["summary", "source_refs"],
                "allowed_source_refs": ["gap_key", "node_key", "event_id"],
            },
            "graph_frontier": {
                "required_fields": ["node_key", "node_type", "state", "summary", "source_refs"],
                "allowed_source_refs": ["node_key"],
            },
            "artifact_index": {
                "required_fields": ["artifact_type", "path_or_ref", "summary", "source_refs"],
                "allowed_source_refs": ["artifact_ref"],
            },
            "validator_rejection_lessons": {
                "required_fields": ["summary", "base_revision", "source_refs"],
                "allowed_source_refs": ["patch_base_revision", "patch_id"],
            },
            "do_not_repeat": {
                "required_fields": ["summary", "source_refs"],
                "allowed_source_refs": ["patch_base_revision", "patch_id"],
            },
            "key_decisions": {
                "required_fields": ["summary", "source_refs"],
                "allowed_source_refs": sorted(CHECKPOINT_FACT_SOURCE_REF_TYPES["key_decisions"]),
            },
            "rejected_approaches": {
                "required_fields": ["summary", "source_refs"],
                "allowed_source_refs": sorted(CHECKPOINT_FACT_SOURCE_REF_TYPES["rejected_approaches"]),
            },
            "known_failure_boundaries": {
                "required_fields": ["summary", "source_refs"],
                "allowed_source_refs": sorted(CHECKPOINT_FACT_SOURCE_REF_TYPES["known_failure_boundaries"]),
            },
            "human_decisions": {
                "required_fields": ["summary", "source_refs"],
                "allowed_source_refs": sorted(CHECKPOINT_FACT_SOURCE_REF_TYPES["human_decisions"]),
            },
            "next_strategy_constraints": {
                "required_fields": ["summary", "source_refs"],
                "allowed_source_refs": sorted(CHECKPOINT_FACT_SOURCE_REF_TYPES["next_strategy_constraints"]),
            },
        },
    }


def render_compaction_prompt(request: CompactionProviderRequest) -> dict[str, Any]:
    return {
        "stable_compaction_contract": {
            "db_is_authoritative": True,
            "checkpoint_is_non_authoritative_context": True,
            "output": "return exactly one JSON checkpoint candidate object",
            "must_include_provenance": True,
            "every_non_empty_fact_item_requires_non_empty_source_refs": True,
            "source_ref_values_must_be_copied_exactly_from_provenance_catalog": True,
            "inventing_source_refs_is_forbidden": True,
            "omit_a_fact_item_when_no_catalog_reference_exists": True,
            "must_not_output_graph_patch": True,
            "must_not_mark_unverified_as_satisfied": True,
            "old_segment_excluded_after_success": True,
        },
        "profile": _profile_public(request.profile),
        "budget": request.budget,
        "source_segment": request.source_segment,
        "db_state": request.db_state,
        "provenance_catalog": request.provenance_catalog,
        "checkpoint_fact_schema": request.checkpoint_fact_schema,
        "segment_entries": request.segment_entries,
        "provider_instruction": {
            "no_markdown": True,
            "no_explanatory_text": True,
            "checkpoint_required_fields": [
                "objective_summary",
                "goal_contract_revision",
                "satisfied_goal_items",
                "open_goal_gaps",
                "open_blockers",
                "graph_frontier",
                "metadata",
            ],
            "all_fact_lists_not_used_must_be_empty_arrays": True,
            "source_refs_example": [{"gap_key": "copy-exact-gap-key-from-provenance-catalog"}],
        },
    }


def render_compaction_messages(request: CompactionProviderRequest) -> tuple[list[dict[str, str]], dict[str, Any], dict[str, Any]]:
    rendered = render_compaction_prompt(request)
    system = (
        "You are the Hermes RuntimeCompactionProvider. You are not an execution agent. "
        "You may not call tools, web_search, write files, create Kanban tasks, write the database, "
        "or propose graph patches. Return exactly one JSON checkpoint candidate object. "
        "Every non-empty checkpoint fact item must include non-empty source_refs whose values are copied "
        "exactly from provenance_catalog; omit facts that have no catalog reference and never invent refs.\n\n"
        f"{request.profile['content']}"
    )
    user = json.dumps(rendered, ensure_ascii=False, sort_keys=True)
    return ([{"role": "system", "content": system}, {"role": "user", "content": user}], rendered, request.profile)


def _render_checkpoint_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)


def _job_compaction_policy(conn: sqlite3.Connection, job_id: str, policy: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    row = conn.execute("SELECT metadata_json FROM runtime_jobs WHERE id = ?", (job_id,)).fetchone()
    metadata = _loads(row["metadata_json"]) if row else {}
    session = latest_decision_session(conn, job_id)
    session_metadata = _loads(session.get("metadata_json"))
    merged = dict(DEFAULT_COMPACTION_POLICY)
    for source in (
        metadata.get("compaction_policy") or {},
        session_metadata.get("compaction_policy") or {},
        policy or {},
    ):
        normalized = dict(source)
        if "max_context_window_ratio" in normalized and "compaction_trigger_ratio" not in normalized:
            normalized["compaction_trigger_ratio"] = normalized["max_context_window_ratio"]
        merged.update(normalized)
    trigger_ratio = float(merged.get("compaction_trigger_ratio") or 0.65)
    input_ratio = float(merged.get("max_compaction_input_ratio") or 0.55)
    if not 0 < trigger_ratio < 1:
        raise ValueError("compaction_trigger_ratio must be between 0 and 1")
    if not 0 < input_ratio < trigger_ratio:
        raise ValueError("max_compaction_input_ratio must be positive and below compaction_trigger_ratio")
    merged["compaction_trigger_ratio"] = trigger_ratio
    merged["max_context_window_ratio"] = trigger_ratio
    merged["max_compaction_input_ratio"] = input_ratio
    return merged


def build_compaction_telemetry(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    delta: Optional[dict[str, Any]] = None,
    policy: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Return token/count telemetry consumed by compaction policy."""

    from hermes_cli import kanban_runtime_kernel as rk

    rk.ensure_runtime_schema(conn)
    resolved_policy = _job_compaction_policy(conn, job_id, policy)
    active = rk.ensure_decision_segment(conn, job_id)
    checkpoint = latest_decision_checkpoint(conn, job_id)
    tail = _short_tail_entries(
        conn,
        job_id,
        checkpoint,
        max_tail_entries=int(resolved_policy["max_tail_entries"]),
        max_tail_tokens=int(resolved_policy["max_tail_tokens"]),
    )
    checkpoint_payload = (checkpoint or {}).get("checkpoint") if checkpoint else None
    stable_prefix = {
        "patch_schema": PATCH_SCHEMA,
        "allowed_ops": sorted(PATCH_OPS),
        "forbidden_ops": ["release_node", "complete_job"],
        "db_is_authoritative": True,
    }
    stable_prefix_tokens = _estimate_tokens(stable_prefix)
    checkpoint_tokens = _estimate_tokens(checkpoint_payload or {})
    tail_tokens = sum(int(entry.get("estimated_tokens") or 0) for entry in tail)
    delta_tokens = _estimate_tokens(delta or {})
    model_output_tokens = conn.execute(
        """
        SELECT COALESCE(SUM(estimated_tokens), 0)
          FROM decision_segment_entries
         WHERE segment_id = ? AND entry_type = 'provider_output'
        """,
        (active["id"],),
    ).fetchone()[0]
    rejected_patch_count = conn.execute(
        """
        SELECT COUNT(*)
          FROM decision_segment_entries
         WHERE segment_id = ? AND entry_type = 'patch_rejected'
        """,
        (active["id"],),
    ).fetchone()[0]
    noop_count = 0
    for row in conn.execute(
        """
        SELECT payload_json FROM decision_segment_entries
         WHERE segment_id = ? AND entry_type IN ('validator_result', 'patch_applied')
        """,
        (active["id"],),
    ).fetchall():
        payload = _loads(row["payload_json"])
        if payload.get("status") == "noop" or payload.get("ops") == []:
            noop_count += 1
    segment_metrics = estimate_segment_tokens(conn, active["id"])
    active_segment_tokens = int(segment_metrics["active_segment_tokens"] or 0)
    cacheable_prefix_tokens = stable_prefix_tokens + checkpoint_tokens
    continuity_tokens = max(active_segment_tokens, tail_tokens)
    projected_context_tokens = cacheable_prefix_tokens + continuity_tokens + delta_tokens
    context_window = max(1, int(resolved_policy["context_window_tokens"]))
    trigger_ratio = float(resolved_policy["compaction_trigger_ratio"])
    reserve_tokens = sum(max(0, int(resolved_policy.get(key) or 0)) for key in (
        "reserved_output_tokens",
        "reserved_reasoning_tokens",
        "estimation_safety_tokens",
    ))
    ratio_trigger_tokens = max(1, int(context_window * trigger_ratio))
    reserve_trigger_tokens = max(1, context_window - reserve_tokens)
    trigger_tokens = min(ratio_trigger_tokens, reserve_trigger_tokens)
    return {
        "stable_prefix_tokens": stable_prefix_tokens,
        "checkpoint_tokens": checkpoint_tokens,
        "tail_tokens": tail_tokens,
        "delta_tokens": delta_tokens,
        "model_output_tokens": int(model_output_tokens or 0),
        "active_segment_tokens": active_segment_tokens,
        "continuity_tokens": continuity_tokens,
        "persisted_segment_tokens": int(segment_metrics["persisted_segment_tokens"] or 0),
        "audit_segment_tokens": int(segment_metrics["audit_segment_tokens"] or 0),
        "cacheable_prefix_tokens": cacheable_prefix_tokens,
        "projected_context_tokens": projected_context_tokens,
        "context_window_tokens": context_window,
        "compaction_trigger_tokens": trigger_tokens,
        "context_window_ratio": projected_context_tokens / context_window,
        "accepted_patch_count": int(
            conn.execute(
                "SELECT COUNT(*) FROM decision_segment_entries WHERE segment_id = ? AND entry_type = 'patch_applied'",
                (active["id"],),
            ).fetchone()[0]
        ),
        "rejected_patch_count": int(rejected_patch_count or 0),
        "noop_count": noop_count,
        "active_segment_id": active["id"],
        "latest_checkpoint_id": (checkpoint or {}).get("id"),
        "policy": resolved_policy,
    }


def should_compact_decision_session(
    conn: sqlite3.Connection,
    job_id: str,
    policy: Optional[dict[str, Any]] = None,
    *,
    delta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Evaluate configured compaction policy against current telemetry."""

    from hermes_cli import kanban_runtime_kernel as rk

    telemetry = build_compaction_telemetry(conn, job_id, delta=delta, policy=policy)
    resolved = telemetry["policy"]
    if resolved.get("mode") == "manual":
        return {"should_compact": False, "reason": "manual_mode", "profile_name": None, "telemetry": telemetry}
    fixed_threshold = resolved.get("max_active_segment_tokens")
    fixed_triggered = fixed_threshold is not None and int(fixed_threshold) > 0 and (
        telemetry["active_segment_tokens"] >= int(fixed_threshold)
    )
    ratio_triggered = telemetry["projected_context_tokens"] >= telemetry["compaction_trigger_tokens"]
    trigger_reason: Optional[str] = None
    profile_name: Optional[str] = None
    if fixed_triggered or ratio_triggered:
        trigger_reason = "token_threshold" if fixed_triggered else "context_window_ratio"
        profile_name = resolved.get("default_profile") or "token_budget_compaction"
    elif telemetry["rejected_patch_count"] >= int(resolved["rejected_patch_threshold"]):
        trigger_reason = "rejection_threshold"
        profile_name = "validator_boundary_compaction"
    elif telemetry["noop_count"] >= int(resolved["noop_threshold"]):
        trigger_reason = "noop_threshold"
        profile_name = "anti_stuck_compaction"
    if trigger_reason is None:
        return {"should_compact": False, "reason": "below_threshold", "profile_name": None, "telemetry": telemetry}

    active = rk.ensure_decision_segment(conn, job_id)
    fingerprint = _compaction_source_fingerprint(conn, job_id, active["id"])
    latest_rejection = conn.execute(
        """
        SELECT payload_json FROM decision_segment_entries
         WHERE segment_id = ? AND entry_type = 'checkpoint_rejected'
         ORDER BY id DESC LIMIT 1
        """,
        (active["id"],),
    ).fetchone()
    rejected_fingerprint = None
    if latest_rejection is not None:
        rejected_fingerprint = _loads(latest_rejection["payload_json"]).get("source_fingerprint")
    if rejected_fingerprint == fingerprint:
        return {
            "should_compact": False,
            "reason": "unchanged_after_rejection",
            "profile_name": None,
            "source_fingerprint": fingerprint,
            "telemetry": telemetry,
        }
    return {
        "should_compact": True,
        "reason": trigger_reason,
        "profile_name": profile_name,
        "source_fingerprint": fingerprint,
        "telemetry": telemetry,
    }


def _compaction_result_audit(result: CompactionProviderResult) -> dict[str, Any]:
    payload = result.to_dict()
    raw_output = payload.pop("raw_output", None)
    checkpoint = payload.pop("checkpoint", None)
    if raw_output is not None:
        raw_text = str(raw_output)
        payload["raw_output_ref"] = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        payload["raw_output_chars"] = len(raw_text)
    if checkpoint is not None:
        checkpoint_text = _json(checkpoint)
        payload["checkpoint_ref"] = hashlib.sha256(checkpoint_text.encode("utf-8")).hexdigest()
        payload["checkpoint_chars"] = len(checkpoint_text)
    return payload


def _record_local_compaction_rejection(
    conn: sqlite3.Connection,
    job_id: str,
    session: dict[str, Any],
    source_segment: dict[str, Any],
    *,
    profile_name: str,
    source_fingerprint: str,
    reason: str,
) -> dict[str, Any]:
    from hermes_cli import kanban_runtime_kernel as rk

    result = CompactionProviderResult(
        checkpoint=None,
        raw_output=None,
        provider_name="local_budget_guard",
        profile_name=profile_name,
        parse_status="input_budget_rejected",
        error=reason,
    )
    audit = _compaction_result_audit(result)
    rk.append_decision_segment_entry(
        conn,
        job_id,
        "compaction_provider_output",
        audit,
        ref_type="decision_session_segment",
        ref_id=source_segment["id"],
    )
    validation = {"status": "rejected", "reason": reason}
    rk.append_decision_segment_entry(
        conn,
        job_id,
        "checkpoint_rejected",
        {
            "validation": validation,
            "provider_validation": validation,
            "provider_result": audit,
            "profile_name": profile_name,
            "source_fingerprint": source_fingerprint,
        },
        ref_type="decision_session_segment",
        ref_id=source_segment["id"],
    )
    now = _now()
    conn.execute(
        """
        UPDATE decision_sessions
           SET last_compaction_status = 'rejected', last_compaction_profile = ?,
               last_compaction_at = ?, updated_at = ?
         WHERE id = ?
        """,
        (profile_name, now, now, session["id"]),
    )
    health = _sync_compaction_health_events(conn, job_id)
    return {
        "status": "rejected",
        "reason": reason,
        "source_segment_id": source_segment["id"],
        "profile_name": profile_name,
        "active_segment_preserved": True,
        "provider_result": audit,
        "provider_validation": validation,
        "fallback_used": False,
        "provider_called": False,
        "source_fingerprint": source_fingerprint,
        "compaction_health": health,
    }


def compact_decision_session(
    conn: sqlite3.Connection,
    job_id: str,
    profile_name: str = "token_budget_compaction",
    reason: str = "manual",
    *,
    compaction_provider: Any = None,
    fallback_to_deterministic: bool = True,
    budget: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Compact the active decision segment through a provider-shaped boundary.

    Rejected provider output is recorded but does not close or poison the active
    segment.  A successful checkpoint is the only path that replaces the active
    segment and excludes the old transcript from future provider input.
    """

    from hermes_cli import kanban_runtime_kernel as rk

    rk.ensure_runtime_schema(conn)
    source_segment = rk.ensure_decision_segment(conn, job_id)
    session = latest_decision_session(conn, job_id)
    provider_mode = "deterministic" if compaction_provider is None else getattr(compaction_provider, "provider_name", "custom")
    source_fingerprint = _compaction_source_fingerprint(conn, job_id, source_segment["id"])
    request_entry = rk.append_decision_segment_entry(
        conn,
        job_id,
        "compaction_requested",
        {
            "profile_name": profile_name,
            "reason": reason,
            "source_segment_id": source_segment["id"],
            "provider_mode": provider_mode,
            "fallback_to_deterministic": bool(fallback_to_deterministic),
            "source_fingerprint": source_fingerprint,
        },
        ref_type="decision_session_segment",
        ref_id=source_segment["id"],
    )
    try:
        request = build_compaction_provider_request(
            conn,
            job_id,
            source_segment=source_segment,
            profile_name=profile_name,
            budget=budget,
        )
    except CompactionInputBudgetError as exc:
        return _record_local_compaction_rejection(
            conn,
            job_id,
            session,
            source_segment,
            profile_name=profile_name,
            source_fingerprint=source_fingerprint,
            reason=str(exc),
        )
    messages, rendered, request_profile = render_compaction_messages(request)
    rendered_text = _json(rendered)
    request_ref = hashlib.sha256(rendered_text.encode("utf-8")).hexdigest()
    provider_input_entry = rk.append_decision_segment_entry(
        conn,
        job_id,
        "compaction_provider_input",
        {
            "request_ref": request_ref,
            "source_fingerprint": request.budget.get("source_fingerprint"),
            "source_segment_id": source_segment["id"],
            "profile": _profile_public(request_profile),
            "provider_name": provider_mode,
            "model": getattr(compaction_provider, "model", None),
            "included_entry_ids": request.budget.get("included_entry_ids") or [],
            "included_entry_count": request.budget.get("included_entry_count", 0),
            "omitted_entry_count": request.budget.get("omitted_entry_count", 0),
            "rendered_input_tokens": request.budget.get("rendered_input_tokens"),
            "rendered_input_chars": request.budget.get("rendered_input_chars"),
            "max_compaction_input_tokens": request.budget.get("max_compaction_input_tokens"),
            "max_compaction_input_chars": request.budget.get("max_compaction_input_chars"),
            "no_tools": True,
            "single_shot": True,
        },
        ref_type="decision_session_segment",
        ref_id=source_segment["id"],
    )

    provider = compaction_provider or DeterministicCompactionProvider(conn)
    try:
        provider_result = provider.compact(request)
        if not isinstance(provider_result, CompactionProviderResult):
            raise ProviderPatchParseError("compaction provider must return CompactionProviderResult")
    except Exception as exc:
        provider_result = CompactionProviderResult(
            checkpoint=None,
            raw_output=None,
            provider_name=getattr(provider, "provider_name", "custom"),
            model=getattr(provider, "model", None),
            profile_name=request.profile["profile_name"],
            profile_version=request.profile["profile_version"],
            profile_hash=request.profile["profile_hash"],
            request_ref=hashlib.sha256(json.dumps(rendered, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
            parse_status="provider_error",
            input_token_estimate=estimate_decision_input_tokens(rendered, request_profile["content"]),
            error=str(exc),
        )

    provider_result_audit = _compaction_result_audit(provider_result)
    rk.append_decision_segment_entry(
        conn,
        job_id,
        "compaction_provider_output",
        provider_result_audit,
        ref_type="decision_session_segment",
        ref_id=source_segment["id"],
    )

    payload = provider_result.checkpoint
    validation = (
        validate_decision_checkpoint(conn, job_id, payload)
        if payload is not None
        else {"status": "rejected", "reason": provider_result.error or provider_result.parse_status}
    )
    fallback_used = False
    provider_validation = dict(validation)
    if validation["status"] != "accepted" and fallback_to_deterministic:
        rk.append_decision_segment_entry(
            conn,
            job_id,
            "compaction_fallback",
            {
                "reason": validation["reason"],
                "provider_result": provider_result_audit,
                "fallback_provider": "deterministic",
            },
            ref_type="decision_session_segment",
            ref_id=source_segment["id"],
        )
        fallback_result = DeterministicCompactionProvider(conn).compact(request)
        payload = fallback_result.checkpoint
        validation = validate_decision_checkpoint(conn, job_id, payload) if payload is not None else {
            "status": "rejected",
            "reason": fallback_result.error or fallback_result.parse_status,
        }
        provider_result = CompactionProviderResult(
            checkpoint=payload,
            raw_output=fallback_result.raw_output,
            provider_name=fallback_result.provider_name,
            model=fallback_result.model,
            profile_name=fallback_result.profile_name,
            profile_version=fallback_result.profile_version,
            profile_hash=fallback_result.profile_hash,
            request_ref=fallback_result.request_ref,
            response_ref=fallback_result.response_ref,
            parse_status=fallback_result.parse_status,
            retry_count=fallback_result.retry_count,
            provider_latency_ms=fallback_result.provider_latency_ms,
            input_token_estimate=fallback_result.input_token_estimate,
            output_token_estimate=fallback_result.output_token_estimate,
            error=fallback_result.error,
            fallback_used=True,
        )
        fallback_used = True
        provider_result_audit = _compaction_result_audit(provider_result)
        rk.append_decision_segment_entry(
            conn,
            job_id,
            "compaction_provider_output",
            provider_result_audit,
            ref_type="decision_session_segment",
            ref_id=source_segment["id"],
        )

    if payload is None or validation["status"] != "accepted":
        rk.append_decision_segment_entry(
            conn,
            job_id,
            "checkpoint_rejected",
            {
                "validation": validation,
                "provider_validation": provider_validation,
                "provider_result": provider_result_audit,
                "profile_name": profile_name,
                "source_fingerprint": request.budget.get("source_fingerprint") or source_fingerprint,
            },
            ref_type="decision_session_segment",
            ref_id=source_segment["id"],
        )
        conn.execute(
            """
            UPDATE decision_sessions
               SET last_compaction_status = 'rejected', last_compaction_profile = ?,
                   last_compaction_at = ?, updated_at = ?
             WHERE id = ?
            """,
            (profile_name, _now(), _now(), session["id"]),
        )
        health = _sync_compaction_health_events(conn, job_id)
        return {
            "status": "rejected",
            "reason": validation["reason"],
            "source_segment_id": source_segment["id"],
            "profile_name": profile_name,
            "active_segment_preserved": True,
            "provider_result": provider_result_audit,
            "provider_validation": provider_validation,
            "fallback_used": fallback_used,
            "provider_called": True,
            "source_fingerprint": request.budget.get("source_fingerprint") or source_fingerprint,
            "compaction_health": health,
        }

    profile = _profile_metadata(profile_name)
    ranges = _segment_range(conn, source_segment["id"])
    checkpoint_id = _id("dchk")
    now = _now()
    revision = int(payload["metadata"]["graph_revision"])
    supersedes = session.get("latest_checkpoint_id")
    provider_audit = {
        "reason": reason,
        "request_entry_id": request_entry["id"],
        "provider_input_entry_id": provider_input_entry["id"],
        "provider_name": provider_result.provider_name,
        "provider_model": provider_result.model,
        "request_ref": provider_result.request_ref,
        "response_ref": provider_result.response_ref,
        "parse_status": provider_result.parse_status,
        "retry_count": provider_result.retry_count,
        "provider_latency_ms": provider_result.provider_latency_ms,
        "input_token_estimate": provider_result.input_token_estimate,
        "output_token_estimate": provider_result.output_token_estimate,
        "fallback_used": fallback_used or provider_result.fallback_used,
        "provider_validation": provider_validation,
    }
    payload.setdefault("metadata", {})
    payload["metadata"].update(
        {
            "provider_audit": provider_audit,
            "profile_name": profile["profile_name"],
            "profile_version": profile["profile_version"],
            "profile_hash": profile["profile_hash"],
            "profile_path": profile["profile_path"],
        }
    )
    conn.execute(
        """
        INSERT INTO decision_checkpoints (
            id, job_id, decision_session_id, revision, checkpoint_json, reason,
            transcript_ref, created_at, source_segment_id, profile_name,
            profile_version, profile_hash, profile_path, checkpoint_revision,
            db_revision, graph_revision, ledger_revision, covered_event_start,
            covered_event_end, covered_decision_start, covered_decision_end,
            covered_entry_start, covered_entry_end, payload_json, payload_text,
            validator_status, reject_reason, supersedes_checkpoint_id, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
        """,
        (
            checkpoint_id,
            job_id,
            session["id"],
            revision,
            _json(payload),
            reason,
            f"decision_segment:{source_segment['id']}",
            now,
            source_segment["id"],
            profile["profile_name"],
            profile["profile_version"],
            profile["profile_hash"],
            profile["profile_path"],
            revision,
            revision,
            revision,
            revision,
            ranges["covered_event_start"],
            ranges["covered_event_end"],
            ranges["covered_decision_start"],
            ranges["covered_decision_end"],
            ranges["covered_entry_start"],
            ranges["covered_entry_end"],
            _json(payload),
            _render_checkpoint_text(payload),
            "accepted",
            supersedes,
            _json(provider_audit),
        ),
    )
    rk.append_decision_segment_entry(
        conn,
        job_id,
        "checkpoint_created",
        {
            "checkpoint_id": checkpoint_id,
            "validation": validation,
            "provider_audit": provider_audit,
            **profile,
        },
        ref_type="decision_checkpoint",
        ref_id=checkpoint_id,
    )
    conn.execute(
        """
        UPDATE decision_session_segments
           SET state = 'compacted', closed_at = ?, covered_graph_revision_end = ?,
               compacted_checkpoint_id = ?, archive_ref = ?
         WHERE id = ?
        """,
        (now, revision, checkpoint_id, f"decision_segment:{source_segment['id']}", source_segment["id"]),
    )
    next_index = int(source_segment["segment_index"]) + 1
    new_segment_id = _id("dseg")
    conn.execute(
        """
        INSERT INTO decision_session_segments (
            id, job_id, decision_session_id, segment_index, state,
            started_at, covered_graph_revision_start, metadata_json
        ) VALUES (?, ?, ?, ?, 'active', ?, ?, '{}')
        """,
        (new_segment_id, job_id, session["id"], next_index, now, revision),
    )
    context_state = _loads(session.get("context_state_json"))
    context_state.update(
        {
            "active_segment_id": new_segment_id,
            "latest_checkpoint_id": checkpoint_id,
            "last_compaction_status": "accepted",
            "last_compaction_profile": profile_name,
        }
    )
    conn.execute(
        """
        UPDATE decision_sessions
           SET active_segment_id = ?, latest_checkpoint_id = ?,
               last_checkpoint_revision = ?, last_compaction_at = ?,
               last_compaction_status = 'accepted', last_compaction_profile = ?,
               context_state_json = ?, updated_at = ?
         WHERE id = ?
        """,
        (new_segment_id, checkpoint_id, revision, now, profile_name, _json(context_state), now, session["id"]),
    )
    rk.append_decision_segment_entry(
        conn,
        job_id,
        "compaction_event",
        {"status": "new_segment_started", "checkpoint_id": checkpoint_id, "source_segment_id": source_segment["id"]},
        ref_type="decision_checkpoint",
        ref_id=checkpoint_id,
    )
    health = _sync_compaction_health_events(conn, job_id)
    return {
        "status": "compacted",
        "job_id": job_id,
        "source_segment_id": source_segment["id"],
        "new_segment_id": new_segment_id,
        "checkpoint_id": checkpoint_id,
        "checkpoint_revision": revision,
        "profile_name": profile["profile_name"],
        "profile_hash": profile["profile_hash"],
        "covered_entry_end": ranges["covered_entry_end"],
        "provider_name": provider_result.provider_name,
        "provider_model": provider_result.model,
        "request_ref": provider_result.request_ref,
        "response_ref": provider_result.response_ref,
        "parse_status": provider_result.parse_status,
        "provider_validation": provider_validation,
        "fallback_used": fallback_used or provider_result.fallback_used,
        "compaction_health": health,
    }


def summarize_compaction_health(
    conn: sqlite3.Connection,
    job_id: str,
    policy: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Derive provider compaction health from persisted decision entries."""

    resolved_policy = _job_compaction_policy(conn, job_id, policy)
    threshold = max(1, int(resolved_policy.get("fallback_degraded_threshold") or 2))
    rows = conn.execute(
        """
        SELECT id, entry_type, payload_json
          FROM decision_segment_entries
         WHERE job_id = ?
           AND entry_type IN (
               'compaction_requested', 'compaction_provider_output', 'compaction_fallback',
               'checkpoint_created', 'checkpoint_rejected'
           )
         ORDER BY id
        """,
        (job_id,),
    ).fetchall()
    attempts: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    for row in rows:
        entry_type = row["entry_type"]
        payload = _loads(row["payload_json"])
        if entry_type == "compaction_requested":
            current = {
                "entry_id": int(row["id"]),
                "provider_mode": payload.get("provider_mode") or "unknown",
                "profile_name": payload.get("profile_name"),
                "fallback_used": False,
                "parse_status": None,
                "provider_validation": None,
                "outcome": "pending",
            }
            attempts.append(current)
            continue
        if current is None:
            continue
        if entry_type == "compaction_provider_output":
            provider_name = payload.get("provider_name")
            if provider_name != "deterministic" and current.get("parse_status") is None:
                current["parse_status"] = payload.get("parse_status")
        elif entry_type == "compaction_fallback":
            current["fallback_used"] = True
            provider_result = payload.get("provider_result") or {}
            current["parse_status"] = current.get("parse_status") or provider_result.get("parse_status")
            current["provider_validation"] = {"status": "rejected", "reason": payload.get("reason")}
        elif entry_type == "checkpoint_created":
            audit = payload.get("provider_audit") or {}
            current["fallback_used"] = bool(audit.get("fallback_used") or current.get("fallback_used"))
            current["parse_status"] = current.get("parse_status") or audit.get("parse_status")
            current["provider_validation"] = audit.get("provider_validation")
            current["outcome"] = "fallback" if current["fallback_used"] else "accepted"
            current["checkpoint_id"] = payload.get("checkpoint_id")
            current = None
        elif entry_type == "checkpoint_rejected":
            provider_result = payload.get("provider_result") or {}
            current["parse_status"] = current.get("parse_status") or provider_result.get("parse_status")
            current["provider_validation"] = payload.get("provider_validation") or payload.get("validation")
            current["outcome"] = "rejected"
            current = None

    provider_attempts = [item for item in attempts if item.get("provider_mode") != "deterministic"]
    fallback_count = sum(item.get("outcome") == "fallback" for item in provider_attempts)
    success_count = sum(item.get("outcome") == "accepted" for item in provider_attempts)
    rejection_count = sum(
        item.get("outcome") == "rejected"
        or (item.get("provider_validation") or {}).get("status") == "rejected"
        for item in provider_attempts
    )
    provider_error_count = sum(item.get("parse_status") == "provider_error" for item in provider_attempts)
    fallback_streak = 0
    for item in provider_attempts:
        if item.get("outcome") == "fallback":
            fallback_streak += 1
        elif item.get("outcome") == "accepted":
            fallback_streak = 0
    last_result = provider_attempts[-1].get("outcome") if provider_attempts else "not_run"
    if last_result in {"rejected", "pending"}:
        status = "unavailable"
    elif fallback_streak >= threshold:
        status = "degraded"
    else:
        status = "healthy"
    return {
        "status": status,
        "provider_attempt_count": len(provider_attempts),
        "provider_success_count": success_count,
        "fallback_count": fallback_count,
        "fallback_streak": fallback_streak,
        "rejection_count": rejection_count,
        "provider_error_count": provider_error_count,
        "last_result": last_result,
        "degraded_threshold": threshold,
        "operator_attention_required": status in {"degraded", "unavailable"},
        "recent_attempts": provider_attempts[-10:],
    }


def _sync_compaction_health_events(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    from hermes_cli import kanban_runtime_kernel as rk

    health = summarize_compaction_health(conn, job_id)
    latest = conn.execute(
        """
        SELECT event_type, payload_json
          FROM execution_events
         WHERE job_id = ?
           AND event_type IN ('compaction_quality_degraded', 'compaction_quality_recovered')
         ORDER BY id DESC LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    latest_type = latest["event_type"] if latest else None
    payload = {
        key: health[key]
        for key in (
            "status",
            "provider_attempt_count",
            "provider_success_count",
            "fallback_count",
            "fallback_streak",
            "last_result",
            "degraded_threshold",
            "operator_attention_required",
        )
    }
    if health["status"] == "degraded" and latest_type != "compaction_quality_degraded":
        rk._event(conn, job_id, "compaction_quality_degraded", payload, source="runtime_compaction")
    elif health["status"] == "healthy" and latest_type == "compaction_quality_degraded":
        rk._event(conn, job_id, "compaction_quality_recovered", payload, source="runtime_compaction")
    return health


def _short_tail_entries(
    conn: sqlite3.Connection,
    job_id: str,
    checkpoint_row: Optional[dict[str, Any]],
    *,
    max_tail_entries: int = 8,
    max_tail_tokens: int = 2000,
) -> list[dict[str, Any]]:
    if checkpoint_row is None:
        return []
    covered_end = checkpoint_row.get("covered_entry_end")
    if covered_end is None:
        return []
    placeholders, excluded = _entry_type_exclusion_sql(COMPACTION_NON_CONTEXT_ENTRY_TYPES)
    rows = conn.execute(
        f"""
        SELECT id, segment_id, entry_index, entry_type, ref_type, ref_id,
               decision_id, event_id, patch_id, graph_revision, payload_json,
               payload_text, estimated_tokens, created_at
          FROM decision_segment_entries
         WHERE job_id = ? AND id > ? AND entry_type NOT IN ({placeholders})
         ORDER BY id ASC
        """,
        (job_id, int(covered_end), *excluded),
    ).fetchall()
    tail: list[dict[str, Any]] = []
    tokens = 0
    for row in rows:
        if len(tail) >= max_tail_entries:
            break
        estimated = int(row["estimated_tokens"] or 0)
        if tokens + estimated > max_tail_tokens:
            continue
        data = _row(row) or {}
        data["payload"] = _loads(data.get("payload_json"))
        tail.append(data)
        tokens += estimated
    return tail


def decision_context_status(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    from hermes_cli import kanban_runtime_kernel as rk

    rk.ensure_runtime_schema(conn)
    active = rk.ensure_decision_segment(conn, job_id)
    latest_checkpoint = latest_decision_checkpoint(conn, job_id)
    checkpoint, context_validation = select_decision_context_checkpoint(conn, job_id, emit_event=False)
    policy_result = should_compact_decision_session(conn, job_id)
    return {
        "job_id": job_id,
        "active_segment": _row(
            conn.execute("SELECT * FROM decision_session_segments WHERE id = ?", (active["id"],)).fetchone()
        ),
        "latest_checkpoint": latest_checkpoint,
        "selected_checkpoint": checkpoint,
        "context_chain_validation": context_validation,
        "active_segment_tokens": int(active.get("active_segment_tokens") or 0),
        "compaction_policy": policy_result,
        "compaction_health": summarize_compaction_health(conn, job_id),
        "provider_input_composition": [
            "stable_runtime_contract",
            "current_goal_contract",
            "latest_validated_checkpoint",
            "strict_short_tail",
            "current_delta",
        ],
        "short_tail": _short_tail_entries(conn, job_id, checkpoint),
    }


def _query_rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [_row(row) or {} for row in conn.execute(sql, params).fetchall()]


def runtime_observability_snapshot(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    limit: int = 50,
) -> dict[str, Any]:
    """Return a bounded JSON surface for dashboard/operator inspection."""

    from hermes_cli import kanban_runtime_kernel as rk
    from hermes_cli import kanban_runtime_memory as rm

    rk.ensure_runtime_schema(conn)
    bounded = max(1, min(int(limit), 200))
    status = rk.status_runtime_job(conn, job_id)
    context = decision_context_status(conn, job_id)
    legal_waiting_reason = rk.runtime_legal_waiting_reason(conn, job_id)
    recovery = rk.summarize_runtime_recovery(conn, job_id, limit=bounded)
    consistency = rk.check_runtime_consistency(conn, job_id, write_events=False)
    decisions = _query_rows(
        conn,
        """
        SELECT id, job_id, db_revision, decision_session_id, delta_json,
               decision_json, model, status, validator_result_json, error,
               created_at, completed_at
          FROM kernel_decisions
         WHERE job_id = ?
         ORDER BY created_at DESC, id DESC
         LIMIT ?
        """,
        (job_id, bounded),
    )
    for decision in decisions:
        decision["delta"] = _loads(decision.get("delta_json"))
        decision["decision"] = _loads(decision.get("decision_json"))
        decision["validator_result"] = _loads(decision.get("validator_result_json"))
        if isinstance(decision["decision"], dict):
            provider_fields = {
                key: decision["decision"].get(key)
                for key in (
                    "provider_name",
                    "model",
                    "profile_name",
                    "profile_hash",
                    "request_ref",
                    "response_ref",
                    "parse_status",
                    "retry_count",
                    "provider_latency_ms",
                )
                if decision["decision"].get(key) is not None
            }
            decision["provider_audit"] = provider_fields
    patches = _query_rows(
        conn,
        """
        SELECT id, job_id, decision_id, base_revision, patch_json,
               status, reject_reason, created_at, applied_at
          FROM graph_patches
         WHERE job_id = ?
         ORDER BY created_at DESC, id DESC
         LIMIT ?
        """,
        (job_id, bounded),
    )
    for patch in patches:
        patch["patch"] = _loads(patch.get("patch_json"))
        patch["normalized_patch"] = patch["patch"]
    checkpoints = _query_rows(
        conn,
        """
        SELECT id, job_id, source_segment_id, profile_name, profile_version,
               profile_hash, profile_path, checkpoint_revision, db_revision,
               graph_revision, ledger_revision, validator_status, reject_reason,
               covered_entry_start, covered_entry_end, metadata_json, created_at,
               supersedes_checkpoint_id
         FROM decision_checkpoints
         WHERE job_id = ?
         ORDER BY created_at DESC, rowid DESC
         LIMIT ?
        """,
        (job_id, bounded),
    )
    for checkpoint in checkpoints:
        checkpoint["metadata"] = _loads(checkpoint.get("metadata_json"))
    entries = _query_rows(
        conn,
        """
        SELECT id, segment_id, entry_index, entry_type, ref_type, ref_id,
               decision_id, event_id, patch_id, graph_revision,
               estimated_tokens, created_at
          FROM decision_segment_entries
         WHERE job_id = ?
         ORDER BY id DESC
         LIMIT ?
        """,
        (job_id, bounded),
    )
    human_gates = [
        node
        for node in status["nodes"]
        if node.get("node_type") == "human_gate" or node.get("state") == "waiting_human"
    ]
    compaction_entries = [
        entry
        for entry in entries
        if str(entry.get("entry_type") or "").startswith("compaction")
        or entry.get("entry_type") in {"checkpoint_created", "checkpoint_rejected"}
    ]
    return {
        "job": status["job"],
        "legal_waiting_reason": legal_waiting_reason,
        "recovery": recovery,
        "worker_execution_continuity": rk.summarize_worker_execution_continuity(
            conn, job_id, limit=bounded
        ),
        "capabilities": status.get("capabilities") or rk.summarize_runtime_capabilities(conn, job_id, limit=bounded),
        "orchestration": status.get("orchestration")
        or rk.summarize_runtime_orchestration(conn, job_id),
        "memory": rm.summarize_runtime_memory(conn, job_id, limit=bounded),
        "consistency": {
            "status": consistency["status"],
            "violation_count": consistency["violation_count"],
            "warning_count": consistency["warning_count"],
            "warnings": consistency["warnings"],
            "violations": consistency["violations"],
        },
        "goals": {
            "contract": status["goal_contract"],
            "items": status["goal_items"],
            "gaps": status["goal_gaps"],
            "ledger_summary": status["ledger_summary"],
        },
        "progress_ledger": status["progress_ledger"],
        "graph": {
            "nodes": status["nodes"],
            "dependencies": status["dependencies"],
            "relations": status["relations"],
            "materializations": status["materializations"],
            "backend_worker_sessions": status.get("backend_worker_sessions") or [],
            "frontier": status["frontier_summary"],
        },
        "events": status["recent_events"][:bounded],
        "patches": patches,
        "decisions": decisions,
        "decision_session": context,
        "decision_segment_entries": entries,
        "checkpoints": checkpoints,
        "compactions": {
            "latest_status": context["active_segment"].get("state"),
            "policy": context["compaction_policy"],
            "health": context["compaction_health"],
            "context_chain_validation": context["context_chain_validation"],
            "entries": compaction_entries,
            "checkpoints": checkpoints,
        },
        "human_gates": human_gates,
        "liveness": status["liveness"],
        "operator_actions": {
            "read_only": True,
            "allowed_commands": [
                "runtime advance",
                "runtime reconcile",
                "runtime consistency",
                "runtime compact",
                "runtime waive-goal",
                "runtime complete-node",
            ],
        },
    }
    # Lanes describe available execution backends, not runtime fact or a
    # permission grant. The provider still only proposes an assignee; the
    # materialization/capability path remains authoritative.
    try:
        from hermes_cli.worker_lanes import list_worker_lanes

        lanes = [
            {
                "name": lane.name,
                "kind": lane.kind,
                "description": lane.description,
                "max_concurrency": lane.max_concurrency,
            }
            for lane in list_worker_lanes()
        ]
        if lanes:
            stable_prefix["available_execution_backends"] = {
                "worker_lanes": lanes,
                "instruction": "Assign a node only to a listed lane when it needs worker execution.",
            }
    except Exception:
        pass


def build_decision_provider_request(
    conn: sqlite3.Connection,
    job_id: str,
    delta: dict[str, Any],
) -> DecisionProviderRequest:
    from hermes_cli import kanban_runtime_kernel as rk
    from hermes_cli import kanban_runtime_memory as rm

    rk.ensure_decision_segment(conn, job_id)
    session = latest_decision_session(conn, job_id)
    checkpoint_row, _context_validation = select_decision_context_checkpoint(conn, job_id, emit_event=True)
    if checkpoint_row is None:
        checkpoint = build_checkpoint_payload(conn, job_id)
        short_tail: list[dict[str, Any]] = []
    else:
        checkpoint = checkpoint_row["checkpoint"]
        short_tail = _short_tail_entries(conn, job_id, checkpoint_row)
    authoritative_goal_contract = build_checkpoint_payload(conn, job_id)["goal_contract"]
    job = _row(conn.execute("SELECT * FROM runtime_jobs WHERE id = ?", (job_id,)).fetchone())
    if job is None:
        raise ValueError(f"unknown runtime job {job_id}")
    stable_prefix = {
        "runtime_contract": {
            "patch_schema": PATCH_SCHEMA,
            "allowed_ops": sorted(PATCH_OPS),
            "forbidden_ops": ["release_node", "complete_job"],
            "db_is_authoritative": True,
            "decision_session_is_non_authoritative": True,
            "provider_may_only_return_patch_json": True,
            "node_type_is_not_phase": True,
        },
        "validator_hard_constraints": {
            "expected_revision_required": True,
            "new_node_requires_goal_gap_or_human_linkage": True,
            "no_direct_db_writes": True,
        },
        "delegation_policy": {
            "role": "structure_upgrade_controller_not_task_decomposer",
            "minimum_runtime_nodes": True,
            "prefer_one_coherent_primary_node": True,
            "primary_node_may_include": [
                "inspection",
                "research",
                "planning",
                "implementation",
                "testing",
                "debugging",
                "local_verification",
            ],
            "forbidden_split_reasons": [
                "different_phase",
                "different_role",
                "cleaner_plan",
                "task_is_complex",
                "could_be_parallelized",
            ],
            "allowed_decomposition_reasons": [
                "independent_verification",
                "capability_boundary",
                "human_authority_boundary",
                "workspace_isolation",
                "durable_parallelism",
                "context_or_runtime_limit",
                "execution_discovered_gap",
            ],
            "without_decomposition_max_new_runnable_worker_nodes": 1,
        },
    }
    orchestration_policy = _loads(job.get("metadata_json")).get(
        "orchestration_policy"
    )
    if isinstance(orchestration_policy, dict):
        stable_prefix["runtime_orchestration_policy"] = {
            key: orchestration_policy.get(key)
            for key in (
                "schema",
                "mode",
                "enabled",
                "worker_lane",
                "max_child_nodes",
                "required_child_capabilities",
                "require_contribution_attribution",
            )
            if orchestration_policy.get(key) is not None
        }
    memory = rm.select_runtime_memory_hints(conn, job_id, delta)
    if memory.get("guidance", {}).get("loaded"):
        stable_prefix["runtime_guidance"] = {
            "sources": memory["guidance"]["sources"],
            "content": memory["guidance"]["content"],
        }
    return DecisionProviderRequest(
        job_id=job_id,
        db_revision=int(job["graph_revision"]),
        session=session,
        stable_prefix=stable_prefix,
        goal_contract=authoritative_goal_contract,
        checkpoint=checkpoint,
        memory={
            "non_authoritative_notice": memory["non_authoritative_notice"],
            "selected_hints": memory["selected_hints"],
            "budget": memory["budget"],
            "index": memory["index"],
            "topic_reads": memory["topic_reads"],
        },
        short_tail=short_tail,
        delta=delta,
    )


def render_decision_prompt(request: DecisionProviderRequest) -> dict[str, Any]:
    """Render a deterministic, cache-friendly provider input object."""

    return {
        "stable_prefix": request.stable_prefix,
        "stable_goal_contract": request.goal_contract,
        "checkpoint": request.checkpoint,
        "memory": request.memory,
        "short_tail": request.short_tail,
        "delta": request.delta,
        "provider_instruction": {
            "output": "return exactly one JSON object matching runtime_graph_patch_v1",
            "no_markdown": True,
            "no_explanatory_text": True,
        },
    }


def render_decision_messages(
    request: DecisionProviderRequest,
    *,
    profile_name: str = "graph_patch_decision",
    validator_feedback: Optional[dict[str, Any]] = None,
) -> tuple[list[dict[str, str]], dict[str, Any], dict[str, Any]]:
    """Render no-tools, single-shot messages for a runtime decision provider."""

    profile = load_decision_profile(profile_name)
    rendered = render_decision_prompt(request)
    if validator_feedback:
        rendered = dict(rendered)
        rendered["validator_feedback"] = validator_feedback
    system = (
        "You are the Hermes RuntimeDecisionProvider. "
        "You are not an execution agent. You may not call tools, web_search, "
        "write files, create Kanban tasks, write the database, or mark work done. "
        "Return exactly one JSON object matching the runtime graph patch schema.\n\n"
        f"{profile['content']}"
    )
    user = json.dumps(rendered, ensure_ascii=False, sort_keys=True)
    return (
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        rendered,
        profile,
    )


def _extract_response_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(message, dict):
                return str(message.get("content") or "")
        return str(response.get("content") or response.get("text") or "")
    try:
        from agent.auxiliary_client import extract_content_or_reasoning

        text = extract_content_or_reasoning(response)
        if text:
            return text
    except Exception:
        pass
    try:
        return str(response.choices[0].message.content or "")
    except Exception:
        return ""


class RuntimeDecisionProvider:
    """No-tools, single-shot decision provider adapter over Hermes model substrate."""

    def __init__(
        self,
        *,
        provider_name: str,
        model: str,
        profile_name: str = "graph_patch_decision",
        client: Any = None,
        client_factory: Optional[Callable[[], tuple[Any, str]]] = None,
        max_retries: int = 1,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        timeout_seconds: Optional[float] = None,
        reasoning_effort: Optional[str] = None,
        explicit_api_key: Optional[str] = None,
        explicit_base_url: Optional[str] = None,
    ) -> None:
        if not provider_name:
            raise ValueError("provider_name is required")
        if not model:
            raise ValueError("model is required")
        self.provider_name = provider_name
        self.model = model
        self.profile_name = profile_name
        self.client = client
        self.client_factory = client_factory
        self.max_retries = max(0, int(max_retries))
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.timeout_seconds = timeout_seconds
        self.reasoning_effort = str(reasoning_effort).strip() if reasoning_effort else None
        self.explicit_api_key = explicit_api_key
        self.explicit_base_url = explicit_base_url

    def _client_and_model(self) -> tuple[Any, str]:
        if self.client is not None:
            return self.client, self.model
        if self.client_factory is not None:
            client, resolved_model = self.client_factory()
            return client, resolved_model or self.model

        from agent.auxiliary_client import resolve_provider_client
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(
            requested=self.provider_name,
            explicit_api_key=self.explicit_api_key,
            explicit_base_url=self.explicit_base_url,
            target_model=self.model,
        )
        client, resolved_model = resolve_provider_client(
            runtime.get("provider") or self.provider_name,
            self.model,
            explicit_base_url=runtime.get("base_url"),
            explicit_api_key=runtime.get("api_key"),
            api_mode=runtime.get("api_mode"),
            main_runtime=runtime,
        )
        if client is None:
            raise RuntimeError(f"could not resolve model client for provider {self.provider_name!r}")
        return client, resolved_model or self.model

    def _call_model(self, client: Any, model: str, messages: list[dict[str, str]]) -> Any:
        if hasattr(client, "with_options"):
            client = client.with_options(max_retries=0)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.timeout_seconds is not None:
            kwargs["timeout"] = self.timeout_seconds
        if self.reasoning_effort is not None:
            kwargs["reasoning_effort"] = self.reasoning_effort
        # Intentionally no tools/tool_choice/web_search arguments here.
        return client.chat.completions.create(**kwargs)

    def decide(self, request: DecisionProviderRequest) -> DecisionProviderResult:
        messages, rendered, profile = render_decision_messages(request, profile_name=self.profile_name)
        return self._decide_from_messages(request, messages, rendered, profile)

    def decide_with_validator_feedback(
        self,
        request: DecisionProviderRequest,
        *,
        rejected_patch: dict[str, Any],
        validation: dict[str, Any],
        profile_name: str = "validator_recovery_decision",
    ) -> DecisionProviderResult:
        feedback = {
            "rejected_patch": rejected_patch,
            "validation": validation,
            "instruction": (
                "Return a corrected runtime_graph_patch_v1 JSON object that satisfies the validator. "
                "Do not explain. Do not repeat the same invalid structure."
            ),
        }
        messages, rendered, profile = render_decision_messages(
            request,
            profile_name=profile_name,
            validator_feedback=feedback,
        )
        return self._decide_from_messages(request, messages, rendered, profile)

    def _decide_from_messages(
        self,
        request: DecisionProviderRequest,
        messages: list[dict[str, str]],
        rendered: dict[str, Any],
        profile: dict[str, Any],
    ) -> DecisionProviderResult:
        request_ref = hashlib.sha256(json.dumps(rendered, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        input_tokens = estimate_decision_input_tokens(rendered, profile["content"])
        client, model = self._client_and_model()
        started = time.monotonic()
        last_raw: Any = None
        last_error: Optional[str] = None
        retry_count = 0

        for attempt in range(self.max_retries + 1):
            active_messages = messages
            if attempt:
                retry_count = attempt
                active_messages = messages + [
                    {
                        "role": "user",
                        "content": (
                            "The previous response did not parse as runtime_graph_patch_v1. "
                            f"Parse error: {last_error}. Return only the corrected JSON object."
                        ),
                    }
                ]
            try:
                response = self._call_model(client, model, active_messages)
            except Exception as exc:
                latency_ms = int((time.monotonic() - started) * 1000)
                return DecisionProviderResult(
                    patch=None,
                    raw_output=last_raw,
                    provider_name=self.provider_name,
                    model=model,
                    profile_name=profile["profile_name"],
                    profile_version=profile["profile_version"],
                    profile_hash=profile["profile_hash"],
                    request_ref=request_ref,
                    parse_status="provider_error",
                    retry_count=retry_count,
                    provider_latency_ms=latency_ms,
                    input_token_estimate=input_tokens,
                    output_token_estimate=_estimate_tokens(last_raw),
                    error=str(exc),
                )
            last_raw = _extract_response_text(response)
            try:
                patch = parse_provider_patch(last_raw, request.db_revision)
                latency_ms = int((time.monotonic() - started) * 1000)
                response_ref = hashlib.sha256(str(last_raw).encode("utf-8")).hexdigest()
                return DecisionProviderResult(
                    patch=patch,
                    raw_output=last_raw,
                    provider_name=self.provider_name,
                    model=model,
                    profile_name=profile["profile_name"],
                    profile_version=profile["profile_version"],
                    profile_hash=profile["profile_hash"],
                    request_ref=request_ref,
                    response_ref=response_ref,
                    parse_status="parsed",
                    retry_count=retry_count,
                    provider_latency_ms=latency_ms,
                    input_token_estimate=input_tokens,
                    output_token_estimate=_estimate_tokens(last_raw),
                )
            except ProviderPatchParseError as exc:
                last_error = str(exc)

        latency_ms = int((time.monotonic() - started) * 1000)
        response_ref = hashlib.sha256(str(last_raw).encode("utf-8")).hexdigest() if last_raw is not None else None
        return DecisionProviderResult(
            patch=None,
            raw_output=last_raw,
            provider_name=self.provider_name,
            model=model,
            profile_name=profile["profile_name"],
            profile_version=profile["profile_version"],
            profile_hash=profile["profile_hash"],
            request_ref=request_ref,
            response_ref=response_ref,
            parse_status="parse_failed",
            retry_count=retry_count,
            provider_latency_ms=latency_ms,
            input_token_estimate=input_tokens,
            output_token_estimate=_estimate_tokens(last_raw),
            error=last_error,
        )


class RuntimeCompactionProvider(RuntimeDecisionProvider):
    """No-tools, single-shot compaction provider adapter over Hermes model substrate."""

    def __init__(self, *, profile_name: str = "token_budget_compaction", max_tokens: int = 4096, **kwargs: Any) -> None:
        super().__init__(profile_name=profile_name, max_tokens=max_tokens, **kwargs)

    def compact(self, request: CompactionProviderRequest) -> CompactionProviderResult:
        messages, rendered, profile = render_compaction_messages(request)
        request_ref = hashlib.sha256(json.dumps(rendered, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        input_tokens = estimate_decision_input_tokens(rendered, profile["content"])
        client, model = self._client_and_model()
        started = time.monotonic()
        last_raw: Any = None
        last_error: Optional[str] = None
        retry_count = 0

        for attempt in range(self.max_retries + 1):
            active_messages = messages
            if attempt:
                retry_count = attempt
                active_messages = messages + [
                    {
                        "role": "user",
                        "content": (
                            "The previous response did not parse as a checkpoint JSON object. "
                            f"Parse error: {last_error}. Return only the corrected checkpoint JSON object."
                        ),
                    }
                ]
            try:
                response = self._call_model(client, model, active_messages)
            except Exception as exc:
                latency_ms = int((time.monotonic() - started) * 1000)
                return CompactionProviderResult(
                    checkpoint=None,
                    raw_output=last_raw,
                    provider_name=self.provider_name,
                    model=model,
                    profile_name=profile["profile_name"],
                    profile_version=profile["profile_version"],
                    profile_hash=profile["profile_hash"],
                    request_ref=request_ref,
                    parse_status="provider_error",
                    retry_count=retry_count,
                    provider_latency_ms=latency_ms,
                    input_token_estimate=input_tokens,
                    output_token_estimate=_estimate_tokens(last_raw),
                    error=str(exc),
                )
            last_raw = _extract_response_text(response)
            try:
                checkpoint = parse_compaction_checkpoint(last_raw, request)
                latency_ms = int((time.monotonic() - started) * 1000)
                response_ref = hashlib.sha256(str(last_raw).encode("utf-8")).hexdigest()
                return CompactionProviderResult(
                    checkpoint=checkpoint,
                    raw_output=last_raw,
                    provider_name=self.provider_name,
                    model=model,
                    profile_name=profile["profile_name"],
                    profile_version=profile["profile_version"],
                    profile_hash=profile["profile_hash"],
                    request_ref=request_ref,
                    response_ref=response_ref,
                    parse_status="parsed",
                    retry_count=retry_count,
                    provider_latency_ms=latency_ms,
                    input_token_estimate=input_tokens,
                    output_token_estimate=_estimate_tokens(last_raw),
                )
            except ProviderPatchParseError as exc:
                last_error = str(exc)

        latency_ms = int((time.monotonic() - started) * 1000)
        response_ref = hashlib.sha256(str(last_raw).encode("utf-8")).hexdigest() if last_raw is not None else None
        return CompactionProviderResult(
            checkpoint=None,
            raw_output=last_raw,
            provider_name=self.provider_name,
            model=model,
            profile_name=profile["profile_name"],
            profile_version=profile["profile_version"],
            profile_hash=profile["profile_hash"],
            request_ref=request_ref,
            response_ref=response_ref,
            parse_status="parse_failed",
            retry_count=retry_count,
            provider_latency_ms=latency_ms,
            input_token_estimate=input_tokens,
            output_token_estimate=_estimate_tokens(last_raw),
            error=last_error,
        )


class DeterministicCompactionProvider:
    """Provider-shaped deterministic fallback used by tests and default compaction."""

    provider_name = "deterministic"
    model = "db-derived"

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def compact(self, request: CompactionProviderRequest) -> CompactionProviderResult:
        checkpoint = build_deterministic_checkpoint(
            self.conn,
            request.job_id,
            request.source_segment["id"],
            profile_name=request.profile["profile_name"],
        )
        return CompactionProviderResult(
            checkpoint=checkpoint,
            raw_output=checkpoint,
            provider_name=self.provider_name,
            model=self.model,
            profile_name=request.profile["profile_name"],
            profile_version=request.profile["profile_version"],
            profile_hash=request.profile["profile_hash"],
            request_ref=hashlib.sha256(json.dumps(request.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
            response_ref=hashlib.sha256(json.dumps(checkpoint, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
            parse_status="parsed",
            input_token_estimate=_estimate_tokens(request.to_dict()) + _estimate_tokens(request.profile.get("content", "")),
            output_token_estimate=_estimate_tokens(checkpoint),
        )


def _extract_raw_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        raise ProviderPatchParseError("provider output must be a JSON object or string")
    text = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    elif "```" in text:
        raise ProviderPatchParseError("provider output has text outside a JSON fence")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProviderPatchParseError(f"provider output is not valid JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ProviderPatchParseError("provider output must be a JSON object")
    return parsed


def parse_compaction_checkpoint(raw: Any, request: CompactionProviderRequest) -> dict[str, Any]:
    checkpoint = _extract_raw_json(raw)
    if checkpoint.get("schema") == PATCH_SCHEMA or "ops" in checkpoint:
        raise ProviderPatchParseError("compaction provider must return checkpoint JSON, not graph patch JSON")
    metadata = checkpoint.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        raise ProviderPatchParseError("checkpoint metadata must be a JSON object")
    current_revision = int(request.db_state["job"]["graph_revision"])
    metadata.setdefault("source_segment_id", request.source_segment["id"])
    metadata.setdefault("profile_name", request.profile["profile_name"])
    metadata.setdefault("provider_generated", True)
    metadata.setdefault("db_revision", current_revision)
    metadata.setdefault("graph_revision", current_revision)
    metadata.setdefault("ledger_revision", current_revision)
    return checkpoint


def parse_provider_patch(raw: Any, expected_revision: int) -> dict[str, Any]:
    patch = _extract_raw_json(raw)
    if patch.get("schema") != PATCH_SCHEMA:
        raise ProviderPatchParseError(f"patch schema must be {PATCH_SCHEMA!r}")
    if "expected_revision" not in patch:
        raise ProviderPatchParseError("patch requires expected_revision")
    try:
        revision = int(patch["expected_revision"])
    except (TypeError, ValueError) as exc:
        raise ProviderPatchParseError("expected_revision must be an integer") from exc
    if revision != int(expected_revision):
        raise ProviderPatchParseError("expected_revision does not match decision revision")
    if not str(patch.get("rationale_summary") or "").strip():
        raise ProviderPatchParseError("patch requires rationale_summary")
    ops = patch.get("ops")
    if not isinstance(ops, list):
        raise ProviderPatchParseError("patch ops must be a list")
    for op in ops:
        if not isinstance(op, dict):
            raise ProviderPatchParseError("patch op must be an object")
        name = op.get("op")
        if name not in PATCH_OPS:
            raise ProviderPatchParseError(f"unsupported patch op {name!r}")
    return patch


class ReplayDecisionProvider:
    """Deterministic provider that replays strict patch records for tests."""

    def __init__(self, patches: list[Any]):
        self._patches = list(patches)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, session: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"session": session, "delta": delta})
        if not self._patches:
            revision = int(delta["job"]["graph_revision"])
            return {
                "schema": PATCH_SCHEMA,
                "expected_revision": revision,
                "rationale_summary": "replay provider exhausted",
                "ops": [],
            }
        raw = self._patches.pop(0)
        return parse_provider_patch(raw, int(delta["job"]["graph_revision"]))


class RecordingDecisionProvider:
    """Wrap a provider and keep raw inputs/outputs in memory for tests."""

    def __init__(self, provider: Callable[[dict[str, Any], dict[str, Any]], Any]):
        self.provider = provider
        self.records: list[dict[str, Any]] = []

    def __call__(self, session: dict[str, Any], delta: dict[str, Any]) -> Any:
        raw = self.provider(session, delta)
        self.records.append({"session": session, "delta": delta, "raw_output": raw})
        return raw
