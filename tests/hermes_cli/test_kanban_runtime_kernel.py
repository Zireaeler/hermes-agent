from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import codex_worker as cw
from hermes_cli import kanban_runtime_decision as rd
from hermes_cli import kanban_runtime_kernel as rk
from hermes_cli import phase4g8_swe_evo as swe_evo
from hermes_cli.worker_lanes import WorkerLane, clear_worker_lanes, register_worker_lane


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kb.connect() as db:
        rk.ensure_runtime_schema(db)
        yield db


@pytest.fixture(autouse=True)
def clean_worker_lanes():
    clear_worker_lanes()
    yield
    clear_worker_lanes()


def _root_task(conn) -> str:
    return kb.create_task(conn, title="root goal", initial_status="running")


def _job(conn, *, goal_key: str = "initial-runtime-result", verifier_required: bool = True) -> str:
    return rk.create_runtime_job(
        conn,
        _root_task(conn),
        "ship a phase1 runtime",
        goal_items=[
            {
                "item_key": goal_key,
                "description": "phase1 runtime evidence exists",
                "required": True,
                "verifier_required": verifier_required,
            }
        ],
        initialization_mode="fixture",
    )


def _revision(conn, job_id: str) -> int:
    return int(conn.execute("SELECT graph_revision FROM runtime_jobs WHERE id = ?", (job_id,)).fetchone()[0])


def _node(conn, job_id: str, key: str):
    return conn.execute(
        "SELECT * FROM execution_nodes WHERE job_id = ? AND node_key = ?",
        (job_id, key),
    ).fetchone()


def _patch(job_id: str, revision: int, *ops):
    return {
        "schema": rk.PATCH_SCHEMA,
        "expected_revision": revision,
        "rationale_summary": f"test patch for {job_id}",
        "ops": list(ops),
    }


def _contract(*scopes: str):
    return {
        "outcome": "Produce a coherent verified runtime result.",
        "acceptance_criteria": ["Result exists", "Verification passes"],
        "success_evidence": ["changed_files", "verification"],
        "declared_write_scope": list(scopes),
        "prohibited_actions": ["production_deployment"],
    }


def _git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=workspace,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout.strip()


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() != 0,
    reason="requires a root supervisor and a distinct POSIX workspace owner",
)
def test_isolated_worktree_git_lifecycle_uses_declared_workspace_owner(
    conn,
    tmp_path,
):
    owner = {"uid": 65534, "gid": 65534}
    policy = {
        "workspace_owner": owner,
        "worktree_root": str(tmp_path / "worktrees"),
        "contribution_root": str(tmp_path / "contributions"),
    }
    for path in (tmp_path.parent.parent, tmp_path.parent, tmp_path):
        os.chmod(path, 0o711)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "feature.py").write_text("value = 1\n", encoding="utf-8")
    _git(workspace, "init")
    _git(workspace, "config", "user.email", "runtime@example.invalid")
    _git(workspace, "config", "user.name", "Runtime Test")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", "base")
    base_revision = _git(workspace, "rev-parse", "HEAD")
    policy["base_revision"] = base_revision
    rk._apply_workspace_owner(workspace, policy)

    worktree_root = Path(policy["worktree_root"])
    worktree_root.mkdir()
    sibling_sentinel = worktree_root / "existing-sibling" / "sentinel.txt"
    sibling_sentinel.parent.mkdir()
    sibling_sentinel.write_text("belongs to another worktree\n", encoding="utf-8")
    sibling_owner = (sibling_sentinel.stat().st_uid, sibling_sentinel.stat().st_gid)

    job_id = rk.create_runtime_job(
        conn,
        _root_task(conn),
        "exercise owner-isolated worktree lifecycle",
        workspace_path=str(workspace),
        goal_items=[{
            "item_key": "owner-result",
            "description": "owner-isolated contribution exists",
            "required": True,
            "verifier_required": False,
        }],
        initialization_mode="fixture",
        runtime_metadata={"orchestration_policy": policy},
    )
    node = _node(conn, job_id, "understand-scope")
    constraints = json.loads(node["constraints_json"])
    constraints["contract"] = {
        **_contract("feature.py"),
        "workspace_mode": "isolated_worktree",
    }
    metadata = json.loads(node["metadata_json"])
    metadata.update({
        "non_authoritative_contribution": True,
        "contribution_to_node_key": "integration-owner",
    })
    conn.execute(
        "UPDATE execution_nodes SET constraints_json = ?, metadata_json = ? WHERE id = ?",
        (json.dumps(constraints), json.dumps(metadata), node["id"]),
    )

    task_id = rk.materialize_runtime_node(
        conn,
        dict(_node(conn, job_id, "understand-scope")),
    )
    task = kb.get_task(conn, task_id)
    worktree = Path(task.workspace_path)
    assert worktree != workspace
    assert worktree.stat().st_uid == owner["uid"]
    assert worktree.stat().st_gid == owner["gid"]
    gitdir = Path(
        (worktree / ".git").read_text(encoding="utf-8").strip().removeprefix("gitdir: ")
    )
    assert gitdir.stat().st_uid == owner["uid"]
    assert gitdir.stat().st_gid == owner["gid"]
    assert (sibling_sentinel.stat().st_uid, sibling_sentinel.stat().st_gid) == sibling_owner

    changed = worktree / "feature.py"
    changed.write_text("value = 2\n", encoding="utf-8")
    os.chown(changed, owner["uid"], owner["gid"])
    node = _node(conn, job_id, "understand-scope")
    _complete_node(
        conn,
        node,
        {
            "verdict": "succeeded",
            "summary": "owner-isolated change complete",
            "claimed_goal_items": ["owner-result"],
            "changed_files": ["feature.py"],
            "verification": {"passed": True, "summary": "focused test passed"},
        },
    )
    assert rk.ingest_runtime_node_evidence(conn, node["id"])
    artifact = conn.execute(
        "SELECT * FROM node_artifacts WHERE node_id = ?",
        (node["id"],),
    ).fetchone()
    assert artifact is not None
    assert Path(artifact["path_or_ref"]).read_text(encoding="utf-8")


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() != 0,
    reason="requires a root supervisor and POSIX symlink ownership",
)
def test_apply_workspace_owner_does_not_follow_symlink_targets(tmp_path):
    owner = {"uid": 65534, "gid": 65534}
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    regular = workspace / "regular.txt"
    regular.write_text("owned by worker\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("must retain supervisor ownership\n", encoding="utf-8")
    outside_owner = (outside.stat().st_uid, outside.stat().st_gid)
    link = workspace / "outside-link"
    link.symlink_to(outside)

    rk._apply_workspace_owner(workspace, {"workspace_owner": owner})

    assert (regular.stat().st_uid, regular.stat().st_gid) == (
        owner["uid"],
        owner["gid"],
    )
    assert (link.lstat().st_uid, link.lstat().st_gid) == (
        owner["uid"],
        owner["gid"],
    )
    assert (outside.stat().st_uid, outside.stat().st_gid) == outside_owner


def _complete_node(conn, node, evidence: dict):
    assert node["latest_task_id"]
    payload = dict(evidence)
    verification = payload.get("verification")
    if (
        node["node_type"] == "verification"
        and isinstance(verification, dict)
        and verification.get("passed") is True
        and "verification_provenance" not in payload
    ):
        payload["verification_provenance"] = rk.build_independent_verification_provenance(conn, node["id"])
    assert kb.complete_task(
        conn,
        node["latest_task_id"],
        result=payload.get("summary", "done"),
        summary=payload.get("summary", "done"),
        metadata=payload,
    )


def _install_task_run(
    conn,
    task_id: str,
    *,
    status: str = "running",
    outcome: str | None = None,
    started_at: int = 100,
    ended_at: int | None = None,
    claim_expires: int | None = None,
    last_heartbeat_at: int | None = None,
    metadata: dict | None = None,
) -> int:
    conn.execute("DELETE FROM task_runs WHERE task_id = ?", (task_id,))
    cur = conn.execute(
        """
        INSERT INTO task_runs (
            task_id, profile, step_key, status, claim_lock, claim_expires,
            worker_pid, max_runtime_seconds, last_heartbeat_at, started_at,
            ended_at, outcome, summary, metadata, error
        ) VALUES (?, NULL, NULL, ?, 'test-claim', ?, NULL, NULL, ?, ?, ?, ?, NULL, ?, NULL)
        """,
        (
            task_id,
            status,
            claim_expires,
            last_heartbeat_at,
            started_at,
            ended_at,
            outcome,
            json.dumps(metadata or {}, ensure_ascii=False) if metadata is not None else None,
        ),
    )
    run_id = int(cur.lastrowid)
    conn.execute(
        """
        UPDATE tasks
           SET status = ?,
               current_run_id = ?, claim_lock = 'test-claim',
               claim_expires = ?, last_heartbeat_at = ?,
               started_at = COALESCE(started_at, ?)
         WHERE id = ?
        """,
        ("running" if status == "running" else "ready", run_id, claim_expires, last_heartbeat_at, started_at, task_id),
    )
    return run_id


def test_schema_initializes_runtime_tables(conn):
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {
        "runtime_jobs",
        "goal_contracts",
        "goal_items",
        "execution_nodes",
        "execution_dependencies",
        "node_relations",
        "node_materializations",
        "backend_worker_sessions",
        "progress_ledger",
        "goal_gaps",
        "execution_events",
        "graph_patches",
        "kernel_decisions",
        "decision_sessions",
        "decision_session_segments",
        "decision_segment_entries",
        "decision_checkpoints",
        "node_artifacts",
        "runtime_capability_policies",
        "runtime_capability_authorizations",
    }.issubset(tables)


def test_create_runtime_job_creates_contract_session_and_initial_node(conn):
    job_id = _job(conn)
    status = rk.status_runtime_job(conn, job_id)
    assert status["job"]["state"] == "active"
    assert status["goal_contract"]["state"] == "active"
    assert [item["item_key"] for item in status["goal_items"]] == ["initial-runtime-result"]
    assert status["nodes"][0]["node_key"] == "understand-scope"
    assert status["nodes"][0]["state"] == "ready"
    assert len(status["goal_gaps"]) == 1
    assert status["goal_gaps"][0]["gap_type"] == "missing_evidence"
    assert conn.execute("SELECT COUNT(*) FROM decision_sessions WHERE job_id = ?", (job_id,)).fetchone()[0] == 1


def test_create_runtime_job_defaults_to_provider_first_waiting_decision(conn):
    job_id = rk.create_runtime_job(
        conn,
        _root_task(conn),
        "implement and verify one coherent runtime result",
        goal_items=[{
            "item_key": "coherent-result",
            "description": "coherent result is verified",
            "required": True,
            "verifier_required": True,
        }],
    )

    status = rk.status_runtime_job(conn, job_id)
    assert status["job"]["state"] == "waiting_decision"
    assert status["job"]["decision_profile"] == "graph_patch_decision"
    assert status["job"]["metadata"]["initialization_mode"] == "provider_first"
    assert status["nodes"] == []
    assert status["frontier_summary"]["has_legal_wait"] is True
    assert status["liveness"]["legal_wait"] is True
    assert status["liveness"]["illegal_idle"] is False
    assert status["liveness"]["pending_decision"] is False
    assert status["liveness"]["decision_requested"] is True
    assert rk.runtime_legal_waiting_reason(conn, job_id) == "waiting_decision"
    event = conn.execute(
        "SELECT payload_json FROM execution_events WHERE job_id = ? AND event_type = 'decision_requested'",
        (job_id,),
    ).fetchone()
    assert json.loads(event["payload_json"])["reason"] == "initial_graph_required"
    consistency = rk.check_runtime_consistency(conn, job_id, write_events=False)
    assert consistency["status"] == "passed"
    assert consistency["warnings"] == []


def test_provider_first_default_goal_uses_worker_owned_verification(conn):
    job_id = rk.create_runtime_job(
        conn,
        _root_task(conn),
        "implement, test, and locally verify one coherent result",
    )

    goal_item = rk.status_runtime_job(conn, job_id)["goal_items"][0]

    assert goal_item["item_key"] == "initial-runtime-result"
    assert goal_item["verifier_required"] == 0


def test_provider_first_worker_owned_goal_completes_without_verifier(conn):
    job_id = rk.create_runtime_job(
        conn,
        _root_task(conn),
        "implement, test, and locally verify one coherent result",
    )
    primary = {
        "op": "create_node",
        "node_key": "primary",
        "node_type": "implementation",
        "title": "Own the complete result",
        "description": "Implement, test, debug, and locally verify the requested result.",
        "goal_item_keys": ["initial-runtime-result"],
        "contract": _contract("**"),
    }
    assert rk.apply_graph_patch(conn, job_id, _patch(job_id, 0, primary))["status"] == "applied"
    node = _node(conn, job_id, "primary")
    rk.materialize_runtime_node(conn, dict(node))
    node = _node(conn, job_id, "primary")
    _complete_node(
        conn,
        node,
        {
            "verdict": "succeeded",
            "summary": "result implemented and locally verified",
            "claimed_goal_items": ["initial-runtime-result"],
            "verification": {"passed": True, "summary": "local tests passed"},
        },
    )

    assert rk.ingest_runtime_node_evidence(conn, node["id"])
    status = rk.status_runtime_job(conn, job_id)
    assert status["job"]["state"] == "done"
    assert status["goal_items"][0]["state"] == "satisfied"
    assert [item for item in status["nodes"] if item["node_type"] == "verification"] == []


def test_worker_owned_goal_does_not_complete_when_local_verification_fails(conn):
    job_id = rk.create_runtime_job(conn, _root_task(conn), "locally verify the result")
    primary = {
        "op": "create_node",
        "node_key": "primary",
        "node_type": "implementation",
        "title": "Own the complete result",
        "description": "Implement and locally verify the requested result.",
        "goal_item_keys": ["initial-runtime-result"],
        "contract": _contract("**"),
    }
    assert rk.apply_graph_patch(conn, job_id, _patch(job_id, 0, primary))["status"] == "applied"
    node = _node(conn, job_id, "primary")
    rk.materialize_runtime_node(conn, dict(node))
    node = _node(conn, job_id, "primary")
    _complete_node(
        conn,
        node,
        {
            "verdict": "succeeded",
            "summary": "implementation exists but local verification failed",
            "claimed_goal_items": ["initial-runtime-result"],
            "verification": {"passed": False, "summary": "local test failed"},
        },
    )

    assert rk.ingest_runtime_node_evidence(conn, node["id"])
    status = rk.status_runtime_job(conn, job_id)
    assert status["job"]["state"] != "done"
    assert status["goal_items"][0]["state"] == "partial"
    assert status["progress_ledger"][-1]["verification_state"] == "failed"
    assert any(
        gap["gap_type"] == "verification_failed"
        for gap in status["goal_gaps"]
        if gap["state"] == "open"
    )


def test_provider_first_job_requires_typed_node_contract(conn):
    job_id = rk.create_runtime_job(conn, _root_task(conn), "typed primary node")
    op = {
        "op": "create_node",
        "node_key": "primary",
        "node_type": "implementation",
        "title": "Primary worker",
        "description": "Own the complete runtime result.",
        "goal_item_keys": ["initial-runtime-result"],
    }

    rejected = rk.apply_graph_patch(conn, job_id, _patch(job_id, 0, op))
    assert rejected["status"] == "rejected"
    assert "requires typed contract" in rejected["reason"]

    op["contract"] = _contract("src/**", "tests/**")
    accepted = rk.apply_graph_patch(conn, job_id, _patch(job_id, 0, op))
    assert accepted["status"] == "applied"
    assert _node(conn, job_id, "primary")["state"] == "ready"


@pytest.mark.parametrize(
    "invalid_scope, reason",
    [
        ("repository/**", "use '**' for the whole workspace"),
        ("workspace/**", "use '**' for the whole workspace"),
        ("/tmp/output/**", "canonical workspace-relative globs"),
        ("../outside/**", "canonical workspace-relative globs"),
    ],
)
def test_node_contract_rejects_noncanonical_write_scope(conn, invalid_scope, reason):
    job_id = rk.create_runtime_job(conn, _root_task(conn), "canonical write scope")
    op = {
        "op": "create_node",
        "node_key": "primary",
        "node_type": "implementation",
        "title": "Primary worker",
        "description": "Own the complete runtime result.",
        "goal_item_keys": ["initial-runtime-result"],
        "contract": _contract(invalid_scope),
    }

    result = rk.apply_graph_patch(conn, job_id, _patch(job_id, 0, op))

    assert result["status"] == "rejected"
    assert reason in result["reason"]


@pytest.mark.parametrize("existing_state", sorted(rk.NONTERMINAL_EXECUTION_STATES))
def test_provider_first_job_requires_decomposition_for_existing_nonterminal_node(
    conn,
    existing_state,
):
    job_id = rk.create_runtime_job(conn, _root_task(conn), "nonterminal expansion")
    primary = {
        "op": "create_node",
        "node_key": "primary",
        "node_type": "implementation",
        "title": "Primary worker",
        "description": "Own the first coherent result.",
        "goal_item_keys": ["initial-runtime-result"],
        "contract": _contract("src/primary/**"),
    }
    assert rk.apply_graph_patch(conn, job_id, _patch(job_id, 0, primary))["status"] == "applied"
    conn.execute(
        "UPDATE execution_nodes SET state = ? WHERE job_id = ? AND node_key = 'primary'",
        (existing_state, job_id),
    )
    parallel = {
        "op": "create_node",
        "node_key": "parallel",
        "node_type": "implementation",
        "title": "Parallel worker",
        "description": "Attempt an unjustified parallel responsibility.",
        "goal_item_keys": ["initial-runtime-result"],
        "contract": _contract("src/parallel/**"),
    }

    result = rk.apply_graph_patch(conn, job_id, _patch(job_id, 1, parallel))

    assert result["status"] == "rejected"
    assert "requires decomposition" in result["reason"]


def test_insert_verifier_rejects_worker_owned_goal(conn):
    job_id = rk.create_runtime_job(conn, _root_task(conn), "worker-owned result")
    primary = {
        "op": "create_node",
        "node_key": "primary",
        "node_type": "implementation",
        "title": "Own the complete result",
        "description": "Implement and locally verify the requested result.",
        "goal_item_keys": ["initial-runtime-result"],
        "contract": _contract("**"),
    }
    assert rk.apply_graph_patch(conn, job_id, _patch(job_id, 0, primary))["status"] == "applied"
    verifier = {
        "op": "insert_verifier",
        "target_node_key": "primary",
        "target_workspace_revision": "git:test-revision",
        "verifier_node_key": "unrequested-verifier",
        "title": "Unrequested verifier",
        "goal_item_keys": ["initial-runtime-result"],
        "contract": _contract(),
    }
    patch = _patch(job_id, 1, verifier)
    patch["decomposition"] = {
        "policy_version": "1",
        "mode": "multiple_runtime_nodes",
        "justifications": [{
            "type": "independent_verification",
            "nodes": ["primary", "unrequested-verifier"],
            "explanation": "Attempt to add an unrequested independent verifier.",
            "evidence_refs": [],
        }],
    }

    result = rk.apply_graph_patch(conn, job_id, patch)

    assert result["status"] == "rejected"
    assert "linked goal item with verifier_required=true" in result["reason"]


def test_create_runtime_job_rejects_unknown_initialization_mode(conn):
    with pytest.raises(ValueError, match="unknown runtime initialization mode"):
        rk.create_runtime_job(
            conn,
            _root_task(conn),
            "invalid initialization mode",
            initialization_mode="legacy",
        )


def test_patch_rejects_release_node_and_direct_complete(conn):
    job_id = _job(conn)
    for op_name in ("release_node", "complete_job"):
        result = rk.apply_graph_patch(
            conn,
            job_id,
            _patch(job_id, _revision(conn, job_id), {"op": op_name}),
        )
        assert result["status"] == "rejected"
    assert conn.execute("SELECT COUNT(*) FROM graph_patches WHERE status = 'rejected'").fetchone()[0] == 2


def test_patch_rejects_node_without_goal_or_gap_linkage(conn):
    job_id = _job(conn)
    result = rk.apply_graph_patch(
        conn,
        job_id,
        _patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "create_node",
                "node_key": "free-plan",
                "node_type": "implementation",
                "title": "Free planning",
                "description": "This node is not linked to a goal.",
            },
        ),
    )
    assert result["status"] == "rejected"
    assert "goal_item_keys" in result["reason"]


def test_patch_rejects_stale_revision(conn):
    job_id = _job(conn)
    result = rk.apply_graph_patch(
        conn,
        job_id,
        _patch(
            job_id,
            999,
            {
                "op": "create_node",
                "node_key": "implement-runtime",
                "node_type": "implementation",
                "title": "Implement runtime",
                "description": "Do implementation work.",
                "goal_item_keys": ["initial-runtime-result"],
            },
        ),
    )
    assert result["status"] == "rejected"
    assert "expected_revision" in result["reason"]


def test_patch_validator_rejects_unknown_or_self_authorized_capability(conn):
    job_id = _job(conn)
    unknown = rk.apply_graph_patch(
        conn,
        job_id,
        _patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "create_node",
                "node_key": "network-thing",
                "node_type": "implementation",
                "title": "Network thing",
                "description": "Request an unknown capability.",
                "goal_item_keys": ["initial-runtime-result"],
                "requested_capabilities": ["not_a_capability"],
            },
        ),
    )
    assert unknown["status"] == "rejected"
    assert "unknown capability" in unknown["reason"]

    self_authorized = rk.apply_graph_patch(
        conn,
        job_id,
        _patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "create_node",
                "node_key": "self-authorized",
                "node_type": "implementation",
                "title": "Self authorized",
                "description": "Provider must not write runtime policy.",
                "goal_item_keys": ["initial-runtime-result"],
                "metadata": {"capability_policy": {"allowed": ["secret_access"]}},
            },
        ),
    )
    assert self_authorized["status"] == "rejected"
    assert "capability_policy" in self_authorized["reason"]


def test_patch_validator_accepts_capability_human_request_shape(conn):
    job_id = _job(conn)
    result = rk.apply_graph_patch(
        conn,
        job_id,
        _patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "request_human",
                "node_key": "authorize-secret",
                "decision_type": "permission",
                "question": "Allow secret access?",
                "why_user_required": "Secret access can expose credentials.",
                "risk_if_defaulted": "The node remains blocked.",
                "default_recommendation": "Do not allow unless credentials are required.",
                "goal_item_keys": ["initial-runtime-result"],
                "capability_request": {
                    "capabilities": ["secret_access"],
                    "scope": "job",
                    "reason": "Worker needs a credential to continue.",
                },
            },
        ),
    )
    assert result["status"] == "applied"


def test_dependency_cycle_rejected(conn):
    job_id = _job(conn)
    for node_key in ("a", "b"):
        result = rk.apply_graph_patch(
            conn,
            job_id,
            _patch(
                job_id,
                _revision(conn, job_id),
                {
                    "op": "create_node",
                    "node_key": node_key,
                    "node_type": "implementation",
                    "title": node_key,
                    "description": node_key,
                    "goal_item_keys": ["initial-runtime-result"],
                },
            ),
        )
        assert result["status"] == "applied"
    assert rk.apply_graph_patch(
        conn,
        job_id,
        _patch(job_id, _revision(conn, job_id), {"op": "add_dependency", "from_node_key": "a", "to_node_key": "b"}),
    )["status"] == "applied"
    result = rk.apply_graph_patch(
        conn,
        job_id,
        _patch(job_id, _revision(conn, job_id), {"op": "add_dependency", "from_node_key": "b", "to_node_key": "a"}),
    )
    assert result["status"] == "rejected"
    assert "cycle" in result["reason"]


def test_reducer_computes_ready_without_release_node(conn):
    job_id = _job(conn)
    result = rk.apply_graph_patch(
        conn,
        job_id,
        _patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "create_node",
                "node_key": "after-analysis",
                "node_type": "implementation",
                "title": "After analysis",
                "description": "Runs only after analysis.",
                "goal_item_keys": ["initial-runtime-result"],
                "depends_on": ["understand-scope"],
            },
        ),
    )
    assert result["status"] == "applied"
    assert _node(conn, job_id, "after-analysis")["state"] == "waiting_dependency"
    conn.execute("UPDATE execution_nodes SET state = 'succeeded' WHERE job_id = ? AND node_key = 'understand-scope'", (job_id,))
    rk.reduce_runtime_job(conn, job_id)
    assert _node(conn, job_id, "after-analysis")["state"] == "ready"


def test_materialization_is_idempotent(conn):
    job_id = _job(conn)
    first = rk.advance_runtime_job(conn, job_id, create_tasks=True)
    second = rk.advance_runtime_job(conn, job_id, create_tasks=True)
    assert first.materialized_nodes == ["understand-scope"]
    assert second.materialized_nodes == []
    assert conn.execute("SELECT COUNT(*) FROM node_materializations WHERE job_id = ?", (job_id,)).fetchone()[0] == 1


def test_default_allowed_capabilities_materialize_and_reach_worker_context(conn):
    job_id = _job(conn)
    result = rk.apply_graph_patch(
        conn,
        job_id,
        _patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "create_node",
                "node_key": "allowed-workspace-work",
                "node_type": "implementation",
                "title": "Allowed workspace work",
                "description": "Run ordinary workspace implementation.",
                "goal_item_keys": ["initial-runtime-result"],
                "requested_capabilities": ["workspace_write", "process_spawn"],
            },
        ),
    )
    assert result["status"] == "applied"
    node = _node(conn, job_id, "allowed-workspace-work")
    task_id = rk.materialize_runtime_node(conn, dict(node))
    assert task_id
    body = kb.get_task(conn, task_id).body or ""
    assert "runtime_capability_policy" in body
    assert "workspace_write" in body
    assert "process_spawn" in body
    assert conn.execute("SELECT COUNT(*) FROM node_materializations WHERE node_id = ?", (node["id"],)).fetchone()[0] == 1


def test_denied_capability_does_not_materialize_and_is_observable(conn):
    job_id = _job(conn)
    assert rk.apply_graph_patch(
        conn,
        job_id,
        _patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "create_node",
                "node_key": "network-node",
                "node_type": "research",
                "title": "Network node",
                "description": "Try to use network access.",
                "goal_item_keys": ["initial-runtime-result"],
                "requested_capabilities": ["network_access"],
            },
        ),
    )["status"] == "applied"
    node = _node(conn, job_id, "network-node")

    assert rk.materialize_runtime_node(conn, dict(node)) is None

    node = _node(conn, job_id, "network-node")
    assert node["state"] == "blocked"
    assert conn.execute("SELECT COUNT(*) FROM node_materializations WHERE node_id = ?", (node["id"],)).fetchone()[0] == 0
    assert rk.runtime_legal_waiting_reason(conn, job_id) == "blocked_by_policy"
    summary = rk.summarize_runtime_capabilities(conn, job_id)
    assert summary["blocked_nodes"][0]["status"] == "denied"
    assert summary["blocked_nodes"][0]["denied"] == ["network_access"]
    events = [row["event_type"] for row in conn.execute("SELECT event_type FROM execution_events WHERE job_id = ?", (job_id,))]
    assert "capability_denied" in events
    assert "capability_policy_blocked" in events


def test_require_human_capability_waits_for_authorization_then_materializes(conn):
    job_id = _job(conn)
    assert rk.apply_graph_patch(
        conn,
        job_id,
        _patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "create_node",
                "node_key": "secret-node",
                "node_type": "implementation",
                "title": "Secret node",
                "description": "Use a secret only after authorization.",
                "goal_item_keys": ["initial-runtime-result"],
                "requested_capabilities": ["secret_access"],
            },
        ),
    )["status"] == "applied"
    node = _node(conn, job_id, "secret-node")
    assert rk.materialize_runtime_node(conn, dict(node)) is None
    assert _node(conn, job_id, "secret-node")["state"] == "waiting_human"
    assert rk.runtime_legal_waiting_reason(conn, job_id) == "waiting_capability_authorization"

    authorization = rk.authorize_runtime_capability(
        conn,
        job_id,
        ["secret_access"],
        reason="User approved secret access for this job.",
    )

    assert authorization["reenabled_nodes"] == ["secret-node"]
    node = _node(conn, job_id, "secret-node")
    assert node["state"] == "ready"
    assert rk.materialize_runtime_node(conn, dict(node))
    assert _node(conn, job_id, "secret-node")["state"] == "running"


def test_expired_or_revoked_authorization_does_not_allow_capability(conn):
    job_id = _job(conn)
    now = rk._now()
    for status, expires_at, revoked_at in (("active", now - 1, None), ("revoked", None, now)):
        conn.execute(
            """
            INSERT INTO runtime_capability_authorizations (
                id, job_id, scope_type, scope_ref, capabilities_json, status,
                expires_at, revoked_at, reason, created_at, updated_at, metadata_json
            ) VALUES (?, ?, 'job', NULL, ?, ?, ?, ?, 'test invalid auth', ?, ?, '{}')
            """,
            (f"auth_{status}", job_id, json.dumps(["secret_access"]), status, expires_at, revoked_at, now, now),
        )
    assert rk.apply_graph_patch(
        conn,
        job_id,
        _patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "create_node",
                "node_key": "secret-invalid-auth",
                "node_type": "implementation",
                "title": "Secret invalid auth",
                "description": "Invalid auth must not allow this.",
                "goal_item_keys": ["initial-runtime-result"],
                "requested_capabilities": ["secret_access"],
            },
        ),
    )["status"] == "applied"
    node = _node(conn, job_id, "secret-invalid-auth")
    assert rk.materialize_runtime_node(conn, dict(node)) is None
    assert _node(conn, job_id, "secret-invalid-auth")["state"] == "waiting_human"


def test_human_authorization_cannot_override_hard_deny(conn):
    job_id = _job(conn)
    rk.authorize_runtime_capability(
        conn,
        job_id,
        ["network_access"],
        reason="Even explicit authorization cannot override hard deny.",
    )
    assert rk.apply_graph_patch(
        conn,
        job_id,
        _patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "create_node",
                "node_key": "network-still-denied",
                "node_type": "research",
                "title": "Network still denied",
                "description": "Hard deny wins over authorization.",
                "goal_item_keys": ["initial-runtime-result"],
                "requested_capabilities": ["network_access"],
            },
        ),
    )["status"] == "applied"
    node = _node(conn, job_id, "network-still-denied")
    assert rk.materialize_runtime_node(conn, dict(node)) is None
    summary = rk.summarize_runtime_capabilities(conn, job_id)
    assert summary["active_authorizations"][0]["capabilities"] == ["network_access"]
    assert summary["blocked_nodes"][0]["status"] == "denied"


def test_lane_physical_incapability_wins_over_authorization(conn):
    job_id = _job(conn)
    rk.authorize_runtime_capability(
        conn,
        job_id,
        ["process_spawn"],
        reason="User authorization cannot add a capability the lane lacks.",
    )
    assert rk.apply_graph_patch(
        conn,
        job_id,
        _patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "create_node",
                "node_key": "lane-lacks-process",
                "node_type": "implementation",
                "title": "Lane lacks process",
                "description": "Lane cannot spawn processes.",
                "goal_item_keys": ["initial-runtime-result"],
                "requested_capabilities": ["process_spawn"],
            },
        ),
    )["status"] == "applied"
    node = _node(conn, job_id, "lane-lacks-process")
    metadata = json.loads(node["metadata_json"])
    metadata["capability_policy"] = {"lane_incapable": ["process_spawn"]}
    conn.execute("UPDATE execution_nodes SET metadata_json = ? WHERE id = ?", (json.dumps(metadata), node["id"]))
    assert rk.materialize_runtime_node(conn, dict(_node(conn, job_id, "lane-lacks-process"))) is None
    summary = rk.summarize_runtime_capabilities(conn, job_id)
    assert summary["blocked_nodes"][0]["status"] == "lane_incapable"


def test_reconcile_missing_task_schedules_retry_attempt(conn):
    job_id = _job(conn)
    assert rk.advance_runtime_job(conn, job_id, create_tasks=True).materialized_nodes == ["understand-scope"]
    node = _node(conn, job_id, "understand-scope")
    first_task_id = node["latest_task_id"]
    conn.execute("DELETE FROM tasks WHERE id = ?", (first_task_id,))

    result = rk.reconcile_runtime_materializations(conn, job_id)

    assert result["events"] == ["materialization_lost"]
    assert result["scheduled_retries"] == ["understand-scope"]
    node = _node(conn, job_id, "understand-scope")
    assert node["state"] == "ready"
    old_mat = conn.execute(
        "SELECT * FROM node_materializations WHERE job_id = ? AND attempt = 1",
        (job_id,),
    ).fetchone()
    assert old_mat["status"] == "lost"

    advanced = rk.advance_runtime_job(conn, job_id, create_tasks=True)
    assert advanced.materialized_nodes == ["understand-scope"]
    mats = conn.execute(
        "SELECT attempt, task_id, status FROM node_materializations WHERE job_id = ? ORDER BY attempt",
        (job_id,),
    ).fetchall()
    assert [row["attempt"] for row in mats] == [1, 2]
    assert mats[0]["task_id"] != mats[1]["task_id"]
    assert mats[1]["status"] == "running"


def test_reconcile_receipt_missing_does_not_ingest_goal_evidence(conn):
    job_id = _job(conn)
    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    node = _node(conn, job_id, "understand-scope")
    assert kb.complete_task(conn, node["latest_task_id"], result="done without receipt", summary="done without receipt")

    result = rk.reconcile_runtime_materializations(conn, job_id)

    assert result["events"] == ["receipt_missing"]
    assert result["scheduled_retries"] == ["understand-scope"]
    assert rk.status_runtime_job(conn, job_id)["progress_ledger"] == []
    node = _node(conn, job_id, "understand-scope")
    assert node["state"] == "ready"
    mat = conn.execute(
        "SELECT status FROM node_materializations WHERE job_id = ? AND attempt = 1",
        (job_id,),
    ).fetchone()
    assert mat["status"] == "receipt_missing"


def test_receipt_recovery_budget_is_independent_from_prior_infra_failure(conn):
    job_id = _job(conn)
    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    node = _node(conn, job_id, "understand-scope")
    first_run_id = _install_task_run(
        conn,
        node["latest_task_id"],
        status="crashed",
        outcome="crashed",
        started_at=100,
        ended_at=120,
    )
    conn.execute(
        "UPDATE node_materializations SET run_id = ? WHERE node_id = ?",
        (first_run_id, node["id"]),
    )

    first_recovery = rk.reconcile_runtime_materializations(conn, job_id, now=200)
    assert first_recovery["scheduled_retries"] == ["understand-scope"]
    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    node = _node(conn, job_id, "understand-scope")
    _complete_node(
        conn,
        node,
        {
            "worker_lane": {
                "name": "codex-smoke",
                "kind": "codex_cli",
                "exit_code": 0,
            },
            "runtime_receipt": {
                "schema": "runtime_worker_receipt_v1",
                "status": "completed",
                "verification": [],
            },
        },
    )

    receipt_recovery = rk.reconcile_runtime_materializations(
        conn,
        job_id,
        now=300,
    )

    assert receipt_recovery["events"] == ["receipt_invalid"]
    assert receipt_recovery["scheduled_retries"] == ["understand-scope"]
    assert receipt_recovery["failed_nodes"] == []
    assert _node(conn, job_id, "understand-scope")["state"] == "ready"
    advanced = rk.advance_runtime_job(conn, job_id, create_tasks=True)
    assert advanced.materialized_nodes == ["understand-scope"]
    retried_node = _node(conn, job_id, "understand-scope")
    retried_task = conn.execute(
        "SELECT body FROM tasks WHERE id = ?",
        (retried_node["latest_task_id"],),
    ).fetchone()
    assert "Receipt protocol recovery" in retried_task["body"]
    assert "do not substitute status/outcome fields" in retried_task["body"]


def test_reconcile_worker_run_stale_without_heartbeat_schedules_retry(conn):
    job_id = _job(conn)
    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    node = _node(conn, job_id, "understand-scope")
    run_id = _install_task_run(
        conn,
        node["latest_task_id"],
        status="running",
        started_at=100,
        claim_expires=1000,
        last_heartbeat_at=None,
    )
    conn.execute("UPDATE node_materializations SET run_id = ? WHERE node_id = ?", (run_id, node["id"]))

    result = rk.reconcile_runtime_materializations(
        conn,
        job_id,
        now=200,
        policy={"run_stale_after_seconds": 50},
    )

    assert result["events"] == ["worker_run_stale"]
    assert result["scheduled_retries"] == ["understand-scope"]
    node = _node(conn, job_id, "understand-scope")
    assert node["state"] == "ready"
    mat = conn.execute("SELECT status FROM node_materializations WHERE node_id = ? AND attempt = 1", (node["id"],)).fetchone()
    assert mat["status"] == "stale"


def test_reconcile_worker_run_timeout_schedules_retry(conn):
    job_id = _job(conn)
    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    node = _node(conn, job_id, "understand-scope")
    run_id = _install_task_run(
        conn,
        node["latest_task_id"],
        status="running",
        started_at=100,
        claim_expires=150,
        last_heartbeat_at=140,
    )
    conn.execute("UPDATE node_materializations SET run_id = ? WHERE node_id = ?", (run_id, node["id"]))

    result = rk.reconcile_runtime_materializations(conn, job_id, now=200)

    assert result["events"] == ["worker_run_timeout"]
    assert result["scheduled_retries"] == ["understand-scope"]
    mat = conn.execute("SELECT status FROM node_materializations WHERE node_id = ? AND attempt = 1", (node["id"],)).fetchone()
    assert mat["status"] == "timed_out"


def test_reconcile_worker_run_crashed_schedules_retry(conn):
    job_id = _job(conn)
    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    node = _node(conn, job_id, "understand-scope")
    run_id = _install_task_run(
        conn,
        node["latest_task_id"],
        status="crashed",
        outcome="crashed",
        started_at=100,
        ended_at=120,
    )
    conn.execute("UPDATE node_materializations SET run_id = ? WHERE node_id = ?", (run_id, node["id"]))

    result = rk.reconcile_runtime_materializations(conn, job_id, now=200)

    assert result["events"] == ["worker_run_crashed"]
    assert result["scheduled_retries"] == ["understand-scope"]
    mat = conn.execute("SELECT status FROM node_materializations WHERE node_id = ? AND attempt = 1", (node["id"],)).fetchone()
    assert mat["status"] == "crashed"


def test_new_backend_session_interrupts_prior_active_session_for_same_node(conn):
    job_id = _job(conn)
    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    node = _node(conn, job_id, "understand-scope")
    materialization = conn.execute(
        "SELECT * FROM node_materializations WHERE node_id = ?",
        (node["id"],),
    ).fetchone()
    first_session = "019f0000-0000-7000-8000-000000000011"
    second_session = "019f0000-0000-7000-8000-000000000012"
    kb.record_task_event(
        conn,
        node["latest_task_id"],
        "worker_backend_session_started",
        {
            "worker_lane": materialization["worker_lane"],
            "worker_kind": "codex_cli",
            "backend_session_id": first_session,
            "execution_mode": "fresh",
        },
    )
    rk.sync_runtime_backend_sessions(conn, job_id)
    kb.record_task_event(
        conn,
        node["latest_task_id"],
        "worker_backend_session_started",
        {
            "worker_lane": materialization["worker_lane"],
            "worker_kind": "codex_cli",
            "backend_session_id": second_session,
            "execution_mode": "fresh",
        },
    )

    synced = rk.sync_runtime_backend_sessions(conn, job_id)

    sessions = {
        row["backend_session_key"]: row["status"]
        for row in conn.execute(
            "SELECT backend_session_key, status FROM backend_worker_sessions WHERE node_id = ?",
            (node["id"],),
        ).fetchall()
    }
    assert sessions == {first_session: "interrupted", second_session: "active"}
    assert len(synced["discovered"]) == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM execution_events WHERE job_id = ? AND event_type = 'worker_session_superseded'",
        (job_id,),
    ).fetchone()[0] == 1


def test_crashed_materialization_resumes_discovered_backend_session(conn, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "partial.txt").write_text("partial\n", encoding="utf-8")
    job_id = rk.create_runtime_job(
        conn,
        _root_task(conn),
        "finish one resumable runtime node",
        workspace_path=str(workspace),
        goal_items=[{
            "item_key": "resumable-result",
            "description": "resumable result exists",
            "required": True,
            "verifier_required": True,
        }],
        initialization_mode="fixture",
    )
    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    node = _node(conn, job_id, "understand-scope")
    materialization = conn.execute(
        "SELECT * FROM node_materializations WHERE node_id = ?",
        (node["id"],),
    ).fetchone()
    kb.record_task_event(
        conn,
        node["latest_task_id"],
        "worker_backend_session_started",
        {
            "worker_lane": materialization["worker_lane"],
            "worker_kind": "codex_cli",
            "backend_session_id": "019f0000-0000-7000-8000-000000000002",
            "execution_mode": "fresh",
        },
        run_id=node["latest_run_id"],
    )
    run_id = _install_task_run(
        conn,
        node["latest_task_id"],
        status="crashed",
        outcome="crashed",
        started_at=100,
        ended_at=120,
    )
    conn.execute(
        "UPDATE node_materializations SET run_id = ? WHERE id = ?",
        (run_id, materialization["id"]),
    )

    reconciled = rk.reconcile_runtime_materializations(conn, job_id, now=200)
    assert reconciled["scheduled_retries"] == ["understand-scope"]
    session = conn.execute(
        "SELECT * FROM backend_worker_sessions WHERE node_id = ?",
        (node["id"],),
    ).fetchone()
    assert session["status"] == "interrupted"
    assert session["backend_session_key"].endswith("0002")

    advanced = rk.advance_runtime_job(conn, job_id, create_tasks=True)
    assert advanced.materialized_nodes == ["understand-scope"]
    attempt = conn.execute(
        "SELECT * FROM node_materializations WHERE node_id = ? AND attempt = 2",
        (node["id"],),
    ).fetchone()
    continuity = json.loads(attempt["metadata_json"])["execution_continuity"]
    assert continuity["mode"] == "resume", continuity.get("rejection_reasons")
    assert continuity["resume_session_id"].endswith("0002")
    assert continuity["resume_from_materialization_id"] == materialization["id"]
    assert continuity["context_reacquisition"] is False
    session = conn.execute(
        "SELECT * FROM backend_worker_sessions WHERE id = ?",
        (session["id"],),
    ).fetchone()
    assert session["status"] == "resume_pending"
    assert session["resume_count"] == 1
    synced = rk.sync_runtime_backend_sessions(conn, job_id)
    assert synced["discovered"] == []
    assert conn.execute(
        "SELECT COUNT(*) FROM backend_worker_sessions WHERE node_id = ?",
        (node["id"],),
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT status FROM backend_worker_sessions WHERE id = ?",
        (session["id"],),
    ).fetchone()[0] == "resume_pending"
    observability = rd.runtime_observability_snapshot(conn, job_id)
    continuity_status = observability["worker_execution_continuity"]
    assert continuity_status["session_count"] == 1
    assert continuity_status["materialization_modes"] == {"fresh": 1, "resume": 1}
    assert rk.check_runtime_consistency(conn, job_id, write_events=False)["status"] == "passed"

    kb.record_task_event(
        conn,
        attempt["task_id"],
        "worker_backend_session_resume_failed",
        {
            "worker_lane": attempt["worker_lane"],
            "backend_session_id": continuity["resume_session_id"],
            "reason": "fake backend session unavailable",
        },
        run_id=attempt["run_id"],
    )
    assert kb.block_task(
        conn,
        attempt["task_id"],
        reason="codex-resume-failed: fake backend session unavailable",
        expected_run_id=attempt["run_id"],
    )
    recovered = rk.reconcile_runtime_materializations(
        conn,
        job_id,
        policy={"receipt_recovery_limit": 3},
    )
    assert recovered["scheduled_retries"] == ["understand-scope"]
    assert conn.execute(
        "SELECT status FROM backend_worker_sessions WHERE id = ?",
        (session["id"],),
    ).fetchone()[0] == "resume_failed"

    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    fallback = conn.execute(
        "SELECT * FROM node_materializations WHERE node_id = ? AND attempt = 3",
        (node["id"],),
    ).fetchone()
    fallback_continuity = json.loads(fallback["metadata_json"])["execution_continuity"]
    assert fallback_continuity["mode"] == "fallback_fresh"
    assert "session_status_resume_failed" in fallback_continuity["rejection_reasons"]
    assert fallback_continuity["context_reacquisition"] is True


def test_workspace_revision_change_falls_back_to_fresh_attempt(conn, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tracked = workspace / "state.txt"
    tracked.write_text("before\n", encoding="utf-8")
    job_id = rk.create_runtime_job(
        conn,
        _root_task(conn),
        "finish one resumable runtime node",
        workspace_path=str(workspace),
        goal_items=[{
            "item_key": "resumable-result",
            "description": "resumable result exists",
            "required": True,
            "verifier_required": True,
        }],
        initialization_mode="fixture",
    )
    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    node = _node(conn, job_id, "understand-scope")
    materialization = conn.execute(
        "SELECT * FROM node_materializations WHERE node_id = ?",
        (node["id"],),
    ).fetchone()
    kb.record_task_event(
        conn,
        node["latest_task_id"],
        "worker_backend_session_started",
        {
            "worker_lane": materialization["worker_lane"],
            "worker_kind": "codex_cli",
            "backend_session_id": "019f0000-0000-7000-8000-000000000003",
        },
        run_id=node["latest_run_id"],
    )
    run_id = _install_task_run(
        conn,
        node["latest_task_id"],
        status="crashed",
        outcome="crashed",
        started_at=100,
        ended_at=120,
    )
    conn.execute(
        "UPDATE node_materializations SET run_id = ? WHERE id = ?",
        (run_id, materialization["id"]),
    )
    rk.reconcile_runtime_materializations(conn, job_id, now=200)
    tracked.write_text("after with external change\n", encoding="utf-8")

    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    attempt = conn.execute(
        "SELECT * FROM node_materializations WHERE node_id = ? AND attempt = 2",
        (node["id"],),
    ).fetchone()
    continuity = json.loads(attempt["metadata_json"])["execution_continuity"]
    assert continuity["mode"] == "fallback_fresh"
    assert "workspace_revision_mismatch" in continuity["rejection_reasons"]
    assert continuity["context_reacquisition"] is True
    events = {
        row["event_type"]
        for row in conn.execute(
            "SELECT event_type FROM execution_events WHERE job_id = ?",
            (job_id,),
        ).fetchall()
    }
    assert "worker_session_fallback_fresh" in events
    assert "worker_context_reacquired" in events


def test_reconcile_retry_limit_marks_node_not_retryable(conn):
    job_id = _job(conn)
    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    node = _node(conn, job_id, "understand-scope")
    run_id = _install_task_run(
        conn,
        node["latest_task_id"],
        status="crashed",
        outcome="crashed",
        started_at=100,
        ended_at=120,
    )
    conn.execute("UPDATE node_materializations SET run_id = ? WHERE node_id = ?", (run_id, node["id"]))

    result = rk.reconcile_runtime_materializations(conn, job_id, now=200, policy={"infra_retry_limit": 0})

    assert result["events"] == ["worker_run_crashed"]
    assert result["scheduled_retries"] == []
    assert result["failed_nodes"] == ["understand-scope"]
    node = _node(conn, job_id, "understand-scope")
    assert node["state"] == "failed"
    events = [
        row["event_type"]
        for row in conn.execute("SELECT event_type FROM execution_events WHERE job_id = ?", (job_id,))
    ]
    assert "node_recovery_not_retryable" in events


def test_business_failure_receipt_is_ingested_not_retried(conn):
    job_id = _job(conn)
    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    node = _node(conn, job_id, "understand-scope")
    _complete_node(
        conn,
        node,
        {
            "verdict": "failed",
            "summary": "business validation failed",
            "unmet_goal_items": ["initial-runtime-result"],
            "verification": {"passed": False, "summary": "assertion failed"},
        },
    )

    result = rk.advance_runtime_job(conn, job_id, create_tasks=False)

    assert result.recovery["events"] == []
    node = _node(conn, job_id, "understand-scope")
    assert node["state"] == "failed"
    assert conn.execute("SELECT COUNT(*) FROM node_materializations WHERE node_id = ?", (node["id"],)).fetchone()[0] == 1
    assert any(gap["gap_type"] == "failed_required_node" for gap in rk.status_runtime_job(conn, job_id)["goal_gaps"] if gap["state"] == "open")


def test_failed_verifier_does_not_rewrite_implementation_success(conn):
    job_id = _job(conn)
    impl_result = rk.apply_graph_patch(
        conn,
        job_id,
        _patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "create_node",
                "node_key": "implement-runtime",
                "node_type": "implementation",
                "title": "Implement runtime",
                "description": "Produce the runtime artifact.",
                "goal_item_keys": ["initial-runtime-result"],
            },
        ),
    )
    assert impl_result["status"] == "applied"
    impl = _node(conn, job_id, "implement-runtime")
    rk.materialize_runtime_node(conn, dict(impl))
    impl = _node(conn, job_id, "implement-runtime")
    _complete_node(
        conn,
        impl,
        {
            "verdict": "succeeded",
            "summary": "implementation self-reported success",
            "claimed_goal_items": ["initial-runtime-result"],
            "verification": {"passed": False},
        },
    )
    assert rk.ingest_runtime_node_evidence(conn, impl["id"])
    assert _node(conn, job_id, "implement-runtime")["state"] == "succeeded"

    verify_result = rk.apply_graph_patch(
        conn,
        job_id,
        {
            **_patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "insert_verifier",
                "target_node_key": "implement-runtime",
                "target_materialization_attempt": 1,
                "verifier_node_key": "verify-runtime",
                "title": "Verify runtime",
                "goal_item_keys": ["initial-runtime-result"],
                "gap_keys": ["initial-runtime-result:needs_verification"],
            },
            ),
            "decomposition": {
                "policy_version": "1",
                "mode": "multiple_runtime_nodes",
                "justifications": [{
                    "type": "independent_verification",
                    "nodes": ["implement-runtime", "verify-runtime"],
                    "explanation": "Verifier must not inherit implementation assumptions.",
                    "evidence_refs": [],
                }],
            },
        },
    )
    assert verify_result["status"] == "applied"
    verifier = _node(conn, job_id, "verify-runtime")
    rk.materialize_runtime_node(conn, dict(verifier))
    verifier = _node(conn, job_id, "verify-runtime")
    _complete_node(
        conn,
        verifier,
        {
            "verdict": "failed",
            "summary": "verifier failed",
            "contradicted_goal_items": ["initial-runtime-result"],
            "verification": {"passed": False, "summary": "pytest failed"},
        },
    )
    assert rk.ingest_runtime_node_evidence(conn, verifier["id"])

    assert _node(conn, job_id, "implement-runtime")["state"] == "succeeded"
    assert _node(conn, job_id, "verify-runtime")["state"] == "failed"
    assert any(
        gap["gap_type"] in {"contradicted_evidence", "verification_failed"}
        for gap in rk.status_runtime_job(conn, job_id)["goal_gaps"]
        if gap["state"] == "open"
    )


def test_reconcile_is_idempotent_for_same_missing_task(conn):
    job_id = _job(conn)
    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    node = _node(conn, job_id, "understand-scope")
    conn.execute("DELETE FROM tasks WHERE id = ?", (node["latest_task_id"],))

    first = rk.reconcile_runtime_materializations(conn, job_id)
    second = rk.reconcile_runtime_materializations(conn, job_id)

    assert first["events"] == ["materialization_lost"]
    assert second["events"] == []
    events = [
        row["event_type"]
        for row in conn.execute("SELECT event_type FROM execution_events WHERE job_id = ?", (job_id,))
    ]
    assert events.count("materialization_lost") == 1


def test_consistency_checker_reports_missing_materialization_task(conn):
    job_id = _job(conn)
    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    node = _node(conn, job_id, "understand-scope")
    conn.execute("DELETE FROM tasks WHERE id = ?", (node["latest_task_id"],))

    result = rk.check_runtime_consistency(conn, job_id)

    assert result["status"] == "failed"
    assert any(item["type"] == "materialization_task_missing" for item in result["violations"])
    events = [
        row["event_type"]
        for row in conn.execute("SELECT event_type FROM execution_events WHERE job_id = ?", (job_id,))
    ]
    assert "consistency_violation" in events


def test_consistency_checker_reports_ledger_and_checkpoint_reference_breaks(conn):
    job_id = _job(conn)
    contract = conn.execute("SELECT * FROM goal_contracts WHERE job_id = ?", (job_id,)).fetchone()
    goal_item = conn.execute("SELECT * FROM goal_items WHERE contract_id = ?", (contract["id"],)).fetchone()
    session = conn.execute("SELECT * FROM decision_sessions WHERE job_id = ?", (job_id,)).fetchone()
    now = 200
    conn.execute(
        """
        INSERT INTO progress_ledger (
            id, job_id, contract_id, goal_item_id, node_id, artifact_id,
            evidence_ref, satisfaction, verification_state, confidence,
            summary, metadata_json, created_at
        ) VALUES ('pledger_missing_node', ?, ?, ?, 'node_missing', NULL,
                  'node:node_missing', 'full', 'verified', 1.0,
                  'bad ledger ref', '{}', ?)
        """,
        (job_id, contract["id"], goal_item["id"], now),
    )
    checkpoint_payload = {
        "key_decisions": [
            {
                "summary": "bad checkpoint ref",
                "source_refs": [
                    {"node_key": "missing-node"},
                    {"goal_item_key": "missing-goal"},
                    {"event_id": 999999},
                    {"decision_id": "kdec_missing"},
                    {"patch_id": "gpatch_missing"},
                    {"artifact_ref": "artifact://missing"},
                ],
            }
        ]
    }
    conn.execute(
        """
        INSERT INTO decision_checkpoints (
            id, job_id, decision_session_id, revision, checkpoint_json,
            reason, created_at, source_segment_id, checkpoint_revision,
            db_revision, graph_revision, ledger_revision, payload_json,
            validator_status
        ) VALUES ('chk_bad_refs', ?, ?, 1, '{}', 'test', ?, NULL, 1, 0, 0, 0, ?, 'accepted')
        """,
        (job_id, session["id"], now, json.dumps(checkpoint_payload, ensure_ascii=False)),
    )

    result = rk.check_runtime_consistency(conn, job_id)

    types = {item["type"] for item in result["violations"]}
    assert "ledger_node_missing" in types
    assert "checkpoint_node_missing" in types
    assert "checkpoint_goal_item_missing" in types
    assert "checkpoint_event_missing" in types
    assert "checkpoint_decision_missing" in types
    assert "checkpoint_patch_missing" in types
    assert "checkpoint_artifact_missing" in types
    events = [
        row["event_type"]
        for row in conn.execute("SELECT event_type FROM execution_events WHERE job_id = ?", (job_id,))
    ]
    assert "ledger_reference_missing" in events
    assert "checkpoint_reference_missing" in events


def test_observability_snapshot_exposes_recovery_and_consistency(conn):
    job_id = _job(conn)
    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    node = _node(conn, job_id, "understand-scope")
    conn.execute("DELETE FROM tasks WHERE id = ?", (node["latest_task_id"],))
    rk.reconcile_runtime_materializations(conn, job_id)

    payload = rd.runtime_observability_snapshot(conn, job_id)

    assert payload["legal_waiting_reason"] == "ready_to_materialize"
    assert payload["recovery"]["open_recovery_events"]
    assert payload["consistency"]["status"] == "failed"
    assert any(item["type"] == "materialization_task_missing" for item in payload["consistency"]["violations"])


def test_observability_snapshot_exposes_capability_policy(conn):
    job_id = _job(conn)
    assert rk.apply_graph_patch(
        conn,
        job_id,
        _patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "create_node",
                "node_key": "needs-secret",
                "node_type": "implementation",
                "title": "Needs secret",
                "description": "Needs a human capability authorization.",
                "goal_item_keys": ["initial-runtime-result"],
                "requested_capabilities": ["secret_access"],
            },
        ),
    )["status"] == "applied"
    rk.materialize_runtime_node(conn, dict(_node(conn, job_id, "needs-secret")))

    payload = rd.runtime_observability_snapshot(conn, job_id)

    assert payload["legal_waiting_reason"] == "waiting_capability_authorization"
    assert payload["capabilities"]["pending_authorizations"][0]["node_key"] == "needs-secret"
    assert payload["capabilities"]["policy_resolution_order"][0] == "lane/backend physical incapability"


def test_advance_lock_is_exclusive_and_expires(conn):
    job_id = _job(conn)
    first = rk.acquire_runtime_advance_lock(conn, job_id, owner="supervisor-a", ttl_seconds=60)
    second = rk.acquire_runtime_advance_lock(conn, job_id, owner="supervisor-b", ttl_seconds=60)

    assert first["acquired"] is True
    assert second["acquired"] is False
    assert second["reason"] == "locked"
    assert second["held_by"] == "supervisor-a"

    conn.execute(
        "UPDATE runtime_jobs SET claim_expires_at = ? WHERE id = ?",
        (0, job_id),
    )
    third = rk.acquire_runtime_advance_lock(conn, job_id, owner="supervisor-b", ttl_seconds=60)
    assert third["acquired"] is True
    assert third["owner"] == "supervisor-b"

    released = rk.release_runtime_advance_lock(conn, job_id, owner="supervisor-b")
    assert released["released"] is True
    job = conn.execute("SELECT advance_lock, claim_expires_at FROM runtime_jobs WHERE id = ?", (job_id,)).fetchone()
    assert job["advance_lock"] is None
    assert job["claim_expires_at"] is None
    events = [
        row["event_type"]
        for row in conn.execute("SELECT event_type FROM execution_events WHERE job_id = ?", (job_id,))
    ]
    assert "advance_lock_acquired" in events
    assert "advance_lock_released" in events


def test_supervisor_tick_uses_lock_and_does_not_duplicate_materialization(conn):
    job_id = _job(conn)

    first = rk.supervisor_runtime_tick(conn, job_id, owner="supervisor-a", create_tasks=True)
    second = rk.supervisor_runtime_tick(conn, job_id, owner="supervisor-a", create_tasks=True)

    assert first["status"] == "advanced"
    assert first["result"]["materialized_nodes"] == ["understand-scope"]
    assert second["status"] == "advanced"
    assert second["result"]["materialized_nodes"] == []
    assert conn.execute("SELECT COUNT(*) FROM node_materializations WHERE job_id = ?", (job_id,)).fetchone()[0] == 1
    starts = conn.execute(
        "SELECT payload_json FROM execution_events "
        "WHERE job_id = ? AND event_type = 'runtime_supervisor_started'",
        (job_id,),
    ).fetchall()
    assert len(starts) == 1
    assert json.loads(starts[0]["payload_json"])["owner"] == "supervisor-a"

    held = rk.acquire_runtime_advance_lock(conn, job_id, owner="supervisor-held", ttl_seconds=60)
    assert held["acquired"] is True
    skipped = rk.supervisor_runtime_tick(conn, job_id, owner="supervisor-other", create_tasks=True)
    assert skipped["status"] == "skipped"
    assert skipped["reason"] == "locked"
    rk.release_runtime_advance_lock(conn, job_id, owner="supervisor-held")


def test_fake_evidence_updates_progress_ledger(conn):
    job_id = _job(conn)
    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    node = _node(conn, job_id, "understand-scope")
    _complete_node(
        conn,
        node,
        {
            "verdict": "succeeded",
            "summary": "verified runtime evidence",
            "claimed_goal_items": ["initial-runtime-result"],
            "verification_state": "independently_verified",
            "verification": {"commands": ["pytest"], "passed": True, "summary": "passed"},
        },
    )
    assert rk.ingest_runtime_node_evidence(conn, node["id"])
    status = rk.status_runtime_job(conn, job_id)
    assert status["progress_ledger"][0]["satisfaction"] == "full"
    assert status["progress_ledger"][0]["verification_state"] == "implementation_verified"
    assert status["goal_items"][0]["state"] == "partial"
    assert status["job"]["state"] == "waiting_decision"
    assert any(gap["gap_type"] == "needs_verification" for gap in status["goal_gaps"] if gap["state"] == "open")


def test_required_evaluator_policy_creates_fixed_target_and_completes_goal(conn):
    root = _root_task(conn)
    job_id = rk.create_runtime_job(
        conn,
        root,
        "verify a fixed implementation revision",
        goal_items=[{
            "item_key": "runtime-result",
            "description": "runtime result is independently verified",
            "required": True,
            "verifier_required": True,
        }],
        initialization_mode="provider_first",
        runtime_metadata={
            "verification_policy": {
                "mode": "required_evaluator",
                "assignee": "runtime-evaluator",
                "require_workspace_revision": True,
            }
        },
    )
    assert rk.apply_graph_patch(conn, job_id, _patch(
        job_id,
        _revision(conn, job_id),
        {
            "op": "create_node",
            "node_key": "implement-runtime-result",
            "node_type": "implementation",
            "title": "Implement runtime result",
            "description": "Produce the candidate runtime result.",
            "goal_item_keys": ["runtime-result"],
            "contract": _contract(),
        },
    ))["status"] == "applied"
    implementation = _node(conn, job_id, "implement-runtime-result")
    rk.reduce_runtime_job(conn, job_id)
    assert rk.materialize_runtime_node(conn, dict(_node(conn, job_id, implementation["node_key"])))
    implementation = _node(conn, job_id, implementation["node_key"])
    _complete_node(conn, implementation, {
        "verdict": "succeeded",
        "summary": "candidate implementation completed",
        "claimed_goal_items": ["runtime-result"],
        "workspace_revision": "git:candidate-sha",
        "verification": {"passed": True, "summary": "implementation tests passed"},
    })

    advanced = rk.advance_runtime_job(conn, job_id, create_tasks=True)
    verifier_keys = [key for key in advanced.materialized_nodes if key.startswith("verify-runtime-result-")]
    assert len(verifier_keys) == 1
    assert advanced.decision_requested is False
    verifier = _node(conn, job_id, verifier_keys[0])
    relation = conn.execute(
        "SELECT * FROM node_relations WHERE from_node_id = ? AND relation_type = 'verifies'",
        (verifier["id"],),
    ).fetchone()
    relation_metadata = json.loads(relation["metadata_json"])
    assert relation_metadata["target_workspace_revision"] == "git:candidate-sha"
    assert relation_metadata["target_materialization_attempt"] == 1

    _complete_node(conn, verifier, {
        "verdict": "succeeded",
        "summary": "official evaluator passed",
        "claimed_goal_items": ["runtime-result"],
        "verification": {"passed": True, "summary": "official tests passed"},
    })
    assert rk.ingest_runtime_node_evidence(conn, verifier["id"])
    status = rk.status_runtime_job(conn, job_id)
    assert status["job"]["state"] == "done"
    assert status["goal_items"][0]["state"] == "satisfied"
    verifier_ledger = next(row for row in status["progress_ledger"] if row["node_id"] == verifier["id"])
    assert verifier_ledger["verification_state"] == "independently_verified"
    assert conn.execute(
        "SELECT COUNT(*) FROM execution_events WHERE job_id = ? AND event_type = 'required_evaluator_created'",
        (job_id,),
    ).fetchone()[0] == 1
    assert rk.ensure_required_evaluator_nodes(conn, job_id) == []


def test_required_evaluator_policy_selects_partial_candidate_ready_receipt(conn):
    root = _root_task(conn)
    job_id = rk.create_runtime_job(
        conn,
        root,
        "verify a worker-local candidate at a fixed revision",
        goal_items=[{
            "item_key": "runtime-result",
            "description": "runtime result is independently verified",
            "required": True,
            "verifier_required": True,
        }],
        initialization_mode="provider_first",
        runtime_metadata={
            "verification_policy": {
                "mode": "required_evaluator",
                "assignee": "runtime-evaluator",
                "require_workspace_revision": True,
            }
        },
    )
    assert rk.apply_graph_patch(conn, job_id, _patch(
        job_id,
        _revision(conn, job_id),
        {
            "op": "create_node",
            "node_key": "implement-runtime-result",
            "node_type": "implementation",
            "title": "Implement runtime result",
            "description": "Produce the candidate runtime result.",
            "goal_item_keys": ["runtime-result"],
            "contract": _contract(),
        },
    ))["status"] == "applied"
    rk.reduce_runtime_job(conn, job_id)
    implementation = _node(conn, job_id, "implement-runtime-result")
    assert rk.materialize_runtime_node(conn, dict(implementation))
    implementation = _node(conn, job_id, implementation["node_key"])
    receipt = rk.bind_runtime_receipt_provenance(
        conn,
        implementation["latest_task_id"],
        {
            "schema": "runtime_worker_receipt_v1",
            "verdict": "candidate_ready",
            "summary": "worker-local candidate is ready for independent evaluation",
            "claimed_goal_items": [],
            "partial_goal_items": ["runtime-result"],
            "unmet_goal_items": [],
            "changed_files": ["src/runtime.py"],
            "workspace_revision": "git:partial-candidate-sha",
            "verification": {"passed": True, "summary": "focused tests passed"},
        },
        backend_session_id="codex-thread-1",
    )
    _complete_node(conn, implementation, receipt)

    advanced = rk.advance_runtime_job(conn, job_id, create_tasks=True)

    implementation = _node(conn, job_id, implementation["node_key"])
    assert implementation["state"] == "candidate_ready"
    ledger = conn.execute(
        "SELECT * FROM progress_ledger WHERE node_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (implementation["id"],),
    ).fetchone()
    assert ledger["satisfaction"] == "partial"
    assert ledger["verification_state"] == "unverified"
    verifier_key = next(key for key in advanced.materialized_nodes if key.startswith("verify-runtime-result-"))
    verifier = _node(conn, job_id, verifier_key)
    relation = conn.execute(
        "SELECT * FROM node_relations WHERE from_node_id = ? AND relation_type = 'verifies'",
        (verifier["id"],),
    ).fetchone()
    relation_metadata = json.loads(relation["metadata_json"])
    assert relation["to_node_id"] == implementation["id"]
    assert relation_metadata["target_workspace_revision"] == "git:partial-candidate-sha"
    assert relation_metadata["target_materialization_attempt"] == 1
    status = rk.status_runtime_job(conn, job_id)
    assert status["job"]["state"] == "waiting_worker"
    assert status["goal_items"][0]["state"] == "partial"


def test_required_evaluator_uses_latest_gap_linked_remediation_receipt(conn):
    root = _root_task(conn)
    job_id = rk.create_runtime_job(
        conn,
        root,
        "verify remediation after an evaluator failure",
        goal_items=[{
            "item_key": "runtime-result",
            "description": "runtime result is independently verified",
            "required": True,
            "verifier_required": True,
        }],
        initialization_mode="provider_first",
        runtime_metadata={
            "phase4g8_run_id": "phase4g8-test",
            "verification_policy": {
                "mode": "required_evaluator",
                "assignee": "runtime-evaluator",
                "require_workspace_revision": True,
            }
        },
    )
    assert rk.apply_graph_patch(conn, job_id, _patch(
        job_id,
        _revision(conn, job_id),
        {
            "op": "create_node",
            "node_key": "implement-runtime-result",
            "node_type": "implementation",
            "title": "Implement runtime result",
            "description": "Produce the candidate runtime result.",
            "goal_item_keys": ["runtime-result"],
            "contract": _contract(),
        },
    ))["status"] == "applied"
    rk.reduce_runtime_job(conn, job_id)
    implementation = _node(conn, job_id, "implement-runtime-result")
    assert rk.materialize_runtime_node(conn, dict(implementation))
    implementation = _node(conn, job_id, implementation["node_key"])
    _complete_node(conn, implementation, {
        "verdict": "succeeded",
        "summary": "initial candidate",
        "claimed_goal_items": ["runtime-result"],
        "workspace_revision": "git:initial-candidate",
        "verification": {"passed": True},
    })
    first = rk.advance_runtime_job(conn, job_id, create_tasks=True)
    first_verifier_key = next(key for key in first.materialized_nodes if key.startswith("verify-runtime-result-"))
    first_verifier = _node(conn, job_id, first_verifier_key)
    _complete_node(conn, first_verifier, {
        "verdict": "failed",
        "summary": "official evaluator failed",
        "contradicted_goal_items": ["runtime-result"],
        "verification": {"passed": False},
    })
    assert rk.ingest_runtime_node_evidence(conn, first_verifier["id"])
    open_gap = next(
        gap["gap_key"]
        for gap in rk.status_runtime_job(conn, job_id)["goal_gaps"]
        if gap["state"] == "open" and gap["gap_key"].startswith("runtime-result:")
    )

    assert rk.apply_graph_patch(conn, job_id, _patch(
        job_id,
        _revision(conn, job_id),
        {
            "op": "strategy_update",
            "node_key": "remediate-runtime-result",
            "title": "Remediate runtime result",
            "description": "Correct the evaluator-reported failures.",
            "gap_keys": [open_gap],
            "strategy_summary": "Correct the concrete failures reported by the independent evaluator.",
            "changes_from_previous_attempts": [
                "use the evaluator failure evidence instead of repeating the initial implementation",
            ],
            "contract": _contract(),
        },
    ))["status"] == "applied"
    rk.reduce_runtime_job(conn, job_id)
    remediation = _node(conn, job_id, "remediate-runtime-result")
    assert rk.materialize_runtime_node(conn, dict(remediation))
    remediation = _node(conn, job_id, remediation["node_key"])
    _complete_node(conn, remediation, {
        "verdict": "blocked",
        "blocked_reason": "official evaluator is outside the worker trust boundary",
        "summary": "remediation candidate completed",
        "claimed_goal_items": [],
        "changed_files": ["src/runtime.py"],
        "workspace_revision": "git:remediation-candidate",
        "verification": {"passed": False},
        "verification_provenance": {
            "kind": "worker_local",
            "official_evaluator": "not_available_within_workspace_boundary",
        },
    })
    assert rk.ingest_runtime_node_evidence(conn, remediation["id"])
    assert _node(conn, job_id, remediation["node_key"])["state"] == "candidate_ready"

    assert rk.apply_graph_patch(conn, job_id, _patch(
        job_id,
        _revision(conn, job_id),
        {
            "op": "strategy_update",
            "node_key": "redundant-remediation",
            "title": "Redundant remediation",
            "description": "A newer dispatch that failed before producing a receipt.",
            "gap_keys": [open_gap],
            "strategy_summary": "This dispatch must not hide the prior valid candidate.",
            "changes_from_previous_attempts": ["none; dispatch failed before execution"],
            "contract": _contract(),
        },
    ))["status"] == "applied"
    redundant = _node(conn, job_id, "redundant-remediation")
    conn.execute(
        "UPDATE execution_nodes SET state = 'failed', completed_at = updated_at WHERE id = ?",
        (redundant["id"],),
    )

    created = rk.ensure_required_evaluator_nodes(conn, job_id)

    assert len(created) == 1
    verifier = _node(conn, job_id, created[0])
    relation = conn.execute(
        "SELECT * FROM node_relations WHERE from_node_id = ? AND relation_type = 'verifies'",
        (verifier["id"],),
    ).fetchone()
    assert relation["to_node_id"] == remediation["id"]
    assert json.loads(relation["metadata_json"])["target_workspace_revision"] == "git:remediation-candidate"


def _prepare_failed_evaluator_remediation(
    conn,
    tmp_path,
    *,
    remediation_overrides: dict | None = None,
    evaluator_result_overrides: dict | None = None,
    evaluator_receipt_overrides: dict | None = None,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    remediation_policy = {
        "mode": "resume_target_session",
        "max_no_progress_streak": 2,
        "diagnostic_batch_size": 20,
        "max_diagnostics_chars_per_case": 4000,
    }
    remediation_policy.update(remediation_overrides or {})
    job_id = rk.create_runtime_job(
        conn,
        _root_task(conn),
        "implement one result and remediate official evaluator failures",
        workspace_path=str(workspace),
        goal_items=[{
            "item_key": "runtime-result",
            "description": "runtime result is independently verified",
            "required": True,
            "verifier_required": True,
        }],
        initialization_mode="provider_first",
        runtime_metadata={
            "phase4g8_run_id": "phase4g8-remediation-test",
            "verification_policy": {
                "mode": "required_evaluator",
                "assignee": "runtime-evaluator",
                "require_workspace_revision": True,
                "remediation": remediation_policy,
            },
        },
    )
    assert rk.apply_graph_patch(conn, job_id, _patch(
        job_id,
        _revision(conn, job_id),
        {
            "op": "create_node",
            "node_key": "primary-result",
            "node_type": "implementation",
            "title": "Implement result",
            "description": "Own implementation, testing, debugging, and remediation.",
            "goal_item_keys": ["runtime-result"],
            "assignee": "codex-test",
            "contract": _contract(),
        },
    ))["status"] == "applied"

    first = rk.advance_runtime_job(conn, job_id, create_tasks=True, auto_compact=False)
    assert first.materialized_nodes == ["primary-result"]
    primary = _node(conn, job_id, "primary-result")
    first_task_id = primary["latest_task_id"]
    first_materialization = conn.execute(
        "SELECT * FROM node_materializations WHERE node_id = ? AND attempt = 1",
        (primary["id"],),
    ).fetchone()
    primary_session_id = "019f0000-0000-7000-8000-00000000aa01"
    kb.record_task_event(
        conn,
        first_task_id,
        "worker_backend_session_started",
        {
            "worker_lane": "codex-test",
            "worker_kind": "codex_cli",
            "backend_session_id": primary_session_id,
            "execution_mode": "fresh",
        },
        run_id=primary["latest_run_id"],
    )
    _complete_node(conn, primary, {
        "verdict": "succeeded",
        "summary": "initial candidate",
        "claimed_goal_items": ["runtime-result"],
        "changed_files": ["src/runtime.py"],
        "workspace_revision": "git:candidate-one",
        "verification": {"passed": True, "summary": "local tests passed"},
    })

    evaluated = rk.advance_runtime_job(conn, job_id, create_tasks=True, auto_compact=False)
    verifier_key = next(
        key for key in evaluated.materialized_nodes if key.startswith("verify-runtime-result-")
    )
    verifier = _node(conn, job_id, verifier_key)
    evaluator_session_id = "official-evaluator:phase4g8-remediation-test:first"
    kb.record_task_event(
        conn,
        verifier["latest_task_id"],
        "worker_backend_session_started",
        {
            "worker_lane": "runtime-evaluator",
            "worker_kind": "phase4g8_evaluator",
            "backend_session_id": evaluator_session_id,
            "target_revision": "git:candidate-one",
        },
        run_id=verifier["latest_run_id"],
    )
    provenance = rk.build_independent_verification_provenance(
        conn,
        verifier["id"],
        producer_kind="official_evaluator",
        producer_session_id=evaluator_session_id,
    )
    evaluator_result = {
        "schema": rk.OFFICIAL_EVALUATOR_RESULT_SCHEMA,
        "resolved": False,
        "fail_to_pass": {
            "passed": 0,
            "failed": 1,
            "total": 1,
            "failed_tests": ["tests/test_runtime.py::test_required_behavior"],
            "failed_tests_truncated": 0,
        },
        "pass_to_pass": {
            "passed": 2,
            "failed": 0,
            "total": 2,
            "failed_tests": [],
            "failed_tests_truncated": 0,
        },
        "failure_diagnostics": {
            "schema": "hermes_phase4g8_pytest_failure_diagnostics_v2",
            "cases": [
                {
                    "test_id": "tests/test_runtime.py::test_required_behavior",
                    "expected": ["'from_pandas-index'"],
                    "actual": ["'getitem'"],
                    "regex": ["must use shuffl"],
                    "emitted_warnings": ["UserWarning: a different warning"],
                    "exception_summary": ["AssertionError: expected behavior was absent"],
                    "diagnostic_excerpt": (
                        "API key sk-test-secret was redacted; /testbed/gold.patch is protected"
                    ),
                    "truncated": False,
                },
                {
                    "test_id": "tests/test_runtime.py::test_not_reported_failed",
                    "expected": ["injected hidden test source"],
                    "actual": ["/workspace/test.patch"],
                    "regex": [],
                    "emitted_warnings": [],
                    "exception_summary": [],
                    "diagnostic_excerpt": "must not be forwarded",
                    "truncated": False,
                },
            ],
            "case_count": 2,
            "omitted_case_count": 0,
            "text": "raw fallback text must not be forwarded when cases are valid",
            "truncated": False,
            "source_sha256": "d" * 64,
        },
        "environment_fingerprint": {"sha256": "e" * 64},
        "raw_stdout": "hidden gold.patch source must not enter the worker bundle",
    }
    evaluator_result.update(evaluator_result_overrides or {})
    evaluator_receipt = {
        "verdict": "failed",
        "summary": "official evaluator failed",
        "claimed_goal_items": [],
        "contradicted_goal_items": ["runtime-result"],
        "verification": {"passed": False, "summary": "official evaluator failed"},
        "verification_provenance": provenance,
        "artifacts": [{
            "artifact_type": "official_evaluator_result",
            "path_or_ref": "evaluator:fixture:first",
            "summary": "bounded evaluator result",
        }],
        "official_evaluator_result": evaluator_result,
    }
    evaluator_receipt.update(evaluator_receipt_overrides or {})
    _complete_node(conn, verifier, evaluator_receipt)
    rk.sync_runtime_backend_sessions(conn, job_id)
    return {
        "workspace": workspace,
        "job_id": job_id,
        "primary": primary,
        "first_materialization": first_materialization,
        "primary_session_id": primary_session_id,
        "verifier": verifier,
        "verifier_key": verifier_key,
        "evaluator_result": evaluator_result,
    }


def test_required_evaluator_failure_resumes_same_node_session_and_retargets_evaluator(
    conn,
    tmp_path,
):
    scenario = _prepare_failed_evaluator_remediation(conn, tmp_path)
    job_id = scenario["job_id"]
    primary = scenario["primary"]
    first_materialization = scenario["first_materialization"]
    primary_session_id = scenario["primary_session_id"]
    verifier_key = scenario["verifier_key"]

    provider_calls = []

    def provider_must_not_run(_session, _delta):
        provider_calls.append(True)
        raise AssertionError("Decision Provider must not run for eligible same-session remediation")

    remediated = rk.advance_runtime_job(
        conn,
        job_id,
        create_tasks=True,
        decision_provider=provider_must_not_run,
        auto_compact=False,
    )

    assert provider_calls == []
    assert remediated.decision_requested is False
    assert remediated.materialized_nodes == ["primary-result"]
    assert remediated.recovery["evaluator_remediation"]["scheduled"] == ["primary-result"]
    primary = _node(conn, job_id, "primary-result")
    assert primary["id"] == first_materialization["node_id"]
    assert primary["state"] == "running"
    second_materialization = conn.execute(
        "SELECT * FROM node_materializations WHERE node_id = ? AND attempt = 2",
        (primary["id"],),
    ).fetchone()
    continuity = json.loads(second_materialization["metadata_json"])["execution_continuity"]
    assert continuity["mode"] == "resume"
    assert continuity["resume_reason"] == "official_evaluator_failure"
    assert continuity["resume_session_id"] == primary_session_id
    assert continuity["resume_from_materialization_id"] == first_materialization["id"]
    assert continuity["context_reacquisition"] is False
    bundle = continuity["remediation_bundle"]
    assert bundle["schema"] == rk.EVALUATOR_FAILURE_BUNDLE_SCHEMA
    assert bundle["target_materialization_id"] == first_materialization["id"]
    assert bundle["fail_to_pass"]["failed_tests"] == [
        "tests/test_runtime.py::test_required_behavior"
    ]
    diagnostics = bundle["failure_diagnostics"]
    assert diagnostics["schema"] == "runtime_evaluator_failure_diagnostics_v2"
    assert diagnostics["case_count"] == 1
    assert diagnostics["omitted_case_count"] == 0
    assert diagnostics["omitted_duplicate_or_unrelated_case_count"] == 1
    assert diagnostics["cases"][0]["expected"] == ["'from_pandas-index'"]
    assert diagnostics["cases"][0]["actual"] == ["'getitem'"]
    assert diagnostics["cases"][0]["regex"] == ["must use shuffl"]
    assert diagnostics["cases"][0]["emitted_warnings"] == [
        "UserWarning: a different warning"
    ]
    assert "sk-test-secret" not in json.dumps(bundle)
    assert "gold.patch" not in json.dumps(bundle)
    assert "test_not_reported_failed" not in json.dumps(bundle)
    assert "hidden test source" not in json.dumps(bundle)
    assert "/testbed" not in json.dumps(bundle)
    assert "/workspace" not in json.dumps(bundle)
    resume_prompt = cw.build_codex_resume_prompt(
        task_id=primary["latest_task_id"],
        lane="codex-test",
        continuity=continuity,
        task_context="LATEST MATERIALIZATION CONTEXT",
    )
    assert "LATEST MATERIALIZATION CONTEXT" in resume_prompt
    assert "Requested changes to address before finishing" in resume_prompt
    assert bundle["bundle_id"] in resume_prompt
    assert "hidden test patches" in resume_prompt
    assert "symmetric relation constraints" in resume_prompt
    assert "neither side is inherently the desired value" in resume_prompt
    assert "condition-dependent contract" in resume_prompt
    assert "expected/actual values" in resume_prompt
    assert "equivalent local assertions" in resume_prompt
    assert "gold.patch" not in resume_prompt
    assert "sk-test-secret" not in resume_prompt
    with kb.connect() as restarted_conn:
        duplicate_tick = rk.advance_runtime_job(
            restarted_conn,
            job_id,
            create_tasks=True,
            decision_provider=provider_must_not_run,
            auto_compact=False,
        )
    assert duplicate_tick.materialized_nodes == []
    assert conn.execute(
        "SELECT COUNT(*) FROM execution_events WHERE job_id = ? AND event_type = 'required_evaluator_remediation_scheduled'",
        (job_id,),
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM node_materializations WHERE node_id = ?",
        (primary["id"],),
    ).fetchone()[0] == 2

    kb.record_task_event(
        conn,
        primary["latest_task_id"],
        "worker_backend_session_resumed",
        {
            "worker_lane": "codex-test",
            "worker_kind": "codex_cli",
            "backend_session_id": primary_session_id,
            "execution_mode": "resume",
        },
        run_id=primary["latest_run_id"],
    )
    _complete_node(conn, primary, {
        "verdict": "candidate_ready",
        "summary": "remediation candidate",
        "claimed_goal_items": ["runtime-result"],
        "changed_files": ["src/runtime.py", "tests/test_runtime.py"],
        "workspace_revision": "git:candidate-two",
        "verification": {"passed": True, "summary": "targeted regression passed"},
        "verification_provenance": {
            "kind": "worker_local",
            "official_evaluator": "not_available_within_workspace_boundary",
        },
    })
    reevaluated = rk.advance_runtime_job(conn, job_id, create_tasks=True, auto_compact=False)
    consumed = conn.execute(
        "SELECT payload_json FROM execution_events WHERE job_id = ? "
        "AND event_type = 'evaluator_failure_feedback_consumed'",
        (job_id,),
    ).fetchall()
    assert len(consumed) == 1
    consumed_payload = json.loads(consumed[0]["payload_json"])
    assert consumed_payload["bundle_id"] == bundle["bundle_id"]
    assert consumed_payload["consumer_materialization_id"] == second_materialization["id"]
    second_verifier_keys = [
        key for key in reevaluated.materialized_nodes if key.startswith("verify-runtime-result-")
    ]
    assert len(second_verifier_keys) == 1
    assert second_verifier_keys[0] != verifier_key
    second_verifier = _node(conn, job_id, second_verifier_keys[0])
    relation = conn.execute(
        "SELECT * FROM node_relations WHERE from_node_id = ? AND relation_type = 'verifies'",
        (second_verifier["id"],),
    ).fetchone()
    relation_metadata = json.loads(relation["metadata_json"])
    assert relation["to_node_id"] == primary["id"]
    assert relation_metadata["target_materialization_attempt"] == 2
    assert relation_metadata["target_workspace_revision"] == "git:candidate-two"
    evidence_refs = [
        row["evidence_ref"]
        for row in conn.execute(
            "SELECT evidence_ref FROM progress_ledger WHERE job_id = ? AND node_id = ? ORDER BY created_at, rowid",
            (job_id, primary["id"]),
        ).fetchall()
    ]
    assert len(evidence_refs) == 2
    assert len(set(evidence_refs)) == 2
    assert all(":materialization:" in ref for ref in evidence_refs)
    assert rk.check_runtime_consistency(conn, job_id, write_events=False)["status"] == "passed"


def test_required_evaluator_failure_reopens_candidate_ready_node(conn, tmp_path):
    scenario = _prepare_failed_evaluator_remediation(conn, tmp_path)
    job_id = scenario["job_id"]
    primary = scenario["primary"]
    conn.execute(
        "UPDATE execution_nodes SET state = 'candidate_ready' WHERE id = ?",
        (primary["id"],),
    )

    remediated = rk.advance_runtime_job(
        conn,
        job_id,
        create_tasks=True,
        auto_compact=False,
    )

    assert remediated.recovery["evaluator_remediation"]["scheduled"] == ["primary-result"]
    assert remediated.materialized_nodes == ["primary-result"]
    primary = _node(conn, job_id, "primary-result")
    assert primary["state"] == "running"
    assert conn.execute(
        "SELECT COUNT(*) FROM node_materializations WHERE node_id = ?",
        (primary["id"],),
    ).fetchone()[0] == 2


def test_official_diagnostics_reach_same_session_resume_prompt_end_to_end(
    conn,
    tmp_path,
):
    output = tmp_path / "test_output.txt"
    unrelated = "".join(
        f"________________ test_unrelated_{index} ________________\n"
        f"tests/test_unrelated.py:{index + 1}: in test_unrelated_{index}\n"
        f"E   AssertionError: unrelated failure {index}\n"
        for index in range(30)
    )
    cli_failures = (
        "________________ test_register_command_ep ________________\n"
        "dask/tests/test_cli.py:80: in test_register_command_ep\n"
        "E   Failed: DID NOT WARN. No warnings were emitted.\n"
        "E   Regex: 'must be instances of'\n"
        "________________ test_repeated_name_registration_warn ________________\n"
        "dask/tests/test_cli.py:108: in test_repeated_name_registration_warn\n"
        "E   Failed: DID NOT WARN. No warnings were emitted.\n"
        "E   Regex: 'While registering the command with name'\n"
        "________________ test_version ________________\n"
        "dask/tests/test_cli.py:20: in test_version\n"
        "E   AssertionError: assert 'dask, version 1.0' == 'cli, version 1.0'\n"
    )
    output.write_text(
        "=================================== FAILURES ===================================\n"
        + unrelated
        + cli_failures
        + "================ short test summary info ================\n",
        encoding="utf-8",
    )
    failed_test_ids = [
        "dask/tests/test_cli.py::test_register_command_ep",
        "dask/tests/test_cli.py::test_repeated_name_registration_warn",
        "dask/tests/test_cli.py::test_version",
    ]
    diagnostics = swe_evo._extract_pytest_failure_diagnostics(
        output,
        failed_test_ids=failed_test_ids,
        max_cases=3,
    )
    scenario = _prepare_failed_evaluator_remediation(
        conn,
        tmp_path,
        remediation_overrides={"max_diagnostic_cases": 3},
        evaluator_result_overrides={
            "fail_to_pass": {
                "passed": 0,
                "failed": 3,
                "total": 3,
                "failed_tests": failed_test_ids,
            },
            "pass_to_pass": {
                "passed": 1,
                "failed": 0,
                "total": 1,
                "failed_tests": [],
            },
            "failure_diagnostics": diagnostics,
            "feedback_coverage": swe_evo._evaluator_feedback_coverage(
                failed_test_ids, diagnostics, max_cases=3
            ),
        },
    )

    advanced = rk.advance_runtime_job(
        conn,
        scenario["job_id"],
        create_tasks=True,
        auto_compact=False,
    )
    assert advanced.materialized_nodes == ["primary-result"]
    primary = _node(conn, scenario["job_id"], "primary-result")
    materialization = conn.execute(
        "SELECT * FROM node_materializations WHERE node_id = ? AND attempt = 2",
        (primary["id"],),
    ).fetchone()
    continuity = json.loads(materialization["metadata_json"])["execution_continuity"]
    prompt = cw.build_codex_resume_prompt(
        task_id=primary["latest_task_id"],
        lane="codex-test",
        continuity=continuity,
        task_context="LATEST MATERIALIZATION CONTEXT",
    )

    assert "LATEST MATERIALIZATION CONTEXT" in prompt
    for value in [
        *failed_test_ids,
        "must be instances of",
        "While registering the command with name",
        "'dask, version 1.0'",
        "'cli, version 1.0'",
    ]:
        assert value in prompt
    assert "test_unrelated" not in prompt


def test_evaluator_v2_diagnostics_never_fall_back_to_text_after_case_rejection():
    diagnostics = rk._safe_evaluator_failure_diagnostics(
        {
            "schema": "hermes_phase4g8_pytest_failure_diagnostics_v2",
            "cases": [{
                "test_id": "tests/test_hidden.py::test_injected",
                "expected": ["hidden test source"],
                "actual": ["/workspace/gold.patch"],
                "diagnostic_excerpt": "protected raw outcome",
            }],
            "text": "raw fallback must not bypass the failed-test allow-list",
            "source_sha256": "a" * 64,
        },
        allowed_test_ids={"tests/test_runtime.py::test_reported_failure"},
        policy={
            "max_diagnostic_cases": 20,
            "max_diagnostics_chars_per_case": 2500,
            "max_diagnostics_chars": 24000,
        },
    )

    assert diagnostics["schema"] == "runtime_evaluator_failure_diagnostics_v2"
    assert diagnostics["cases"] == []
    assert diagnostics["case_count"] == 0
    assert diagnostics["missing_test_ids"] == [
        "tests/test_runtime.py::test_reported_failure"
    ]
    assert diagnostics["truncated"] is True
    assert diagnostics["source_sha256"] == "a" * 64
    assert "raw fallback" not in json.dumps(diagnostics)


def test_resume_prompt_preserves_latest_frozen_contribution_context():
    task_context = """# Runtime node

Frozen dependency contributions:
[{"artifact_id":"art_child_1","patch_ref":"/tmp/contribution.patch"}]

Apply or adapt every contribution and classify it in the final receipt.

Runtime footer: {"runtime_job_id":"rjob_test"}
"""
    prompt = cw.build_codex_resume_prompt(
        task_id="task-integration",
        lane="codex-test",
        continuity={
            "resume_from_materialization_id": "mat-assessment",
            "workspace_revision": "git:base",
        },
        task_context=task_context,
    )

    assert "art_child_1" in prompt
    assert "/tmp/contribution.patch" in prompt
    assert "accepted_contributions" in prompt
    assert "new frozen dependency contributions" in prompt
    assert "apply or adapt" in prompt.lower()
    assert "infrastructure failure" not in prompt


def test_evaluator_v3_diagnostics_preserve_relation_and_safe_conditions():
    diagnostics = rk._safe_evaluator_failure_diagnostics(
        {
            "schema": "hermes_phase4g8_pytest_failure_diagnostics_v3",
            "cases": [{
                "test_id": "tests/test_runtime.py::test_reported_failure",
                "failure_kind": "expected_exception_not_raised",
                "comparisons": [{
                    "operator": "==",
                    "left": "'array-key'",
                    "right": "'copy-key'",
                    "required_relation": "equal",
                }],
                "conditions": [
                    "shuffle=False",
                    "match='must use shuffl'",
                    "api_key='must-not-pass'",
                    "arbitrary code()",
                ],
                "diagnostic_excerpt": "Failed: DID NOT RAISE ValueError",
            }],
            "source_sha256": "b" * 64,
            "missing_test_ids": [
                "tests/test_runtime.py::test_missing_outcome",
                "tests/test_injected.py::test_not_allowed",
            ],
        },
        allowed_test_ids={
            "tests/test_runtime.py::test_reported_failure",
            "tests/test_runtime.py::test_missing_outcome",
        },
        policy={
            "max_diagnostic_cases": 20,
            "max_diagnostics_chars_per_case": 2500,
            "max_diagnostics_chars": 24000,
        },
    )

    assert diagnostics["schema"] == "runtime_evaluator_failure_diagnostics_v4"
    case = diagnostics["cases"][0]
    assert case["failure_kind"] == "expected_exception_not_raised"
    assert case["comparisons"] == [{
        "operator": "==",
        "left": "'array-key'",
        "right": "'copy-key'",
        "required_relation": "equal",
    }]
    assert case["conditions"] == ["shuffle=False", "match='must use shuffl'"]
    assert diagnostics["missing_test_ids"] == [
        "tests/test_runtime.py::test_missing_outcome"
    ]


def test_fixed_evaluator_attempt_budget_does_not_suppress_same_session_remediation(
    conn,
    tmp_path,
):
    scenario = _prepare_failed_evaluator_remediation(
        conn,
        tmp_path,
        remediation_overrides={"max_unresolved_attempts": 1},
    )
    provider_calls = []

    def provider_must_not_run(_session, _delta):
        provider_calls.append(True)
        raise AssertionError("Decision Provider must not run for deterministic remediation")

    result = rk.advance_runtime_job(
        conn,
        scenario["job_id"],
        create_tasks=True,
        decision_provider=provider_must_not_run,
        auto_compact=False,
    )

    remediation = result.recovery["evaluator_remediation"]
    assert provider_calls == []
    assert result.decision_requested is False
    assert result.materialized_nodes == ["primary-result"]
    assert remediation["budget_exhausted"] is False
    assert remediation["decision_suppressed"] is True
    assert remediation["failure_count"] == 1
    assert _node(conn, scenario["job_id"], "primary-result")["state"] == "running"
    assert conn.execute(
        "SELECT COUNT(*) FROM node_materializations WHERE node_id = ?",
        (scenario["primary"]["id"],),
    ).fetchone()[0] == 2

    with kb.connect() as restarted_conn:
        repeated = rk.advance_runtime_job(
            restarted_conn,
            scenario["job_id"],
            create_tasks=True,
            decision_provider=provider_must_not_run,
            auto_compact=False,
        )
    assert repeated.materialized_nodes == []
    assert conn.execute(
        "SELECT COUNT(*) FROM execution_events WHERE job_id = ? AND event_type = 'required_evaluator_remediation_budget_exhausted'",
        (scenario["job_id"],),
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("result_overrides", "receipt_overrides"),
    [
        ({"error": "stale_target_revision"}, {}),
        ({"error": "evaluator_feedback_extraction_incomplete"}, {}),
        ({"feedback_coverage": {"status": "extraction_incomplete"}}, {}),
        ({}, {"infrastructure_invalid": True}),
        ({}, {"verification": {"passed": False, "infrastructure_invalid": True}}),
    ],
)
def test_stale_or_infrastructure_invalid_evaluator_result_does_not_trigger_remediation(
    conn,
    tmp_path,
    result_overrides,
    receipt_overrides,
):
    scenario = _prepare_failed_evaluator_remediation(
        conn,
        tmp_path,
        evaluator_result_overrides=result_overrides,
        evaluator_receipt_overrides=receipt_overrides,
    )

    first = rk.schedule_required_evaluator_remediation(conn, scenario["job_id"])
    second = rk.schedule_required_evaluator_remediation(conn, scenario["job_id"])

    assert first["scheduled"] == []
    assert second["scheduled"] == []
    assert first["failure_count"] == 0
    assert first["budget_exhausted"] is False
    assert _node(conn, scenario["job_id"], "primary-result")["state"] == "succeeded"
    assert conn.execute(
        "SELECT COUNT(*) FROM execution_events WHERE job_id = ? AND event_type = 'required_evaluator_remediation_scheduled'",
        (scenario["job_id"],),
    ).fetchone()[0] == 0


def test_invalid_evaluator_provenance_does_not_consume_remediation_budget(
    conn,
    tmp_path,
):
    scenario = _prepare_failed_evaluator_remediation(
        conn,
        tmp_path,
        remediation_overrides={"max_unresolved_attempts": 1},
        evaluator_receipt_overrides={
            "verification_provenance": {"producer_kind": "official_evaluator"}
        },
    )

    result = rk.schedule_required_evaluator_remediation(conn, scenario["job_id"])

    assert result["failure_count"] == 0
    assert result["budget_exhausted"] is False
    assert result["scheduled"] == []
    assert result["not_resumable"][0]["reasons"] == ["verification_provenance_invalid"]
    assert conn.execute(
        "SELECT COUNT(*) FROM execution_events WHERE job_id = ? AND event_type = 'required_evaluator_remediation_budget_exhausted'",
        (scenario["job_id"],),
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("mismatch", "expected_reason"),
    [
        ("workspace", "workspace_revision_mismatch"),
        ("worker_lane", "worker_lane_mismatch"),
        ("capability", "capability_fingerprint_mismatch"),
        ("node_contract", "node_contract_fingerprint_mismatch"),
        ("session_status", "session_status_active"),
    ],
)
def test_required_evaluator_remediation_rejects_false_session_resume(
    conn,
    tmp_path,
    mismatch,
    expected_reason,
):
    scenario = _prepare_failed_evaluator_remediation(conn, tmp_path)
    primary = _node(conn, scenario["job_id"], "primary-result")
    session = conn.execute(
        "SELECT * FROM backend_worker_sessions WHERE node_id = ?",
        (primary["id"],),
    ).fetchone()
    assert session["status"] == "completed"

    if mismatch == "workspace":
        (scenario["workspace"] / "external-change.txt").write_text(
            "changed outside the completed worker session\n",
            encoding="utf-8",
        )
    elif mismatch == "worker_lane":
        conn.execute(
            "UPDATE execution_nodes SET assignee = 'different-lane' WHERE id = ?",
            (primary["id"],),
        )
    elif mismatch == "capability":
        metadata = json.loads(primary["metadata_json"])
        metadata["capability_policy"]["allowed"] = ["network_access"]
        conn.execute(
            "UPDATE execution_nodes SET metadata_json = ? WHERE id = ?",
            (json.dumps(metadata), primary["id"]),
        )
    elif mismatch == "node_contract":
        constraints = json.loads(primary["constraints_json"])
        constraints["contract"]["outcome"] = "A materially different responsibility."
        conn.execute(
            "UPDATE execution_nodes SET constraints_json = ? WHERE id = ?",
            (json.dumps(constraints), primary["id"]),
        )
    elif mismatch == "session_status":
        conn.execute(
            "UPDATE backend_worker_sessions SET status = 'active' WHERE id = ?",
            (session["id"],),
        )

    result = rk.schedule_required_evaluator_remediation(conn, scenario["job_id"])

    assert result["scheduled"] == []
    assert result["decision_suppressed"] is False
    assert result["not_resumable"]
    assert expected_reason in result["not_resumable"][0]["reasons"]
    assert _node(conn, scenario["job_id"], "primary-result")["state"] == "succeeded"
    assert conn.execute(
        "SELECT COUNT(*) FROM node_materializations WHERE node_id = ?",
        (primary["id"],),
    ).fetchone()[0] == 1


def test_stale_evaluator_target_cannot_satisfy_verifier_required_goal(conn):
    job_id = _job(conn)
    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    implementation = _node(conn, job_id, "understand-scope")
    _complete_node(conn, implementation, {
        "verdict": "succeeded",
        "summary": "implementation completed",
        "claimed_goal_items": ["initial-runtime-result"],
        "verification": {"passed": True},
    })
    assert rk.ingest_runtime_node_evidence(conn, implementation["id"])
    assert rk.apply_graph_patch(conn, job_id, {
        **_patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "insert_verifier",
                "target_node_key": "understand-scope",
                "target_materialization_attempt": 1,
                "target_workspace_revision": "git:fixed-revision",
                "verifier_node_key": "verify-stale-target",
                "title": "Verify fixed target",
                "goal_item_keys": ["initial-runtime-result"],
                "gap_keys": ["initial-runtime-result:needs_verification"],
            },
        ),
        "decomposition": {
            "policy_version": "1",
            "mode": "multiple_runtime_nodes",
            "justifications": [{
                "type": "independent_verification",
                "nodes": ["understand-scope", "verify-stale-target"],
                "explanation": "Verifier uses a separate execution responsibility.",
                "evidence_refs": [],
            }],
        },
    })["status"] == "applied"
    rk.reduce_runtime_job(conn, job_id)
    verifier = _node(conn, job_id, "verify-stale-target")
    assert rk.materialize_runtime_node(conn, dict(verifier))
    verifier = _node(conn, job_id, "verify-stale-target")
    provenance = rk.build_independent_verification_provenance(conn, verifier["id"])
    provenance["target_materialization_id"] = "mat_stale"
    _complete_node(conn, verifier, {
        "verdict": "succeeded",
        "summary": "stale evaluator result",
        "claimed_goal_items": ["initial-runtime-result"],
        "verification": {"passed": True},
        "verification_provenance": provenance,
    })
    assert rk.ingest_runtime_node_evidence(conn, verifier["id"])
    status = rk.status_runtime_job(conn, job_id)
    assert status["job"]["state"] != "done"
    assert status["goal_items"][0]["state"] == "partial"
    verifier_ledger = next(row for row in status["progress_ledger"] if row["node_id"] == verifier["id"])
    assert verifier_ledger["verification_state"] == "self_reported"
    assert verifier_ledger["metadata"]["verification_provenance_result"]["reason"] == (
        "target materialization is stale or mismatched"
    )


def test_node_completed_does_not_directly_call_provider(conn):
    job_id = _job(conn)
    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    node = _node(conn, job_id, "understand-scope")
    _complete_node(
        conn,
        node,
        {
            "verdict": "succeeded",
            "summary": "self reported only",
            "claimed_goal_items": ["initial-runtime-result"],
            "verification": {"passed": False},
        },
    )
    assert rk.ingest_runtime_node_evidence(conn, node["id"])
    events = [row["event_type"] for row in conn.execute("SELECT event_type FROM execution_events WHERE job_id = ?", (job_id,))]
    assert "node_completed" in events
    assert "decision_requested" in events
    assert conn.execute("SELECT COUNT(*) FROM kernel_decisions WHERE job_id = ?", (job_id,)).fetchone()[0] == 0


def test_partial_ledger_creates_partial_evidence_gap(conn):
    job_id = _job(conn)
    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    node = _node(conn, job_id, "understand-scope")
    _complete_node(
        conn,
        node,
        {
            "verdict": "succeeded",
            "summary": "only part of the goal is covered",
            "partial_goal_items": ["initial-runtime-result"],
            "remaining_gaps": ["missing end-to-end verification"],
            "verification": {"passed": False},
        },
    )
    assert rk.ingest_runtime_node_evidence(conn, node["id"])
    status = rk.status_runtime_job(conn, job_id)
    assert status["goal_items"][0]["state"] == "partial"
    assert any(gap["gap_type"] == "partial_evidence" for gap in status["goal_gaps"] if gap["state"] == "open")
    assert status["progress_ledger"][0]["metadata"]["remaining_gaps"] == ["missing end-to-end verification"]


def test_failed_verifier_creates_verification_failed_gap(conn):
    job_id = _job(conn)
    assert rk.apply_graph_patch(
        conn,
        job_id,
        {
            **_patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "insert_verifier",
                "target_goal_item_key": "initial-runtime-result",
                "target_workspace_revision": "fixture:goal-evidence-revision-1",
                "verifier_node_key": "verify-runtime",
                "title": "Verify runtime",
                "goal_item_keys": ["initial-runtime-result"],
                "gap_keys": ["initial-runtime-result:needs_verification"],
            },
            ),
            "decomposition": {
                "policy_version": "1",
                "mode": "multiple_runtime_nodes",
                "justifications": [{
                    "type": "independent_verification",
                    "nodes": ["verify-runtime"],
                    "explanation": "Goal verification is an independent responsibility.",
                    "evidence_refs": [],
                }],
            },
        },
    )["status"] == "applied"
    verifier = _node(conn, job_id, "verify-runtime")
    rk.materialize_runtime_node(conn, dict(verifier))
    verifier = _node(conn, job_id, "verify-runtime")
    _complete_node(
        conn,
        verifier,
        {
            "verdict": "failed",
            "summary": "verification failed",
            "claimed_goal_items": ["initial-runtime-result"],
            "verification": {"passed": False, "summary": "pytest failed"},
        },
    )
    assert rk.ingest_runtime_node_evidence(conn, verifier["id"])
    status = rk.status_runtime_job(conn, job_id)
    assert status["job"]["state"] != "done"
    assert any(gap["gap_type"] == "verification_failed" for gap in status["goal_gaps"] if gap["state"] == "open")


def test_contradicted_ledger_blocks_completion(conn):
    job_id = _job(conn)
    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    node = _node(conn, job_id, "understand-scope")
    _complete_node(
        conn,
        node,
        {
            "verdict": "succeeded",
            "summary": "evidence contradicts the goal",
            "contradicted_goal_items": ["initial-runtime-result"],
            "known_failure_boundaries": ["current artifact violates expected interface"],
            "verification": {"passed": False},
        },
    )
    assert rk.ingest_runtime_node_evidence(conn, node["id"])
    status = rk.status_runtime_job(conn, job_id)
    assert status["goal_items"][0]["state"] == "contradicted"
    assert status["job"]["state"] != "done"
    assert any(gap["gap_type"] == "contradicted_evidence" for gap in status["goal_gaps"] if gap["state"] == "open")


def test_later_evidence_reopens_and_then_resolves_satisfied_goal(conn):
    job_id = _job(conn, verifier_required=False)
    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    node = _node(conn, job_id, "understand-scope")
    _complete_node(
        conn,
        node,
        {
            "verdict": "succeeded",
            "summary": "initial evidence verified",
            "claimed_goal_items": ["initial-runtime-result"],
            "verification": {"passed": True},
        },
    )
    assert rk.ingest_runtime_node_evidence(conn, node["id"])
    assert rk.reduce_runtime_job(conn, job_id)["state"] == "done"

    rk.update_progress_ledger(
        conn,
        node["id"],
        {
            "summary": "later evidence contradicted the result",
            "contradicted_goal_items": ["initial-runtime-result"],
            "verification": {"passed": False},
        },
    )
    reopened = rk.reduce_runtime_job(conn, job_id)

    assert reopened["state"] == "waiting_decision"
    assert any(gap["gap_type"] == "contradicted_evidence" for gap in reopened["gaps"])
    assert conn.execute(
        "SELECT COUNT(*) FROM execution_events WHERE job_id = ? AND event_type = 'goal_gap_reopened'",
        (job_id,),
    ).fetchone()[0] == 1

    rk.update_progress_ledger(
        conn,
        node["id"],
        {
            "summary": "replacement evidence verified",
            "claimed_goal_items": ["initial-runtime-result"],
            "verification": {"passed": True},
        },
    )
    resolved = rk.reduce_runtime_job(conn, job_id)

    assert resolved["state"] == "done"
    assert rk.status_runtime_job(conn, job_id)["goal_items"][0]["state"] == "satisfied"


def test_no_runnable_unmet_goal_requests_decision_without_liveness_violation(conn):
    job_id = _job(conn)
    conn.execute("UPDATE execution_nodes SET state = 'failed' WHERE job_id = ? AND node_key = 'understand-scope'", (job_id,))
    reduction = rk.reduce_runtime_job(conn, job_id)
    assert reduction["state"] == "waiting_decision"
    assert any(gap["gap_type"] == "no_runnable_for_open_goal" for gap in reduction["gaps"])
    events = [row["event_type"] for row in conn.execute("SELECT event_type FROM execution_events WHERE job_id = ?", (job_id,))]
    assert "decision_requested" in events
    assert "liveness_violation" not in events
    assert rk.summarize_liveness(conn, job_id)["illegal_idle"] is False


def test_done_requires_required_goal_items_satisfied(conn):
    job_id = _job(conn, verifier_required=False)
    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    node = _node(conn, job_id, "understand-scope")
    _complete_node(
        conn,
        node,
        {
            "verdict": "succeeded",
            "summary": "node finished without ledger claim",
            "verification": {"passed": True},
        },
    )
    assert rk.ingest_runtime_node_evidence(conn, node["id"])
    assert rk.status_runtime_job(conn, job_id)["job"]["state"] != "done"
    rk.update_progress_ledger(
        conn,
        node["id"],
        {
            "summary": "now verified",
            "claimed_goal_items": ["initial-runtime-result"],
            "verification": {"passed": True},
        },
    )
    rk.reduce_runtime_job(conn, job_id)
    assert rk.status_runtime_job(conn, job_id)["job"]["state"] == "done"


def test_propose_blocked_requires_machine_blocker_type(conn):
    job_id = _job(conn)
    result = rk.apply_graph_patch(
        conn,
        job_id,
        _patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "propose_blocked",
                "target": "job",
                "blocker_type": "confused",
                "reason": "not sure",
                "evidence_ref": "none",
            },
        ),
    )
    assert result["status"] == "rejected"

    result = rk.apply_graph_patch(
        conn,
        job_id,
        _patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "propose_blocked",
                "target": "job",
                "blocker_type": "missing_secret",
                "reason": "API key absent",
                "evidence_ref": "event:1",
            },
        ),
    )
    assert result["status"] == "applied"
    assert rk.status_runtime_job(conn, job_id)["job"]["state"] != "blocked"


def test_request_human_requires_policy_reason(conn):
    job_id = _job(conn)
    result = rk.apply_graph_patch(
        conn,
        job_id,
        _patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "request_human",
                "node_key": "ask-user",
                "question": "Choose?",
                "goal_item_keys": ["initial-runtime-result"],
            },
        ),
    )
    assert result["status"] == "rejected"

    result = rk.apply_graph_patch(
        conn,
        job_id,
        _patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "request_human",
                "node_key": "ask-user",
                "question": "Provide API key?",
                "decision_type": "credential",
                "why_user_required": "The runtime cannot invent credentials.",
                "default_recommendation": "Pause until key is provided.",
                "goal_item_keys": ["initial-runtime-result"],
            },
        ),
    )
    assert result["status"] == "applied"
    status = rk.status_runtime_job(conn, job_id)
    assert status["job"]["state"] == "waiting_human"
    assert status["liveness"]["illegal_idle"] is False


def test_stale_gap_generates_structure_audit(conn):
    job_id = _job(conn)
    conn.execute("UPDATE execution_nodes SET state = 'failed' WHERE job_id = ? AND node_key = 'understand-scope'", (job_id,))
    for _ in range(4):
        rk.reduce_runtime_job(conn, job_id)
    status = rk.status_runtime_job(conn, job_id)
    assert any(gap["gap_type"] == "stale_or_no_progress" for gap in status["goal_gaps"] if gap["state"] == "open")
    events = [
        (row["event_type"], row["payload_json"])
        for row in conn.execute("SELECT event_type, payload_json FROM execution_events WHERE job_id = ?", (job_id,))
    ]
    assert any(event_type == "structure_audit_requested" and "stale" in payload for event_type, payload in events)


def test_rejected_patch_counts_toward_anti_stuck(conn):
    job_id = _job(conn)
    for node_key in ("bad-a", "bad-b"):
        assert rk.apply_graph_patch(
            conn,
            job_id,
            _patch(
                job_id,
                _revision(conn, job_id),
                {
                    "op": "create_node",
                    "node_key": node_key,
                    "node_type": "implementation",
                    "title": node_key,
                    "description": "missing linkage",
                },
            ),
        )["status"] == "rejected"
    rk.reduce_runtime_job(conn, job_id)
    events = [
        (row["event_type"], row["payload_json"])
        for row in conn.execute("SELECT event_type, payload_json FROM execution_events WHERE job_id = ?", (job_id,))
    ]
    assert any(
        event_type == "structure_audit_requested" and "repeated_patch_rejections" in payload
        for event_type, payload in events
    )


def test_strategy_update_patch_creates_materializable_strategy_node(conn):
    job_id = _job(conn)
    bad = rk.apply_graph_patch(
        conn,
        job_id,
        _patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "strategy_update",
                "node_key": "revise-runtime-strategy",
                "title": "Revise runtime strategy",
                "description": "Review failed attempts and choose a smaller verifiable path.",
                "goal_item_keys": ["initial-runtime-result"],
                "strategy_summary": "Change approach after repeated failed implementation attempts.",
            },
        ),
    )
    assert bad["status"] == "rejected"
    assert "changes_from_previous_attempts" in bad["reason"]

    result = rk.apply_graph_patch(
        conn,
        job_id,
        _patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "strategy_update",
                "node_key": "revise-runtime-strategy",
                "title": "Revise runtime strategy",
                "description": "Review failed attempts and choose a smaller verifiable path.",
                "goal_item_keys": ["initial-runtime-result"],
                "gap_keys": ["initial-runtime-result:stale_or_no_progress"],
                "strategy_summary": "Change approach after repeated failed implementation attempts.",
                "changes_from_previous_attempts": [
                    "stop repeating the same implementation node",
                    "insert a debug or research node before another implementation attempt",
                ],
            },
        ),
    )
    assert result["status"] == "applied"
    strategy = _node(conn, job_id, "revise-runtime-strategy")
    assert strategy["node_type"] == "strategy_update"
    assert strategy["state"] == "ready"

    task_id = rk.materialize_runtime_node(conn, dict(strategy))
    assert task_id
    strategy = _node(conn, job_id, "revise-runtime-strategy")
    assert strategy["state"] == "running"
    events = [
        row["event_type"]
        for row in conn.execute("SELECT event_type FROM execution_events WHERE job_id = ?", (job_id,))
    ]
    assert "strategy_update_requested" in events


def test_goal_waiver_is_reducer_owned_completion(conn):
    job_id = _job(conn)
    result = rk.waive_goal_item(
        conn,
        job_id,
        "initial-runtime-result",
        reason="user accepted deferring this goal item",
        source="user",
    )
    assert result["state"] == "waived"
    assert result["job_state"] == "done"
    status = rk.status_runtime_job(conn, job_id)
    assert status["job"]["state"] == "done"
    assert status["goal_items"][0]["state"] == "waived"
    assert status["progress_ledger"][0]["satisfaction"] == "waived"
    events = [
        row["event_type"]
        for row in conn.execute("SELECT event_type FROM execution_events WHERE job_id = ?", (job_id,))
    ]
    assert "goal_item_waived" in events
    assert "human_decision_received" in events


def test_status_json_has_phase2c_observability(conn):
    job_id = _job(conn)
    status = rk.status_runtime_job(conn, job_id)
    assert "ledger_summary" in status
    assert "frontier_summary" in status
    assert "liveness" in status
    assert status["frontier_summary"]["ready"][0]["node_key"] == "understand-scope"
    assert status["liveness"]["ready_count"] == 1


def test_fixture_provider_runs_phase1_implementation_verifier_closure(conn):
    job_id = _job(conn)

    # Analysis runs first but only establishes that the goal is still unmet.
    assert rk.advance_runtime_job(conn, job_id, create_tasks=True).materialized_nodes == ["understand-scope"]
    analysis = _node(conn, job_id, "understand-scope")
    _complete_node(
        conn,
        analysis,
        {
            "verdict": "succeeded",
            "summary": "analysis found the implementation gap",
            "unmet_goal_items": ["initial-runtime-result"],
            "verification": {"passed": False},
        },
    )
    assert rk.ingest_runtime_node_evidence(conn, analysis["id"])

    # The deterministic provider reacts to the missing-evidence gap by adding
    # an implementation node. This is a fixture path, not a built-in phase rule.
    result = rk.advance_runtime_job(
        conn,
        job_id,
        create_tasks=False,
        decision_provider=rk.fixture_decision_provider,
    )
    assert result.patch_status == "applied"
    impl = _node(conn, job_id, "implement-initial-runtime-result")
    assert impl["node_type"] == "implementation"
    assert impl["state"] == "ready"

    assert rk.advance_runtime_job(conn, job_id, create_tasks=True).materialized_nodes == [
        "implement-initial-runtime-result"
    ]
    impl = _node(conn, job_id, "implement-initial-runtime-result")
    _complete_node(
        conn,
        impl,
        {
            "verdict": "succeeded",
            "summary": "implementation produced locally verified evidence",
            "claimed_goal_items": ["initial-runtime-result"],
            "verification": {"passed": True, "summary": "local tests passed; independent verification pending"},
        },
    )
    assert rk.ingest_runtime_node_evidence(conn, impl["id"])
    assert rk.status_runtime_job(conn, job_id)["job"]["state"] != "done"

    # The next structural decision inserts a verifier for the implementation.
    result = rk.advance_runtime_job(
        conn,
        job_id,
        create_tasks=False,
        decision_provider=rk.fixture_decision_provider,
    )
    assert result.patch_status == "applied"
    verifier = _node(conn, job_id, "verify-initial-runtime-result")
    assert verifier["node_type"] == "verification"
    assert verifier["state"] == "ready"
    relations = rk.status_runtime_job(conn, job_id)["relations"]
    assert any(rel["relation_type"] == "verifies" for rel in relations)

    assert rk.advance_runtime_job(conn, job_id, create_tasks=True).materialized_nodes == [
        "verify-initial-runtime-result"
    ]
    verifier = _node(conn, job_id, "verify-initial-runtime-result")
    _complete_node(
        conn,
        verifier,
        {
            "verdict": "succeeded",
            "summary": "verification passed",
            "claimed_goal_items": ["initial-runtime-result"],
            "verification": {"commands": ["pytest"], "passed": True, "summary": "passed"},
        },
    )
    assert rk.ingest_runtime_node_evidence(conn, verifier["id"])
    assert rk.status_runtime_job(conn, job_id)["job"]["state"] == "done"


def test_bounded_supervisor_records_decision_delta_and_session_context(conn):
    job_id = _job(conn)
    conn.execute(
        "UPDATE execution_nodes SET state = 'failed' WHERE job_id = ? AND node_key = 'understand-scope'",
        (job_id,),
    )

    result = rk.advance_runtime_job_until_idle(
        conn,
        job_id,
        create_tasks=False,
        decision_provider=rk.fixture_decision_provider,
        max_steps=2,
    )

    assert result["steps"][0]["patch_status"] == "applied"
    decision = conn.execute(
        "SELECT * FROM kernel_decisions WHERE job_id = ? ORDER BY created_at DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    assert decision is not None
    assert "goal_gaps" in decision["delta_json"]
    session = conn.execute(
        "SELECT * FROM decision_sessions WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert "deltas" in session["context_state_json"]


def test_runtime_materialized_task_dispatch_and_ingest_fixture_lane(conn, monkeypatch):
    from hermes_cli import profiles

    calls = []

    def spawn(task, workspace, *, board=None):
        calls.append((task.id, task.assignee, workspace, board))
        return 2468

    register_worker_lane(
        WorkerLane(
            name="runtime-fixture",
            kind="test",
            description="runtime fixture lane",
            spawn_fn=spawn,
            max_concurrency=1,
        )
    )
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)

    root = kb.create_task(conn, title="runtime root", initial_status="running")
    job_id = rk.create_runtime_job(
        conn,
        root,
        "dispatch runtime node",
        goal_items=[
            {
                "item_key": "initial-runtime-result",
                "description": "runtime node can flow through dispatcher",
                "required": True,
                "verifier_required": False,
            }
        ],
        initial_assignee="runtime-fixture",
        initialization_mode="fixture",
    )
    assert rk.advance_runtime_job(conn, job_id, create_tasks=True).materialized_nodes == ["understand-scope"]
    node = _node(conn, job_id, "understand-scope")

    dispatch = kb.dispatch_once(conn, only_task_ids=[node["latest_task_id"]])
    assert dispatch.spawned == [(node["latest_task_id"], "runtime-fixture", calls[0][2])]
    assert kb.get_task(conn, node["latest_task_id"]).status == "running"

    _complete_node(
        conn,
        node,
        {
            "verdict": "succeeded",
            "summary": "dispatcher-backed runtime evidence",
            "claimed_goal_items": ["initial-runtime-result"],
            "verification": {"commands": ["fixture"], "passed": True, "summary": "passed"},
        },
    )
    assert rk.ingest_runtime_node_evidence(conn, node["id"])
    refreshed = _node(conn, job_id, "understand-scope")
    assert refreshed["latest_run_id"] is not None
    assert rk.status_runtime_job(conn, job_id)["job"]["state"] == "done"


def test_materialization_uses_job_default_worker_lane_when_node_is_unassigned(conn):
    root = kb.create_task(conn, title="default lane root", initial_status="running")
    job_id = rk.create_runtime_job(
        conn,
        root,
        "default lane materialization",
        initial_assignee="runtime-default",
        goal_items=[{"item_key": "runtime-result", "description": "result", "required": True}],
        initialization_mode="fixture",
    )
    assert rk.apply_graph_patch(
        conn,
        job_id,
        _patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "create_node",
                "node_key": "unassigned-followup",
                "node_type": "implementation",
                "title": "Unassigned follow-up",
                "description": "Uses the job execution default.",
                "goal_item_keys": ["runtime-result"],
            },
        ),
    )["status"] == "applied"
    node = _node(conn, job_id, "unassigned-followup")
    assert rk.materialize_runtime_node(conn, dict(node))
    node = _node(conn, job_id, "unassigned-followup")
    assert node["assignee"] == "runtime-default"


def test_codex_runtime_receipt_is_required_for_runtime_goal_evidence(conn):
    job_id = _job(conn, verifier_required=False)
    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    node = _node(conn, job_id, "understand-scope")
    _complete_node(
        conn,
        node,
        {
            "worker_lane": {"name": "codex-smoke", "kind": "codex_cli", "exit_code": 0},
            "worker_receipt": {"schema": "codex_cli_receipt_v1", "verdict": "pass"},
            "runtime_receipt": {
                "schema": "runtime_worker_receipt_v1",
                "verdict": "pass",
                "summary": "real lane receipt verified the runtime node",
                "claimed_goal_items": ["initial-runtime-result"],
                "verification": {"passed": True, "summary": "local smoke command passed"},
            },
            "verification": {"passed": True},
        },
    )

    assert rk.ingest_runtime_node_evidence(conn, node["id"])
    assert _node(conn, job_id, "understand-scope")["state"] == "succeeded"
    assert rk.status_runtime_job(conn, job_id)["job"]["state"] == "done"


def test_required_evaluator_candidate_receipt_gets_worker_local_provenance(conn):
    job_id = rk.create_runtime_job(
        conn,
        _root_task(conn),
        "prepare one independently evaluated candidate",
        goal_items=[{
            "item_key": "runtime-result",
            "description": "runtime result",
            "required": True,
            "verifier_required": True,
        }],
        initialization_mode="provider_first",
        runtime_metadata={
            "verification_policy": {
                "mode": "required_evaluator",
                "assignee": "runtime-evaluator",
            },
        },
    )
    assert rk.apply_graph_patch(conn, job_id, _patch(
        job_id,
        _revision(conn, job_id),
        {
            "op": "create_node",
            "node_key": "implementation",
            "node_type": "implementation",
            "title": "Implementation",
            "description": "Produce a fixed candidate.",
            "goal_item_keys": ["runtime-result"],
            "contract": _contract(),
        },
    ))["status"] == "applied"
    rk.reduce_runtime_job(conn, job_id)
    node = _node(conn, job_id, "implementation")
    assert rk.materialize_runtime_node(conn, dict(node))
    node = _node(conn, job_id, "implementation")

    receipt = rk.bind_runtime_receipt_provenance(
        conn,
        node["latest_task_id"],
        {
            "schema": "runtime_worker_receipt_v1",
            "verdict": "candidate_ready",
            "summary": "candidate is locally verified",
            "claimed_goal_items": [],
            "partial_goal_items": [],
            "unmet_goal_items": [],
            "changed_files": ["src/runtime.py"],
            "verification": {"passed": True, "summary": "focused tests passed"},
        },
        backend_session_id="codex-thread-1",
    )

    assert receipt["verification_provenance"] == {
        "kind": "worker_local",
        "producer_node_id": node["id"],
        "producer_materialization_id": conn.execute(
            "SELECT id FROM node_materializations WHERE node_id = ?",
            (node["id"],),
        ).fetchone()["id"],
        "producer_session_id": "codex-thread-1",
        "independent": False,
    }


def test_codex_runtime_receipt_rejects_goal_item_outside_node_linkage(conn):
    job_id = _job(conn)
    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    node = _node(conn, job_id, "understand-scope")
    _complete_node(
        conn,
        node,
        {
            "worker_lane": {"name": "codex-smoke", "kind": "codex_cli", "exit_code": 0},
            "runtime_receipt": {
                "schema": "runtime_worker_receipt_v1",
                "verdict": "pass",
                "summary": "invalid cross-goal claim",
                "claimed_goal_items": ["unlinked-goal"],
                "verification": {"passed": True, "summary": "not relevant"},
            },
        },
    )

    assert not rk.ingest_runtime_node_evidence(conn, node["id"])
    reconciled = rk.reconcile_runtime_materializations(conn, job_id)
    assert reconciled["events"] == ["receipt_invalid"]
    assert _node(conn, job_id, "understand-scope")["state"] == "ready"


def test_codex_runtime_receipt_rejects_overlapping_goal_outcomes(conn):
    job_id = _job(conn)
    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    node = _node(conn, job_id, "understand-scope")
    _complete_node(
        conn,
        node,
        {
            "worker_lane": {"name": "codex-smoke", "kind": "codex_cli", "exit_code": 0},
            "runtime_receipt": {
                "schema": "runtime_worker_receipt_v1",
                "verdict": "failed",
                "summary": "one item cannot have two outcomes in one receipt",
                "unmet_goal_items": ["initial-runtime-result"],
                "contradicted_goal_items": ["initial-runtime-result"],
                "verification": {"passed": False, "summary": "failed"},
            },
        },
    )

    assert not rk.ingest_runtime_node_evidence(conn, node["id"])
    assert rk.reconcile_runtime_materializations(conn, job_id)["events"] == ["receipt_invalid"]


def test_progress_ledger_coalesces_overlapping_non_codex_goal_outcomes(conn):
    job_id = _job(conn)
    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    node = _node(conn, job_id, "understand-scope")
    _complete_node(
        conn,
        node,
        {
            "verdict": "failed",
            "summary": "trusted evaluator contradiction",
            "unmet_goal_items": ["initial-runtime-result"],
            "contradicted_goal_items": ["initial-runtime-result"],
            "verification": {"passed": False, "summary": "failed"},
        },
    )

    assert rk.ingest_runtime_node_evidence(conn, node["id"])
    rows = conn.execute(
        "SELECT satisfaction, verification_state FROM progress_ledger WHERE node_id = ?",
        (node["id"],),
    ).fetchall()
    assert [tuple(row) for row in rows] == [("contradicted", "failed")]


def test_codex_runtime_receipt_accepts_goal_item_linked_through_gap(conn):
    job_id = _job(conn, verifier_required=False)
    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    node = _node(conn, job_id, "understand-scope")
    gap = conn.execute(
        """
        SELECT gg.gap_key
          FROM goal_gaps gg
          JOIN goal_items gi ON gi.id = gg.goal_item_id
         WHERE gg.job_id = ? AND gi.item_key = 'initial-runtime-result'
         ORDER BY gg.created_at DESC LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    metadata = json.loads(node["metadata_json"] or "{}")
    metadata["goal_item_keys"] = []
    metadata["gap_keys"] = [gap["gap_key"]]
    conn.execute(
        "UPDATE execution_nodes SET metadata_json = ? WHERE id = ?",
        (json.dumps(metadata), node["id"]),
    )
    node = _node(conn, job_id, "understand-scope")
    _complete_node(
        conn,
        node,
        {
            "worker_lane": {"name": "phase4g8-codex", "kind": "codex_cli", "exit_code": 0},
            "runtime_receipt": {
                "schema": "runtime_worker_receipt_v1",
                "verdict": "pass",
                "summary": "gap-linked runtime result",
                "claimed_goal_items": ["initial-runtime-result"],
                "verification": {"passed": False, "summary": "independent verification remains required"},
            },
        },
    )

    assert rk.ingest_runtime_node_evidence(conn, node["id"])
    assert _node(conn, job_id, "understand-scope")["state"] == "succeeded"


def test_delegation_policy_rejects_multiple_worker_nodes_without_decomposition(conn):
    job_id = _job(conn)
    patch = _patch(
        job_id,
        _revision(conn, job_id),
        {
            "op": "create_node",
            "node_key": "backend",
            "node_type": "implementation",
            "title": "Backend",
            "description": "Implement backend work.",
            "goal_item_keys": ["initial-runtime-result"],
            "contract": _contract("src/backend/**"),
        },
        {
            "op": "create_node",
            "node_key": "docs-site",
            "node_type": "implementation",
            "title": "Docs site",
            "description": "Implement independent docs work.",
            "goal_item_keys": ["initial-runtime-result"],
            "contract": _contract("docs/site/**"),
        },
    )

    result = rk.apply_graph_patch(conn, job_id, patch)

    assert result["status"] == "rejected"
    assert "requires decomposition" in result["reason"]


def test_delegation_policy_allows_nonoverlapping_durable_parallelism(conn):
    job_id = _job(conn)
    patch = _patch(
        job_id,
        _revision(conn, job_id),
        {
            "op": "create_node",
            "node_key": "backend",
            "node_type": "implementation",
            "title": "Backend",
            "description": "Implement backend work.",
            "goal_item_keys": ["initial-runtime-result"],
            "contract": _contract("src/backend/**"),
        },
        {
            "op": "create_node",
            "node_key": "docs-site",
            "node_type": "implementation",
            "title": "Docs site",
            "description": "Implement independent docs work.",
            "goal_item_keys": ["initial-runtime-result"],
            "contract": _contract("docs/site/**"),
        },
    )
    patch["decomposition"] = {
        "policy_version": "1",
        "mode": "multiple_runtime_nodes",
        "justifications": [{
            "type": "durable_parallelism",
            "nodes": ["backend", "docs-site"],
            "explanation": "Outputs are independently owned and integrated by backend.",
            "evidence_refs": [],
            "declared_write_scopes": {
                "backend": ["src/backend/**"],
                "docs-site": ["docs/site/**"],
            },
            "integration_owner_node_key": "backend",
        }],
    }

    result = rk.apply_graph_patch(conn, job_id, patch)

    assert result["status"] == "applied"
    constraints = json.loads(_node(conn, job_id, "backend")["constraints_json"])
    assert constraints["contract"]["declared_write_scope"] == ["src/backend/**"]


def test_delegation_policy_rejects_overlapping_parallel_write_scopes(conn):
    job_id = _job(conn)
    patch = _patch(
        job_id,
        _revision(conn, job_id),
        {
            "op": "create_node", "node_key": "one", "node_type": "implementation",
            "title": "One", "description": "First writer.",
            "goal_item_keys": ["initial-runtime-result"], "contract": _contract("src/**"),
        },
        {
            "op": "create_node", "node_key": "two", "node_type": "implementation",
            "title": "Two", "description": "Second writer.",
            "goal_item_keys": ["initial-runtime-result"], "contract": _contract("src/auth/**"),
        },
    )
    patch["decomposition"] = {
        "policy_version": "1", "mode": "multiple_runtime_nodes",
        "justifications": [{
            "type": "durable_parallelism", "nodes": ["one", "two"],
            "explanation": "Attempt parallel writes.", "evidence_refs": [],
            "declared_write_scopes": {"one": ["src/**"], "two": ["src/auth/**"]},
            "integration_owner_node_key": "one",
        }],
    }

    result = rk.apply_graph_patch(conn, job_id, patch)

    assert result["status"] == "rejected"
    assert "write scopes overlap" in result["reason"]


def test_scope_overlap_does_not_treat_constrained_test_glob_as_all_tests():
    assert not rk._scopes_obviously_overlap(
        ["tests/**/test_plot*.py"],
        ["tests/func/test_stage.py"],
    )
    assert rk._scopes_obviously_overlap(
        ["tests/**/test_plot*.py"],
        ["tests/func/test_plot.py"],
    )
    assert rk._scopes_obviously_overlap(["src/**"], ["src/auth/**"])


def test_structure_checkpoint_overlap_error_names_nodes_and_scopes():
    checkpoint = {
        "schema": rk.STRUCTURE_CHECKPOINT_SCHEMA,
        "kind": "early_structure_assessment",
        "recommendation": "expand",
        "summary": "Two responsibilities were inspected.",
        "inspected_scope": ["src", "tests"],
        "repository_facts": [],
        "proposed_nodes": [
            {
                "node_key": "plots",
                "outcome": "Implement plots.",
                "acceptance_criteria": ["Plots pass."],
                "declared_write_scope": ["tests/**/test_plot*.py"],
                "requested_capabilities": [],
            },
            {
                "node_key": "stage",
                "outcome": "Implement stage behavior.",
                "acceptance_criteria": ["Stage tests pass."],
                "declared_write_scope": ["tests/func/test_plot.py"],
                "requested_capabilities": [],
            },
        ],
        "integration_owner_node_key": "primary",
        "shared_integration_scope": [],
        "risks": [],
        "worker_session_should_resume": True,
    }

    assert rk._structure_checkpoint_validation_error(
        checkpoint,
        node_key="primary",
    ) == (
        "structure checkpoint declared write scope overlap: "
        "node 'plots' scope 'tests/**/test_plot*.py' vs "
        "node 'stage' scope 'tests/func/test_plot.py'"
    )

    checkpoint["proposed_nodes"][1]["declared_write_scope"] = [
        "tests/func/test_stage.py"
    ]
    assert rk._structure_checkpoint_validation_error(
        checkpoint,
        node_key="primary",
    ) is None


def test_terminal_structure_request_is_evented_and_projected_to_decision_delta(conn):
    job_id = _job(conn)
    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    node = _node(conn, job_id, "understand-scope")
    structure_request = {
        "required": True,
        "blocking": True,
        "reason_type": "capability_boundary",
        "completed_scope": ["local adapter implemented"],
        "discovered_gaps": [{
            "gap_key": "verify-staging-contract",
            "description": "Staging credentials are required.",
            "evidence_refs": ["artifact:adapter-test-report"],
        }],
        "suggested_nodes": [{"objective": "Verify the staging contract."}],
    }
    _complete_node(conn, node, {
        "verdict": "blocked",
        "summary": "Local work completed; staging verification is blocked.",
        "claimed_goal_items": [],
        "unmet_goal_items": ["initial-runtime-result"],
        "verification": {"passed": True},
        "structure_request": structure_request,
    })

    assert rk.ingest_runtime_node_evidence(conn, node["id"])
    event = conn.execute(
        "SELECT id, payload_json FROM execution_events WHERE job_id = ? AND event_type = 'worker_structure_requested'",
        (job_id,),
    ).fetchone()
    assert event is not None
    delta = rk.build_decision_delta(conn, job_id)
    assert delta["structure_requests"][0]["event_id"] == event["id"]
    assert delta["structure_requests"][0]["structure_request"] == structure_request


def test_early_structure_checkpoint_pauses_without_ledger_and_resumes_same_session(conn):
    job_id = rk.create_runtime_job(
        conn,
        _root_task(conn),
        "assess and implement one durable orchestra result",
        goal_items=[{
            "item_key": "runtime-result",
            "description": "runtime result exists",
            "required": True,
            "verifier_required": False,
        }],
        initial_assignee="codex-runtime",
        initialization_mode="fixture",
        runtime_metadata={
            "orchestration_policy": {
                "mode": "early_structure_assessment",
                "required": True,
                "assessment_replay": {
                    "schema": "runtime_early_structure_replay_v1",
                    "required_recommendation": "expand",
                    "validated_responsibility_families": ["alpha", "beta", "gamma"],
                    "primary_owned_shared_scope": ["integration"],
                },
            },
        },
    )
    assert rk.advance_runtime_job(conn, job_id, create_tasks=True).materialized_nodes == [
        "understand-scope"
    ]
    node = _node(conn, job_id, "understand-scope")
    task_body = conn.execute(
        "SELECT body FROM tasks WHERE id = ?",
        (node["latest_task_id"],),
    ).fetchone()[0]
    assert "Frozen replay topology" in task_body
    assert '"required_recommendation": "expand"' in task_body
    materialization = conn.execute(
        "SELECT * FROM node_materializations WHERE node_id = ?",
        (node["id"],),
    ).fetchone()
    session_id = "019f0000-0000-7000-8000-000000000010"
    kb.record_task_event(
        conn,
        node["latest_task_id"],
        "worker_backend_session_started",
        {
            "worker_lane": "codex-runtime",
            "worker_kind": "codex_cli",
            "backend_session_id": session_id,
            "execution_mode": "fresh",
        },
    )
    rk.sync_runtime_backend_sessions(conn, job_id)
    checkpoint = {
        "schema": rk.STRUCTURE_CHECKPOINT_SCHEMA,
        "kind": "early_structure_assessment",
        "recommendation": "continue_single_node",
        "summary": "The responsibility is coherent after repository inspection.",
        "inspected_scope": ["src", "tests"],
        "repository_facts": [{
            "fact": "Implementation and tests share one feedback loop.",
            "evidence_refs": ["workspace:path:src"],
        }],
        "proposed_nodes": [],
        "integration_owner_node_key": "understand-scope",
        "shared_integration_scope": [],
        "risks": [],
        "worker_session_should_resume": True,
        "changed_files": [],
    }
    _complete_node(
        conn,
        node,
        {
            "worker_lane": {
                "name": "codex-runtime",
                "kind": "codex_cli",
                "exit_code": 0,
            },
            "runtime_receipt": checkpoint,
        },
    )

    advanced = rk.advance_runtime_job(conn, job_id, create_tasks=False)
    assert advanced.ingested_nodes == ["understand-scope"]
    assert _node(conn, job_id, "understand-scope")["state"] == "waiting_structure"
    assert conn.execute(
        "SELECT COUNT(*) FROM progress_ledger WHERE job_id = ?",
        (job_id,),
    ).fetchone()[0] == 0
    event = conn.execute(
        """
        SELECT id FROM execution_events
         WHERE job_id = ? AND event_type = 'worker_structure_checkpointed'
        """,
        (job_id,),
    ).fetchone()
    assert event is not None
    assert conn.execute(
        "SELECT status FROM node_materializations WHERE id = ?",
        (materialization["id"],),
    ).fetchone()[0] == "structure_checkpoint"
    rk.sync_runtime_backend_sessions(conn, job_id)
    session = conn.execute(
        "SELECT * FROM backend_worker_sessions WHERE node_id = ?",
        (node["id"],),
    ).fetchone()
    assert session["status"] == "interrupted"
    session_checkpoint = json.loads(session["checkpoint_json"])
    assert session_checkpoint["resume_reason"] == "early_structure_integration"
    delta = rk.build_decision_delta(conn, job_id)
    assert delta["structure_checkpoints"][0]["event_id"] == event["id"]

    result = rk.apply_graph_patch(
        conn,
        job_id,
        _patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "continue_node",
                "node_key": "understand-scope",
                "checkpoint_event_id": event["id"],
            },
        ),
    )
    assert result["status"] == "applied"
    resumed_task = rk.materialize_runtime_node(
        conn,
        dict(_node(conn, job_id, "understand-scope")),
    )
    assert resumed_task
    attempt = conn.execute(
        "SELECT * FROM node_materializations WHERE node_id = ? AND attempt = 2",
        (node["id"],),
    ).fetchone()
    continuity = json.loads(attempt["metadata_json"])["execution_continuity"]
    assert continuity["mode"] == "resume"
    assert continuity["resume_session_id"] == session_id
    assert continuity["resume_reason"] == "early_structure_integration"

    conn.execute(
        "UPDATE backend_worker_sessions SET completed_at = 1 WHERE id = ?",
        (session["id"],),
    )
    kb.record_task_event(
        conn,
        resumed_task,
        "worker_backend_session_resumed",
        {
            "worker_lane": "codex-runtime",
            "worker_kind": "codex_cli",
            "backend_session_id": session_id,
            "execution_mode": "resume",
        },
    )
    resumed_node = _node(conn, job_id, "understand-scope")
    _complete_node(
        conn,
        resumed_node,
        {
            "verdict": "succeeded",
            "summary": "The coherent responsibility completed after resume.",
            "claimed_goal_items": ["runtime-result"],
            "changed_files": [],
            "verification": {"passed": True, "summary": "local checks passed"},
        },
    )
    assert rk.ingest_runtime_node_evidence(conn, resumed_node["id"])
    rk.sync_runtime_backend_sessions(conn, job_id)
    completed_session = conn.execute(
        "SELECT * FROM backend_worker_sessions WHERE id = ?",
        (session["id"],),
    ).fetchone()
    completed_attempt = conn.execute(
        "SELECT * FROM node_materializations WHERE id = ?",
        (attempt["id"],),
    ).fetchone()
    assert completed_session["status"] == "completed"
    assert completed_session["completed_at"] == completed_attempt["completed_at"]
    orchestration = rk.summarize_runtime_orchestration(conn, job_id)
    assert orchestration["worker_sessions"][0]["backend_session_key"] == session_id
    assert orchestration["worker_sessions"][0]["resume_count"] == 1


def test_early_structure_checkpoint_rejects_workspace_mutation(conn):
    job_id = rk.create_runtime_job(
        conn,
        _root_task(conn),
        "assess before implementation",
        goal_items=[{"item_key": "result", "description": "result", "required": True}],
        initialization_mode="fixture",
        runtime_metadata={
            "orchestration_policy": {"mode": "early_structure_assessment"},
        },
    )
    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    node = _node(conn, job_id, "understand-scope")
    checkpoint = {
        "schema": rk.STRUCTURE_CHECKPOINT_SCHEMA,
        "kind": "early_structure_assessment",
        "recommendation": "continue_single_node",
        "summary": "Assessment mutated source and must be rejected.",
        "inspected_scope": ["src"],
        "repository_facts": [],
        "proposed_nodes": [],
        "integration_owner_node_key": "understand-scope",
        "shared_integration_scope": [],
        "risks": [],
        "worker_session_should_resume": True,
        "changed_files": ["src/runtime.py"],
    }
    _complete_node(
        conn,
        node,
        {
            "worker_lane": {"name": "codex-runtime", "kind": "codex_cli"},
            "runtime_receipt": checkpoint,
        },
    )

    assert not rk.ingest_runtime_node_evidence(conn, node["id"])
    assert _node(conn, job_id, "understand-scope")["state"] == "running"

    reconciled = rk.reconcile_runtime_materializations(conn, job_id)
    assert reconciled["events"] == ["receipt_invalid"]
    event = conn.execute(
        """
        SELECT payload_json FROM execution_events
         WHERE job_id = ? AND event_type = 'receipt_invalid'
        """,
        (job_id,),
    ).fetchone()
    assert json.loads(event["payload_json"])["validation_error"] == (
        "early structure assessment must not modify workspace files"
    )


def test_early_structure_expansion_requires_checkpoint_backed_child_dependencies(conn):
    job_id = _job(conn, verifier_required=False)
    primary = _node(conn, job_id, "understand-scope")
    conn.execute(
        "UPDATE execution_nodes SET state = 'waiting_structure' WHERE id = ?",
        (primary["id"],),
    )
    checkpoint_event_id = rk._event(
        conn,
        job_id,
        "worker_structure_checkpointed",
        {
            "node_key": primary["node_key"],
            "materialization_id": "mat-assessment",
            "attempt": 1,
            "checkpoint": {
                "schema": rk.STRUCTURE_CHECKPOINT_SCHEMA,
                "kind": "early_structure_assessment",
                "recommendation": "expand",
                "summary": "Two isolated responsibilities were found.",
            },
        },
        node_id=primary["id"],
    )
    children = [
        {
            "op": "create_node",
            "node_key": "plots-child",
            "node_type": "implementation",
            "title": "Plots child",
            "description": "Implement plots responsibility.",
            "goal_item_keys": ["initial-runtime-result"],
            "contract": {
                **_contract("src/plots/**"),
                "workspace_mode": "isolated_worktree",
            },
        },
        {
            "op": "create_node",
            "node_key": "tree-child",
            "node_type": "implementation",
            "title": "Tree child",
            "description": "Implement tree responsibility.",
            "goal_item_keys": ["initial-runtime-result"],
            "contract": {
                **_contract("src/tree/**"),
                "workspace_mode": "isolated_worktree",
            },
        },
    ]
    dependency_ops = [
        {
            "op": "add_dependency",
            "from_node_key": child["node_key"],
            "to_node_key": primary["node_key"],
        }
        for child in children
    ]
    patch = _patch(
        job_id,
        _revision(conn, job_id),
        *children,
        *dependency_ops,
    )
    patch["decomposition"] = {
        "policy_version": "1",
        "mode": "multiple_runtime_nodes",
        "justifications": [{
            "type": "durable_parallelism",
            "nodes": ["plots-child", "tree-child"],
            "explanation": "The write scopes are independent and primary owns integration.",
            "evidence_refs": [],
            "declared_write_scopes": {
                "plots-child": ["src/plots/**"],
                "tree-child": ["src/tree/**"],
            },
            "integration_owner_node_key": primary["node_key"],
        }],
    }

    rejected = rk.apply_graph_patch(conn, job_id, patch)
    assert rejected["status"] == "rejected"
    assert "reference checkpoint event evidence" in rejected["reason"]

    patch["decomposition"]["justifications"][0]["evidence_refs"] = [
        f"event:{checkpoint_event_id}"
    ]
    applied = rk.apply_graph_patch(conn, job_id, patch)

    assert applied["status"] == "applied"
    assert _node(conn, job_id, primary["node_key"])["state"] == "waiting_dependency"
    assert _node(conn, job_id, "plots-child")["state"] == "ready"
    assert _node(conn, job_id, "tree-child")["state"] == "ready"
    dependencies = {
        row["node_key"]
        for row in conn.execute(
            """
            SELECT source.node_key
              FROM execution_dependencies dep
              JOIN execution_nodes source ON source.id = dep.from_node_id
             WHERE dep.to_node_id = ?
            """,
            (primary["id"],),
        ).fetchall()
    }
    assert dependencies == {"plots-child", "tree-child"}


def test_isolated_children_freeze_contributions_for_primary_integration(
    conn,
    tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "plots").mkdir()
    (workspace / "tree").mkdir()
    (workspace / "plots" / "feature.py").write_text("value = 1\n", encoding="utf-8")
    (workspace / "tree" / "feature.py").write_text("value = 1\n", encoding="utf-8")
    _git(workspace, "init")
    _git(workspace, "config", "user.email", "runtime@example.invalid")
    _git(workspace, "config", "user.name", "Runtime Test")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", "base")
    base_revision = _git(workspace, "rev-parse", "HEAD")
    job_id = rk.create_runtime_job(
        conn,
        _root_task(conn),
        "integrate isolated runtime contributions",
        workspace_path=str(workspace),
        goal_items=[{
            "item_key": "runtime-result",
            "description": "integrated result",
            "required": True,
            "verifier_required": False,
        }],
        initialization_mode="fixture",
        runtime_metadata={
            "orchestration_policy": {
                "mode": "early_structure_assessment",
                "base_revision": base_revision,
                "worktree_root": str(tmp_path / "worktrees"),
                "contribution_root": str(tmp_path / "contributions"),
                "require_contribution_attribution": True,
                "minimum_integrated_contributions": 2,
            },
        },
    )
    primary = _node(conn, job_id, "understand-scope")
    conn.execute(
        "UPDATE execution_nodes SET state = 'waiting_structure' WHERE id = ?",
        (primary["id"],),
    )
    checkpoint_event_id = rk._event(
        conn,
        job_id,
        "worker_structure_checkpointed",
        {
            "node_key": primary["node_key"],
            "checkpoint": {
                "schema": rk.STRUCTURE_CHECKPOINT_SCHEMA,
                "kind": "early_structure_assessment",
                "recommendation": "expand",
                "summary": "Plots and tree are isolated responsibilities.",
            },
        },
        node_id=primary["id"],
    )
    child_specs = [
        ("plots-child", "plots/**"),
        ("tree-child", "tree/**"),
    ]
    create_ops = [
        {
            "op": "create_node",
            "node_key": key,
            "node_type": "implementation",
            "title": key,
            "description": f"Implement {key}.",
            "goal_item_keys": ["runtime-result"],
            "contract": {
                **_contract(scope),
                "workspace_mode": "isolated_worktree",
            },
        }
        for key, scope in child_specs
    ]
    dependency_ops = [
        {
            "op": "add_dependency",
            "from_node_key": key,
            "to_node_key": primary["node_key"],
        }
        for key, _scope in child_specs
    ]
    patch = _patch(
        job_id,
        _revision(conn, job_id),
        *create_ops,
        *dependency_ops,
    )
    patch["decomposition"] = {
        "policy_version": "1",
        "mode": "multiple_runtime_nodes",
        "justifications": [{
            "type": "durable_parallelism",
            "nodes": [key for key, _scope in child_specs],
            "explanation": "Independent files with primary integration.",
            "evidence_refs": [f"event:{checkpoint_event_id}"],
            "declared_write_scopes": {
                key: [scope] for key, scope in child_specs
            },
            "integration_owner_node_key": primary["node_key"],
        }],
    }
    assert rk.apply_graph_patch(conn, job_id, patch)["status"] == "applied"

    contribution_ids = []
    contribution_paths = []
    for key, scope in child_specs:
        node = _node(conn, job_id, key)
        task_id = rk.materialize_runtime_node(conn, dict(node))
        assert task_id
        node = _node(conn, job_id, key)
        task = kb.get_task(conn, task_id)
        child_workspace = Path(task.workspace_path)
        assert child_workspace != workspace
        assert _git(child_workspace, "rev-parse", "HEAD") == base_revision
        relative = scope.removesuffix("**") + "feature.py"
        (child_workspace / relative).write_text("value = 2\n", encoding="utf-8")
        _complete_node(
            conn,
            node,
            {
                "verdict": "succeeded",
                "summary": f"Completed {key}.",
                "claimed_goal_items": ["runtime-result"],
                "changed_files": [relative],
                "verification": {"passed": True, "summary": "focused test passed"},
            },
        )
        assert rk.ingest_runtime_node_evidence(conn, node["id"])
        artifact = conn.execute(
            """
            SELECT * FROM node_artifacts
             WHERE node_id = ? AND artifact_type = 'runtime_node_contribution'
            """,
            (node["id"],),
        ).fetchone()
        assert artifact is not None
        contribution_ids.append(str(artifact["id"]))
        contribution_paths.append(str(artifact["path_or_ref"]))
        assert Path(artifact["path_or_ref"]).read_text(encoding="utf-8")

    assert _node(conn, job_id, primary["node_key"])["state"] == "ready"
    primary_task_id = rk.materialize_runtime_node(
        conn,
        dict(_node(conn, job_id, primary["node_key"])),
    )
    primary_task = kb.get_task(conn, primary_task_id)
    assert all(value in primary_task.body for value in contribution_ids)
    for patch_path in contribution_paths:
        _git(workspace, "apply", patch_path)
    primary = _node(conn, job_id, primary["node_key"])
    _complete_node(
        conn,
        primary,
        {
            "verdict": "succeeded",
            "summary": "Integrated both isolated contributions.",
            "claimed_goal_items": ["runtime-result"],
            "changed_files": ["plots/feature.py", "tree/feature.py"],
            "accepted_contributions": contribution_ids,
            "modified_contributions": [],
            "rejected_contributions": [],
            "verification": {"passed": True, "summary": "merged tests passed"},
        },
    )
    assert rk.ingest_runtime_node_evidence(conn, primary["id"])
    assert rk.status_runtime_job(conn, job_id)["job"]["state"] == "done"
    ledger = conn.execute(
        "SELECT satisfaction, node_id FROM progress_ledger "
        "WHERE job_id = ? ORDER BY created_at, rowid",
        (job_id,),
    ).fetchall()
    assert [row["satisfaction"] for row in ledger[:-1]] == ["partial", "partial"]
    assert ledger[-1]["satisfaction"] == "full"


def test_primary_integration_rejects_unknown_or_unclassified_contributions(
    conn,
    tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job_id = rk.create_runtime_job(
        conn,
        _root_task(conn),
        "validate contribution classifications",
        workspace_path=str(workspace),
        goal_items=[{
            "item_key": "result",
            "description": "integrated result",
            "required": True,
            "verifier_required": False,
        }],
        initialization_mode="fixture",
        runtime_metadata={
            "orchestration_policy": {
                "mode": "early_structure_assessment",
                "require_contribution_attribution": True,
                "minimum_integrated_contributions": 1,
            },
        },
    )
    primary = _node(conn, job_id, "understand-scope")
    child_ids = []
    for index in range(2):
        child_id = f"rnode_contribution_{index}"
        child_ids.append(child_id)
        conn.execute(
            """
            INSERT INTO execution_nodes (
                id, job_id, node_key, node_type, state, title, description,
                assumptions_json, constraints_json, metadata_json,
                created_at, updated_at, completed_at
            ) VALUES (?, ?, ?, 'implementation', 'succeeded', ?, ?, '{}', '{}',
                      '{"non_authoritative_contribution":true}', 1, 1, 1)
            """,
            (child_id, job_id, f"child-{index}", f"child-{index}", f"child-{index}"),
        )
        conn.execute(
            """
            INSERT INTO execution_dependencies (
                id, job_id, from_node_id, to_node_id, dependency_type,
                required, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, 'depends_on', 1, '{}', 1)
            """,
            (f"dep_{index}", job_id, child_id, primary["id"]),
        )
        conn.execute(
            """
            INSERT INTO node_artifacts (
                id, job_id, node_id, artifact_type, path_or_ref, summary,
                metadata_json, created_at
            ) VALUES (?, ?, ?, 'runtime_node_contribution', ?, 'contribution', ?, 1)
            """,
            (
                f"artifact-{index}",
                job_id,
                child_id,
                str(tmp_path / f"artifact-{index}.patch"),
                json.dumps({"changed_files": [f"src/{index}.py"], "file_sha256": {}}),
            ),
        )

    evidence, violations = rk._verify_integrated_contributions(
        conn,
        dict(conn.execute("SELECT * FROM runtime_jobs WHERE id = ?", (job_id,)).fetchone()),
        dict(_node(conn, job_id, "understand-scope")),
        {
            "verdict": "succeeded",
            "claimed_goal_items": ["result"],
            "accepted_contributions": ["artifact-0", "artifact-missing"],
            "modified_contributions": ["artifact-0"],
            "rejected_contributions": [],
            "changed_files": ["src/0.py"],
            "verification": {"passed": True},
        },
    )

    assert "unknown_contribution:artifact-missing" in violations
    assert "contribution_classification_overlap:artifact-0" in violations
    assert "contribution_not_classified:artifact-1" in violations
    assert evidence["verdict"] == "failed"
    assert evidence["claimed_goal_items"] == []


def test_primary_integration_preserves_modified_lineage_across_remediation_attempts(
    conn,
    tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job_id = rk.create_runtime_job(
        conn,
        _root_task(conn),
        "preserve contribution lineage across remediation",
        workspace_path=str(workspace),
        goal_items=[{
            "item_key": "result",
            "description": "integrated result",
            "required": True,
            "verifier_required": True,
        }],
        initialization_mode="fixture",
        runtime_metadata={
            "orchestration_policy": {
                "mode": "early_structure_assessment",
                "require_contribution_attribution": True,
                "minimum_integrated_contributions": 2,
            },
        },
    )
    primary = dict(_node(conn, job_id, "understand-scope"))
    assert rk.materialize_runtime_node(conn, primary)
    primary = dict(_node(conn, job_id, "understand-scope"))
    for index in range(3):
        child_id = f"rnode_lineage_{index}"
        artifact_id = f"artifact-lineage-{index}"
        conn.execute(
            """
            INSERT INTO execution_nodes (
                id, job_id, node_key, node_type, state, title, description,
                assumptions_json, constraints_json, metadata_json,
                created_at, updated_at, completed_at
            ) VALUES (?, ?, ?, 'implementation', 'succeeded', ?, ?, '{}', '{}',
                      '{"non_authoritative_contribution":true}', 1, 1, 1)
            """,
            (child_id, job_id, f"child-{index}", f"child-{index}", f"child-{index}"),
        )
        conn.execute(
            """
            INSERT INTO execution_dependencies (
                id, job_id, from_node_id, to_node_id, dependency_type,
                required, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, 'depends_on', 1, '{}', 1)
            """,
            (f"dep-lineage-{index}", job_id, child_id, primary["id"]),
        )
        conn.execute(
            """
            INSERT INTO node_artifacts (
                id, job_id, node_id, artifact_type, path_or_ref, summary,
                metadata_json, created_at
            ) VALUES (?, ?, ?, 'runtime_node_contribution', ?, 'contribution', ?, 1)
            """,
            (
                artifact_id,
                job_id,
                child_id,
                str(tmp_path / f"{artifact_id}.patch"),
                json.dumps({
                    "changed_files": [f"src/{index}.py"],
                    "file_sha256": {},
                }),
            ),
        )

    job = dict(conn.execute(
        "SELECT * FROM runtime_jobs WHERE id = ?",
        (job_id,),
    ).fetchone())
    classifications = [f"artifact-lineage-{index}" for index in range(3)]
    initial, violations = rk._verify_integrated_contributions(
        conn,
        job,
        primary,
        {
            "verdict": "candidate_ready",
            "modified_contributions": classifications,
            "accepted_contributions": [],
            "rejected_contributions": [],
            "changed_files": ["src/0.py", "src/1.py", "src/2.py"],
        },
    )
    assert violations == []
    assert "contribution_lineage_refs" not in initial
    prior_event_id = rk._event(
        conn,
        job_id,
        "contribution_attribution_verified",
        {
            "node_key": primary["node_key"],
            "modified_contributions": classifications,
            "accepted_contributions": [],
            "rejected_contributions": [],
        },
        node_id=primary["id"],
    )

    remediated, violations = rk._verify_integrated_contributions(
        conn,
        job,
        primary,
        {
            "verdict": "candidate_ready",
            "modified_contributions": classifications,
            "accepted_contributions": [],
            "rejected_contributions": [],
            # Worker receipts report the cumulative candidate diff, not only
            # files touched during the current materialization.
            "changed_files": ["src/0.py", "src/1.py", "src/2.py"],
        },
    )

    assert violations == []
    assert remediated["contribution_lineage_refs"] == {
        "artifact-lineage-0": f"event:{prior_event_id}",
        "artifact-lineage-1": f"event:{prior_event_id}",
        "artifact-lineage-2": f"event:{prior_event_id}",
    }


def test_declared_write_scope_violation_prevents_goal_satisfaction(conn):
    job_id = _job(conn)
    assert rk.apply_graph_patch(conn, job_id, _patch(
        job_id,
        _revision(conn, job_id),
        {
            "op": "create_node", "node_key": "scoped-writer", "node_type": "implementation",
            "title": "Scoped writer", "description": "Write only auth files.",
            "goal_item_keys": ["initial-runtime-result"], "contract": _contract("src/auth/**"),
        },
    ))["status"] == "applied"
    node = _node(conn, job_id, "scoped-writer")
    rk.materialize_runtime_node(conn, dict(node))
    node = _node(conn, job_id, "scoped-writer")
    _complete_node(conn, node, {
        "verdict": "succeeded",
        "summary": "Changed an out-of-scope file.",
        "claimed_goal_items": ["initial-runtime-result"],
        "changed_files": ["src/payments/api.py"],
        "verification": {"passed": True},
    })

    assert rk.ingest_runtime_node_evidence(conn, node["id"])
    assert _node(conn, job_id, "scoped-writer")["state"] == "failed"
    assert rk.status_runtime_job(conn, job_id)["goal_items"][0]["state"] != "satisfied"
    assert conn.execute(
        "SELECT 1 FROM execution_events WHERE job_id = ? AND event_type = 'write_scope_violation'",
        (job_id,),
    ).fetchone() is not None


def _runtime_orchestration_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("runtime smoke\n", encoding="utf-8")
    _git(workspace, "init")
    _git(workspace, "config", "user.email", "runtime@example.invalid")
    _git(workspace, "config", "user.name", "Runtime Test")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", "base")
    return workspace


def _register_runtime_orchestration_lane(
    *,
    sandbox: str = "workspace-write",
    max_concurrency: int = 3,
) -> None:
    register_worker_lane(
        WorkerLane(
            name="codex-runtime",
            kind="codex_cli",
            description="runtime orchestration test lane",
            spawn_fn=lambda **_kwargs: None,
            max_concurrency=max_concurrency,
            source="test",
            config={"sandbox": sandbox},
        )
    )


def test_runtime_orchestration_defaults_to_coherent_single_primary(conn):
    job_id = _job(conn, verifier_required=False)

    summary = rk.status_runtime_job(conn, job_id)["orchestration"]

    assert summary["schema"] == rk.RUNTIME_ORCHESTRATION_POLICY_SCHEMA
    assert summary["mode"] == "coherent_single_primary"
    assert summary["enabled"] is False
    assert summary["child_count"] == 0
    assert summary["contribution_count"] == 0


def test_runtime_orchestration_resolves_trusted_early_policy(conn, tmp_path):
    workspace = _runtime_orchestration_workspace(tmp_path)
    artifact_root = tmp_path / "artifacts"
    _register_runtime_orchestration_lane(max_concurrency=2)

    job_id = rk.create_runtime_job(
        conn,
        _root_task(conn),
        "split a real coherent deliverable when repository evidence supports it",
        workspace_path=str(workspace),
        initial_assignee="codex-runtime",
        goal_items=[{
            "item_key": "result",
            "description": "integrated runtime result",
            "required": True,
            "verifier_required": False,
        }],
        initialization_mode="fixture",
        orchestration_policy={
            "mode": "early_structure_assessment",
            "max_child_nodes": 3,
            "artifact_root": str(artifact_root),
            "retention": "cleanup_on_terminal",
        },
    )

    summary = rk.status_runtime_job(conn, job_id)["orchestration"]
    assert summary["enabled"] is True
    assert summary["worker_lane"] == "codex-runtime"
    assert summary["max_child_nodes"] == 2
    assert summary["base_revision"] == _git(workspace, "rev-parse", "HEAD")
    assert Path(summary["root"]).parent == artifact_root.resolve()
    assert summary["retention"] == {
        "worktrees": "cleanup_on_terminal",
        "contributions": "retain",
    }


def test_runtime_orchestration_rejects_dirty_workspace(conn, tmp_path):
    workspace = _runtime_orchestration_workspace(tmp_path)
    (workspace / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    _register_runtime_orchestration_lane()

    with pytest.raises(ValueError, match="clean Git workspace"):
        rk.create_runtime_job(
            conn,
            _root_task(conn),
            "reject dirty workspace",
            workspace_path=str(workspace),
            initial_assignee="codex-runtime",
            initialization_mode="fixture",
            orchestration_policy={
                "mode": "early_structure_assessment",
                "artifact_root": str(tmp_path / "artifacts"),
            },
        )


def test_runtime_orchestration_rejects_non_git_workspace(conn, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _register_runtime_orchestration_lane()

    with pytest.raises(ValueError, match="Git repository root"):
        rk.create_runtime_job(
            conn,
            _root_task(conn),
            "reject non-git workspace",
            workspace_path=str(workspace),
            initial_assignee="codex-runtime",
            initialization_mode="fixture",
            orchestration_policy={
                "mode": "early_structure_assessment",
                "artifact_root": str(tmp_path / "artifacts"),
            },
        )


def test_runtime_orchestration_rejects_artifact_root_inside_workspace(
    conn,
    tmp_path,
):
    workspace = _runtime_orchestration_workspace(tmp_path)
    _register_runtime_orchestration_lane()

    with pytest.raises(ValueError, match="must be outside"):
        rk.create_runtime_job(
            conn,
            _root_task(conn),
            "reject unsafe artifact root",
            workspace_path=str(workspace),
            initial_assignee="codex-runtime",
            initialization_mode="fixture",
            orchestration_policy={
                "mode": "early_structure_assessment",
                "artifact_root": str(workspace / ".runtime-artifacts"),
            },
        )


@pytest.mark.parametrize(
    "sandbox,max_concurrency,error",
    [
        ("read-only", 3, "must allow workspace writes"),
        ("workspace-write", 1, "max_concurrency >= 2"),
    ],
)
def test_runtime_orchestration_rejects_incapable_lane(
    conn,
    tmp_path,
    sandbox,
    max_concurrency,
    error,
):
    workspace = _runtime_orchestration_workspace(tmp_path)
    _register_runtime_orchestration_lane(
        sandbox=sandbox,
        max_concurrency=max_concurrency,
    )

    with pytest.raises(ValueError, match=error):
        rk.create_runtime_job(
            conn,
            _root_task(conn),
            "reject incapable lane",
            workspace_path=str(workspace),
            initial_assignee="codex-runtime",
            initialization_mode="fixture",
            orchestration_policy={
                "mode": "early_structure_assessment",
                "artifact_root": str(tmp_path / "artifacts"),
            },
        )


def _runtime_orchestration_cleanup_job(conn, tmp_path):
    workspace = _runtime_orchestration_workspace(tmp_path)
    _register_runtime_orchestration_lane()
    job_id = rk.create_runtime_job(
        conn,
        _root_task(conn),
        "clean isolated worktrees only after frozen contributions are durable",
        workspace_path=str(workspace),
        initial_assignee="codex-runtime",
        goal_items=[{
            "item_key": "result",
            "description": "integrated runtime result",
            "required": True,
            "verifier_required": False,
        }],
        initialization_mode="fixture",
        orchestration_policy={
            "mode": "early_structure_assessment",
            "artifact_root": str(tmp_path / "artifacts"),
            "retention": "cleanup_on_terminal",
        },
    )
    policy = json.loads(
        conn.execute(
            "SELECT metadata_json FROM runtime_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()[0]
    )["orchestration_policy"]
    worktree = Path(policy["worktree_root"]) / "child-one"
    worktree.parent.mkdir(parents=True)
    _git(workspace, "worktree", "add", "--detach", str(worktree), policy["base_revision"])
    contribution = Path(policy["contribution_root"]) / "child-one" / "attempt-1.patch"
    contribution.parent.mkdir(parents=True)
    contribution.write_text("diff --git a/README.md b/README.md\n", encoding="utf-8")
    digest = hashlib.sha256(contribution.read_bytes()).hexdigest()
    child_id = "rnode_cleanup_child"
    now = 1
    conn.execute(
        """
        INSERT INTO execution_nodes (
            id, job_id, node_key, node_type, state, title, description,
            assumptions_json, constraints_json, metadata_json,
            created_at, updated_at, completed_at
        ) VALUES (?, ?, 'child-one', 'implementation', 'succeeded',
                  'Child one', 'Child one', '{}', '{}', ?, ?, ?, ?)
        """,
        (
            child_id,
            job_id,
            json.dumps({
                "non_authoritative_contribution": True,
                "contribution_to_node_key": "understand-scope",
                "runtime_workspace": {"path": str(worktree)},
            }),
            now,
            now,
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO node_artifacts (
            id, job_id, node_id, artifact_type, path_or_ref, summary,
            metadata_json, created_at
        ) VALUES ('artifact-cleanup-child', ?, ?, 'runtime_node_contribution',
                  ?, 'frozen contribution', ?, ?)
        """,
        (
            job_id,
            child_id,
            str(contribution),
            json.dumps({
                "patch_sha256": digest,
                "patch_bytes": contribution.stat().st_size,
                "scope_status": "verified",
                "integration_owner_node_key": "understand-scope",
            }),
            now,
        ),
    )
    return job_id, worktree, contribution


def test_runtime_orchestration_cleanup_refuses_active_job_and_hash_mismatch(
    conn,
    tmp_path,
):
    job_id, worktree, contribution = _runtime_orchestration_cleanup_job(conn, tmp_path)

    active = rk.cleanup_runtime_orchestration_worktrees(conn, job_id)
    assert active["status"] == "refused"
    assert worktree.is_dir()

    conn.execute("UPDATE runtime_jobs SET state = 'done' WHERE id = ?", (job_id,))
    contribution.write_text("tampered\n", encoding="utf-8")
    mismatched = rk.cleanup_runtime_orchestration_worktrees(conn, job_id)
    assert mismatched["status"] == "refused"
    assert "hash mismatch" in mismatched["reason"]
    assert worktree.is_dir()


def test_runtime_orchestration_terminal_cleanup_retains_contribution(conn, tmp_path):
    job_id, worktree, contribution = _runtime_orchestration_cleanup_job(conn, tmp_path)
    conn.execute("UPDATE runtime_jobs SET state = 'done' WHERE id = ?", (job_id,))

    cleaned = rk.cleanup_runtime_orchestration_worktrees(conn, job_id)

    assert cleaned["status"] == "cleaned"
    assert not worktree.exists()
    assert contribution.is_file()
    summary = rk.summarize_runtime_orchestration(conn, job_id)
    assert summary["latest_cleanup"]["event_type"] == (
        "runtime_orchestration_worktrees_cleaned"
    )


def test_runtime_orchestration_advance_applies_terminal_retention(conn, tmp_path):
    job_id, worktree, contribution = _runtime_orchestration_cleanup_job(conn, tmp_path)
    rk.waive_goal_item(
        conn,
        job_id,
        "result",
        reason="test closes the goal after contribution archival",
        source="test",
    )

    advanced = rk.advance_runtime_job(conn, job_id, create_tasks=False)

    assert advanced.job_state == "done"
    assert advanced.recovery["orchestration_cleanup"]["status"] == "cleaned"
    assert not worktree.exists()
    assert contribution.is_file()


def _ordinary_early_expand_patch(
    conn,
    job_id: str,
    primary,
    checkpoint_event_id: int,
    *,
    child_count: int = 2,
    assignee: str = "codex-runtime",
    capabilities: list[str] | None = None,
):
    capabilities = capabilities or ["filesystem_read", "workspace_write"]
    children = []
    dependencies = []
    scopes = {}
    for index in range(child_count):
        key = f"child-{index}"
        scope = f"src/area-{index}/**"
        children.append({
            "op": "create_node",
            "node_key": key,
            "node_type": "implementation",
            "title": f"Child {index}",
            "description": f"Implement isolated area {index}.",
            "assignee": assignee,
            "goal_item_keys": ["initial-runtime-result"],
            "requested_capabilities": capabilities,
            "contract": {
                **_contract(scope),
                "workspace_mode": "isolated_worktree",
            },
        })
        dependencies.append({
            "op": "add_dependency",
            "from_node_key": key,
            "to_node_key": primary["node_key"],
        })
        scopes[key] = [scope]
    patch = _patch(
        job_id,
        _revision(conn, job_id),
        *children,
        *dependencies,
    )
    patch["decomposition"] = {
        "policy_version": "1",
        "mode": "multiple_runtime_nodes",
        "justifications": [{
            "type": "durable_parallelism",
            "nodes": [child["node_key"] for child in children],
            "explanation": "Repository evidence shows isolated write scopes.",
            "evidence_refs": [f"event:{checkpoint_event_id}"],
            "declared_write_scopes": scopes,
            "integration_owner_node_key": primary["node_key"],
        }],
    }
    return patch


def _ordinary_early_validator_job(conn):
    job_id = _job(conn, verifier_required=False)
    job = conn.execute(
        "SELECT metadata_json FROM runtime_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    metadata = json.loads(job["metadata_json"])
    metadata["orchestration_policy"] = {
        "schema": rk.RUNTIME_ORCHESTRATION_POLICY_SCHEMA,
        "mode": "early_structure_assessment",
        "enabled": True,
        "worker_lane": "codex-runtime",
        "max_child_nodes": 2,
        "required_child_capabilities": list(
            rk.RUNTIME_ORCHESTRATION_CHILD_CAPABILITIES
        ),
    }
    conn.execute(
        "UPDATE runtime_jobs SET metadata_json = ? WHERE id = ?",
        (json.dumps(metadata), job_id),
    )
    primary = _node(conn, job_id, "understand-scope")
    conn.execute(
        "UPDATE execution_nodes SET state = 'waiting_structure' WHERE id = ?",
        (primary["id"],),
    )
    checkpoint_event_id = rk._event(
        conn,
        job_id,
        "worker_structure_checkpointed",
        {
            "checkpoint": {
                "schema": rk.STRUCTURE_CHECKPOINT_SCHEMA,
                "kind": "early_structure_assessment",
                "recommendation": "expand",
                "summary": "Two isolated responsibilities were found.",
            }
        },
        node_id=primary["id"],
    )
    return job_id, primary, checkpoint_event_id


def test_ordinary_runtime_orchestration_enforces_child_budget(conn):
    job_id, primary, event_id = _ordinary_early_validator_job(conn)
    patch = _ordinary_early_expand_patch(
        conn,
        job_id,
        primary,
        event_id,
        child_count=3,
    )

    result = rk.apply_graph_patch(conn, job_id, patch)

    assert result["status"] == "rejected"
    assert "between two and 2 child nodes" in result["reason"]


@pytest.mark.parametrize(
    "assignee,capabilities,error",
    [
        (
            "other-lane",
            ["filesystem_read", "workspace_write"],
            "configured worker lane",
        ),
        (
            "codex-runtime",
            ["network_access"],
            "capabilities exceed",
        ),
    ],
)
def test_ordinary_runtime_orchestration_enforces_lane_and_capabilities(
    conn,
    assignee,
    capabilities,
    error,
):
    job_id, primary, event_id = _ordinary_early_validator_job(conn)
    patch = _ordinary_early_expand_patch(
        conn,
        job_id,
        primary,
        event_id,
        assignee=assignee,
        capabilities=capabilities,
    )

    result = rk.apply_graph_patch(conn, job_id, patch)

    assert result["status"] == "rejected"
    assert error in result["reason"]


def _closed_loop_coordination_job(conn):
    job_id = rk.create_runtime_job(
        conn,
        _root_task(conn),
        "coordinate parser and renderer through Runtime",
        goal_items=[{
            "item_key": "coordinated-result",
            "description": "parser and renderer share one token contract",
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
            },
        },
    )
    parser = _node(conn, job_id, "understand-scope")
    constraints = json.loads(parser["constraints_json"])
    constraints["contract"] = _contract("src/parser/**")
    metadata = json.loads(parser["metadata_json"])
    metadata["coordination_checkpoint_required"] = True
    conn.execute(
        """
        UPDATE execution_nodes
           SET title = 'Implement parser', description = 'Implement parser token model.',
               constraints_json = ?, metadata_json = ?
         WHERE id = ?
        """,
        (json.dumps(constraints), json.dumps(metadata), parser["id"]),
    )
    result = rk.apply_graph_patch(
        conn,
        job_id,
        _patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "create_node",
                "node_key": "renderer",
                "node_type": "implementation",
                "title": "Implement renderer",
                "description": "Implement rendering for parser tokens.",
                "assignee": "codex-runtime",
                "goal_item_keys": ["coordinated-result"],
                "contract": _contract("src/renderer/**"),
            },
        ),
    )
    assert result["status"] == "applied"
    renderer = _node(conn, job_id, "renderer")
    renderer_metadata = json.loads(renderer["metadata_json"])
    renderer_metadata["coordination_checkpoint_required"] = True
    conn.execute(
        "UPDATE execution_nodes SET metadata_json = ? WHERE id = ?",
        (json.dumps(renderer_metadata), renderer["id"]),
    )
    rk.reduce_runtime_job(conn, job_id)
    for key, session_id in (
        ("understand-scope", "019f0000-0000-7000-8000-0000000000a1"),
        ("renderer", "019f0000-0000-7000-8000-0000000000b2"),
    ):
        node = _node(conn, job_id, key)
        assert rk.materialize_runtime_node(conn, dict(node))
        node = _node(conn, job_id, key)
        task = conn.execute(
            "SELECT body FROM tasks WHERE id = ?",
            (node["latest_task_id"],),
        ).fetchone()
        assert "Runtime coordination checkpoint mode:" in task["body"]
        kb.record_task_event(
            conn,
            node["latest_task_id"],
            "worker_backend_session_started",
            {
                "worker_lane": "codex-runtime",
                "worker_kind": "codex_cli",
                "backend_session_id": session_id,
                "execution_mode": "fresh",
            },
        )
    rk.sync_runtime_backend_sessions(conn, job_id)
    return job_id


def _coordination_checkpoint(
    *,
    kind: str,
    summary: str,
    finding_key: str,
    affected_node_keys: list[str],
    changed_files: list[str],
    consumed_directive_ids: list[str] | None = None,
):
    return {
        "schema": rk.COORDINATION_CHECKPOINT_SCHEMA,
        "kind": kind,
        "summary": summary,
        "phase": "implementation",
        "completed_scope": [summary],
        "remaining_scope": ["finish focused tests"],
        "findings": [{
            "finding_key": finding_key,
            "type": kind,
            "summary": summary,
            "affected_node_keys": affected_node_keys,
            "evidence_refs": [f"workspace:path:{changed_files[0]}"],
        }],
        "next_intent": "consume Runtime coordination and finish the responsibility",
        "changed_files": changed_files,
        "consumed_directive_ids": consumed_directive_ids or [],
        "worker_session_should_resume": True,
    }


def _complete_codex_checkpoint(conn, node, checkpoint):
    _complete_node(
        conn,
        node,
        {
            "worker_lane": {
                "name": "codex-runtime",
                "kind": "codex_cli",
                "exit_code": 0,
            },
            "runtime_receipt": checkpoint,
        },
    )


def test_coordination_checkpoint_controls_other_active_node_and_acks_resume(conn):
    job_id = _closed_loop_coordination_job(conn)
    parser = _node(conn, job_id, "understand-scope")
    renderer = _node(conn, job_id, "renderer")
    _complete_codex_checkpoint(
        conn,
        parser,
        _coordination_checkpoint(
            kind="shared_contract_changed",
            summary="Parser introduced token-model-v2.",
            finding_key="token-model-v2",
            affected_node_keys=["understand-scope", "renderer"],
            changed_files=["src/parser/token.py"],
        ),
    )
    _complete_codex_checkpoint(
        conn,
        renderer,
        _coordination_checkpoint(
            kind="milestone_completed",
            summary="Renderer completed the v1 adapter slice.",
            finding_key="renderer-v1-slice",
            affected_node_keys=["renderer"],
            changed_files=["src/renderer/render.py"],
        ),
    )
    advanced = rk.advance_runtime_job(conn, job_id, create_tasks=False)
    assert set(advanced.ingested_nodes) == {"understand-scope", "renderer"}
    assert _node(conn, job_id, "understand-scope")["state"] == "waiting_coordination"
    assert _node(conn, job_id, "renderer")["state"] == "waiting_coordination"
    assert conn.execute(
        "SELECT COUNT(*) FROM progress_ledger WHERE job_id = ?",
        (job_id,),
    ).fetchone()[0] == 0
    checkpoints = conn.execute(
        """
        SELECT id, node_id FROM execution_events
         WHERE job_id = ? AND event_type = 'worker_coordination_checkpointed'
         ORDER BY id
        """,
        (job_id,),
    ).fetchall()
    checkpoint_by_node = {row["node_id"]: int(row["id"]) for row in checkpoints}
    delta = rk.build_decision_delta(conn, job_id)
    snapshot = delta["global_execution_snapshot"]
    assert {item["node_key"] for item in snapshot["active_responsibilities"]} == {
        "understand-scope",
        "renderer",
    }
    assert len(snapshot["coordination_checkpoints"]) == 2

    parser_event = checkpoint_by_node[parser["id"]]
    renderer_event = checkpoint_by_node[renderer["id"]]
    renderer_contract = {
        **_contract("src/renderer/**"),
        "acceptance_criteria": [
            "Renderer consumes token-model-v2",
            "Renderer focused tests pass",
        ],
    }
    applied = rk.apply_graph_patch(
        conn,
        job_id,
        _patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "issue_directive",
                "target_node_key": "understand-scope",
                "source_checkpoint_event_id": parser_event,
                "target_checkpoint_event_id": parser_event,
                "action": "continue",
                "expected_contract_revision": 1,
                "summary": "Finish parser tests and publish token-model-v2 evidence.",
                "instructions": ["Finish parser tests without entering renderer scope."],
                "evidence_refs": [f"event:{parser_event}"],
            },
            {
                "op": "issue_directive",
                "target_node_key": "renderer",
                "source_checkpoint_event_id": parser_event,
                "target_checkpoint_event_id": renderer_event,
                "action": "revise_contract",
                "expected_contract_revision": 1,
                "summary": "Consume token-model-v2 in renderer.",
                "instructions": [
                    "Replace the v1 adapter with token-model-v2 before final verification."
                ],
                "evidence_refs": [f"event:{parser_event}"],
                "contract": renderer_contract,
            },
        ),
        decision_id="decision-test",
    )
    assert applied["status"] == "applied"
    assert _node(conn, job_id, "understand-scope")["state"] == "ready"
    assert _node(conn, job_id, "renderer")["state"] == "ready"
    assert _node(conn, job_id, "renderer")["contract_revision"] == 2

    for key in ("understand-scope", "renderer"):
        resumed_task = rk.materialize_runtime_node(
            conn,
            dict(_node(conn, job_id, key)),
        )
        materialization = conn.execute(
            """
            SELECT * FROM node_materializations
             WHERE node_id = ? ORDER BY attempt DESC LIMIT 1
            """,
            (_node(conn, job_id, key)["id"],),
        ).fetchone()
        continuity = json.loads(materialization["metadata_json"])[
            "execution_continuity"
        ]
        assert continuity["mode"] == "resume"
        assert continuity["resume_reason"] == "coordination_directive"
        task_body = conn.execute(
            "SELECT body FROM tasks WHERE id = ?",
            (resumed_task,),
        ).fetchone()[0]
        assert "Runtime coordination directives:" in task_body
        assert "consumed_directive_ids" in task_body

    renderer_directive = conn.execute(
        "SELECT * FROM runtime_node_directives WHERE target_node_id = ?",
        (renderer["id"],),
    ).fetchone()
    assert renderer_directive["status"] == "delivered"
    resumed_renderer = _node(conn, job_id, "renderer")
    invalid_receipt = {
        "schema": "runtime_worker_receipt_v1",
        "verdict": "succeeded",
        "summary": "Renderer ignored its delivered directive.",
        "claimed_goal_items": [],
        "partial_goal_items": ["coordinated-result"],
        "unmet_goal_items": [],
        "contradicted_goal_items": [],
        "changed_files": ["src/renderer/render.py"],
        "verification": {"passed": True, "summary": "renderer tests passed"},
        "artifacts": [],
        "accepted_contributions": [],
        "modified_contributions": [],
        "rejected_contributions": [],
        "active_assumptions": [],
        "rejected_approaches": [],
        "known_failure_boundaries": [],
        "consumed_directive_ids": [],
        "structure_request": None,
    }
    assert rk._runtime_receipt_from_evidence(
        {
            "worker_lane": {"kind": "codex_cli"},
            "runtime_receipt": invalid_receipt,
        },
        dict(resumed_renderer),
        conn=conn,
    ) is None
    kb.record_task_event(
        conn,
        resumed_renderer["latest_task_id"],
        "worker_backend_session_resumed",
        {
            "worker_lane": "codex-runtime",
            "worker_kind": "codex_cli",
            "backend_session_id": "019f0000-0000-7000-8000-000000000031",
            "execution_mode": "resume",
        },
        run_id=resumed_renderer["latest_run_id"],
    )
    _complete_node(
        conn,
        resumed_renderer,
        {
            "worker_lane": {
                "name": "codex-runtime",
                "kind": "codex_cli",
                "exit_code": 0,
            },
            "runtime_receipt": {
                "schema": "runtime_worker_receipt_v1",
                "verdict": "succeeded",
                "summary": "Renderer now consumes token-model-v2.",
                "claimed_goal_items": [],
                "partial_goal_items": ["coordinated-result"],
                "unmet_goal_items": [],
                "contradicted_goal_items": [],
                "changed_files": ["src/renderer/render.py"],
                "verification": {
                    "passed": True,
                    "summary": "renderer tests passed",
                },
                "artifacts": [],
                "accepted_contributions": [],
                "modified_contributions": [],
                "rejected_contributions": [],
                "active_assumptions": [],
                "rejected_approaches": [],
                "known_failure_boundaries": [],
                "consumed_directive_ids": [renderer_directive["id"]],
                "structure_request": None,
            },
        },
    )
    assert rk.ingest_runtime_node_evidence(conn, resumed_renderer["id"])
    acknowledged = conn.execute(
        "SELECT * FROM runtime_node_directives WHERE id = ?",
        (renderer_directive["id"],),
    ).fetchone()
    assert acknowledged["status"] == "acknowledged"
    assert acknowledged["acknowledged_materialization_id"] is not None
    rk.sync_runtime_backend_sessions(conn, job_id)
    orchestration = rk.summarize_runtime_orchestration(conn, job_id)
    assert orchestration["coordination"]["checkpoint_count"] == 2
    assert orchestration["coordination"]["directive_status_counts"][
        "acknowledged"
    ] == 1
    assert orchestration["coordination"]["contract_revisions"]["renderer"] == 2
    assert rk.check_runtime_consistency(conn, job_id, write_events=False)[
        "violations"
    ] == []


def test_running_target_queues_directive_until_its_coordination_safe_point(conn):
    job_id = _closed_loop_coordination_job(conn)
    parser = _node(conn, job_id, "understand-scope")
    renderer = _node(conn, job_id, "renderer")
    _complete_codex_checkpoint(
        conn,
        parser,
        _coordination_checkpoint(
            kind="shared_contract_changed",
            summary="Parser introduced token-model-v2.",
            finding_key="token-model-v2",
            affected_node_keys=["renderer"],
            changed_files=["src/parser/token.py"],
        ),
    )
    assert rk.ingest_runtime_node_evidence(conn, parser["id"])
    parser_event = conn.execute(
        """
        SELECT id FROM execution_events
         WHERE job_id = ? AND node_id = ?
           AND event_type = 'worker_coordination_checkpointed'
        """,
        (job_id, parser["id"]),
    ).fetchone()[0]
    assert _node(conn, job_id, "renderer")["state"] == "running"
    patch = _patch(
        job_id,
        _revision(conn, job_id),
        {
            "op": "issue_directive",
            "target_node_key": "understand-scope",
            "source_checkpoint_event_id": parser_event,
            "target_checkpoint_event_id": parser_event,
            "action": "continue",
            "expected_contract_revision": 1,
            "summary": "Finish parser tests.",
            "instructions": ["Finish parser tests."],
            "evidence_refs": [f"event:{parser_event}"],
        },
        {
            "op": "issue_directive",
            "target_node_key": "renderer",
            "source_checkpoint_event_id": parser_event,
            "action": "revise_contract",
            "expected_contract_revision": 1,
            "summary": "Consume token-model-v2 at the next safe point.",
            "instructions": ["Replace the v1 adapter with token-model-v2."],
            "evidence_refs": [f"event:{parser_event}"],
            "contract": {
                **_contract("src/renderer/**"),
                "acceptance_criteria": ["Renderer consumes token-model-v2"],
            },
        },
    )
    assert rk.apply_graph_patch(conn, job_id, patch)["status"] == "applied"
    queued = conn.execute(
        "SELECT * FROM runtime_node_directives WHERE target_node_id = ?",
        (renderer["id"],),
    ).fetchone()
    assert queued["status"] == "queued"
    assert _node(conn, job_id, "renderer")["contract_revision"] == 1

    _complete_codex_checkpoint(
        conn,
        _node(conn, job_id, "renderer"),
        _coordination_checkpoint(
            kind="milestone_completed",
            summary="Renderer reached its safe point under contract v1.",
            finding_key="renderer-safe-point",
            affected_node_keys=["renderer"],
            changed_files=["src/renderer/render.py"],
        ),
    )
    assert rk.ingest_runtime_node_evidence(conn, renderer["id"])
    activated = conn.execute(
        "SELECT * FROM runtime_node_directives WHERE id = ?",
        (queued["id"],),
    ).fetchone()
    assert activated["status"] == "queued"
    assert activated["target_checkpoint_event_id"] is not None
    assert _node(conn, job_id, "renderer")["state"] == "ready"
    assert _node(conn, job_id, "renderer")["contract_revision"] == 2

    resumed_task = rk.materialize_runtime_node(
        conn,
        dict(_node(conn, job_id, "renderer")),
    )
    delivered = conn.execute(
        "SELECT * FROM runtime_node_directives WHERE id = ?",
        (queued["id"],),
    ).fetchone()
    assert delivered["status"] == "delivered"
    body = conn.execute(
        "SELECT body FROM tasks WHERE id = ?",
        (resumed_task,),
    ).fetchone()[0]
    assert queued["id"] in body
    recovery_body = rk._worker_context(
        conn,
        dict(conn.execute("SELECT * FROM runtime_jobs WHERE id = ?", (job_id,)).fetchone()),
        dict(_node(conn, job_id, "renderer")),
        "mat-recovery-test",
    )
    assert queued["id"] in recovery_body
    assert "Runtime coordination directives:" in recovery_body


def test_coordination_directive_rejects_stale_revision_and_scope_overlap(conn):
    job_id = _closed_loop_coordination_job(conn)
    parser = _node(conn, job_id, "understand-scope")
    renderer = _node(conn, job_id, "renderer")
    for node, checkpoint in (
        (
            parser,
            _coordination_checkpoint(
                kind="shared_contract_changed",
                summary="Parser changed token contract.",
                finding_key="token-contract",
                affected_node_keys=["renderer"],
                changed_files=["src/parser/token.py"],
            ),
        ),
        (
            renderer,
            _coordination_checkpoint(
                kind="milestone_completed",
                summary="Renderer reached safe point.",
                finding_key="renderer-safe-point",
                affected_node_keys=["renderer"],
                changed_files=["src/renderer/render.py"],
            ),
        ),
    ):
        _complete_codex_checkpoint(conn, node, checkpoint)
    rk.advance_runtime_job(conn, job_id, create_tasks=False)
    events = {
        row["node_id"]: int(row["id"])
        for row in conn.execute(
            """
            SELECT id, node_id FROM execution_events
             WHERE job_id = ? AND event_type = 'worker_coordination_checkpointed'
            """,
            (job_id,),
        ).fetchall()
    }
    source_event = events[parser["id"]]
    target_event = events[renderer["id"]]
    stale = rk.apply_graph_patch(
        conn,
        job_id,
        _patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "issue_directive",
                "target_node_key": "renderer",
                "source_checkpoint_event_id": source_event,
                "target_checkpoint_event_id": target_event,
                "action": "continue",
                "expected_contract_revision": 2,
                "summary": "stale contract directive",
                "instructions": ["continue"],
                "evidence_refs": [f"event:{source_event}"],
            },
        ),
    )
    assert stale["status"] == "rejected"
    assert "expected_contract_revision" in stale["reason"]

    overlap = rk.apply_graph_patch(
        conn,
        job_id,
        _patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "issue_directive",
                "target_node_key": "renderer",
                "source_checkpoint_event_id": source_event,
                "target_checkpoint_event_id": target_event,
                "action": "revise_contract",
                "expected_contract_revision": 1,
                "summary": "invalid overlapping scope",
                "instructions": ["enter parser scope"],
                "evidence_refs": [f"event:{source_event}"],
                "contract": _contract("src/parser/**"),
            },
        ),
    )
    assert overlap["status"] == "rejected"
    assert "write scopes overlap" in overlap["reason"]
