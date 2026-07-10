from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_kernel as rk
from hermes_cli import kanban_runtime_soak as soak


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


def _root_task(conn) -> str:
    return kb.create_task(conn, title="root goal", initial_status="running")


def _job(conn, workspace: Path | None = None) -> str:
    return rk.create_runtime_job(
        conn,
        _root_task(conn),
        "phase4g consistency checker fixture",
        workspace_path=str(workspace) if workspace else None,
        goal_items=[
            {
                "item_key": "runtime-verified",
                "description": "runtime verified evidence exists",
                "required": True,
                "verifier_required": True,
            }
        ],
        initialization_mode="fixture",
    )


def _revision(conn, job_id: str) -> int:
    return int(conn.execute("SELECT graph_revision FROM runtime_jobs WHERE id = ?", (job_id,)).fetchone()[0])


def _patch(job_id: str, revision: int, *ops):
    return {
        "schema": rk.PATCH_SCHEMA,
        "expected_revision": revision,
        "rationale_summary": f"test patch for {job_id}",
        "ops": list(ops),
    }


def _node(conn, job_id: str, key: str):
    return conn.execute(
        "SELECT * FROM execution_nodes WHERE job_id = ? AND node_key = ?",
        (job_id, key),
    ).fetchone()


def test_phase4g_soak_baseline_report(conn, tmp_path):
    report = soak.run_runtime_soak(
        conn,
        max_ticks=20,
        workspace_path=str(tmp_path / "workspace"),
    )

    assert report["scenario"] == soak.PHASE4G_SCENARIO
    assert report["final_state"] == "done"
    assert report["ticks"] >= 20
    assert report["patch_applied"] >= 1
    assert report["patch_rejected"] >= 2
    assert report["stale_patch_rejected"] >= 1
    assert report["worker_recoveries"] >= 1
    assert report["materialization_attempts"] >= 3
    assert report["compactions"] >= 2
    assert report["memory_hints_used"] >= 1
    assert report["capability_blocks"] >= 1
    assert report["human_decisions"] >= 1
    assert report["old_segment_excluded_from_provider_input"] is True
    assert report["checkpoint_memory_hint_leak"] is False
    assert report["consistency"]["status"] == "passed"
    assert {item["state"] for item in report["required_goals"]} == {"satisfied"}


def test_consistency_checker_reports_memory_and_capability_cross_module_violations(conn, tmp_path):
    job_id = _job(conn, tmp_path / "workspace")
    session = conn.execute("SELECT * FROM decision_sessions WHERE job_id = ?", (job_id,)).fetchone()
    segment = conn.execute("SELECT * FROM decision_session_segments WHERE job_id = ?", (job_id,)).fetchone()
    now = rk._now()
    checkpoint_payload = {
        "selected_hints": [{"entry_id": "bad-memory", "non_authoritative": True}],
        "metadata": {"graph_revision": 0},
    }
    conn.execute(
        """
        INSERT INTO decision_checkpoints (
            id, job_id, decision_session_id, revision, checkpoint_json,
            reason, created_at, source_segment_id, checkpoint_revision,
            db_revision, graph_revision, ledger_revision, payload_json,
            validator_status
        ) VALUES ('chk_memory_leak', ?, ?, 1, ?, 'test', ?, ?, 1, 0, 0, 0, ?, 'accepted')
        """,
        (
            job_id,
            session["id"],
            json.dumps(checkpoint_payload, ensure_ascii=False),
            now,
            segment["id"],
            json.dumps(checkpoint_payload, ensure_ascii=False),
        ),
    )
    assert rk.apply_graph_patch(
        conn,
        job_id,
        _patch(
            job_id,
            _revision(conn, job_id),
            {
                "op": "create_node",
                "node_key": "blocked-secret-node",
                "node_type": "implementation",
                "title": "Blocked secret node",
                "description": "This node should not be materialized while blocked.",
                "goal_item_keys": ["runtime-verified"],
                "requested_capabilities": ["secret_access"],
            },
        ),
    )["status"] == "applied"
    node = _node(conn, job_id, "blocked-secret-node")
    metadata = json.loads(node["metadata_json"])
    metadata["capability_policy"] = {
        "status": "requires_human",
        "requested": ["secret_access"],
        "requires_human": ["secret_access"],
    }
    conn.execute(
        "UPDATE execution_nodes SET state = 'waiting_human', metadata_json = ? WHERE id = ?",
        (json.dumps(metadata), node["id"]),
    )
    task_id = kb.create_task(conn, title="bad materialization", initial_status="running")
    conn.execute(
        """
        INSERT INTO node_materializations (
            id, job_id, node_id, attempt, task_id, run_id, status,
            created_at, started_at, metadata_json
        ) VALUES ('mat_bad_capability', ?, ?, 1, ?, NULL, 'running', ?, ?, '{}')
        """,
        (job_id, node["id"], task_id, now, now),
    )

    result = rk.check_runtime_consistency(conn, job_id, write_events=False)

    types = {item["type"] for item in result["violations"]}
    assert "memory_hint_leaked_into_checkpoint" in types
    assert "capability_blocked_node_materialized" in types
