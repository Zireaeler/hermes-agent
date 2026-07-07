from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
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


def _complete_node(conn, node, evidence: dict):
    assert node["latest_task_id"]
    assert kb.complete_task(
        conn,
        node["latest_task_id"],
        result=evidence.get("summary", "done"),
        summary=evidence.get("summary", "done"),
        metadata=evidence,
    )


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
        _patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "insert_verifier",
                "target_goal_item_key": "initial-runtime-result",
                "verifier_node_key": "verify-runtime",
                "title": "Verify runtime",
                "goal_item_keys": ["initial-runtime-result"],
                "gap_keys": ["initial-runtime-result:needs_verification"],
            },
        ),
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


def test_no_runnable_unmet_goal_records_liveness_violation(conn):
    job_id = _job(conn)
    conn.execute("UPDATE execution_nodes SET state = 'failed' WHERE job_id = ? AND node_key = 'understand-scope'", (job_id,))
    reduction = rk.reduce_runtime_job(conn, job_id)
    assert reduction["state"] == "waiting_decision"
    assert any(gap["gap_type"] == "no_runnable_for_open_goal" for gap in reduction["gaps"])
    events = [row["event_type"] for row in conn.execute("SELECT event_type FROM execution_events WHERE job_id = ?", (job_id,))]
    assert "liveness_violation" in events


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
