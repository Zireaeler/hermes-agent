"""Tests for gateway Kanban controller wiring."""

from __future__ import annotations

import asyncio
import inspect
import os
from types import SimpleNamespace


def _review_required_metadata() -> dict:
    return {
        "worker_lane": {
            "name": "codex-deep",
            "kind": "codex_cli",
            "exit_code": 0,
            "timed_out": False,
            "binary_missing": False,
        },
        "verification": {
            "commands": ["pytest -q"],
            "summary": "passed",
        },
        "review": {
            "required": True,
            "reason": "Codex completed; Hermes review required",
        },
    }


def test_gateway_dispatcher_watcher_invokes_advance_controller():
    from gateway.run import GatewayRunner

    source = inspect.getsource(GatewayRunner._kanban_dispatcher_watcher)

    assert "advance_controller_in_gateway" in source
    assert "HERMES_KANBAN_ADVANCE_CONTROLLER_IN_GATEWAY" in source
    assert "advance_controller_once" in source
    assert "advance_controller_dispatch_max" in source


def test_default_config_enables_gateway_controller_tick():
    from hermes_cli.config import DEFAULT_CONFIG

    kanban = DEFAULT_CONFIG["kanban"]

    assert kanban["advance_controller_in_gateway"] is True
    assert kanban["advance_controller_max_items"] == 8
    assert kanban["advance_controller_max_iterations"] == 8
    assert kanban["advance_controller_dispatch_max"] == 8


def test_gateway_dispatcher_watcher_runs_controller_tick(monkeypatch, tmp_path):
    from gateway import run as gateway_run
    from gateway.run import GatewayRunner
    from hermes_cli import kanban_db as kb

    runner = object.__new__(GatewayRunner)
    runner._running = True

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": True,
                "dispatch_interval_seconds": 1,
                "auto_decompose": False,
                "advance_controller_in_gateway": True,
                "advance_controller_dispatch_max": 3,
                "advance_controller_max_items": 2,
                "advance_controller_max_iterations": 4,
                "advance_controller_review_assignee": "review-lane",
                "advance_controller_test_assignee": "test-lane",
            }
        },
    )

    class FakeConn:
        def close(self):
            pass

    dispatch_calls = []
    controller_calls = []

    def fake_dispatch_once(conn, **kwargs):
        dispatch_calls.append(kwargs)
        return SimpleNamespace(
            spawned=[],
            reclaimed=0,
            crashed=[],
            timed_out=[],
            stale=[],
            promoted=0,
            auto_blocked=[],
        )

    def fake_advance_controller_once(conn, **kwargs):
        controller_calls.append(kwargs)
        return {
            "advanced": True,
            "item_count": 1,
            "dispatch_used": 0,
            "stop_reason": "idle",
        }

    monkeypatch.setattr(kb, "list_boards", lambda include_archived=False: [{"slug": "default"}])
    monkeypatch.setattr(kb, "read_board_metadata", lambda slug: {"slug": slug})
    monkeypatch.setattr(kb, "kanban_db_path", lambda slug=None, board=None: tmp_path / "kanban.db")
    monkeypatch.setattr(kb, "connect", lambda board=None: FakeConn())
    monkeypatch.setattr(kb, "dispatch_once", fake_dispatch_once)
    monkeypatch.setattr(kb, "advance_controller_once", fake_advance_controller_once)
    monkeypatch.setattr(kb, "has_spawnable_ready", lambda conn: False)
    monkeypatch.setattr(kb, "has_spawnable_review", lambda conn: False)

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    sleep_calls = {"count": 0}

    async def fake_sleep(_delay):
        sleep_calls["count"] += 1
        if sleep_calls["count"] > 1:
            runner._running = False

    monkeypatch.setattr(gateway_run.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(gateway_run.asyncio, "sleep", fake_sleep)

    asyncio.run(GatewayRunner._kanban_dispatcher_watcher(runner))

    assert dispatch_calls
    assert controller_calls
    assert controller_calls[0]["board"] == "default"
    assert controller_calls[0]["dispatch_max"] == 3
    assert controller_calls[0]["max_items"] == 2
    assert controller_calls[0]["max_iterations"] == 4
    assert controller_calls[0]["review_assignee"] == "review-lane"
    assert controller_calls[0]["test_assignee"] == "test-lane"


def test_gateway_controller_boolean_config_accepts_string_false(
    monkeypatch,
    tmp_path,
):
    from gateway import run as gateway_run
    from gateway.run import GatewayRunner
    from hermes_cli import kanban_db as kb

    runner = object.__new__(GatewayRunner)
    runner._running = True

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": True,
                "dispatch_interval_seconds": 1,
                "auto_decompose": "false",
                "advance_controller_in_gateway": True,
                "advance_controller_request_changes_on_failure": "false",
            }
        },
    )

    class FakeConn:
        def close(self):
            pass

    controller_calls = []

    def fake_dispatch_once(conn, **kwargs):
        return SimpleNamespace(
            spawned=[],
            reclaimed=0,
            crashed=[],
            timed_out=[],
            stale=[],
            promoted=0,
            auto_blocked=[],
        )

    def fake_advance_controller_once(conn, **kwargs):
        controller_calls.append(kwargs)
        return {
            "advanced": False,
            "item_count": 0,
            "dispatch_used": 0,
            "stop_reason": "idle",
        }

    auto_decompose_calls = []

    def fail_if_auto_decomposed():
        auto_decompose_calls.append(True)
        return []

    monkeypatch.setattr(kb, "list_boards", lambda include_archived=False: [{"slug": "default"}])
    monkeypatch.setattr(kb, "read_board_metadata", lambda slug: {"slug": slug})
    monkeypatch.setattr(kb, "kanban_db_path", lambda slug=None, board=None: tmp_path / "kanban.db")
    monkeypatch.setattr(kb, "connect", lambda board=None: FakeConn())
    monkeypatch.setattr(kb, "dispatch_once", fake_dispatch_once)
    monkeypatch.setattr(kb, "advance_controller_once", fake_advance_controller_once)
    monkeypatch.setattr(kb, "has_spawnable_ready", lambda conn: False)
    monkeypatch.setattr(kb, "has_spawnable_review", lambda conn: False)
    monkeypatch.setattr(
        "hermes_cli.kanban_decompose.list_triage_ids",
        fail_if_auto_decomposed,
    )

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    sleep_calls = {"count": 0}

    async def fake_sleep(_delay):
        sleep_calls["count"] += 1
        if sleep_calls["count"] > 1:
            runner._running = False

    monkeypatch.setattr(gateway_run.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(gateway_run.asyncio, "sleep", fake_sleep)

    asyncio.run(GatewayRunner._kanban_dispatcher_watcher(runner))

    assert controller_calls
    assert controller_calls[0]["request_changes_on_failure"] is False
    assert auto_decompose_calls == []


def test_gateway_controller_boolean_config_can_disable_controller(
    monkeypatch,
    tmp_path,
):
    from gateway import run as gateway_run
    from gateway.run import GatewayRunner
    from hermes_cli import kanban_db as kb

    runner = object.__new__(GatewayRunner)
    runner._running = True

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": True,
                "dispatch_interval_seconds": 1,
                "auto_decompose": False,
                "advance_controller_in_gateway": "false",
            }
        },
    )

    class FakeConn:
        def close(self):
            pass

    dispatch_calls = []
    controller_calls = []

    def fake_dispatch_once(conn, **kwargs):
        dispatch_calls.append(kwargs)
        return SimpleNamespace(
            spawned=[],
            reclaimed=0,
            crashed=[],
            timed_out=[],
            stale=[],
            promoted=0,
            auto_blocked=[],
        )

    def fake_advance_controller_once(conn, **kwargs):
        controller_calls.append(kwargs)
        return {"advanced": True}

    monkeypatch.setattr(kb, "list_boards", lambda include_archived=False: [{"slug": "default"}])
    monkeypatch.setattr(kb, "read_board_metadata", lambda slug: {"slug": slug})
    monkeypatch.setattr(kb, "kanban_db_path", lambda slug=None, board=None: tmp_path / "kanban.db")
    monkeypatch.setattr(kb, "connect", lambda board=None: FakeConn())
    monkeypatch.setattr(kb, "dispatch_once", fake_dispatch_once)
    monkeypatch.setattr(kb, "advance_controller_once", fake_advance_controller_once)
    monkeypatch.setattr(kb, "has_spawnable_ready", lambda conn: False)
    monkeypatch.setattr(kb, "has_spawnable_review", lambda conn: False)

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    sleep_calls = {"count": 0}

    async def fake_sleep(_delay):
        sleep_calls["count"] += 1
        if sleep_calls["count"] > 1:
            runner._running = False

    monkeypatch.setattr(gateway_run.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(gateway_run.asyncio, "sleep", fake_sleep)

    asyncio.run(GatewayRunner._kanban_dispatcher_watcher(runner))

    assert dispatch_calls
    assert controller_calls == []


def test_gateway_controller_tick_advances_real_kanban_goal(
    monkeypatch,
    tmp_path,
):
    from gateway import run as gateway_run
    from gateway.run import GatewayRunner
    from hermes_cli import kanban_db as kb
    from hermes_cli.worker_lanes import (
        WorkerLane,
        clear_worker_lanes,
        register_worker_lane,
    )

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACES_ROOT", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    spawned: list[tuple[str, str, str | None, int]] = []

    def fake_spawn(task, workspace_path, *, board=None):
        pid = os.getpid()
        spawned.append((task.id, task.assignee, board, pid))
        return pid

    clear_worker_lanes()
    try:
        register_worker_lane(
            WorkerLane(
                name="codex-review",
                kind="codex_cli",
                description="fake review lane",
                spawn_fn=fake_spawn,
                max_concurrency=1,
            )
        )
        register_worker_lane(
            WorkerLane(
                name="codex-test",
                kind="codex_cli",
                description="fake test lane",
                spawn_fn=fake_spawn,
                max_concurrency=1,
            )
        )
        kb.init_db()
        with kb.connect() as conn:
            root = kb.create_task(
                conn,
                title="gateway controller root",
                workspace_kind="dir",
                workspace_path=str(workspace),
                triage=True,
            )
            child_ids = kb.decompose_triage_task(
                conn,
                root,
                root_assignee="orchestrator",
                children=[{"title": "implement", "assignee": "codex-deep"}],
                author="planner",
            )
            assert child_ids is not None
            child = child_ids[0]
            claimed = kb.claim_task(conn, child, claimer="worker:codex-deep")
            assert claimed is not None
            assert kb.block_task(
                conn,
                child,
                reason="review-required: Codex completed; Hermes review required",
                expected_run_id=claimed.current_run_id,
                metadata=_review_required_metadata(),
            )

        runner = object.__new__(GatewayRunner)
        runner._running = True

        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {
                "kanban": {
                    "dispatch_in_gateway": True,
                    "dispatch_interval_seconds": 1,
                    "auto_decompose": False,
                    "advance_controller_in_gateway": True,
                    "advance_controller_dispatch_max": 4,
                    "advance_controller_max_items": 4,
                    "advance_controller_max_iterations": 4,
                    "advance_controller_review_assignee": "codex-review",
                    "advance_controller_test_assignee": "codex-test",
                    "advance_controller_request_changes_on_failure": True,
                }
            },
        )

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        sleep_calls = {"count": 0}

        async def fake_sleep(_delay):
            sleep_calls["count"] += 1
            if sleep_calls["count"] > 1:
                runner._running = False

        monkeypatch.setattr(gateway_run.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(gateway_run.asyncio, "sleep", fake_sleep)

        asyncio.run(GatewayRunner._kanban_dispatcher_watcher(runner))

        with kb.connect() as conn:
            child_snapshot = kb.task_acceptance_snapshot(conn, child)
            root_snapshot = kb.task_progress_snapshot(
                conn,
                root,
                include_children=True,
            )
            followup_tasks = [
                kb.get_task(conn, item["task_id"])
                for item in (
                    child_snapshot["review_followup_gate"]["items"]
                    if child_snapshot
                    and child_snapshot.get("review_followup_gate")
                    else []
                )
            ]
            root_events = kb.list_events(conn, root)
    finally:
        clear_worker_lanes()

    assert len(spawned) == 2
    assert {assignee for _tid, assignee, _board, _pid in spawned} == {
        "codex-review",
        "codex-test",
    }
    assert {board for _tid, _assignee, board, _pid in spawned} == {"default"}
    assert child_snapshot is not None
    gate = child_snapshot["review_followup_gate"]
    assert gate["required"] == 2
    assert gate["running"] == 2
    assert gate["ready"] is False
    assert [task.status for task in followup_tasks if task is not None] == [
        "running",
        "running",
    ]
    assert all(task.worker_pid == os.getpid() for task in followup_tasks if task)
    assert root_snapshot is not None
    assert root_snapshot.child_summary["total"] == 1
    assert root_snapshot.child_summary["review_required"] == 1
    assert root_snapshot.child_summary["recommended_actions"] == {
        "wait_for_followups": 1
    }
    assert any(event.kind == "goal_acceptance_advanced" for event in root_events)
