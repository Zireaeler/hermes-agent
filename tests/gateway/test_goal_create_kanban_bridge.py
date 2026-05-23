"""Tests for the gateway /goal create Kanban bridge."""

from __future__ import annotations

import asyncio
import json

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import goals


class _FakeSessionEntry:
    session_id = "sid-gateway-goal-create"


class _FakeSessionStore:
    def __init__(self):
        self.entry = _FakeSessionEntry()

    def get_or_create_session(self, source):
        return self.entry

    def _generate_session_key(self, source):
        return "agent:main:discord:channel:goal-create"


def test_gateway_goal_create_routes_to_kanban_without_setting_goal(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )
    runner.session_store = _FakeSessionStore()
    runner.adapters = {}
    runner._queued_events = {}

    event = MessageEvent(
        text="/goal create ship kanban bridge --assignee orchestrator",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="chat-goal-create",
            chat_type="channel",
            user_id="user-goal-create",
        ),
        message_id="msg-goal-create",
    )

    try:
        response = asyncio.run(GatewayRunner._handle_goal_command(runner, event))

        assert "Goal task:" in response
        from hermes_cli import kanban_db as kb

        with kb.connect() as conn:
            tasks = kb.list_tasks(conn, status="triage")
        assert len(tasks) == 1
        assert tasks[0].assignee == "orchestrator"
        assert tasks[0].session_id == "sid-gateway-goal-create"
        assert goals.GoalManager("sid-gateway-goal-create").state is None
    finally:
        goals._DB_CACHE.clear()


def test_gateway_goal_create_can_decompose_and_advance(
    tmp_path,
    monkeypatch,
):
    from hermes_cli import kanban_db as kb
    from hermes_cli import profiles
    from hermes_cli.kanban_decompose import DecomposeOutcome

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    goals._DB_CACHE.clear()

    def fake_decompose(task_id, *, author=None, timeout=None):
        with kb.connect() as conn:
            child_ids = kb.decompose_triage_task(
                conn,
                task_id,
                root_assignee="orchestrator",
                children=[{"title": "implement from gateway", "assignee": "codex-deep"}],
                author=author or "test",
            )
        return DecomposeOutcome(
            task_id=task_id,
            ok=bool(child_ids),
            reason="fake decomposed",
            fanout=True,
            child_ids=child_ids or [],
        )

    monkeypatch.setattr("hermes_cli.kanban_decompose.decompose_task", fake_decompose)

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )
    runner.session_store = _FakeSessionStore()
    runner.adapters = {}
    runner._queued_events = {}

    event = MessageEvent(
        text=(
            "/goal create ship kanban bridge "
            f"--workspace dir:{tmp_path} "
            "--assignee orchestrator "
            "--decompose "
            "--advance "
            "--loop "
            "--advance-dry-run"
        ),
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="chat-goal-create",
            chat_type="channel",
            user_id="user-goal-create",
        ),
        message_id="msg-goal-create",
    )

    try:
        response = asyncio.run(GatewayRunner._handle_goal_command(runner, event))

        assert "Decompose: decomposed" in response
        assert "Advance loop:" in response
        assert "Advance: dispatch_goal_children" in response
        with kb.connect() as conn:
            roots = kb.list_tasks(conn, status="todo")
            children = [task for task in kb.list_tasks(conn, status="ready") if task.assignee == "codex-deep"]
        assert len(roots) == 1
        assert len(children) == 1
        assert roots[0].session_id == "sid-gateway-goal-create"
        assert children[0].session_id == "sid-gateway-goal-create"
    finally:
        goals._DB_CACHE.clear()


def test_gateway_goal_advance_routes_current_kanban_goal(
    tmp_path,
    monkeypatch,
):
    from hermes_cli import kanban_db as kb
    from hermes_cli import profiles
    from hermes_cli.goals import create_kanban_task_from_goal

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    goals._DB_CACHE.clear()

    root = create_kanban_task_from_goal(
        "gateway advance current root",
        session_id="sid-gateway-goal-create",
        assignee="orchestrator",
        workspace_kind="dir",
        workspace_path=str(tmp_path),
    )
    with kb.connect() as conn:
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[{"title": "implement from gateway advance", "assignee": "codex-deep"}],
            author="planner",
        )
        assert child_ids is not None

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )
    runner.session_store = _FakeSessionStore()
    runner.adapters = {}
    runner._queued_events = {}

    event = MessageEvent(
        text="/goal advance --dry-run --json",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="chat-goal-advance",
            chat_type="channel",
            user_id="user-goal-advance",
        ),
        message_id="msg-goal-advance",
    )

    try:
        response = asyncio.run(GatewayRunner._handle_goal_command(runner, event))
        payload = json.loads(response)
        spawned_ids = {
            item["task_id"]
            for step in payload["steps"]
            if step["kind"] == "dispatch_goal_children"
            for item in step["dispatch"]["spawned"]
        }

        assert payload["task_id"] == root
        assert spawned_ids == {child_ids[0]}
    finally:
        goals._DB_CACHE.clear()


def test_gateway_goal_status_reads_kanban_goal_without_interrupting_worker(
    tmp_path,
    monkeypatch,
):
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )
    runner.session_store = _FakeSessionStore()
    runner.adapters = {}
    runner._queued_events = {}

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="chat-goal-status",
        chat_type="channel",
        user_id="user-goal-status",
    )
    create_event = MessageEvent(
        text="/goal create ship kanban status --assignee orchestrator",
        message_type=MessageType.TEXT,
        source=source,
        message_id="msg-goal-create-status",
    )

    try:
        create_response = asyncio.run(
            GatewayRunner._handle_goal_command(runner, create_event)
        )
        assert "Goal task:" in create_response

        with kb.connect() as conn:
            roots = kb.list_tasks(conn, session_id="sid-gateway-goal-create")
            root = next(
                task
                for task in roots
                if (task.idempotency_key or "").startswith(
                    "goal:sid-gateway-goal-create:"
                )
            )
            child_ids = kb.decompose_triage_task(
                conn,
                root.id,
                root_assignee="orchestrator",
                children=[
                    {"title": "implement status reader", "assignee": "codex-deep"},
                    {"title": "review status reader", "assignee": "codex-review"},
                ],
                author="planner",
            )
            assert child_ids is not None
            running_id, review_id = child_ids
            running = kb.claim_task(conn, running_id, claimer="worker:codex-deep")
            assert running is not None
            kb._set_worker_pid(conn, running_id, 43210)
            kb.record_task_event(
                conn,
                running_id,
                "worker_progress",
                {
                    "lane": "codex-deep",
                    "items": [
                        {"index": 1, "status": "done", "text": "inspect"},
                        {"index": 2, "status": "running", "text": "wire status"},
                    ],
                },
                run_id=running.current_run_id,
            )
            reviewing = kb.claim_task(conn, review_id, claimer="worker:codex-review")
            assert reviewing is not None
            assert kb.block_task(
                conn,
                review_id,
                reason="review-required: Codex completed; Hermes review required",
                expected_run_id=reviewing.current_run_id,
                metadata={
                    "worker_lane": {
                        "name": "codex-review",
                        "kind": "codex_cli",
                        "exit_code": 0,
                    },
                    "verification": {"summary": "Verdict: approve"},
                    "review": {
                        "required": True,
                        "reason": "Codex completed; Hermes review required",
                    },
                },
            )
            before = kb.get_task(conn, running_id)

        status_event = MessageEvent(
            text="/goal status",
            message_type=MessageType.TEXT,
            source=source,
            message_id="msg-goal-status",
        )
        status_response = asyncio.run(
            GatewayRunner._handle_goal_command(runner, status_event)
        )

        with kb.connect() as conn:
            after = kb.get_task(conn, running_id)

        assert "Kanban goal" in status_response
        assert root.id in status_response
        assert "root-next: wait_for_workers" in status_response
        assert "children=0/2 done running=1 review-required=1" in status_response
        assert "next: plan_review_followups=1, wait_for_implementation=1" in status_response
        assert running_id in status_response
        assert "progress=running: wire status" in status_response
        assert "No active goal" not in status_response
        assert goals.GoalManager("sid-gateway-goal-create").state is None
        assert after.status == before.status == "running"
        assert after.claim_lock == before.claim_lock
        assert after.current_run_id == before.current_run_id
        assert after.worker_pid == before.worker_pid == 43210
    finally:
        goals._DB_CACHE.clear()
