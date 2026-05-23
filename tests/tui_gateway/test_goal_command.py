"""Tests for /goal handling in tui_gateway.

The TUI routes ``/goal`` through ``command.dispatch`` (not ``slash.exec``)
because the CLI's ``_handle_goal_command`` queues the kickoff message onto
``_pending_input``, which the slash-worker subprocess has no reader for.
Instead we handle ``/goal`` directly in the server and return a
``{"type": "send", "notice": ..., "message": ...}`` payload the TUI client
uses to render a system line and fire the kickoff prompt.
"""

from __future__ import annotations

import importlib
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    # Bust the goal-module DB cache so it re-resolves HERMES_HOME.
    from hermes_cli import goals

    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


@pytest.fixture()
def server(hermes_home):
    with patch.dict(
        "sys.modules",
        {
            "hermes_cli.env_loader": MagicMock(),
            "hermes_cli.banner": MagicMock(),
        },
    ):
        mod = importlib.import_module("tui_gateway.server")
        yield mod
        mod._sessions.clear()
        mod._pending.clear()
        mod._answers.clear()
        mod._methods.clear()
        importlib.reload(mod)


@pytest.fixture()
def session(server):
    sid = "sid-test"
    session_key = "tui-goal-session-1"
    s = {
        "session_key": session_key,
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "cols": 120,
    }
    server._sessions[sid] = s
    return sid, session_key, s


def _call(server, method, **params):
    handler = server._methods[method]
    return handler(1, params)


# ── command.dispatch /goal ────────────────────────────────────────────


def test_goal_bare_shows_status_when_none_set(server, session):
    sid, _, _ = session
    r = _call(server, "command.dispatch", name="goal", arg="", session_id=sid)
    assert r["result"]["type"] == "exec"
    assert "No active goal" in r["result"]["output"]


def test_goal_whitespace_only_shows_status(server, session):
    sid, _, _ = session
    r = _call(server, "command.dispatch", name="goal", arg="   ", session_id=sid)
    assert r["result"]["type"] == "exec"
    assert "No active goal" in r["result"]["output"]


def test_goal_status_alias_shows_status(server, session):
    sid, _, _ = session
    r = _call(server, "command.dispatch", name="goal", arg="status", session_id=sid)
    assert r["result"]["type"] == "exec"
    assert "No active goal" in r["result"]["output"]


def test_goal_set_returns_send_with_notice(server, session):
    sid, session_key, _ = session
    r = _call(server, "command.dispatch", name="goal", arg="build a rocket", session_id=sid)
    result = r["result"]
    assert result["type"] == "send"
    assert result["message"] == "build a rocket"
    assert "notice" in result
    assert "Goal set" in result["notice"]
    assert "20-turn budget" in result["notice"]

    # Persisted in SessionDB
    from hermes_cli.goals import GoalManager

    mgr = GoalManager(session_key)
    assert mgr.state is not None
    assert mgr.state.goal == "build a rocket"
    assert mgr.state.status == "active"


def test_goal_create_routes_to_kanban_without_setting_standing_goal(server, session):
    sid = "sid-goal-create"
    session_key = "tui-goal-create-session"
    server._sessions[sid] = {
        "session_key": session_key,
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "cols": 120,
    }
    r = _call(
        server,
        "command.dispatch",
        name="goal",
        arg="create 'build a kanban bridge' --assignee orchestrator",
        session_id=sid,
    )
    result = r["result"]
    assert result["type"] == "exec"
    assert "Goal task:" in result["output"]

    from hermes_cli import kanban_db as kb
    from hermes_cli.goals import GoalManager

    with kb.connect() as conn:
        tasks = kb.list_tasks(conn, status="triage")
    assert len(tasks) == 1
    assert tasks[0].assignee == "orchestrator"
    assert tasks[0].session_id == session_key
    assert GoalManager(session_key).state is None


def test_goal_advance_routes_current_kanban_goal(server, session, tmp_path, monkeypatch):
    import json
    from hermes_cli import kanban_db as kb
    from hermes_cli import profiles
    from hermes_cli.goals import create_kanban_task_from_goal

    sid, session_key, _ = session
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    root = create_kanban_task_from_goal(
        "tui advance current root",
        session_id=session_key,
        assignee="orchestrator",
        workspace_kind="dir",
        workspace_path=str(tmp_path),
    )
    with kb.connect() as conn:
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[{"title": "implement from tui advance", "assignee": "codex-deep"}],
            author="planner",
        )
        assert child_ids is not None

    r = _call(
        server,
        "command.dispatch",
        name="goal",
        arg="advance --dry-run --json",
        session_id=sid,
    )
    payload = json.loads(r["result"]["output"])
    spawned_ids = {
        item["task_id"]
        for step in payload["steps"]
        if step["kind"] == "dispatch_goal_children"
        for item in step["dispatch"]["spawned"]
    }

    assert payload["task_id"] == root
    assert spawned_ids == {child_ids[0]}


def test_goal_status_after_create_reads_kanban_goal_without_interrupting_worker(
    server,
):
    sid = "sid-goal-status"
    session_key = "tui-goal-status-session"
    server._sessions[sid] = {
        "session_key": session_key,
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "cols": 120,
    }
    r = _call(
        server,
        "command.dispatch",
        name="goal",
        arg="create 'build status bridge' --assignee orchestrator",
        session_id=sid,
    )
    assert "Goal task:" in r["result"]["output"]

    from hermes_cli import kanban_db as kb
    from hermes_cli.goals import GoalManager

    with kb.connect() as conn:
        roots = kb.list_tasks(conn, session_id=session_key)
        root = next(
            task
            for task in roots
            if (task.idempotency_key or "").startswith(f"goal:{session_key}:")
        )
        child_ids = kb.decompose_triage_task(
            conn,
            root.id,
            root_assignee="orchestrator",
            children=[{"title": "implement tui status", "assignee": "codex-deep"}],
            author="planner",
        )
        assert child_ids is not None
        running_id = child_ids[0]
        running = kb.claim_task(conn, running_id, claimer="worker:codex-deep")
        assert running is not None
        kb._set_worker_pid(conn, running_id, 54321)
        kb.record_task_event(
            conn,
            running_id,
            "worker_progress",
            {
                "lane": "codex-deep",
                "items": [
                    {"index": 1, "status": "running", "text": "render tui status"},
                ],
            },
            run_id=running.current_run_id,
        )
        before = kb.get_task(conn, running_id)

    status = _call(
        server,
        "command.dispatch",
        name="goal",
        arg="status",
        session_id=sid,
    )

    with kb.connect() as conn:
        after = kb.get_task(conn, running_id)

    output = status["result"]["output"]
    assert status["result"]["type"] == "exec"
    assert "Kanban goal" in output
    assert root.id in output
    assert "root-next: wait_for_workers" in output
    assert "children=0/1 done running=1 review-required=0" in output
    assert "progress=running: render tui status" in output
    assert "No active goal" not in output
    assert GoalManager(session_key).state is None
    assert after.status == before.status == "running"
    assert after.claim_lock == before.claim_lock
    assert after.current_run_id == before.current_run_id
    assert after.worker_pid == before.worker_pid == 54321


def test_goal_pause_after_set(server, session):
    sid, session_key, _ = session
    _call(server, "command.dispatch", name="goal", arg="write a story", session_id=sid)
    r = _call(server, "command.dispatch", name="goal", arg="pause", session_id=sid)
    assert r["result"]["type"] == "exec"
    assert "paused" in r["result"]["output"].lower()

    from hermes_cli.goals import GoalManager

    assert GoalManager(session_key).state.status == "paused"


def test_goal_resume_reactivates(server, session):
    sid, session_key, _ = session
    _call(server, "command.dispatch", name="goal", arg="write a story", session_id=sid)
    _call(server, "command.dispatch", name="goal", arg="pause", session_id=sid)
    r = _call(server, "command.dispatch", name="goal", arg="resume", session_id=sid)
    assert r["result"]["type"] == "exec"
    assert "resumed" in r["result"]["output"].lower()

    from hermes_cli.goals import GoalManager

    assert GoalManager(session_key).state.status == "active"


def test_goal_clear_removes_active_goal(server, session):
    sid, session_key, _ = session
    _call(server, "command.dispatch", name="goal", arg="write a story", session_id=sid)
    r = _call(server, "command.dispatch", name="goal", arg="clear", session_id=sid)
    assert r["result"]["type"] == "exec"
    assert "cleared" in r["result"]["output"].lower()

    from hermes_cli.goals import GoalManager

    # After clear the row is marked status=cleared (kept for audit);
    # ``has_goal()`` / ``is_active()`` return False so the goal loop
    # stays off and ``status`` reports "No active goal".
    mgr = GoalManager(session_key)
    assert not mgr.has_goal()
    assert not mgr.is_active()
    assert "No active goal" in mgr.status_line()


def test_goal_stop_and_done_are_clear_aliases(server, session):
    sid, _, _ = session
    _call(server, "command.dispatch", name="goal", arg="first goal", session_id=sid)
    r = _call(server, "command.dispatch", name="goal", arg="stop", session_id=sid)
    assert "cleared" in r["result"]["output"].lower()

    _call(server, "command.dispatch", name="goal", arg="second goal", session_id=sid)
    r = _call(server, "command.dispatch", name="goal", arg="done", session_id=sid)
    assert "cleared" in r["result"]["output"].lower()


def test_goal_requires_session(server):
    r = _call(server, "command.dispatch", name="goal", arg="nope", session_id="unknown")
    assert "error" in r
    assert r["error"]["code"] == 4001


# ── slash.exec /goal routing ──────────────────────────────────────────


def test_slash_exec_rejects_goal_routes_to_command_dispatch(server, session):
    """slash.exec must reject /goal with 4018 so the TUI client falls through
    to command.dispatch. Without this, the HermesCLI slash-worker subprocess
    would set the goal but silently drop the kickoff — the queue is in-proc."""
    sid, _, _ = session
    r = _call(server, "slash.exec", command="goal status", session_id=sid)
    assert "error" in r
    assert r["error"]["code"] == 4018
    assert "command.dispatch" in r["error"]["message"]


def test_pending_input_commands_includes_goal(server):
    """Guard: _PENDING_INPUT_COMMANDS must list 'goal' — removing it would
    silently re-break the TUI."""
    assert "goal" in server._PENDING_INPUT_COMMANDS
