from __future__ import annotations

import json
import os
from pathlib import Path
import stat

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_continuity_smoke as cs
from hermes_cli import kanban_runtime_kernel as rk
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


def _fake_resumable_codex(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "codex"
    receipt = (
        "Progress:\n- [x] resumed and completed\n\n"
        "Changed files:\n- result.txt\n\n"
        "Verification:\n- command: test -f result.txt\n  result: passed\n\n"
        "Remaining risks:\n- none\n\n"
        "Recommended reviewer action:\n- inspect\n\n"
        "Verdict: pass\n"
        "```json\n"
        '{"schema":"runtime_worker_receipt_v1","verdict":"pass",'
        '"summary":"resumed worker completed the result",'
        '"claimed_goal_items":["continuity-result"],'
        '"partial_goal_items":[],"unmet_goal_items":[],"changed_files":["result.txt"],'
        '"verification":{"passed":true,"summary":"test -f result.txt passed"},'
        '"artifacts":[]}\n'
        "```\n"
    )
    session_id = "019f0000-0000-7000-8000-000000000010"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys, time\n"
        "_ = sys.stdin.read()\n"
        f"session_id = {session_id!r}\n"
        "print(json.dumps({'type':'thread.started','thread_id':session_id}), flush=True)\n"
        "if 'resume' not in sys.argv:\n"
        "    pathlib.Path('partial.txt').write_text('partial\\n', encoding='utf-8')\n"
        "    time.sleep(5)\n"
        "    raise SystemExit(0)\n"
        "pathlib.Path('result.txt').write_text('resumed\\n', encoding='utf-8')\n"
        f"receipt = {receipt!r}\n"
        "print(json.dumps({'type':'item.completed','item':"
        "{'id':'item-msg','type':'agent_message','text':receipt}}), flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':"
        "{'input_tokens':2,'cached_input_tokens':1,'output_tokens':1}}), flush=True)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def test_continuity_smoke_interrupts_then_resumes_same_session(
    kanban_home, tmp_path, monkeypatch,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.txt").write_text("continuity smoke\n", encoding="utf-8")
    fake_bin = _fake_resumable_codex(tmp_path)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ.get("PATH", ""))
    register_worker_lane(
        make_codex_worker_lane(
            {
                "name": "codex-continuity-smoke",
                "type": "codex_cli",
                "sandbox": "workspace-write",
                "approval": "never",
                "max_concurrency": 1,
                "timeout_seconds": 1,
                "success_policy": "block_for_review",
            },
            source="test",
        )
    )

    with kb.connect() as conn:
        root = kb.create_task(conn, title="continuity root", initial_status="running")
        job_id = rk.create_runtime_job(
            conn,
            root,
            "Complete one worker responsibility across an interrupted backend session.",
            workspace_path=str(workspace),
            initial_assignee="codex-continuity-smoke",
            goal_items=[{
                "item_key": "continuity-result",
                "description": "resumed worker result is verified",
                "required": True,
                "verifier_required": False,
            }],
        )
        assert rk.apply_graph_patch(
            conn,
            job_id,
            {
                "schema": rk.PATCH_SCHEMA,
                "expected_revision": 0,
                "rationale_summary": "create one coherent resumable worker node",
                "ops": [{
                    "op": "create_node",
                    "node_key": "continuity-result",
                    "node_type": "implementation",
                    "title": "Complete resumable result",
                    "description": "Create the result and verify it in one worker responsibility.",
                    "assignee": "codex-continuity-smoke",
                    "goal_item_keys": ["continuity-result"],
                    "gap_keys": ["continuity-result:missing_evidence"],
                    "contract": {
                        "outcome": "A verified continuity result exists.",
                        "acceptance_criteria": ["result.txt exists", "local verification passes"],
                        "success_evidence": ["changed_files", "verification", "worker_summary"],
                        "declared_write_scope": ["**"],
                        "prohibited_actions": ["production_deployment"],
                    },
                }],
            },
        )["status"] == "applied"

        report = cs.run_worker_continuity_smoke(
            conn,
            job_id,
            lane_name="codex-continuity-smoke",
            worker_wait_seconds=5,
            poll_interval_seconds=0.05,
        )

    assert report["reason"] == "completed", json.dumps(report, sort_keys=True)
    assert report["final_state"] == "done"
    assert report["resumed"] is True
    assert report["materialization_modes"] == {"fresh": 1, "resume": 1}
    assert report["context_reacquisition_count"] == 0
    assert [attempt["status"] for attempt in report["attempts"]] == ["timed_out", "succeeded"]
    assert report["backend_session_count"] == 1
    assert report["consistency"]["status"] == "passed"
    assert (workspace / "partial.txt").read_text(encoding="utf-8") == "partial\n"
    assert (workspace / "result.txt").read_text(encoding="utf-8") == "resumed\n"
