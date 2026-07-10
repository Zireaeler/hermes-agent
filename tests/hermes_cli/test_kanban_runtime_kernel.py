from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_decision as rd
from hermes_cli import kanban_runtime_kernel as rk
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


def _job(conn, *, goal_key: str = "initial-runtime-result") -> str:
    return rk.create_runtime_job(
        conn,
        _root_task(conn),
        "ship a phase1 runtime",
        goal_items=[
            {
                "item_key": goal_key,
                "description": "phase1 runtime evidence exists",
                "required": True,
                "verifier_required": True,
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


def _complete_node(conn, node, evidence: dict):
    assert node["latest_task_id"]
    assert kb.complete_task(
        conn,
        node["latest_task_id"],
        result=evidence.get("summary", "done"),
        summary=evidence.get("summary", "done"),
        metadata=evidence,
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
            "verification": {"commands": ["pytest"], "passed": True, "summary": "passed"},
        },
    )
    assert rk.ingest_runtime_node_evidence(conn, node["id"])
    status = rk.status_runtime_job(conn, job_id)
    assert status["progress_ledger"][0]["satisfaction"] == "full"
    assert status["progress_ledger"][0]["verification_state"] == "verified"
    assert status["goal_items"][0]["state"] == "satisfied"
    assert status["job"]["state"] == "done"


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
    job_id = _job(conn)
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
            "summary": "implementation produced self-reported evidence",
            "claimed_goal_items": ["initial-runtime-result"],
            "verification": {"passed": False, "summary": "not independently verified"},
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
                "verifier_required": True,
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
    job_id = _job(conn)
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
