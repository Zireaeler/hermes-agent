from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_decision as rd
from hermes_cli import kanban_runtime_kernel as rk
from hermes_cli import kanban_runtime_worker_smoke as ws
from hermes_cli.codex_worker import make_codex_worker_lane
from hermes_cli.worker_lanes import clear_worker_lanes, register_worker_lane


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture(autouse=True)
def lanes():
    clear_worker_lanes()
    yield
    clear_worker_lanes()


def _source():
    return {
        "source": "explicit",
        "provider_name": "fake-real",
        "display_provider": "fake-real",
        "model": "fake-model",
        "explicit_base_url": "http://127.0.0.1:1/v1",
        "explicit_api_key": "worker-smoke-secret",
    }


class Provider:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def decide(self, request):
        patch = {
            "schema": rk.PATCH_SCHEMA,
            "expected_revision": request.db_revision,
            "rationale_summary": "create the lane-assigned smoke node",
            "ops": [{
                "op": "create_node",
                "node_key": "worker-smoke-result",
                "node_type": "implementation",
                "title": "Write bounded worker smoke evidence",
                "description": "Use the assigned lane and return a verified runtime receipt.",
                "assignee": "codex-runtime-smoke",
                "goal_item_keys": ["worker-smoke-result"],
                "gap_keys": ["worker-smoke-result:missing_evidence"],
                "contract": {
                    "outcome": "Implement and verify the complete bounded worker smoke result.",
                    "acceptance_criteria": ["The result exists", "Local verification passes"],
                    "success_evidence": ["changed_files", "verification", "worker_summary"],
                    "declared_write_scope": [],
                    "prohibited_actions": ["production_deployment"],
                },
            }],
        }
        return rd.DecisionProviderResult(
            patch=patch,
            raw_output=json.dumps(patch),
            provider_name=self.kwargs["provider_name"],
            model=self.kwargs["model"],
            profile_name=self.kwargs["profile_name"],
            request_ref="fake-request",
            response_ref="fake-response",
            parse_status="parsed",
        )


def _fake_codex(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "codex"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "prompt = sys.stdin.read()\n"
        "goal = 'worker-smoke-result'\n"
        "print('Progress:')\n"
        "print('- [x] completed bounded worker smoke')\n"
        "print('\\nChanged files:')\nprint('- none')\n"
        "print('\\nVerification:')\nprint('- command: python3 -c pass')\nprint('  result: passed')\n"
        "print('\\nRemaining risks:')\nprint('- none')\n"
        "print('\\nRecommended reviewer action:')\nprint('- inspect runtime receipt')\n"
        "print('\\nVerdict: pass')\n"
        "print('```json')\n"
        "print(json.dumps({'schema':'runtime_worker_receipt_v1','verdict':'pass','summary':'fake external Codex worker verified '+goal,'claimed_goal_items':[goal],'verification':{'passed':True,'summary':'fake local verification passed'},'artifacts':[]}))\n"
        "print('```')\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def test_worker_smoke_uses_dispatcher_wrapper_and_runtime_receipt(kanban_home, tmp_path, monkeypatch):
    bin_dir = _fake_codex(tmp_path)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    register_worker_lane(make_codex_worker_lane({
        "name": "codex-runtime-smoke",
        "type": "codex_cli",
        "sandbox": "workspace-write",
        "approval": "never",
        "max_concurrency": 1,
        "timeout_seconds": 30,
        "success_policy": "block_for_review",
    }, source="test"))
    monkeypatch.setattr(rd, "RuntimeDecisionProvider", Provider)

    with kb.connect() as conn:
        root = kb.create_task(conn, title="worker smoke root", initial_status="running")
        job_id = rk.create_runtime_job(
            conn,
            root,
            "Use one coherent worker to implement and verify the bounded worker smoke result.",
            workspace_path=str(tmp_path),
            initial_assignee="codex-runtime-smoke",
            goal_items=[
                {"item_key": "worker-smoke-result", "description": "worker evidence", "required": True, "verifier_required": False},
            ],
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM execution_nodes WHERE job_id = ?",
            (job_id,),
        ).fetchone()[0] == 0
        report = ws.run_real_worker_lane_smoke(
            conn,
            job_id,
            provider_source=_source(),
            lane_name="codex-runtime-smoke",
            max_decision_ticks=1,
            max_steps=8,
            worker_wait_seconds=15,
            poll_interval_seconds=0.05,
        )

        events = {
            row["event_type"]
            for row in conn.execute("SELECT event_type FROM execution_events WHERE job_id = ?", (job_id,)).fetchall()
        }

    assert report["accepted_patch_count"] == 1
    assert report["final_state"] == "done"
    assert report["decision_tick_count"] == 1
    assert len(report["dispatches"]) == 1
    assert report["materialization_attempt_count"] == 1
    assert report["materialized_node_keys"] == ["worker-smoke-result"]
    assert report["single_primary_node"] is True
    assert report["single_worker_attempt"] is True
    assert report["materialization_attempts"][0]["status"] == "succeeded"
    assert {item["node_key"] for item in report["terminal_receipts"]} == {"worker-smoke-result"}
    assert report["consistency"]["status"] == "passed"
    assert report["secrets_leaked"] is False
    assert "node_completed" in events
    assert "real_worker_lane_smoke_completed" in events


def test_terminal_poll_projects_status_without_decoding_task_body(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="poll projection",
            body="initial body",
            initial_status="running",
        )
        conn.execute(
            "UPDATE tasks SET status = 'done', body = CAST(X'80' AS TEXT) WHERE id = ?",
            (task_id,),
        )

        ws._wait_for_terminal_tasks(
            conn,
            [task_id],
            timeout=0.1,
            interval=0.01,
        )

        assert conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()["status"] == "done"


def test_terminal_poll_observes_external_writer_through_fresh_read_connection(
    kanban_home,
):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="external terminal writer",
            initial_status="running",
        )
        db_path = Path(
            conn.execute("PRAGMA database_list").fetchone()["file"]
        )
        with kb.connect(db_path=db_path) as writer:
            writer.execute(
                "UPDATE tasks SET status = 'blocked' WHERE id = ?",
                (task_id,),
            )

        ws._wait_for_terminal_tasks(
            conn,
            [task_id],
            timeout=0.2,
            interval=0.01,
        )

        with sqlite3.connect(db_path) as observer:
            status = observer.execute(
                "SELECT status FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()[0]
        assert status == "blocked"
