from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_kernel as rk


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    db = kb.connect()
    try:
        rk.ensure_runtime_schema(db)
        yield db
    finally:
        try:
            db.close()
        except sqlite3.ProgrammingError:
            pass


def _git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=workspace,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout.strip()


def _node(conn, job_id: str, node_key: str) -> dict:
    return dict(
        conn.execute(
            "SELECT * FROM execution_nodes WHERE job_id = ? AND node_key = ?",
            (job_id, node_key),
        ).fetchone()
    )


def _complete(conn, node: dict, receipt: dict) -> None:
    assert kb.complete_task(
        conn,
        node["latest_task_id"],
        result=receipt.get("summary", "done"),
        summary=receipt.get("summary", "done"),
        metadata={
            "summary": receipt.get("summary", "done"),
            "worker_lane": {
                "name": "codex-runtime",
                "kind": "codex_cli",
                "exit_code": 0,
            },
            "runtime_receipt": receipt,
        },
    )


def _revision(conn, job_id: str) -> int:
    return int(
        conn.execute(
            "SELECT graph_revision FROM runtime_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()[0]
    )


def _contract(outcome: str, scopes: list[str]) -> dict:
    return {
        "outcome": outcome,
        "acceptance_criteria": [outcome + " is verified"],
        "success_evidence": ["changed_files", "verification"],
        "declared_write_scope": scopes,
        "prohibited_actions": ["modify_shared_contract"],
        "workspace_mode": "isolated_worktree",
    }


def test_deferred_milestone_uses_fixed_seed_and_child_patch_is_incremental(
    conn,
    tmp_path,
):
    workspace = tmp_path / "workspace"
    (workspace / "src" / "shared").mkdir(parents=True)
    (workspace / "src" / "adapter_a.py").write_text(
        "def adapt(value):\n    return value\n",
        encoding="utf-8",
    )
    (workspace / "src" / "adapter_b.py").write_text(
        "def audit(value):\n    return value\n",
        encoding="utf-8",
    )
    (workspace / "src" / "shared" / "contract.py").write_text(
        "VERSION = 1\n",
        encoding="utf-8",
    )
    _git(workspace, "init")
    _git(workspace, "config", "user.email", "runtime@example.invalid")
    _git(workspace, "config", "user.name", "Runtime Test")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", "base")
    base_revision = _git(workspace, "rev-parse", "HEAD")
    root_task_id = kb.create_task(
        conn,
        title="root",
        initial_status="running",
    )
    job_id = rk.create_runtime_job(
        conn,
        root_task_id,
        "establish one shared contract and complete two durable adapters",
        workspace_path=str(workspace),
        goal_items=[{
            "item_key": "runtime-result",
            "description": "both adapters consume the stable contract",
            "required": True,
            "verifier_required": False,
        }],
        initial_assignee="codex-runtime",
        initialization_mode="fixture",
        runtime_metadata={
            "orchestration_policy": {
                "schema": rk.RUNTIME_ORCHESTRATION_POLICY_SCHEMA,
                "mode": "closed_loop_coordination",
                "enabled": True,
                "worker_lane": "codex-runtime",
                "max_child_nodes": 3,
                "required_child_capabilities": list(
                    rk.RUNTIME_ORCHESTRATION_CHILD_CAPABILITIES
                ),
                "base_revision": base_revision,
                "worktree_root": str(tmp_path / "worktrees"),
                "contribution_root": str(tmp_path / "contributions"),
                "retention": {
                    "worktrees": "retain",
                    "contributions": "retain",
                },
            },
        },
    )
    primary = _node(conn, job_id, "understand-scope")
    constraints = json.loads(primary["constraints_json"])
    constraints["contract"] = {
        **_contract("integrated result", ["**"]),
        "workspace_mode": "shared_job_workspace",
    }
    conn.execute(
        "UPDATE execution_nodes SET constraints_json = ? WHERE id = ?",
        (json.dumps(constraints), primary["id"]),
    )
    assert rk.advance_runtime_job(
        conn,
        job_id,
        create_tasks=True,
    ).materialized_nodes == ["understand-scope"]
    primary = _node(conn, job_id, "understand-scope")
    session_id = "019f0000-0000-7000-8000-00000000d016"
    kb.record_task_event(
        conn,
        primary["latest_task_id"],
        "worker_backend_session_started",
        {
            "worker_lane": "codex-runtime",
            "worker_kind": "codex_cli",
            "backend_session_id": session_id,
            "execution_mode": "fresh",
        },
    )
    rk.sync_runtime_backend_sessions(conn, job_id)
    proposed = [
        {
            "node_key": "adapter-a",
            "outcome": "Upgrade adapter A to contract v2.",
            "acceptance_criteria": ["Adapter A consumes v2"],
            "declared_write_scope": ["src/adapter_a.py"],
            "requested_capabilities": list(
                rk.RUNTIME_ORCHESTRATION_CHILD_CAPABILITIES
            ),
        },
        {
            "node_key": "adapter-b",
            "outcome": "Upgrade adapter B to contract v2.",
            "acceptance_criteria": ["Adapter B consumes v2"],
            "declared_write_scope": ["src/adapter_b.py"],
            "requested_capabilities": list(
                rk.RUNTIME_ORCHESTRATION_CHILD_CAPABILITIES
            ),
        },
    ]
    _complete(
        conn,
        primary,
        {
            "schema": rk.STRUCTURE_CHECKPOINT_SCHEMA,
            "kind": "early_structure_assessment",
            "recommendation": "defer_until_milestone",
            "summary": "Adapters become independent after contract v2 is frozen.",
            "inspected_scope": ["src"],
            "repository_facts": [{
                "fact": "Both adapters depend on the shared contract version.",
                "evidence_refs": ["workspace:path:src/shared/contract.py"],
            }],
            "proposed_nodes": proposed,
            "integration_owner_node_key": "understand-scope",
            "shared_integration_scope": ["src/shared/**"],
            "milestone_contract": {
                "milestone_key": "contract-v2",
                "summary": "Stable contract v2",
                "artifact_scope": ["src/shared/**"],
                "verification_criteria": ["contract checks pass"],
            },
            "risks": ["Adapters must not start from contract v1."],
            "worker_session_should_resume": True,
            "changed_files": [],
        },
    )
    assert rk.advance_runtime_job(
        conn,
        job_id,
        create_tasks=False,
    ).ingested_nodes == ["understand-scope"]
    primary = _node(conn, job_id, "understand-scope")
    assert primary["state"] == "ready"
    assert conn.execute(
        "SELECT COUNT(*) FROM kernel_decisions WHERE job_id = ?",
        (job_id,),
    ).fetchone()[0] == 0
    rk.sync_runtime_backend_sessions(conn, job_id)
    resumed_task = rk.materialize_runtime_node(conn, primary)
    resumed_metadata = json.loads(
        conn.execute(
            "SELECT metadata_json FROM node_materializations WHERE task_id = ?",
            (resumed_task,),
        ).fetchone()[0]
    )
    assert resumed_metadata["execution_continuity"]["mode"] == "resume"
    assert resumed_metadata["execution_continuity"]["resume_session_id"] == session_id
    task_body = conn.execute(
        "SELECT body FROM tasks WHERE id = ?",
        (resumed_task,),
    ).fetchone()[0]
    assert "Deferred decomposition is active" in task_body
    assert '"milestone_key": "contract-v2"' in task_body

    (workspace / "src" / "shared" / "contract.py").write_text(
        "VERSION = 2\n",
        encoding="utf-8",
    )
    primary = _node(conn, job_id, "understand-scope")
    _complete(
        conn,
        primary,
        {
            "schema": rk.COORDINATION_CHECKPOINT_SCHEMA,
            "kind": "milestone_completed",
            "summary": "Contract v2 is implemented and verified.",
            "phase": "implementation",
            "completed_scope": ["contract v2"],
            "remaining_scope": ["integrate adapter contributions"],
            "findings": [{
                "finding_key": "contract-v2-ready",
                "type": "milestone_completed",
                "summary": "Contract v2 is stable.",
                "affected_node_keys": [],
                "evidence_refs": ["workspace:path:src/shared/contract.py"],
            }],
            "next_intent": "integrate child contributions",
            "milestone_key": "contract-v2",
            "changed_files": ["src/shared/contract.py"],
            "responsibility_candidates": [],
            "consumed_directive_ids": [],
            "worker_session_should_resume": True,
        },
    )
    assert rk.advance_runtime_job(
        conn,
        job_id,
        create_tasks=False,
    ).ingested_nodes == ["understand-scope"]
    primary = _node(conn, job_id, "understand-scope")
    assert primary["state"] == "waiting_coordination"
    event = conn.execute(
        """
        SELECT * FROM execution_events
         WHERE job_id = ? AND event_type = 'worker_coordination_checkpointed'
         ORDER BY id DESC LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    event_payload = json.loads(event["payload_json"])
    assert event_payload["deferred_activation"]["candidate_keys"] == [
        "adapter-a",
        "adapter-b",
    ]
    candidates = {
        item["candidate_key"]: item
        for item in event_payload["checkpoint"]["responsibility_candidates"]
    }
    seed = conn.execute(
        """
        SELECT * FROM node_artifacts
         WHERE job_id = ? AND artifact_type = 'runtime_milestone_seed'
        """,
        (job_id,),
    ).fetchone()
    seed_payload = json.loads(seed["metadata_json"])
    assert _git(workspace, "rev-parse", seed_payload["seed_ref"]) == (
        seed_payload["seed_revision"]
    )
    conn.execute(
        """
        DELETE FROM execution_events
         WHERE job_id = ? AND event_type = 'runtime_milestone_seed_frozen'
        """,
        (job_id,),
    )
    recovered_seed = rk._freeze_runtime_milestone_seed(
        conn,
        dict(conn.execute(
            "SELECT * FROM runtime_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()),
        _node(conn, job_id, "understand-scope"),
        dict(conn.execute(
            "SELECT * FROM node_materializations WHERE id = ?",
            (seed_payload["materialization_id"],),
        ).fetchone()),
        event_payload["checkpoint"],
        {
            "source_structure_event_id": seed_payload[
                "source_structure_event_id"
            ],
            "milestone_contract": {
                "milestone_key": seed_payload["milestone_key"],
            },
        },
    )
    assert recovered_seed["artifact_id"] == seed["id"]
    assert conn.execute(
        """
        SELECT COUNT(*) FROM execution_events
         WHERE job_id = ? AND event_type = 'runtime_milestone_seed_frozen'
        """,
        (job_id,),
    ).fetchone()[0] == 1
    rk.advance_runtime_job(conn, job_id, create_tasks=False)
    assert conn.execute(
        """
        SELECT COUNT(*) FROM node_artifacts
         WHERE job_id = ? AND artifact_type = 'runtime_milestone_seed'
        """,
        (job_id,),
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM runtime_coordination_actions WHERE job_id = ?",
        (job_id,),
    ).fetchone()[0] == 1

    event_id = int(event["id"])
    ops = []
    scopes = {}
    for key, candidate in candidates.items():
        scopes[key] = candidate["declared_write_scope"]
        ops.extend([
            {
                "op": "create_node",
                "node_key": key,
                "node_type": "implementation",
                "title": f"Implement {key}",
                "description": candidate["outcome"],
                "assignee": "codex-runtime",
                "goal_item_keys": candidate["goal_item_keys"],
                "source_responsibility_ref": (
                    f"event:{event_id}#responsibility:{key}"
                ),
                "requested_capabilities": list(
                    rk.RUNTIME_ORCHESTRATION_CHILD_CAPABILITIES
                ),
                "contract": {
                    **_contract(
                        candidate["outcome"],
                        candidate["declared_write_scope"],
                    ),
                    "acceptance_criteria": candidate[
                        "acceptance_criteria"
                    ],
                },
            },
            {
                "op": "add_dependency",
                "from_node_key": key,
                "to_node_key": "understand-scope",
            },
        ])
    provider_calls = []

    def provider(_session, delta):
        provider_calls.append(delta)
        pending = delta["pending_coordination_actions"]
        assert len(pending) == 1
        assert int(pending[0]["source_checkpoint_event_id"]) == event_id
        return {
            "schema": rk.PATCH_SCHEMA,
            "expected_revision": int(delta["job"]["graph_revision"]),
            "rationale_summary": "Activate adapters from the frozen v2 milestone.",
            "ops": ops,
            "decomposition": {
                "policy_version": "1",
                "mode": "multiple_runtime_nodes",
                "justifications": [{
                    "type": "execution_discovered_gap",
                    "nodes": sorted(candidates),
                    "explanation": (
                        "The stable contract makes adapter scopes independent."
                    ),
                    "evidence_refs": [f"event:{event_id}"],
                    "declared_write_scopes": scopes,
                    "integration_owner_node_key": "understand-scope",
                }],
            },
        }

    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    conn.close()
    conn = kb.connect(db_path=db_path)
    advanced = rk.advance_runtime_job(
        conn,
        job_id,
        create_tasks=False,
        decision_provider=provider,
        auto_compact=False,
    )
    assert advanced.patch_status == "applied"
    assert len(provider_calls) == 1
    assert _node(conn, job_id, "understand-scope")["state"] == (
        "waiting_dependency"
    )
    assert conn.execute(
        """
        SELECT COUNT(*) FROM node_artifacts
         WHERE job_id = ? AND artifact_type = 'runtime_milestone_seed'
        """,
        (job_id,),
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM runtime_coordination_actions WHERE job_id = ?",
        (job_id,),
    ).fetchone()[0] == 1
    child = _node(conn, job_id, "adapter-a")
    child_metadata = json.loads(child["metadata_json"])
    assert child_metadata["runtime_workspace_seed"]["base_revision"] == (
        seed_payload["seed_revision"]
    )
    child_task = rk.materialize_runtime_node(conn, child)
    child_workspace = Path(kb.get_task(conn, child_task).workspace_path)
    assert _git(child_workspace, "rev-parse", "HEAD") == seed_payload["seed_revision"]
    assert (child_workspace / "src" / "shared" / "contract.py").read_text(
        encoding="utf-8"
    ) == "VERSION = 2\n"
    (child_workspace / "src" / "adapter_a.py").write_text(
        "from .shared.contract import VERSION\n\n"
        "def adapt(value):\n    return (VERSION, value)\n",
        encoding="utf-8",
    )
    child = _node(conn, job_id, "adapter-a")
    _complete(
        conn,
        child,
        {
            "schema": "runtime_contribution_receipt_v1",
            "verdict": "succeeded",
            "summary": "Adapter A consumes the frozen contract.",
            "claimed_goal_items": [],
            "partial_goal_items": ["runtime-result"],
            "unmet_goal_items": [],
            "contradicted_goal_items": [],
            "changed_files": ["src/adapter_a.py"],
            "verification": {
                "passed": True,
                "summary": "adapter check passed",
            },
            "artifacts": [],
            "accepted_contributions": [],
            "modified_contributions": [],
            "rejected_contributions": [],
            "active_assumptions": [],
            "rejected_approaches": [],
            "known_failure_boundaries": [],
            "consumed_directive_ids": [],
            "structure_request": None,
            "responsibility_candidates": [],
        },
    )
    assert rk.advance_runtime_job(
        conn,
        job_id,
        create_tasks=False,
    ).ingested_nodes == ["adapter-a"]
    contribution = conn.execute(
        """
        SELECT * FROM node_artifacts
         WHERE node_id = ? AND artifact_type = 'runtime_node_contribution'
        """,
        (child["id"],),
    ).fetchone()
    contribution_patch = Path(contribution["path_or_ref"]).read_text(
        encoding="utf-8"
    )
    assert "src/adapter_a.py" in contribution_patch
    assert "src/shared/contract.py" not in contribution_patch
    conn.close()


def test_deferred_milestone_rejects_wrong_key():
    checkpoint = {
        "schema": rk.COORDINATION_CHECKPOINT_SCHEMA,
        "kind": "milestone_completed",
        "summary": "wrong milestone",
        "phase": "implementation",
        "completed_scope": ["wrong milestone"],
        "remaining_scope": ["work remains"],
        "findings": [{
            "finding_key": "wrong-milestone",
            "type": "milestone_completed",
            "summary": "wrong milestone",
            "affected_node_keys": [],
            "evidence_refs": ["workspace:path:src/shared/contract.py"],
        }],
        "next_intent": "continue",
        "milestone_key": "wrong-key",
        "changed_files": ["src/shared/contract.py"],
        "responsibility_candidates": [],
        "consumed_directive_ids": [],
        "worker_session_should_resume": True,
    }
    node = {
        "node_key": "primary",
        "metadata_json": json.dumps({
            "deferred_decomposition": {
                "status": "waiting_milestone",
                "milestone_contract": {"milestone_key": "contract-v2"},
            }
        }),
    }

    assert rk._coordination_checkpoint_validation_error(
        checkpoint,
        node=node,
    ) == "coordination checkpoint milestone_key does not match deferred milestone"
