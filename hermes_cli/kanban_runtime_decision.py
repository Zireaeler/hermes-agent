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
}
DEFAULT_COMPACTION_POLICY = {
    "mode": "auto",
    "max_active_segment_tokens": 12000,
    "max_context_window_ratio": 0.8,
    "context_window_tokens": 128000,
    "rejected_patch_threshold": 5,
    "noop_threshold": 5,
    "max_tail_entries": 8,
    "max_tail_tokens": 2000,
    "default_profile": "token_budget_compaction",
}


class ProviderPatchParseError(ValueError):
    """Raised when provider output is not a strict runtime graph patch."""


@dataclass(frozen=True)
class DecisionProviderRequest:
    job_id: str
    db_revision: int
    session: dict[str, Any]
    stable_prefix: dict[str, Any]
    goal_contract: dict[str, Any]
    checkpoint: Optional[dict[str, Any]]
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
            "short_tail": self.short_tail,
            "delta": self.delta,
        }


@dataclass(frozen=True)
class DecisionProviderResult:
    patch: Optional[dict[str, Any]]
    raw_output: Any
    provider_name: str
    model: Optional[str] = None
    parse_status: str = "parsed"
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "patch": self.patch,
            "raw_output": self.raw_output,
            "provider_name": self.provider_name,
            "model": self.model,
            "parse_status": self.parse_status,
            "error": self.error,
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
        "SELECT * FROM decision_checkpoints WHERE job_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    data = _row(row)
    if data is not None:
        data["checkpoint"] = _loads(data.get("payload_json") or data.get("checkpoint_json") or data.get("checkpoint"))
    return data


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


def _profile_metadata(profile_name: str) -> dict[str, Any]:
    profile = load_compaction_profile(profile_name)
    return {key: profile[key] for key in ("profile_name", "profile_version", "profile_hash", "profile_path")}


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
            if entry["satisfaction"] == "full" and entry["verification_state"] in {"verified", "waived"}
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
    if not artifacts:
        artifacts = set()

    for key, items in _checkpoint_fact_lists(checkpoint_payload):
        for item in items:
            refs = item.get("source_refs") or []
            if not refs:
                return {"status": "rejected", "reason": f"{key} item lacks provenance"}
            for ref in refs:
                if "node_key" in ref and ref["node_key"] not in node_keys:
                    return {"status": "rejected", "reason": f"unknown node_key {ref['node_key']!r}"}
                if "goal_item_key" in ref and ref["goal_item_key"] not in goal_keys:
                    return {"status": "rejected", "reason": f"unknown goal_item_key {ref['goal_item_key']!r}"}
                if "artifact_ref" in ref and artifacts and ref["artifact_ref"] not in artifacts:
                    return {"status": "rejected", "reason": f"unknown artifact_ref {ref['artifact_ref']!r}"}
            if key == "satisfied_goal_items":
                if item.get("verification_state") not in {"verified", "waived"}:
                    return {"status": "rejected", "reason": "satisfied goal item must be verified or waived"}

    current_open_human = conn.execute(
        "SELECT COUNT(*) FROM execution_nodes WHERE job_id = ? AND state = 'waiting_human'",
        (job_id,),
    ).fetchone()[0]
    if current_open_human and not checkpoint_payload.get("open_blockers"):
        return {"status": "rejected", "reason": "checkpoint omits active human gate blocker"}
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


def _render_checkpoint_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)


def _job_compaction_policy(conn: sqlite3.Connection, job_id: str, policy: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    row = conn.execute("SELECT metadata_json FROM runtime_jobs WHERE id = ?", (job_id,)).fetchone()
    metadata = _loads(row["metadata_json"]) if row else {}
    session = latest_decision_session(conn, job_id)
    session_metadata = _loads(session.get("metadata_json"))
    merged = dict(DEFAULT_COMPACTION_POLICY)
    merged.update(metadata.get("compaction_policy") or {})
    merged.update(session_metadata.get("compaction_policy") or {})
    if policy:
        merged.update(policy)
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
    active_segment_tokens = int(active.get("active_segment_tokens") or 0)
    cacheable_prefix_tokens = stable_prefix_tokens + checkpoint_tokens
    input_tokens = cacheable_prefix_tokens + tail_tokens + delta_tokens
    context_window = max(1, int(resolved_policy["context_window_tokens"]))
    return {
        "stable_prefix_tokens": stable_prefix_tokens,
        "checkpoint_tokens": checkpoint_tokens,
        "tail_tokens": tail_tokens,
        "delta_tokens": delta_tokens,
        "model_output_tokens": int(model_output_tokens or 0),
        "active_segment_tokens": active_segment_tokens,
        "cacheable_prefix_tokens": cacheable_prefix_tokens,
        "context_window_ratio": input_tokens / context_window,
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

    telemetry = build_compaction_telemetry(conn, job_id, delta=delta, policy=policy)
    resolved = telemetry["policy"]
    if resolved.get("mode") == "manual":
        return {"should_compact": False, "reason": "manual_mode", "profile_name": None, "telemetry": telemetry}
    if telemetry["active_segment_tokens"] >= int(resolved["max_active_segment_tokens"]):
        return {
            "should_compact": True,
            "reason": "token_threshold",
            "profile_name": resolved.get("default_profile") or "token_budget_compaction",
            "telemetry": telemetry,
        }
    if telemetry["context_window_ratio"] >= float(resolved["max_context_window_ratio"]):
        return {
            "should_compact": True,
            "reason": "context_window_ratio",
            "profile_name": resolved.get("default_profile") or "token_budget_compaction",
            "telemetry": telemetry,
        }
    if telemetry["rejected_patch_count"] >= int(resolved["rejected_patch_threshold"]):
        return {
            "should_compact": True,
            "reason": "rejection_threshold",
            "profile_name": "validator_boundary_compaction",
            "telemetry": telemetry,
        }
    if telemetry["noop_count"] >= int(resolved["noop_threshold"]):
        return {
            "should_compact": True,
            "reason": "noop_threshold",
            "profile_name": "anti_stuck_compaction",
            "telemetry": telemetry,
        }
    return {"should_compact": False, "reason": "below_threshold", "profile_name": None, "telemetry": telemetry}


def compact_decision_session(
    conn: sqlite3.Connection,
    job_id: str,
    profile_name: str = "token_budget_compaction",
    reason: str = "manual",
) -> dict[str, Any]:
    """Close the active segment, create a validated deterministic checkpoint, and open a new segment."""

    from hermes_cli import kanban_runtime_kernel as rk

    rk.ensure_runtime_schema(conn)
    source_segment = rk.ensure_decision_segment(conn, job_id)
    session = latest_decision_session(conn, job_id)
    request_entry = rk.append_decision_segment_entry(
        conn,
        job_id,
        "compaction_requested",
        {"profile_name": profile_name, "reason": reason, "source_segment_id": source_segment["id"]},
        ref_type="decision_session_segment",
        ref_id=source_segment["id"],
    )
    payload = build_deterministic_checkpoint(conn, job_id, source_segment["id"], profile_name=profile_name)
    validation = validate_decision_checkpoint(conn, job_id, payload)
    if validation["status"] != "accepted":
        rk.append_decision_segment_entry(
            conn,
            job_id,
            "checkpoint_rejected",
            {"validation": validation, "profile_name": profile_name},
            ref_type="decision_session_segment",
            ref_id=source_segment["id"],
        )
        conn.execute(
            """
            UPDATE decision_session_segments
               SET state = 'failed_compaction', closed_at = ?, covered_graph_revision_end = ?
             WHERE id = ?
            """,
            (_now(), int(payload["metadata"]["graph_revision"]), source_segment["id"]),
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
        return {"status": "rejected", "reason": validation["reason"], "source_segment_id": source_segment["id"]}

    profile = _profile_metadata(profile_name)
    ranges = _segment_range(conn, source_segment["id"])
    checkpoint_id = _id("dchk")
    now = _now()
    revision = int(payload["metadata"]["graph_revision"])
    supersedes = session.get("latest_checkpoint_id")
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
            _json({"reason": reason, "request_entry_id": request_entry["id"]}),
        ),
    )
    rk.append_decision_segment_entry(
        conn,
        job_id,
        "checkpoint_created",
        {"checkpoint_id": checkpoint_id, "validation": validation, **profile},
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
    }


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
    rows = conn.execute(
        """
        SELECT id, segment_id, entry_index, entry_type, ref_type, ref_id,
               decision_id, event_id, patch_id, graph_revision, payload_json,
               payload_text, estimated_tokens, created_at
          FROM decision_segment_entries
         WHERE job_id = ? AND id > ?
         ORDER BY id ASC
        """,
        (job_id, int(covered_end)),
    ).fetchall()
    tail: list[dict[str, Any]] = []
    tokens = 0
    for row in rows:
        if len(tail) >= max_tail_entries:
            break
        estimated = int(row["estimated_tokens"] or 0)
        if tokens + estimated > max_tail_tokens:
            break
        data = _row(row) or {}
        data["payload"] = _loads(data.get("payload_json"))
        tail.append(data)
        tokens += estimated
    return tail


def decision_context_status(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    from hermes_cli import kanban_runtime_kernel as rk

    rk.ensure_runtime_schema(conn)
    active = rk.ensure_decision_segment(conn, job_id)
    checkpoint = latest_decision_checkpoint(conn, job_id)
    policy_result = should_compact_decision_session(conn, job_id)
    return {
        "job_id": job_id,
        "active_segment": _row(
            conn.execute("SELECT * FROM decision_session_segments WHERE id = ?", (active["id"],)).fetchone()
        ),
        "latest_checkpoint": checkpoint,
        "active_segment_tokens": int(active.get("active_segment_tokens") or 0),
        "compaction_policy": policy_result,
        "provider_input_composition": [
            "stable_runtime_contract",
            "current_goal_contract",
            "latest_validated_checkpoint",
            "strict_short_tail",
            "current_delta",
        ],
        "short_tail": _short_tail_entries(conn, job_id, checkpoint),
    }


def build_decision_provider_request(
    conn: sqlite3.Connection,
    job_id: str,
    delta: dict[str, Any],
) -> DecisionProviderRequest:
    from hermes_cli import kanban_runtime_kernel as rk

    rk.ensure_decision_segment(conn, job_id)
    session = latest_decision_session(conn, job_id)
    checkpoint_row = latest_decision_checkpoint(conn, job_id)
    if checkpoint_row is None:
        checkpoint = build_checkpoint_payload(conn, job_id)
        short_tail: list[dict[str, Any]] = []
    else:
        checkpoint = checkpoint_row["checkpoint"]
        short_tail = _short_tail_entries(conn, job_id, checkpoint_row)
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
    }
    return DecisionProviderRequest(
        job_id=job_id,
        db_revision=int(job["graph_revision"]),
        session=session,
        stable_prefix=stable_prefix,
        goal_contract=checkpoint["goal_contract"],
        checkpoint=checkpoint,
        short_tail=short_tail,
        delta=delta,
    )


def render_decision_prompt(request: DecisionProviderRequest) -> dict[str, Any]:
    """Render a deterministic, cache-friendly provider input object."""

    return {
        "stable_prefix": request.stable_prefix,
        "stable_goal_contract": request.goal_contract,
        "checkpoint": request.checkpoint,
        "short_tail": request.short_tail,
        "delta": request.delta,
        "provider_instruction": {
            "output": "return exactly one JSON object matching runtime_graph_patch_v1",
            "no_markdown": True,
            "no_explanatory_text": True,
        },
    }


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
        raise ProviderPatchParseError("provider patch must be a JSON object")
    return parsed


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
