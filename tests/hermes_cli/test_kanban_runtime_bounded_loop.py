from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_bounded_loop as bl
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
    root_task_id = kb.create_task(conn, title="bounded loop root", initial_status="running")
    return rk.create_runtime_job(
        conn,
        root_task_id,
        "phase4g2 bounded loop fixture",
        goal_items=[
            {
                "item_key": "runtime-result",
                "description": "runtime result is verified",
                "required": True,
                "verifier_required": True,
            }
        ],
    )


def _source() -> dict:
    return {
        "source": "explicit",
        "provider_name": "fake-real",
        "display_provider": "fake-real",
        "model": "fake-real-model",
        "explicit_base_url": "http://127.0.0.1:1/v1",
        "explicit_api_key": "bounded-loop-secret",
    }


class ScriptedRealProvider:
    calls = 0

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def decide(self, request):
        type(self).calls += 1
        number = type(self).calls
        patch = {
            "schema": rk.PATCH_SCHEMA,
            "expected_revision": request.db_revision,
            "rationale_summary": f"fake real bounded recovery {number}",
            "ops": [
                {
                    "op": "strategy_update",
                    "node_key": f"bounded-recovery-{number}",
                    "title": f"Bounded recovery {number}",
                    "description": "Create one goal-linked recovery node.",
                    "goal_item_keys": ["runtime-result"],
                    "gap_keys": ["runtime-result:failed_required_node"],
                    "strategy_summary": "Use a changed evidence-producing approach.",
                    "changes_from_previous_attempts": ["Use a focused retry node."],
                }
            ],
        }
        return rd.DecisionProviderResult(
            patch=patch,
            raw_output=json.dumps(patch),
            provider_name=self.kwargs["provider_name"],
            model=self.kwargs["model"],
            profile_name=self.kwargs["profile_name"],
            request_ref=f"request-{number}",
            response_ref=f"response-{number}",
            parse_status="parsed",
        )


def test_real_provider_bounded_loop_uses_synthetic_receipts_and_finishes_goal(conn, monkeypatch):
    job_id = _job(conn)
    ScriptedRealProvider.calls = 0
    monkeypatch.setattr(rd, "RuntimeDecisionProvider", ScriptedRealProvider)

    report = bl.run_real_provider_bounded_loop(
        conn,
        job_id,
        provider_source=_source(),
        max_decision_ticks=3,
        max_steps=12,
    )

    assert report["decision_tick_count"] == 3
    assert report["accepted_patch_count"] == 3
    assert report["rejected_patch_count"] == 0
    assert report["synthetic_receipt_count"] >= 4
    assert report["final_state"] == "done"
    assert report["goal_items"] == [{"item_key": "runtime-result", "state": "satisfied", "required": True}]
    assert report["consistency"]["status"] == "passed"
    assert report["secrets_leaked"] is False
    assert "bounded-loop-secret" not in json.dumps(report, ensure_ascii=False)
    event_types = {
        row["event_type"]
        for row in conn.execute("SELECT event_type FROM execution_events WHERE job_id = ?", (job_id,)).fetchall()
    }
    assert "real_provider_bounded_loop_completed" in event_types


def test_real_provider_bounded_loop_requires_explicit_source(conn):
    with pytest.raises(ValueError, match="explicit model source"):
        bl.run_real_provider_bounded_loop(conn, _job(conn), provider_source={})
