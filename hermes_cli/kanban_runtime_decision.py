"""Decision-provider support for the Kanban runtime kernel.

This module is intentionally non-authoritative: it renders DB-derived context,
creates DB-derived checkpoints, and parses provider output.  Graph changes still
go through ``kanban_runtime_kernel.apply_graph_patch`` and its validator.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
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
    delta: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "db_revision": self.db_revision,
            "session": self.session,
            "stable_prefix": self.stable_prefix,
            "goal_contract": self.goal_contract,
            "checkpoint": self.checkpoint,
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
        data["checkpoint"] = _loads(data.get("checkpoint_json") or data.get("checkpoint"))
    return data


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


def build_decision_provider_request(
    conn: sqlite3.Connection,
    job_id: str,
    delta: dict[str, Any],
) -> DecisionProviderRequest:
    session = latest_decision_session(conn, job_id)
    checkpoint_row = latest_decision_checkpoint(conn, job_id)
    if checkpoint_row is None:
        checkpoint = build_checkpoint_payload(conn, job_id)
    else:
        checkpoint = checkpoint_row["checkpoint"]
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
        delta=delta,
    )


def render_decision_prompt(request: DecisionProviderRequest) -> dict[str, Any]:
    """Render a deterministic, cache-friendly provider input object."""

    return {
        "stable_prefix": request.stable_prefix,
        "stable_goal_contract": request.goal_contract,
        "checkpoint": request.checkpoint,
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
