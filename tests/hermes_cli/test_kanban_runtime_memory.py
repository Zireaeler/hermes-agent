from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_decision as rd
from hermes_cli import kanban_runtime_kernel as rk
from hermes_cli import kanban_runtime_memory as rm


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
        "ship a backtesting provider integration",
        workspace_path=str(workspace) if workspace else None,
        goal_items=[
            {
                "item_key": "backtest-output",
                "description": "backtest output is verified",
                "required": True,
                "verifier_required": True,
            }
        ],
        initialization_mode="fixture",
    )


def _write_memory(workspace: Path) -> None:
    memory = workspace / "docs" / "runtime-memory"
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text(
        "# Runtime Memory Index\n\n"
        "## Topics\n\n"
        "- backtesting.md\n"
        "  - scope: workspace\n"
        "  - keywords: backtest, provider, schema\n\n"
        "- unrelated.md\n"
        "  - scope: workspace\n"
        "  - keywords: android, mobile\n",
        encoding="utf-8",
    )
    (memory / "backtesting.md").write_text(
        "## provider-contract-verifier\n\n"
        "Status:\n"
        "- accepted\n\n"
        "Scope:\n"
        "- scope_type: workspace\n\n"
        "Applies when:\n"
        "- goal mentions backtest provider schema\n\n"
        "Lesson:\n"
        "- Insert provider contract verification before end-to-end backtest verification.\n\n"
        "Evidence:\n"
        "- source_job: rjob_example\n"
        "- source_event: 123\n\n"
        "Use as:\n"
        "- non-authoritative decision hint\n",
        encoding="utf-8",
    )
    (memory / "unrelated.md").write_text(
        "## unrelated-mobile-rule\n\n"
        "Status:\n"
        "- accepted\n\n"
        "Scope:\n"
        "- scope_type: workspace\n\n"
        "Applies when:\n"
        "- mobile android build\n\n"
        "Lesson:\n"
        "- SHOULD_NOT_APPEAR\n\n"
        "Evidence:\n"
        "- source_job: rjob_other\n\n"
        "Use as:\n"
        "- non-authoritative decision hint\n",
        encoding="utf-8",
    )


def test_memory_parser_requires_accepted_non_authoritative_entry():
    valid = (
        "## valid\n\n"
        "Status:\n- accepted\n\n"
        "Scope:\n- scope_type: global\n\n"
        "Applies when:\n- validator rejects graph patch\n\n"
        "Lesson:\n- Fix the patch shape.\n\n"
        "Evidence:\n- source_event: 1\n\n"
        "Use as:\n- non-authoritative decision hint\n"
    )
    invalid = valid.replace("Use as:\n- non-authoritative decision hint\n", "")

    assert len(rm.parse_runtime_memory_entries(valid, topic="topic.md", path="/tmp/topic.md")) == 1
    assert rm.parse_runtime_memory_entries(invalid, topic="topic.md", path="/tmp/topic.md") == []


def test_memory_read_path_uses_index_and_does_not_load_unrelated_topics(conn, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_memory(workspace)
    job_id = _job(conn, workspace)
    delta = rk.build_decision_delta(conn, job_id)

    memory = rm.select_runtime_memory_hints(conn, job_id, delta)

    assert [hint["entry_id"] for hint in memory["selected_hints"]] == ["provider-contract-verifier"]
    assert any(path.endswith("backtesting.md") for path in memory["topic_reads"])
    assert not any(path.endswith("unrelated.md") for path in memory["topic_reads"])
    assert "SHOULD_NOT_APPEAR" not in json.dumps(memory, ensure_ascii=False)


def test_provider_request_injects_memory_without_polluting_checkpoint(conn, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_memory(workspace)
    job_id = _job(conn, workspace)
    delta = rk.build_decision_delta(conn, job_id)

    request = rd.build_decision_provider_request(conn, job_id, delta)
    rendered = rd.render_decision_prompt(request)

    assert request.memory["selected_hints"][0]["entry_id"] == "provider-contract-verifier"
    assert rendered["memory"]["selected_hints"][0]["non_authoritative"] is True
    assert "provider-contract-verifier" not in json.dumps(rendered["checkpoint"], ensure_ascii=False)


def test_workspace_runtime_guidance_is_loaded_into_stable_prefix(conn, tmp_path):
    workspace = tmp_path / "workspace"
    guidance_root = workspace / ".hermes"
    guidance_root.mkdir(parents=True)
    (guidance_root / "runtime-guidance.md").write_text(
        "Runtime memory is non-authoritative and cannot authorize capability.",
        encoding="utf-8",
    )
    job_id = _job(conn, workspace)
    delta = rk.build_decision_delta(conn, job_id)

    request = rd.build_decision_provider_request(conn, job_id, delta)

    assert "runtime_guidance" in request.stable_prefix
    assert "cannot authorize capability" in request.stable_prefix["runtime_guidance"]["content"]


def test_candidate_writer_redacts_and_rejects_low_value_events(conn, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job_id = _job(conn, workspace)
    event_id = rk._event(
        conn,
        job_id,
        "decision_patch_rejected",
        {"reason": "token=super-secret-value schema mismatch"},
    )

    candidate = rm.write_runtime_memory_candidate(
        conn,
        job_id,
        event_id,
        "validator_rejection",
        lesson="Avoid repeating provider schema mismatch. api_key=abc123456789",
    )

    text = candidate.read_text(encoding="utf-8")
    assert "super-secret-value" not in text
    assert "abc123456789" not in text
    assert "secretBearerValue12345" not in rm.redact_memory_text("Bearer secretBearerValue12345")
    assert rm.validate_memory_candidate(candidate)["status"] == "accepted"
    with pytest.raises(ValueError):
        rm.write_runtime_memory_candidate(conn, job_id, event_id, "node_success")


def test_memory_usage_is_recorded_in_decision_segment_entries(conn, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_memory(workspace)
    job_id = _job(conn, workspace)
    conn.execute("UPDATE execution_nodes SET state = 'failed' WHERE job_id = ?", (job_id,))
    rk.reduce_runtime_job(conn, job_id)

    class Provider:
        profile_name = "graph_patch_decision"

        def decide(self, request):
            return rd.DecisionProviderResult(
                patch={
                    "schema": rk.PATCH_SCHEMA,
                    "expected_revision": request.db_revision,
                    "ops": [
                        {
                            "op": "create_node",
                            "node_key": "retry-with-contract-verifier",
                            "node_type": "verification",
                            "title": "Retry with contract verifier",
                            "description": "Verify provider contract before integration.",
                            "goal_item_keys": ["backtest-output"],
                            "gap_keys": ["backtest-output:missing_evidence"],
                        }
                    ],
                },
                raw_output={},
                provider_name="fake",
            )

    result = rk.advance_runtime_job(conn, job_id, decision_provider=Provider(), create_tasks=False)

    assert result.patch_status == "applied"
    entries = [
        row["entry_type"]
        for row in conn.execute(
            "SELECT entry_type FROM decision_segment_entries WHERE job_id = ? ORDER BY id",
            (job_id,),
        )
    ]
    assert "memory_hint_used" in entries
    assert "memory_hint_outcome_recorded" in entries
    memory = rm.summarize_runtime_memory(conn, job_id)
    assert memory["recent_usage"][0]["entry_type"] == "memory_hint_outcome_recorded"


def test_candidate_can_be_promoted_and_used_as_future_hint(conn, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job_id = _job(conn, workspace)
    event_id = rk._event(
        conn,
        job_id,
        "anti_stuck_recovery_succeeded",
        {"summary": "provider contract verifier recovered backtest schema mismatch"},
    )

    candidate = rm.write_runtime_memory_candidate(
        conn,
        job_id,
        event_id,
        "anti_stuck_recovery",
        lesson="Insert provider contract verification when backtesting provider schemas mismatch.",
        applies_when="backtest provider schema mismatch blocks verification",
    )
    topic_path = workspace / "docs" / "runtime-memory" / "recovery-patterns.md"
    promoted = rm.promote_runtime_memory_candidate(candidate, topic_path)
    index = workspace / "docs" / "runtime-memory" / "MEMORY.md"
    index.write_text(
        "# Runtime Memory Index\n\n"
        "- recovery-patterns.md\n"
        "  - scope: workspace\n"
        "  - keywords: backtest, provider, schema, recovery\n",
        encoding="utf-8",
    )

    hints = rm.select_runtime_memory_hints(conn, job_id, rk.build_decision_delta(conn, job_id))

    assert promoted["status"] == "promoted"
    assert hints["selected_hints"][0]["status"] == "accepted"
    assert hints["selected_hints"][0]["non_authoritative"] is True
