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


def test_job_creation_creates_active_decision_segment(conn):
    job_id = _job(conn)
    segment = rk.ensure_decision_segment(conn, job_id)

    assert segment["state"] == "active"
    assert segment["segment_index"] == 0
    session = conn.execute("SELECT * FROM decision_sessions WHERE job_id = ?", (job_id,)).fetchone()
    assert session["active_segment_id"] == segment["id"]


def test_decision_delta_and_patch_entries_preserve_order(conn):
    job_id = _job(conn)
    conn.execute("UPDATE execution_nodes SET state = 'failed' WHERE job_id = ? AND node_key = 'understand-scope'", (job_id,))
    before = _revision(conn, job_id)
    patch = {
        "schema": rk.PATCH_SCHEMA,
        "expected_revision": before,
        "rationale_summary": "create implementation",
        "ops": [
            {
                "op": "create_node",
                "node_key": "implement-a",
                "node_type": "implementation",
                "title": "Implement A",
                "description": "Produce A evidence.",
                "goal_item_keys": ["a-item"],
            }
        ],
    }

    result = rk.advance_runtime_job(conn, job_id, create_tasks=False, decision_provider=lambda session, delta: patch)

    assert result.patch_status == "applied"
    entries = [
        row["entry_type"]
        for row in conn.execute(
            "SELECT entry_type FROM decision_segment_entries WHERE job_id = ? ORDER BY id",
            (job_id,),
        ).fetchall()
    ]
    assert entries == [
        "delta_appended",
        "provider_output",
        "patch_parsed",
        "validator_result",
        "patch_applied",
    ]


def test_manual_compaction_archives_old_segment_and_creates_new_active_segment(conn):
    job_id = _job(conn)
    old_segment = rk.ensure_decision_segment(conn, job_id)
    rk.append_decision_segment_entry(conn, job_id, "delta_appended", {"old": "context"})

    result = rd.compact_decision_session(conn, job_id, profile_name="token_budget_compaction", reason="test")

    assert result["status"] == "compacted"
    old_row = conn.execute("SELECT * FROM decision_session_segments WHERE id = ?", (old_segment["id"],)).fetchone()
    assert old_row["state"] == "compacted"
    assert old_row["compacted_checkpoint_id"] == result["checkpoint_id"]
    new_row = conn.execute("SELECT * FROM decision_session_segments WHERE id = ?", (result["new_segment_id"],)).fetchone()
    assert new_row["state"] == "active"
    session = conn.execute("SELECT * FROM decision_sessions WHERE job_id = ?", (job_id,)).fetchone()
    assert session["active_segment_id"] == result["new_segment_id"]
    assert session["latest_checkpoint_id"] == result["checkpoint_id"]


def test_checkpoint_records_profile_hash_and_revision_binding(conn):
    job_id = _job(conn)
    result = rd.compact_decision_session(conn, job_id, profile_name="token_budget_compaction", reason="test")
    checkpoint = conn.execute("SELECT * FROM decision_checkpoints WHERE id = ?", (result["checkpoint_id"],)).fetchone()

    assert checkpoint["profile_name"] == "token_budget_compaction"
    assert checkpoint["profile_version"] == "1"
    assert checkpoint["profile_hash"]
    assert checkpoint["profile_path"].endswith("token_budget_compaction.md")
    assert checkpoint["graph_revision"] == _revision(conn, job_id)
    assert checkpoint["ledger_revision"] == _revision(conn, job_id)
    assert checkpoint["validator_status"] == "accepted"


def test_compaction_profile_loader_reads_markdown_profile():
    profile = rd.load_compaction_profile("validator_boundary_compaction")

    assert profile["profile_name"] == "validator_boundary_compaction"
    assert profile["profile_version"] == "1"
    assert profile["profile_hash"]
    assert profile["profile_path"].endswith("validator_boundary_compaction.md")
    assert "Validator Boundary Compaction" in profile["content"]


def test_should_compact_uses_token_telemetry(conn):
    job_id = _job(conn)
    rk.append_decision_segment_entry(conn, job_id, "delta_appended", {"large": "x" * 200})

    result = rd.should_compact_decision_session(
        conn,
        job_id,
        {"max_active_segment_tokens": 1},
    )

    assert result["should_compact"] is True
    assert result["reason"] == "token_threshold"
    assert result["profile_name"] == "token_budget_compaction"
    assert result["telemetry"]["active_segment_tokens"] >= 1


def test_should_compact_rejection_threshold_selects_validator_profile(conn):
    job_id = _job(conn)
    rk.append_decision_segment_entry(conn, job_id, "patch_rejected", {"status": "rejected", "reason": "bad"})

    result = rd.should_compact_decision_session(
        conn,
        job_id,
        {"max_active_segment_tokens": 999999, "rejected_patch_threshold": 1},
    )

    assert result["should_compact"] is True
    assert result["reason"] == "rejection_threshold"
    assert result["profile_name"] == "validator_boundary_compaction"


def test_advance_runtime_job_auto_compacts_when_policy_triggers(conn):
    job_id = _job(conn)
    old_segment = rk.ensure_decision_segment(conn, job_id)
    rk.append_decision_segment_entry(conn, job_id, "delta_appended", {"large": "x" * 200})

    rk.advance_runtime_job(
        conn,
        job_id,
        create_tasks=False,
        compaction_policy={"max_active_segment_tokens": 1},
    )

    old_row = conn.execute("SELECT * FROM decision_session_segments WHERE id = ?", (old_segment["id"],)).fetchone()
    assert old_row["state"] == "compacted"
    context = rd.decision_context_status(conn, job_id)
    assert context["latest_checkpoint"]["profile_name"] == "token_budget_compaction"
    assert context["active_segment"]["id"] != old_segment["id"]


def test_new_provider_input_uses_checkpoint_not_old_transcript(conn):
    job_id = _job(conn)
    rk.append_decision_segment_entry(
        conn,
        job_id,
        "provider_output",
        {"raw_output": "OLD_TRANSCRIPT_SHOULD_NOT_RETURN"},
    )
    rd.compact_decision_session(conn, job_id, profile_name="token_budget_compaction", reason="test")
    delta = rk.build_decision_delta(conn, job_id)
    request = rd.build_decision_provider_request(conn, job_id, delta)
    rendered = rd.render_decision_prompt(request)

    assert rendered["checkpoint"]["metadata"]["deterministic"] is True
    assert "OLD_TRANSCRIPT_SHOULD_NOT_RETURN" not in json.dumps(rendered, ensure_ascii=False)
    assert all(entry["entry_type"] != "provider_output" for entry in rendered["short_tail"])


def test_checkpoint_validator_rejects_unknown_node_reference(conn):
    job_id = _job(conn)
    segment = rk.ensure_decision_segment(conn, job_id)
    payload = rd.build_deterministic_checkpoint(conn, job_id, segment["id"])
    payload["graph_frontier"].append(
        {
            "node_key": "missing",
            "state": "ready",
            "source_refs": [{"node_key": "missing"}],
        }
    )

    result = rd.validate_decision_checkpoint(conn, job_id, payload)

    assert result["status"] == "rejected"
    assert "unknown node_key" in result["reason"]


def test_checkpoint_validator_rejects_failed_verifier_as_confirmed(conn):
    job_id = _job(conn)
    segment = rk.ensure_decision_segment(conn, job_id)
    payload = rd.build_deterministic_checkpoint(conn, job_id, segment["id"])
    payload["satisfied_goal_items"].append(
        {
            "goal_item_key": "a-item",
            "state": "satisfied",
            "summary": "failed verifier was misread as satisfied",
            "verification_state": "failed",
            "source_refs": [{"goal_item_key": "a-item"}],
        }
    )

    result = rd.validate_decision_checkpoint(conn, job_id, payload)

    assert result["status"] == "rejected"
    assert "verified or waived" in result["reason"]


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


def test_runtime_compact_cli_outputs_segment_replacement(kanban_home):
    created = json.loads(kc.run_slash("runtime create 'phase2d compact' --json"))
    payload = json.loads(kc.run_slash(f"runtime compact {created['id']} --profile token_budget_compaction --json"))

    assert payload["status"] == "compacted"
    assert payload["checkpoint_id"]
    assert payload["source_segment_id"] != payload["new_segment_id"]


def test_runtime_context_cli_outputs_active_segment_and_checkpoint(kanban_home):
    created = json.loads(kc.run_slash("runtime create 'phase2d context' --json"))
    json.loads(kc.run_slash(f"runtime compact {created['id']} --json"))
    payload = json.loads(kc.run_slash(f"runtime context {created['id']} --json"))

    assert payload["job_id"] == created["id"]
    assert payload["active_segment"]["state"] == "active"
    assert payload["latest_checkpoint"]["validator_status"] == "accepted"
    assert "strict_short_tail" in payload["provider_input_composition"]
    assert "compaction_policy" in payload


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
