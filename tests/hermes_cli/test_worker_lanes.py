from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import codex_worker as cw
from hermes_cli.codex_worker import (
    CodexLaneConfig,
    build_codex_argv,
    parse_progress_items,
    run_codex_worker,
    _safe_env_for_codex,
    _safe_env_for_worker,
    _extract_runtime_receipt,
    _extract_worker_receipt,
    _metadata,
)
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from hermes_cli.worker_lanes import (
    WorkerLane,
    clear_worker_lanes,
    enable_worker_lane_request,
    get_worker_lane,
    list_worker_lanes,
    register_configured_worker_lanes,
    register_worker_lane,
    resolve_worker_assignee,
    validate_worker_lane_request,
)


@pytest.fixture(autouse=True)
def clean_lanes():
    clear_worker_lanes()
    yield
    clear_worker_lanes()


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _lane(name="codex-deep", *, pid=1234, max_concurrency=None):
    def spawn(task, workspace, *, board=None):
        return pid

    return WorkerLane(
        name=name,
        kind="codex_cli",
        description="fake lane",
        spawn_fn=spawn,
        success_policy="block_for_review",
        max_concurrency=max_concurrency,
        source="test",
    )


def test_worker_lane_registry_register_query_and_conflict():
    lane = register_worker_lane(_lane("codex-deep"))
    assert get_worker_lane("CODEX-DEEP") is lane
    assert [x.name for x in list_worker_lanes()] == ["codex-deep"]
    with pytest.raises(ValueError, match="already registered"):
        register_worker_lane(_lane("codex-deep"))


def test_resolve_worker_assignee_prefers_lane_over_profile(monkeypatch):
    from hermes_cli import profiles

    register_worker_lane(_lane("daily"))
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    res = resolve_worker_assignee("daily")
    assert res.kind == "worker_lane"
    assert res.lane is not None


def test_plugin_context_register_worker_lane():
    mgr = PluginManager()
    ctx = PluginContext(PluginManifest(name="worker-plugin"), mgr)

    def spawn(task, workspace, *, board=None):
        return 9

    ctx.register_worker_lane(
        name="plugin-codex",
        kind="plugin",
        description="plugin lane",
        spawn_fn=spawn,
    )
    lane = get_worker_lane("plugin-codex")
    assert lane is not None
    assert lane.source == "plugin:worker-plugin"
    assert "plugin-codex" in mgr._plugin_worker_lane_names


def test_plugin_context_worker_lane_failure_is_logged(caplog):
    mgr = PluginManager()
    ctx = PluginContext(PluginManifest(name="broken-plugin"), mgr)
    ctx.register_worker_lane(name="bad lane", spawn_fn=None)
    assert get_worker_lane("bad lane") is None
    assert any("failed to register worker lane" in r.message for r in caplog.records)


def test_config_registers_multiple_codex_lanes():
    register_configured_worker_lanes({
        "kanban": {
            "worker_lanes": {
                "codex-fast": {
                    "type": "codex_cli",
                    "model": "gpt-5.4-mini",
                    "sandbox": "workspace-write",
                    "approval": "never",
                    "max_concurrency": 2,
                },
                "codex-deep": {
                    "type": "codex_cli",
                    "model": "gpt-5.5",
                    "sandbox": "workspace-write",
                    "approval": "never",
                    "max_concurrency": 1,
                    "json_events": True,
                },
            }
        }
    })
    lanes = {lane.name: lane for lane in list_worker_lanes()}
    assert set(lanes) == {"codex-deep", "codex-fast"}
    assert lanes["codex-fast"].max_concurrency == 2
    assert lanes["codex-deep"].config["model"] == "gpt-5.5"
    assert lanes["codex-deep"].config["json_events"] is True


def test_lane_request_validator_rejects_shell_command():
    with pytest.raises(ValueError, match="command"):
        validate_worker_lane_request({
            "name": "codex-unsafe",
            "type": "codex_cli",
            "command": "rm -rf /",
        })


def test_enable_worker_lane_request_registers_sanitized_lane():
    lane = enable_worker_lane_request({
        "name": "codex-long-context",
        "type": "codex_cli",
        "model": "gpt-5.5",
        "sandbox": "workspace-write",
        "approval": "never",
        "max_concurrency": 1,
        "success_policy": "block_for_review",
        "json_events": "true",
        "reason": "large refactor",
    })

    assert lane.name == "codex-long-context"
    assert lane.kind == "codex_cli"
    assert lane.source == "lane_request"
    assert get_worker_lane("codex-long-context") is lane
    assert lane.config["json_events"] is True
    assert resolve_worker_assignee("codex-long-context", refresh_config=False).kind == "worker_lane"


def test_enable_worker_lane_request_can_persist_sanitized_config(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    lane = enable_worker_lane_request(
        {
            "name": "codex-approved",
            "type": "codex_cli",
            "model": "gpt-5.4-mini",
            "sandbox": "workspace-write",
            "approval": "never",
            "max_concurrency": 2,
            "success_policy": "block_for_review",
            "json_events": True,
            "reason": "operator approved",
        },
        persist=True,
    )

    assert lane.source == "config"
    from hermes_cli.config import read_raw_config

    raw = read_raw_config()
    stored = raw["kanban"]["worker_lanes"]["codex-approved"]
    assert stored["type"] == "codex_cli"
    assert stored["model"] == "gpt-5.4-mini"
    assert stored["max_concurrency"] == 2
    assert stored["json_events"] is True
    assert "reason" not in stored
    assert "command" not in stored


def test_lane_request_validator_rejects_invalid_json_events():
    with pytest.raises(ValueError, match="json_events"):
        validate_worker_lane_request({
            "name": "codex-events",
            "type": "codex_cli",
            "json_events": "maybe",
        })


def test_dispatcher_uses_external_lane_assignee(kanban_home, monkeypatch):
    from hermes_cli import profiles

    calls = []

    def spawn(task, workspace, *, board=None):
        calls.append((task.id, task.assignee, workspace, board))
        return 4321

    register_worker_lane(WorkerLane(
        name="codex-deep",
        kind="codex_cli",
        description="fake",
        spawn_fn=spawn,
        max_concurrency=2,
    ))
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="external", assignee="codex-deep")
        res = kb.dispatch_once(conn)
        task = kb.get_task(conn, tid)
    assert res.spawned == [(tid, "codex-deep", calls[0][2])]
    assert task.status == "running"
    assert task.worker_pid == 4321
    assert calls[0][1] == "codex-deep"


def test_unregistered_assignee_still_skipped_nonspawnable(kanban_home, monkeypatch):
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="terminal", assignee="orion-cc")
        res = kb.dispatch_once(conn, dry_run=True)
    assert tid in res.skipped_nonspawnable
    assert not res.spawned


def test_scheduled_tasks_are_not_dispatchable_for_external_lane(kanban_home, monkeypatch):
    from hermes_cli import profiles

    register_worker_lane(_lane("codex-deep"))
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="later", assignee="codex-deep")
        assert kb.schedule_task(conn, tid, reason="wait for clock")
        res = kb.dispatch_once(conn, dry_run=True)
        task = kb.get_task(conn, tid)
    assert task.status == "scheduled"
    assert not res.spawned


def test_hermes_profile_lane_behavior_unchanged(kanban_home, monkeypatch):
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "worker")
    calls = []

    def spawn(task, workspace):
        calls.append((task.id, task.assignee, workspace))
        return 77

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="profile", assignee="worker")
        res = kb.dispatch_once(conn, spawn_fn=spawn)
        task = kb.get_task(conn, tid)
    assert res.spawned[0][0] == tid
    assert task.worker_pid == 77
    assert calls[0][1] == "worker"


def test_lane_max_concurrency_and_instances_are_distinct(kanban_home):
    calls = []

    def spawn(task, workspace, *, board=None):
        calls.append(task.id)
        return 9000 + len(calls)

    register_worker_lane(WorkerLane(
        name="codex-fast",
        kind="codex_cli",
        description="fake",
        spawn_fn=spawn,
        max_concurrency=2,
    ))
    with kb.connect() as conn:
        t1 = kb.create_task(conn, title="a", assignee="codex-fast")
        t2 = kb.create_task(conn, title="b", assignee="codex-fast")
        t3 = kb.create_task(conn, title="c", assignee="codex-fast")
        res = kb.dispatch_once(conn, max_spawn=10)
        task1 = kb.get_task(conn, t1)
        task2 = kb.get_task(conn, t2)
        task3 = kb.get_task(conn, t3)
    assert calls == [t1, t2]
    assert task1.worker_pid != task2.worker_pid
    assert task3.status == "ready"
    assert t3 in res.skipped_concurrency


def test_worker_lane_statuses_report_capacity_and_active_instances(kanban_home):
    calls = []

    def spawn(task, workspace, *, board=None):
        calls.append(task.id)
        return 8100 + len(calls)

    register_worker_lane(WorkerLane(
        name="codex-deep",
        kind="codex_cli",
        description="deep lane",
        spawn_fn=spawn,
        max_concurrency=2,
        source="test",
        config={
            "type": "codex_cli",
            "model": "gpt-5.5",
            "sandbox": "workspace-write",
            "approval": "never",
            "secret": "do-not-return",
        },
    ))
    with kb.connect() as conn:
        t1 = kb.create_task(conn, title="active", assignee="codex-deep")
        t2 = kb.create_task(conn, title="queued", assignee="codex-deep")
        res = kb.dispatch_once(conn, max_spawn=1)
        assert res.spawned[0][0] == t1
        kb.heartbeat_worker(
            conn,
            t1,
            note="alive",
            expected_run_id=kb.get_task(conn, t1).current_run_id,
        )
        lane = kb.worker_lane_statuses(conn)[0]

    data = lane.to_dict()
    assert data["name"] == "codex-deep"
    assert data["max_concurrency"] == 2
    assert data["active_count"] == 1
    assert data["available_capacity"] == 1
    assert data["counts"]["running"] == 1
    assert data["counts"]["ready"] == 1
    assert data["active"][0]["task_id"] == t1
    assert data["active"][0]["run_id"] is not None
    assert data["active"][0]["worker_pid"] == 8101
    assert data["active"][0]["last_heartbeat_at"] is not None
    assert data["config"]["model"] == "gpt-5.5"
    assert "secret" not in data["config"]
    assert t2 not in [item["task_id"] for item in data["active"]]


def test_review_profile_lane_behavior_unchanged(kanban_home, monkeypatch):
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "reviewer")
    spawned = []

    def spawn(task, workspace):
        spawned.append(task)
        return 88

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="review", assignee="reviewer")
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
        res = kb.dispatch_once(conn, spawn_fn=spawn)
        task = kb.get_task(conn, tid)
    assert res.spawned[0][0] == tid
    assert task.status == "running"
    assert task.worker_pid == 88
    assert spawned[0].skills == ["sdlc-review"]


def test_review_external_lane_dispatches_without_profile_review_skill(kanban_home, monkeypatch):
    from hermes_cli import profiles

    calls = []

    def spawn(task, workspace, *, board=None):
        calls.append((task.id, task.assignee, task.skills))
        return 501

    register_worker_lane(WorkerLane(
        name="codex-review",
        kind="codex_cli",
        description="fake review external lane",
        spawn_fn=spawn,
        max_concurrency=1,
    ))
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="review externally", assignee="codex-review")
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
        res = kb.dispatch_once(conn)
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    assert res.spawned[0][0] == tid
    assert calls == [(tid, "codex-review", None)]
    assert task.status == "running"
    assert task.worker_pid == 501
    spawned_events = [e for e in events if e.kind == "spawned"]
    assert spawned_events[-1].payload["worker_lane"] == "codex-review"


def _claim_for_codex(conn, title="codex task"):
    tid = kb.create_task(
        conn,
        title=title,
        body="Edit the repository and report progress.",
        assignee="codex-deep",
        workspace_kind="dir",
        workspace_path=os.getcwd(),
    )
    task = kb.claim_task(conn, tid, claimer="host:test")
    assert task is not None
    return tid, kb.get_task(conn, tid)


def _make_fake_codex(tmp_path: Path, body: str, *, exit_code: int = 0) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "codex"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "_ = sys.stdin.read()\n"
        f"sys.stdout.write({body!r})\n"
        "sys.stdout.flush()\n"
        f"sys.exit({int(exit_code)})\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def test_codex_argv_model_parameter():
    argv = build_codex_argv(
        binary="/usr/bin/codex",
        workspace="/tmp/ws",
        sandbox="workspace-write",
        approval="never",
        model="gpt-5.5",
    )
    assert argv == [
        "/usr/bin/codex",
        "--cd",
        "/tmp/ws",
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "never",
        "--model",
        "gpt-5.5",
        "exec",
        "-",
    ]


def test_codex_argv_json_events():
    argv = build_codex_argv(
        binary="/usr/bin/codex",
        workspace="/tmp/ws",
        sandbox="read-only",
        approval="never",
        model="gpt-5.4-mini",
        json_events=True,
    )
    assert argv == [
        "/usr/bin/codex",
        "--cd",
        "/tmp/ws",
        "--sandbox",
        "read-only",
        "--ask-for-approval",
        "never",
        "--model",
        "gpt-5.4-mini",
        "exec",
        "--json",
        "-",
    ]


def test_codex_argv_resume_preserves_execution_envelope():
    argv = build_codex_argv(
        binary="/usr/bin/codex",
        workspace="/tmp/ws",
        sandbox="workspace-write",
        approval="never",
        model="gpt-5.5",
        json_events=True,
        resume_session_id="019f-session",
    )
    assert argv == [
        "/usr/bin/codex",
        "--cd",
        "/tmp/ws",
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "never",
        "--model",
        "gpt-5.5",
        "exec",
        "resume",
        "--json",
        "019f-session",
        "-",
    ]


def test_codex_receipt_ignores_verdict_instruction_template():
    receipt = _extract_worker_receipt(
        "## Required review output\n"
        "End with exactly one structured verdict line:\n"
        "Verdict: approve | request_changes | blocked\n"
        "Use approve only when the implementation should pass this review gate.\n"
    )

    assert "verdict" not in receipt


def test_codex_receipt_extracts_final_allowed_verdict():
    receipt = _extract_worker_receipt(
        "Verdict: approve | request_changes | blocked\n"
        "Progress:\n"
        "- [x] inspected evidence\n\n"
        "Verdict: request_changes\n"
    )

    assert receipt["verdict"] == "request_changes"


def test_codex_runtime_receipt_extracts_only_explicit_json_envelope():
    receipt = _extract_runtime_receipt(
        "Progress:\n- [x] bounded task complete\n\n"
        "```json\n"
        '{"schema":"runtime_worker_receipt_v1","verdict":"pass","summary":"verified",'
        '"claimed_goal_items":["runtime-result"],"verification":{"passed":true}}\n'
        "```\n"
    )

    assert receipt is not None
    assert receipt["claimed_goal_items"] == ["runtime-result"]
    assert _extract_runtime_receipt("runtime_worker_receipt_v1 in prose") is None


def test_codex_runtime_receipt_ignores_truncated_prior_closing_fence():
    receipt = _extract_runtime_receipt(
        "truncated output from an earlier code block\n"
        "```\n"
        "Progress:\n- [x] complete\n\n"
        "```json\n"
        '{"schema":"runtime_worker_receipt_v1","verdict":"pass","summary":"verified",'
        '"claimed_goal_items":["runtime-result"],"verification":{"passed":true}}\n'
        "```\n"
    )

    assert receipt is not None
    assert receipt["verdict"] == "pass"


def test_codex_metadata_output_tail_excludes_echoed_prompt_prefix():
    meta = _metadata(
        lane="codex-deep",
        task_id="t1",
        run_id=7,
        worker_pid=123,
        claim_lock="host:123",
        workspace="/tmp/no-such-workspace",
        model="gpt-5.5",
        exit_code=0,
        timed_out=False,
        output_tail=(
            "OpenAI Codex v0.132.0\n"
            "--------\n"
            "user\n"
            "# Kanban task t1: implementation\n\n"
            "## External worker instructions\n"
            "When finished, print a concise structured receipt:\n\n"
            "Progress:\n"
            "- [x] ...\n"
            "- [ ] ...\n\n"
            "Changed files:\n"
            "- ...\n\n"
            "Verification:\n"
            "- command: ...\n"
            "  result: ...\n\n"
            "codex\n"
            "I am working now.\n"
            "exec\n"
            "/bin/bash -lc 'pytest smoke'\n"
            "Progress:\n"
            "- [x] Implemented worker lane evidence trimming.\n\n"
            "Changed files:\n"
            "- hermes_cli/codex_worker.py\n\n"
            "Verification:\n"
            "- command: pytest smoke\n"
            "  result: passed\n\n"
            "Remaining risks:\n"
            "- none\n\n"
            "Recommended reviewer action:\n"
            "- approve\n"
        ),
    )

    tail = meta["worker_lane"]["output_tail"]
    assert tail.startswith("Progress:\n- [x] Implemented worker lane evidence trimming.")
    assert "hermes_cli/codex_worker.py" in tail
    assert "pytest smoke" in tail
    assert "External worker instructions" not in tail
    assert "# Kanban task t1" not in tail


def test_codex_metadata_output_tail_keeps_followup_verdict_without_prompt_prefix():
    meta = _metadata(
        lane="codex-review",
        task_id="t2",
        run_id=8,
        worker_pid=124,
        claim_lock="host:124",
        workspace="/tmp/no-such-workspace",
        model="gpt-5.5",
        exit_code=0,
        timed_out=False,
        output_tail=(
            "# Kanban task t2: Independent review task\n\n"
            "## Required review output\n"
            "End with exactly one structured verdict line:\n"
            "Verdict: approve | request_changes | blocked\n\n"
            "codex\n"
            "I inspected the bounded evidence and workspace.\n"
            "Findings:\n"
            "- No blocking issues found.\n\n"
            "Verification:\n"
            "- command: pytest smoke\n"
            "  result: passed\n\n"
            "Verdict: approve\n"
        ),
    )

    tail = meta["worker_lane"]["output_tail"]
    assert tail.startswith("Verification:\n- command: pytest smoke")
    assert "Result" not in tail
    assert tail.count("Verdict: approve") == 1
    assert "Verdict: approve | request_changes | blocked" not in tail
    assert "Required review output" not in tail


def test_codex_prompt_marks_requested_changes_as_mandatory():
    from hermes_cli.codex_worker import build_codex_prompt

    prompt = build_codex_prompt(
        "# Kanban task t_retry: retry\n\n"
        "## Requested changes to address before finishing\n"
        "Fix the failed exact-file acceptance check.\n",
        lane="codex-deep",
        model="gpt-5.5",
    )

    assert "Requested changes to address before finishing" in prompt
    assert "mandatory retry feedback" in prompt
    assert "ordinary task instructions or examples" in prompt
    assert "Fix the failed exact-file acceptance check." in prompt


def test_codex_prompt_uses_implementation_role_for_normal_tasks():
    from hermes_cli.codex_worker import build_codex_prompt

    prompt = build_codex_prompt(
        "# Kanban task t_impl: Implement the feature\n\nWrite the file.\n",
        lane="codex-deep",
        model="gpt-5.5",
    )

    assert "worker lane `codex-deep`" in prompt
    assert "Implement the assigned task in the workspace" in prompt
    assert "review lane `codex-deep`" not in prompt
    assert "test lane `codex-deep`" not in prompt


def test_codex_prompt_requires_runtime_receipt_for_runtime_node():
    from hermes_cli.codex_worker import build_codex_prompt

    prompt = build_codex_prompt(
        "# Runtime node\n\nGoal items: runtime-result\n\nRuntime footer: {}\n",
        lane="codex-deep",
        model="gpt-5.5",
    )

    assert "runtime_worker_receipt_v1" in prompt
    assert "Do not claim success merely" in prompt


def test_codex_prompt_uses_review_followup_role():
    from hermes_cli.codex_worker import build_codex_prompt

    prompt = build_codex_prompt(
        "# Kanban task t_review: Review implementation evidence\n\n"
        "## Required review output\n"
        "End with exactly one structured verdict line:\n"
        "Verdict: approve | request_changes | blocked\n",
        lane="codex-review",
        model="gpt-5.5",
    )

    assert "review lane `codex-review`" in prompt
    assert "Review the bounded implementation evidence" in prompt
    assert "Do not implement feature work" in prompt
    assert "minimal inspection commands" in prompt
    assert "final `Verdict: ...` line" in prompt
    assert "Implement the assigned task in the workspace" not in prompt


def test_codex_prompt_uses_test_followup_role():
    from hermes_cli.codex_worker import build_codex_prompt

    prompt = build_codex_prompt(
        "# Kanban task t_test: Verify implementation evidence\n\n"
        "## Required test output\n"
        "End with exactly one structured verdict line:\n"
        "Verdict: pass | fail | blocked\n",
        lane="codex-test",
        model="gpt-5.5",
    )

    assert "test lane `codex-test`" in prompt
    assert "Verify the bounded implementation evidence" in prompt
    assert "Do not implement feature work" in prompt
    assert "smallest sufficient verification" in prompt
    assert "final `Verdict: ...` line" in prompt
    assert "Implement the assigned task in the workspace" not in prompt


def test_codex_env_preserves_existing_writable_codex_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    codex_home = home / ".codex"
    codex_home.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)

    env = _safe_env_for_codex(str(workspace))

    assert env["HOME"] == str(home)
    assert env.get("CODEX_HOME") is None


def test_codex_env_does_not_forward_proxy_variables(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:7891")
    monkeypatch.setenv("NO_PROXY", "localhost")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:7890")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7890")
    monkeypatch.setenv("all_proxy", "socks5://127.0.0.1:7891")
    monkeypatch.setenv("no_proxy", "localhost")

    env = _safe_env_for_codex(str(workspace))

    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ):
        assert name not in env


def test_codex_wrapper_env_does_not_forward_proxy_variables(
    kanban_home, tmp_path, monkeypatch,
):
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ):
        monkeypatch.setenv(name, "http://127.0.0.1:7890")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="wrapper env",
            assignee="codex-deep",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        task = kb.get_task(conn, tid)

    env = _safe_env_for_worker(
        task,
        str(workspace),
        CodexLaneConfig(name="codex-deep"),
        board=None,
    )

    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ):
        assert name not in env


def test_collect_git_evidence_preserves_short_status_paths(tmp_path):
    from hermes_cli.codex_worker import collect_git_evidence

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Hermes Test"], cwd=tmp_path, check=True)
    tracked = tmp_path / "codex_followup_dispatch_smoke.txt"
    tracked.write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "codex_followup_dispatch_smoke.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, stdout=subprocess.PIPE)
    tracked.write_text("updated\n", encoding="utf-8")

    evidence = collect_git_evidence(str(tmp_path))

    assert evidence["status"] == " M codex_followup_dispatch_smoke.txt"
    assert evidence["changed_files"] == ["codex_followup_dispatch_smoke.txt"]


def test_codex_env_uses_workspace_home_when_inherited_home_unwritable(
    tmp_path, monkeypatch,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "blocked-home"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(
        "hermes_cli.codex_worker._path_is_writable_dir",
        lambda path: False,
    )

    env = _safe_env_for_codex(str(workspace))

    assert env["HOME"] == str(workspace / ".hermes-codex-home")
    assert env["CODEX_HOME"] == str(workspace / ".hermes-codex")


def test_progress_parser_supports_ordinals_and_checkboxes():
    items = parse_progress_items(
        "o (1) 分析入口\nx (2) 修改 dispatcher\n- [ ] 补测试\n- [x] 完成文档\n"
    )
    assert items[:2] == [
        {"index": 1, "status": "done", "text": "分析入口"},
        {"index": 2, "status": "running", "text": "修改 dispatcher"},
    ]
    assert {"index": 1, "status": "pending", "text": "补测试"} in items
    assert {"index": 2, "status": "done", "text": "完成文档"} in items


def test_progress_parser_ignores_template_placeholders():
    assert parse_progress_items(
        "Progress:\n- [x] ...\n- [ ] ...\no (1) ...\nx (2) ...\n"
    ) == []


def test_progress_parser_deduplicates_repeated_items():
    items = parse_progress_items(
        "Progress:\n- [ ] Create smoke_result.txt\n"
        "Progress:\n- [x] Create smoke_result.txt\n"
    )
    assert items == [
        {"index": 1, "status": "done", "text": "Create smoke_result.txt"}
    ]


def test_codex_binary_missing_blocks_with_metadata(kanban_home, monkeypatch):
    monkeypatch.setenv("PATH", "/tmp/definitely-no-codex")
    with kb.connect() as conn:
        tid, task = _claim_for_codex(conn)
        run_id = task.current_run_id
        assert run_id is not None
    rc = run_codex_worker(
        task_id=tid,
        lane="codex-deep",
        workspace=os.getcwd(),
        sandbox="workspace-write",
        approval="never",
        run_id=run_id,
        claim_lock=task.claim_lock,
        heartbeat_interval=0.01,
    )
    assert rc == 0
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        run = kb.latest_run(conn, tid)
        events = kb.list_events(conn, tid)
    assert task.status == "blocked"
    assert run.metadata["worker_lane"]["binary_missing"] is True
    assert run.metadata["review"]["required"] is False
    assert "codex binary not found" in run.summary
    assert any(e.kind == "worker_failed" for e in events)


def test_codex_exit_zero_blocks_for_review_and_records_progress_metadata(
    kanban_home, tmp_path, monkeypatch,
):
    old_path = os.environ.get("PATH", "")
    fake_bin = _make_fake_codex(
        tmp_path,
        "o (1) 分析入口\nx (2) 修改 dispatcher\n"
        "Progress:\n- [x] 分析入口\n- [ ] 补测试\n"
        "Changed files:\n- hermes_cli/kanban_db.py\n"
        "Verification:\n- command: pytest fake\n  result: passed\n",
    )
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + old_path)
    with kb.connect() as conn:
        tid, task = _claim_for_codex(conn)
        run_id = task.current_run_id
    rc = run_codex_worker(
        task_id=tid,
        lane="codex-deep",
        workspace=os.getcwd(),
        sandbox="workspace-write",
        approval="never",
        model="gpt-5.5",
        run_id=run_id,
        claim_lock=task.claim_lock,
        heartbeat_interval=0.01,
    )
    assert rc == 0
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        run = kb.latest_run(conn, tid)
        events = kb.list_events(conn, tid)
        snapshot = kb.task_progress_snapshot(conn, tid)
        log = kb.read_worker_log(tid)
    assert task.status == "blocked"
    assert run.outcome == "blocked"
    assert run.summary.startswith("review-required:")
    assert run.metadata["worker_instance"]["worker_lane"] == "codex-deep"
    assert run.metadata["worker_instance"]["run_id"] == run_id
    assert run.metadata["worker_lane"]["exit_code"] == 0
    assert run.metadata["review"]["required"] is True
    assert "hermes_cli/kanban_db.py" in run.metadata["worker_lane"]["output_tail"]
    assert run.metadata["verification"]["commands"] == ["pytest fake"]
    assert any(e.kind == "heartbeat" for e in events)
    worker_heartbeats = [e for e in events if e.kind == "worker_heartbeat"]
    assert worker_heartbeats
    assert worker_heartbeats[-1].payload["worker_lane"] == "codex-deep"
    assert worker_heartbeats[-1].payload["worker_kind"] == "codex_cli"
    assert worker_heartbeats[-1].payload["run_id"] == run_id
    assert snapshot.heartbeat_event is not None
    assert snapshot.heartbeat_event.kind == "worker_heartbeat"
    assert snapshot.to_dict()["last_heartbeat_event"]["payload"]["worker_lane"] == "codex-deep"
    progress = [e for e in events if e.kind == "worker_progress"]
    assert progress
    assert progress[-1].payload["worker_lane"] == "codex-deep"
    assert progress[-1].payload["lane"] == "codex-deep"
    assert progress[-1].payload["run_id"] == run_id
    assert any(item["text"] == "修改 dispatcher" for item in progress[-1].payload["items"])
    assert "[codex-worker]" in log


def test_codex_exit_zero_records_structured_receipt_verdict(
    kanban_home, tmp_path, monkeypatch,
):
    old_path = os.environ.get("PATH", "")
    fake_bin = _make_fake_codex(
        tmp_path,
        "Progress:\n- [x] inspected evidence\n\n"
        "Verification:\n- command: pytest fake\n  result: passed\n\n"
        "Remaining risks:\n- none\n\n"
        "Recommended reviewer action:\n- approve\n\n"
        "Verdict: pass\n",
    )
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + old_path)
    with kb.connect() as conn:
        tid, task = _claim_for_codex(conn)
        run_id = task.current_run_id
    run_codex_worker(
        task_id=tid,
        lane="codex-test",
        workspace=os.getcwd(),
        sandbox="workspace-write",
        approval="never",
        run_id=run_id,
        claim_lock=task.claim_lock,
        heartbeat_interval=0.01,
    )

    with kb.connect() as conn:
        run = kb.latest_run(conn, tid)

    assert run.metadata["worker_receipt"]["schema"] == "codex_cli_receipt_v1"
    assert run.metadata["worker_receipt"]["verdict"] == "pass"
    assert run.metadata["worker_receipt"]["remaining_risks"] == "- none"
    assert run.metadata["verification"]["verdict"] == "pass"
    assert run.metadata["worker_lane"]["verdict"] == "pass"
    assert run.metadata["worker_lane"]["receipt"]["sections"]["verification"]


def test_codex_json_events_write_task_events_and_feed_progress_metadata(
    kanban_home, tmp_path, monkeypatch,
):
    old_path = os.environ.get("PATH", "")
    agent_message = (
        "Progress:\n"
        "- [x] analyzed task\n"
        "- [ ] update docs\n\n"
        "Changed files:\n"
        "- hermes_cli/codex_worker.py\n\n"
        "Verification:\n"
        "- command: pytest json-events\n"
        "  result: passed\n\n"
        "Remaining risks:\n"
        "- event stream shape may evolve\n\n"
        "Recommended reviewer action:\n"
        "- approve\n"
    )
    body = "\n".join(
        json.dumps(event)
        for event in [
            {"type": "thread.started", "thread_id": "thread-json-test"},
            {
                "type": "item.completed",
                "item": {
                    "id": "item-msg",
                    "type": "agent_message",
                    "text": agent_message,
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "item-file",
                    "type": "file_change",
                    "changes": [
                        {
                            "path": str(tmp_path / "workspace" / "smoke.txt"),
                            "kind": "add",
                        }
                    ],
                    "status": "completed",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "item-cmd",
                    "type": "command_execution",
                    "command": "pytest json-events",
                    "aggregated_output": "passed\n" + ("A" * 5000),
                    "exit_code": 0,
                    "status": "completed",
                },
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 12,
                    "cached_input_tokens": 3,
                    "output_tokens": 4,
                    "reasoning_output_tokens": 5,
                    "ignored_future_field": 999,
                },
            },
        ]
    ) + "\n"
    fake_bin = _make_fake_codex(tmp_path, body)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + old_path)
    with kb.connect() as conn:
        tid, task = _claim_for_codex(conn)
        run_id = task.current_run_id

    rc = run_codex_worker(
        task_id=tid,
        lane="codex-deep",
        workspace=os.getcwd(),
        sandbox="workspace-write",
        approval="never",
        model="gpt-5.5",
        run_id=run_id,
        claim_lock=task.claim_lock,
        heartbeat_interval=0.01,
        json_events=True,
    )

    assert rc == 0
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        run = kb.latest_run(conn, tid)
        events = kb.list_events(conn, tid)
        snapshot = kb.task_progress_snapshot(conn, tid)
        log = kb.read_worker_log(tid)

    assert task.status == "blocked"
    assert run.outcome == "blocked"
    assert run.metadata["worker_instance"]["json_events"] is True
    assert run.metadata["worker_lane"]["json_events"] is True
    assert run.metadata["worker_receipt"]["changed_files_text"] == "- hermes_cli/codex_worker.py"
    assert run.metadata["worker_receipt"]["remaining_risks"] == "- event stream shape may evolve"
    assert run.metadata["verification"]["commands"] == ["pytest json-events"]
    assert "hermes_cli/codex_worker.py" in run.metadata["worker_lane"]["output_tail"]
    assert '"type": "thread.started"' in log
    session_events = [event for event in events if event.kind == "worker_backend_session_started"]
    assert len(session_events) == 1
    assert session_events[0].payload["backend_session_id"] == "thread-json-test"

    progress = [event for event in events if event.kind == "worker_progress"]
    assert progress
    assert progress[-1].payload["worker_lane"] == "codex-deep"
    assert progress[-1].payload["run_id"] == run_id
    progress_items = progress[-1].payload["items"]
    assert progress_items[:2] == [
        {"index": 1, "status": "done", "text": "analyzed task"},
        {"index": 2, "status": "pending", "text": "update docs"},
    ]
    assert any(
        item["status"] == "done" and item["text"] == "apply file changes: smoke.txt"
        for item in progress_items
    )
    assert any(
        item["status"] == "done" and item["text"] == "run command: pytest json-events"
        for item in progress_items
    )

    codex_events = [event for event in events if event.kind == "worker_codex_event"]
    assert len(codex_events) == 5
    first_payload = codex_events[0].payload
    assert first_payload["worker_lane"] == "codex-deep"
    assert first_payload["worker_kind"] == "codex_cli"
    assert first_payload["run_id"] == run_id
    assert first_payload["event_type"] == "thread.started"
    assert first_payload["thread_id"] == "thread-json-test"
    file_payload = codex_events[2].payload["item"]
    assert file_payload["type"] == "file_change"
    assert file_payload["changes"][0]["path"].endswith("smoke.txt")
    assert file_payload["changes"][0]["kind"] == "add"
    command_payload = codex_events[3].payload["item"]
    assert command_payload["type"] == "command_execution"
    assert command_payload["command"] == "pytest json-events"
    assert command_payload["status"] == "completed"
    assert command_payload["exit_code"] == 0
    assert len(command_payload["output_tail"]) < 2300
    assert "truncated" in command_payload["output_tail"]
    usage_payload = codex_events[4].payload["usage"]
    assert usage_payload == {
        "input_tokens": 12,
        "cached_input_tokens": 3,
        "output_tokens": 4,
        "reasoning_output_tokens": 5,
    }
    assert snapshot is not None
    snapshot_payload = snapshot.to_dict()
    snapshot_events = snapshot_payload["worker_codex_events"]
    assert len(snapshot_events) == 5
    assert snapshot_events[0]["payload"]["event_type"] == "thread.started"
    assert snapshot_events[2]["payload"]["item"]["changes"][0]["path"].endswith("smoke.txt")
    assert snapshot_events[3]["payload"]["item"]["command"] == "pytest json-events"
    assert "ignored_future_field" not in snapshot_events[4]["payload"]["usage"]


def test_codex_resume_records_resumed_session_and_receipt(
    kanban_home, tmp_path, monkeypatch,
):
    old_path = os.environ.get("PATH", "")
    argv_path = tmp_path / "resume-argv.json"
    session_id = "019f0000-0000-7000-8000-000000000001"
    receipt = (
        "Progress:\n- [x] resumed original work\n\n"
        "Changed files:\n- none\n\n"
        "Verification:\n- command: python3 -c pass\n  result: passed\n\n"
        "Remaining risks:\n- none\n\n"
        "Recommended reviewer action:\n- inspect\n\n"
        "Verdict: pass\n"
        "```json\n"
        '{"schema":"runtime_worker_receipt_v1","verdict":"pass","summary":"resumed",'
        '"claimed_goal_items":[],"verification":{"passed":true},"artifacts":[]}\n'
        "```\n"
    )
    body = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": session_id}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "item-msg", "type": "agent_message", "text": receipt},
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}),
        ]
    ) + "\n"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "codex"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(argv_path)!r}).write_text(json.dumps(sys.argv), encoding='utf-8')\n"
        "_ = sys.stdin.read()\n"
        f"sys.stdout.write({body!r})\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + old_path)
    monkeypatch.setattr(
        cw,
        "_runtime_execution_continuity",
        lambda task_id: {
            "mode": "resume",
            "eligibility": "accepted",
            "resume_session_id": session_id,
            "resume_from_materialization_id": "mat-prior",
            "workspace_revision": "git:test",
        },
    )
    with kb.connect() as conn:
        tid, task = _claim_for_codex(conn)
        run_id = task.current_run_id

    assert run_codex_worker(
        task_id=tid,
        lane="codex-deep",
        workspace=os.getcwd(),
        sandbox="workspace-write",
        approval="never",
        run_id=run_id,
        claim_lock=task.claim_lock,
        heartbeat_interval=0.01,
    ) == 0

    argv = json.loads(argv_path.read_text(encoding="utf-8"))
    assert argv[-5:] == ["exec", "resume", "--json", session_id, "-"]
    with kb.connect() as conn:
        run = kb.latest_run(conn, tid)
        events = kb.list_events(conn, tid)
    assert run.metadata["worker_instance"]["execution_mode"] == "resume"
    assert run.metadata["worker_instance"]["backend_session_id"] == session_id
    assert run.metadata["worker_instance"]["resume_status"] == "resumed"
    assert run.metadata["runtime_receipt"]["summary"] == "resumed"
    assert any(event.kind == "worker_backend_session_resumed" for event in events)


def test_codex_terminal_event_gets_process_exit_grace(
    kanban_home, tmp_path, monkeypatch,
):
    old_path = os.environ.get("PATH", "")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "codex"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        "_ = sys.stdin.read()\n"
        "print(json.dumps({'type':'thread.started','thread_id':'thread-grace'}), flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1}}), flush=True)\n"
        "time.sleep(0.35)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + old_path)
    with kb.connect() as conn:
        tid, task = _claim_for_codex(conn)
        run_id = task.current_run_id

    assert run_codex_worker(
        task_id=tid,
        lane="codex-deep",
        workspace=os.getcwd(),
        sandbox="workspace-write",
        approval="never",
        run_id=run_id,
        claim_lock=task.claim_lock,
        timeout_seconds=0.1,
        heartbeat_interval=0.01,
        json_events=True,
    ) == 0
    with kb.connect() as conn:
        run = kb.latest_run(conn, tid)
        events = kb.list_events(conn, tid)
    assert run.metadata["worker_lane"]["timed_out"] is False
    assert not any(event.kind == "worker_timed_out" for event in events)


def test_codex_metadata_ignores_prompt_template_verification(
    kanban_home, tmp_path, monkeypatch,
):
    old_path = os.environ.get("PATH", "")
    fake_bin = _make_fake_codex(
        tmp_path,
        "Progress:\n- [x] ...\n- [ ] ...\n"
        "Verification:\n- command: ...\n  result: ...\n\n"
        "Progress:\n- [x] Create smoke_result.txt\n\n"
        "Verification:\n"
        "- command: `cmp -s smoke_result.txt <(printf 'codex worker lane smoke ok\\n') && echo exact`\n"
        "  result: `exact`\n",
    )
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + old_path)
    with kb.connect() as conn:
        tid, task = _claim_for_codex(conn)
        run_id = task.current_run_id
    run_codex_worker(
        task_id=tid,
        lane="codex-smoke",
        workspace=os.getcwd(),
        sandbox="workspace-write",
        approval="never",
        run_id=run_id,
        claim_lock=task.claim_lock,
        heartbeat_interval=0.01,
    )
    with kb.connect() as conn:
        run = kb.latest_run(conn, tid)
        events = kb.list_events(conn, tid)
    assert run.metadata["verification"]["commands"] == [
        "cmp -s smoke_result.txt <(printf 'codex worker lane smoke ok\\n') && echo exact"
    ]
    assert "result: `exact`" in run.metadata["verification"]["summary"]
    progress = [e for e in events if e.kind == "worker_progress"]
    assert progress[-1].payload["items"] == [
        {"index": 1, "status": "done", "text": "Create smoke_result.txt"}
    ]


def test_codex_receipt_parser_uses_last_real_receipt_block(
    kanban_home, tmp_path, monkeypatch,
):
    old_path = os.environ.get("PATH", "")
    fake_bin = _make_fake_codex(
        tmp_path,
        "## External worker instructions\n"
        "When finished, print a concise structured receipt:\n\n"
        "Progress:\n- [x] ...\n- [ ] ...\n\n"
        "Changed files:\n- ...\n\n"
        "Verification:\n- command: ...\n  result: ...\n\n"
        "Remaining risks:\n- ...\n\n"
        "Recommended reviewer action:\n- ...\n\n"
        "If this is an independent review or test follow-up task, include a Verdict line.\n"
        "codex\nI am doing the actual work now.\n"
        "exec\n/bin/bash -lc 'git status --short'\n"
        "Progress:\n- [x] Create final file\n\n"
        "Changed files:\n- smoke_result.txt\n\n"
        "Verification:\n- command: pytest smoke\n  result: passed\n\n"
        "Remaining risks:\n- none\n\n"
        "Recommended reviewer action:\n- approve\n",
    )
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + old_path)
    with kb.connect() as conn:
        tid, task = _claim_for_codex(conn)
        run_id = task.current_run_id
    run_codex_worker(
        task_id=tid,
        lane="codex-smoke",
        workspace=os.getcwd(),
        sandbox="workspace-write",
        approval="never",
        run_id=run_id,
        claim_lock=task.claim_lock,
        heartbeat_interval=0.01,
    )

    with kb.connect() as conn:
        run = kb.latest_run(conn, tid)

    receipt = run.metadata["worker_receipt"]
    assert receipt["sections"]["progress"] == "- [x] Create final file"
    assert receipt["changed_files_text"] == "- smoke_result.txt"
    assert receipt["remaining_risks"] == "- none"
    assert receipt["recommended_reviewer_action"] == "- approve"
    assert "External worker instructions" not in receipt["recommended_reviewer_action"]


def test_codex_receipt_parser_accepts_markdown_bold_headers():
    receipt = _extract_worker_receipt(
        "**Progress:**\n"
        "- [x] Create final file\n\n"
        "**Changed files:**\n"
        "- smoke_result.txt\n\n"
        "**Verification:**\n"
        "- command: pytest smoke\n"
        "  result: passed\n\n"
        "**Remaining risks:**\n"
        "- none\n\n"
        "**Recommended reviewer action:**\n"
        "- approve\n"
    )

    assert receipt["sections"]["progress"] == "- [x] Create final file"
    assert receipt["changed_files_text"] == "- smoke_result.txt"
    assert receipt["remaining_risks"] == "- none"
    assert receipt["recommended_reviewer_action"] == "- approve"


def test_codex_metadata_output_tail_normalizes_markdown_bold_receipt_headers():
    meta = _metadata(
        lane="codex-deep",
        task_id="t2-md",
        run_id=9,
        worker_pid=125,
        claim_lock="host:125",
        workspace="/tmp/no-such-workspace",
        model="gpt-5.5",
        exit_code=0,
        timed_out=False,
        output_tail=(
            "# Kanban task t2-md: Independent review task\n\n"
            "## Required review output\n"
            "End with exactly one structured verdict line:\n"
            "Verdict: approve | request_changes | blocked\n\n"
            "codex\n"
            "I inspected the bounded evidence and workspace.\n"
            "**Progress:**\n"
            "- [x] checked evidence\n\n"
            "**Changed files:**\n"
            "- smoke_result.txt\n\n"
            "**Verification:**\n"
            "- command: pytest smoke\n"
            "  result: passed\n\n"
            "**Remaining risks:**\n"
            "- none\n\n"
            "**Recommended reviewer action:**\n"
            "- approve\n\n"
            "Verdict: approve\n"
        ),
    )

    tail = meta["worker_lane"]["output_tail"]
    assert tail.startswith("Progress:\n- [x] checked evidence")
    assert "**Progress:**" not in tail
    assert "Verdict: approve" in tail
    assert meta["worker_receipt"]["sections"]["progress"] == "- [x] checked evidence"


def test_codex_worker_heartbeat_ignores_superseded_run(
    kanban_home,
):
    from hermes_cli.codex_worker import _heartbeat

    with kb.connect() as conn:
        tid, task = _claim_for_codex(conn)
        old_run_id = task.current_run_id
        assert old_run_id is not None
        assert kb.block_task(
            conn,
            tid,
            reason="review-required: first run",
            expected_run_id=old_run_id,
            metadata={
                "worker_lane": {"name": "codex-deep", "kind": "codex_cli", "exit_code": 0},
                "review": {"required": True, "reason": "first run"},
            },
        )
        assert kb.unblock_task(conn, tid)
        second = kb.claim_task(conn, tid, claimer="worker:codex-deep")
        assert second is not None
        new_run_id = second.current_run_id
        assert new_run_id is not None and new_run_id != old_run_id

    _heartbeat(
        tid,
        run_id=old_run_id,
        claim_lock=task.claim_lock,
        lane="codex-deep",
    )

    with kb.connect() as conn:
        events = kb.list_events(conn, tid)
        current = kb.get_task(conn, tid)

    stale_worker_heartbeats = [
        event
        for event in events
        if event.kind == "worker_heartbeat" and event.run_id == old_run_id
    ]
    assert stale_worker_heartbeats == []
    assert current.current_run_id == new_run_id
    assert current.status == "running"


def test_codex_exit_nonzero_blocks_failed(kanban_home, tmp_path, monkeypatch):
    old_path = os.environ.get("PATH", "")
    fake_bin = _make_fake_codex(tmp_path, "boom\n", exit_code=7)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + old_path)
    with kb.connect() as conn:
        tid, task = _claim_for_codex(conn)
        run_id = task.current_run_id
    run_codex_worker(
        task_id=tid,
        lane="codex-fast",
        workspace=os.getcwd(),
        sandbox="workspace-write",
        approval="never",
        run_id=run_id,
        claim_lock=task.claim_lock,
        heartbeat_interval=0.01,
    )
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        run = kb.latest_run(conn, tid)
    assert task.status == "blocked"
    assert run.summary == "codex-failed: exit code 7"
    assert run.metadata["worker_lane"]["exit_code"] == 7
    assert run.metadata["review"]["required"] is False


def test_codex_timeout_blocks_and_records_metadata(kanban_home, tmp_path, monkeypatch):
    old_path = os.environ.get("PATH", "")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "codex"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "sys.stdin.read()\n"
        "print('started', flush=True)\n"
        "time.sleep(5)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + old_path)
    with kb.connect() as conn:
        tid, task = _claim_for_codex(conn)
        run_id = task.current_run_id
    run_codex_worker(
        task_id=tid,
        lane="codex-deep",
        workspace=os.getcwd(),
        sandbox="workspace-write",
        approval="never",
        run_id=run_id,
        claim_lock=task.claim_lock,
        timeout_seconds=0.2,
        heartbeat_interval=0.01,
    )
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        run = kb.latest_run(conn, tid)
        events = kb.list_events(conn, tid)
    assert task.status == "blocked"
    assert run.summary.startswith("codex-timeout:")
    assert run.metadata["worker_lane"]["timed_out"] is True
    assert any(e.kind == "worker_timed_out" for e in events)


def test_codex_output_tail_is_truncated(kanban_home, tmp_path, monkeypatch):
    old_path = os.environ.get("PATH", "")
    fake_bin = _make_fake_codex(tmp_path, "A" * 20000)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + old_path)
    with kb.connect() as conn:
        tid, task = _claim_for_codex(conn)
        run_id = task.current_run_id
    run_codex_worker(
        task_id=tid,
        lane="codex-deep",
        workspace=os.getcwd(),
        sandbox="workspace-write",
        approval="never",
        run_id=run_id,
        claim_lock=task.claim_lock,
        heartbeat_interval=0.01,
    )
    with kb.connect() as conn:
        run = kb.latest_run(conn, tid)
    assert len(run.metadata["worker_lane"]["output_tail"].encode("utf-8")) <= 8192
