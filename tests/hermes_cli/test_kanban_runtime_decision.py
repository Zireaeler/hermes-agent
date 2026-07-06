from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_decision as rd
from hermes_cli import kanban_runtime_kernel as rk


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


def _job(conn) -> str:
    root = kb.create_task(conn, title="root", initial_status="running")
    return rk.create_runtime_job(
        conn,
        root,
        "ship phase2b decision layer",
        goal_items=[
            {
                "item_key": "b-item",
                "description": "B item",
                "required": True,
                "verifier_required": True,
            },
            {
                "item_key": "a-item",
                "description": "A item",
                "required": True,
                "verifier_required": True,
            },
        ],
    )


def _revision(conn, job_id: str) -> int:
    return int(conn.execute("SELECT graph_revision FROM runtime_jobs WHERE id = ?", (job_id,)).fetchone()[0])


def test_decision_checkpoint_schema_and_creation(conn):
    job_id = _job(conn)
    checkpoint = rd.create_decision_checkpoint(conn, job_id, reason="test")

    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert "decision_checkpoints" in tables
    assert checkpoint["reason"] == "test"
    assert checkpoint["checkpoint"]["job"]["id"] == job_id
    assert [item["item_key"] for item in checkpoint["checkpoint"]["goal_items"]] == ["a-item", "b-item"]
    assert conn.execute("SELECT COUNT(*) FROM decision_checkpoints WHERE job_id = ?", (job_id,)).fetchone()[0] == 1


def test_decision_prompt_layout_is_canonical(conn):
    job_id = _job(conn)
    delta = rk.build_decision_delta(conn, job_id)
    first = rd.render_decision_prompt(rd.build_decision_provider_request(conn, job_id, delta))
    second = rd.render_decision_prompt(rd.build_decision_provider_request(conn, job_id, delta))

    assert first == second
    assert "delta" not in first["stable_prefix"]
    assert first["stable_prefix"]["runtime_contract"]["forbidden_ops"] == ["release_node", "complete_job"]
    assert [item["item_key"] for item in first["checkpoint"]["goal_items"]] == ["a-item", "b-item"]


def test_provider_patch_parser_accepts_strict_json_object():
    patch = {
        "schema": rk.PATCH_SCHEMA,
        "expected_revision": 7,
        "rationale_summary": "noop",
        "ops": [],
    }
    assert rd.parse_provider_patch(json.dumps(patch), 7) == patch
    assert rd.parse_provider_patch(f"```json\n{json.dumps(patch)}\n```", 7) == patch


@pytest.mark.parametrize(
    "raw,reason",
    [
        ("Here is a patch: {}", "not valid JSON"),
        ({"schema": rk.PATCH_SCHEMA, "expected_revision": 0, "rationale_summary": "bad", "ops": [{"op": "release_node"}]}, "unsupported"),
        ({"schema": rk.PATCH_SCHEMA, "expected_revision": 0, "rationale_summary": "bad", "ops": [{"op": "complete_job"}]}, "unsupported"),
        ({"schema": "other", "expected_revision": 0, "rationale_summary": "bad", "ops": []}, "schema"),
        ({"schema": rk.PATCH_SCHEMA, "rationale_summary": "bad", "ops": []}, "expected_revision"),
        ({"schema": rk.PATCH_SCHEMA, "expected_revision": 99, "rationale_summary": "bad", "ops": []}, "does not match"),
    ],
)
def test_provider_patch_parser_rejects_free_text_and_unknown_ops(raw, reason):
    with pytest.raises(rd.ProviderPatchParseError, match=reason):
        rd.parse_provider_patch(raw, 0)


def test_provider_parse_failure_records_decision_without_graph_change(conn):
    job_id = _job(conn)
    conn.execute("UPDATE execution_nodes SET state = 'failed' WHERE job_id = ? AND node_key = 'understand-scope'", (job_id,))
    before = _revision(conn, job_id)

    result = rk.advance_runtime_job(
        conn,
        job_id,
        create_tasks=False,
        decision_provider=lambda session, delta: "not a patch",
    )

    assert result.patch_status == "parse_failed"
    assert _revision(conn, job_id) == before
    decision = conn.execute("SELECT * FROM kernel_decisions WHERE job_id = ?", (job_id,)).fetchone()
    assert decision["status"] == "parse_failed"
    assert "not valid JSON" in decision["error"]
    events = [row["event_type"] for row in conn.execute("SELECT event_type FROM execution_events WHERE job_id = ?", (job_id,))]
    assert "decision_parse_failed" in events


def test_replay_provider_can_drive_existing_runtime_advance(conn):
    job_id = _job(conn)
    conn.execute("UPDATE execution_nodes SET state = 'failed' WHERE job_id = ? AND node_key = 'understand-scope'", (job_id,))
    patch = {
        "schema": rk.PATCH_SCHEMA,
        "expected_revision": _revision(conn, job_id),
        "rationale_summary": "replay creates implementation",
        "ops": [
            {
                "op": "create_node",
                "node_key": "implement-a-item",
                "node_type": "implementation",
                "title": "Implement A",
                "description": "Produce evidence for A.",
                "goal_item_keys": ["a-item"],
            }
        ],
    }

    provider = rd.ReplayDecisionProvider([json.dumps(patch)])
    result = rk.advance_runtime_job(conn, job_id, create_tasks=False, decision_provider=provider)

    assert result.patch_status == "applied"
    assert conn.execute(
        "SELECT node_type FROM execution_nodes WHERE job_id = ? AND node_key = 'implement-a-item'",
        (job_id,),
    ).fetchone()["node_type"] == "implementation"
    assert len(provider.calls) == 1


def test_provider_patch_rejected_records_decision_without_graph_change(conn):
    job_id = _job(conn)
    conn.execute("UPDATE execution_nodes SET state = 'failed' WHERE job_id = ? AND node_key = 'understand-scope'", (job_id,))
    before = _revision(conn, job_id)
    patch = {
        "schema": rk.PATCH_SCHEMA,
        "expected_revision": before,
        "rationale_summary": "invalid unlinked node",
        "ops": [
            {
                "op": "create_node",
                "node_key": "unlinked-node",
                "node_type": "implementation",
                "title": "Unlinked",
                "description": "No goal or gap linkage.",
            }
        ],
    }

    result = rk.advance_runtime_job(conn, job_id, create_tasks=False, decision_provider=lambda session, delta: patch)

    assert result.patch_status == "rejected"
    assert _revision(conn, job_id) == before
    decision = conn.execute("SELECT * FROM kernel_decisions WHERE job_id = ?", (job_id,)).fetchone()
    validator = json.loads(decision["validator_result_json"])
    assert validator["status"] == "rejected"
    assert "goal_item_keys" in validator["reason"]


def test_runtime_prompt_cli_outputs_provider_request_json(kanban_home):
    created = json.loads(kc.run_slash("runtime create 'phase2b prompt' --json"))
    payload = json.loads(kc.run_slash(f"runtime prompt {created['id']} --json"))

    assert payload["request"]["job_id"] == created["id"]
    assert payload["rendered"]["stable_prefix"]["runtime_contract"]["db_is_authoritative"] is True
    assert "worker_log_tail" not in json.dumps(payload)


def test_runtime_checkpoint_cli_outputs_db_derived_checkpoint(kanban_home):
    created = json.loads(kc.run_slash("runtime create 'phase2b checkpoint' --json"))
    payload = json.loads(kc.run_slash(f"runtime checkpoint {created['id']} --create --json"))

    assert payload["job_id"] == created["id"]
    assert payload["checkpoint"]["job"]["id"] == created["id"]
    assert payload["checkpoint"]["goal_items"][0]["item_key"] == "initial-runtime-result"


def test_runtime_decision_cli_outputs_parse_failure_record(kanban_home):
    created = json.loads(kc.run_slash("runtime create 'phase2b decision list' --json"))
    with kb.connect() as conn:
        conn.execute(
            "UPDATE execution_nodes SET state = 'failed' WHERE job_id = ? AND node_key = 'understand-scope'",
            (created["id"],),
        )
        rk.advance_runtime_job(
            conn,
            created["id"],
            create_tasks=False,
            decision_provider=lambda session, delta: "not a patch",
        )

    rows = json.loads(kc.run_slash(f"runtime decision {created['id']} --json"))
    assert rows[0]["status"] == "parse_failed"
    assert rows[0]["validator_result"]["status"] == "parse_failed"
