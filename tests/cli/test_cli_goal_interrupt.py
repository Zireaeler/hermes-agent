"""Tests for CLI goal-continuation interrupt handling.

Covers:
- Ctrl+C during a /goal turn auto-pauses the goal (no more continuations).
- Empty/whitespace-only responses skip the judge (no phantom continuations).
- Clean response without interrupt still drives the judge + enqueues.

These tests exercise ``_maybe_continue_goal_after_turn`` directly on a
minimal ``HermesCLI`` stub (pattern used elsewhere in tests/cli).
"""

from __future__ import annotations

import queue
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME so SessionDB.state_meta writes stay hermetic."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    # Bust the goal module's DB cache so it re-resolves HERMES_HOME each test.
    from hermes_cli import goals
    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


def _make_cli_with_goal(session_id: str, goal_text: str = "build a thing"):
    """Build a minimal HermesCLI stub with an active goal wired in."""
    from cli import HermesCLI
    from hermes_cli.goals import GoalManager

    cli = HermesCLI.__new__(HermesCLI)
    # State the hook + helpers touch directly.
    cli._pending_input = queue.Queue()
    cli._last_turn_interrupted = False
    cli.conversation_history = []
    # `_get_goal_manager()` reads `self.session_id` directly, not
    # `self.agent.session_id`. Match the production lookup.
    cli.session_id = session_id
    cli.agent = MagicMock()
    cli.agent.session_id = session_id

    mgr = GoalManager(session_id=session_id, default_max_turns=5)
    mgr.set(goal_text)
    cli._goal_manager = mgr
    return cli, mgr


# ──────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────


class TestInterruptAutoPause:
    def test_interrupted_turn_pauses_goal_and_skips_continuation(self, hermes_home):
        """Ctrl+C mid-turn must auto-pause the goal, not queue another round."""
        sid = f"sid-interrupt-{uuid.uuid4().hex}"
        cli, mgr = _make_cli_with_goal(sid)
        # Simulate an interrupted turn with a partial assistant reply.
        cli._last_turn_interrupted = True
        cli.conversation_history = [
            {"role": "user", "content": "kickoff"},
            {"role": "assistant", "content": "starting work..."},
        ]

        # Judge MUST NOT run on an interrupted turn. If it does, we've
        # regressed — fail loudly instead of silently querying a mock.
        with patch("hermes_cli.goals.judge_goal") as judge_mock:
            judge_mock.side_effect = AssertionError(
                "judge_goal called on an interrupted turn"
            )
            cli._maybe_continue_goal_after_turn()

        # Pending input must NOT contain a continuation prompt.
        assert cli._pending_input.empty(), (
            "Interrupted turn should not enqueue a continuation prompt"
        )

        # Goal should be paused, not active.
        state = mgr.state
        assert state is not None
        assert state.status == "paused"
        assert "interrupt" in (state.paused_reason or "").lower()

    def test_interrupted_turn_is_resumable(self, hermes_home):
        """After auto-pause from Ctrl+C, /goal resume puts it back to active."""
        sid = f"sid-resume-{uuid.uuid4().hex}"
        cli, mgr = _make_cli_with_goal(sid)
        cli._last_turn_interrupted = True
        cli.conversation_history = [
            {"role": "assistant", "content": "partial"},
        ]
        with patch("hermes_cli.goals.judge_goal"):
            cli._maybe_continue_goal_after_turn()
        assert mgr.state.status == "paused"

        mgr.resume()
        assert mgr.state.status == "active"


class TestEmptyResponseSkip:
    def test_empty_response_does_not_invoke_judge(self, hermes_home):
        """Whitespace-only replies skip judging (transient failure guard)."""
        sid = f"sid-empty-{uuid.uuid4().hex}"
        cli, mgr = _make_cli_with_goal(sid)
        cli._last_turn_interrupted = False
        cli.conversation_history = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "   \n\n   "},
        ]

        with patch("hermes_cli.goals.judge_goal") as judge_mock:
            judge_mock.side_effect = AssertionError(
                "judge_goal called on an empty response"
            )
            cli._maybe_continue_goal_after_turn()

        # No continuation queued; goal still active (neither paused nor done).
        assert cli._pending_input.empty()
        assert mgr.state.status == "active"

    def test_no_assistant_message_skipped(self, hermes_home):
        """Conversation with zero assistant replies must not trip the judge."""
        sid = f"sid-noassistant-{uuid.uuid4().hex}"
        cli, mgr = _make_cli_with_goal(sid)
        cli._last_turn_interrupted = False
        cli.conversation_history = [
            {"role": "user", "content": "go"},
        ]

        with patch("hermes_cli.goals.judge_goal") as judge_mock:
            judge_mock.side_effect = AssertionError(
                "judge_goal called without an assistant response"
            )
            cli._maybe_continue_goal_after_turn()

        assert cli._pending_input.empty()
        assert mgr.state.status == "active"


class TestHealthyTurnStillRuns:
    def test_clean_response_enqueues_continuation_when_judge_says_continue(
        self, hermes_home,
    ):
        """Sanity check: the hook still works in the happy path."""
        sid = f"sid-healthy-{uuid.uuid4().hex}"
        cli, mgr = _make_cli_with_goal(sid)
        cli._last_turn_interrupted = False
        cli.conversation_history = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "did some work, more to do"},
        ]

        # Force the judge to say "continue" without touching the network.
        with patch(
            "hermes_cli.goals.judge_goal",
            return_value=("continue", "needs more steps", False),
        ):
            cli._maybe_continue_goal_after_turn()

        # Continuation prompt must be queued.
        assert not cli._pending_input.empty()
        queued = cli._pending_input.get_nowait()
        assert "Continuing toward your standing goal" in queued
        assert mgr.state.status == "active"

    def test_clean_response_marks_done_when_judge_says_done(self, hermes_home):
        sid = f"sid-done-{uuid.uuid4().hex}"
        cli, mgr = _make_cli_with_goal(sid)
        cli._last_turn_interrupted = False
        cli.conversation_history = [
            {"role": "assistant", "content": "all finished, here's the result"},
        ]

        with patch(
            "hermes_cli.goals.judge_goal",
            return_value=("done", "goal satisfied", False),
        ):
            cli._maybe_continue_goal_after_turn()

        assert cli._pending_input.empty()
        assert mgr.state.status == "done"


class TestCliGoalKanbanBridge:
    def test_goal_advance_routes_current_kanban_goal(
        self,
        hermes_home,
        tmp_path,
        monkeypatch,
    ):
        sid = f"sid-cli-advance-{uuid.uuid4().hex}"

        from cli import HermesCLI
        from hermes_cli import kanban_db as kb
        from hermes_cli import profiles
        from hermes_cli.goals import create_kanban_task_from_goal

        monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
        root = create_kanban_task_from_goal(
            "cli advance current root",
            session_id=sid,
            assignee="orchestrator",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        with kb.connect() as conn:
            child_ids = kb.decompose_triage_task(
                conn,
                root,
                root_assignee="orchestrator",
                children=[{"title": "implement cli advance", "assignee": "codex-deep"}],
                author="planner",
            )
            assert child_ids is not None

        cli = HermesCLI.__new__(HermesCLI)
        cli._pending_input = queue.Queue()
        cli.session_id = sid
        cli.agent = MagicMock()
        cli.agent.session_id = sid

        printed: list[str] = []
        with patch("cli._cprint", side_effect=lambda msg: printed.append(str(msg))):
            cli._handle_goal_command("/goal advance --dry-run --json")

        import json

        payload = json.loads("\n".join(line.strip() for line in printed))
        spawned_ids = {
            item["task_id"]
            for step in payload["steps"]
            if step["kind"] == "dispatch_goal_children"
            for item in step["dispatch"]["spawned"]
        }

        assert payload["task_id"] == root
        assert spawned_ids == {child_ids[0]}

    def test_goal_create_and_status_read_kanban_without_interrupting_worker(
        self,
        hermes_home,
    ):
        sid = f"sid-cli-kanban-{uuid.uuid4().hex}"

        from cli import HermesCLI
        from hermes_cli import kanban_db as kb
        from hermes_cli.goals import GoalManager

        cli = HermesCLI.__new__(HermesCLI)
        cli._pending_input = queue.Queue()
        cli.session_id = sid
        cli.agent = MagicMock()
        cli.agent.session_id = sid

        printed: list[str] = []
        with patch("cli._cprint", side_effect=lambda msg: printed.append(str(msg))):
            cli._handle_goal_command("/goal create 'ship cli bridge' --assignee orchestrator")

        assert any("Goal task:" in line for line in printed)
        assert GoalManager(sid).state is None

        with kb.connect() as conn:
            roots = kb.list_tasks(conn, session_id=sid)
            root = next(
                task
                for task in roots
                if (task.idempotency_key or "").startswith(f"goal:{sid}:")
            )
            child_ids = kb.decompose_triage_task(
                conn,
                root.id,
                root_assignee="orchestrator",
                children=[{"title": "implement cli status", "assignee": "codex-deep"}],
                author="planner",
            )
            assert child_ids is not None
            running_id = child_ids[0]
            running = kb.claim_task(conn, running_id, claimer="worker:codex-deep")
            assert running is not None
            kb._set_worker_pid(conn, running_id, 65432)
            kb.record_task_event(
                conn,
                running_id,
                "worker_progress",
                {
                    "lane": "codex-deep",
                    "items": [
                        {"index": 1, "status": "running", "text": "render cli status"},
                    ],
                },
                run_id=running.current_run_id,
            )
            before = kb.get_task(conn, running_id)

        printed.clear()
        with patch("cli._cprint", side_effect=lambda msg: printed.append(str(msg))):
            cli._handle_goal_command("/goal status")

        with kb.connect() as conn:
            after = kb.get_task(conn, running_id)

        output = "\n".join(printed)
        assert "Kanban goal" in output
        assert root.id in output
        assert "root-next: wait_for_workers" in output
        assert "children=0/1 done running=1 review-required=0" in output
        assert "progress=running: render cli status" in output
        assert "No active goal" not in output
        assert after.status == before.status == "running"
        assert after.claim_lock == before.claim_lock
        assert after.current_run_id == before.current_run_id
        assert after.worker_pid == before.worker_pid == 65432

    def test_standing_goal_status_takes_priority_over_kanban_goal(
        self,
        hermes_home,
    ):
        sid = f"sid-cli-priority-{uuid.uuid4().hex}"

        from cli import HermesCLI
        from hermes_cli.goals import GoalManager, create_kanban_task_from_goal

        create_kanban_task_from_goal(
            "kanban task exists",
            session_id=sid,
            assignee="orchestrator",
        )
        mgr = GoalManager(session_id=sid, default_max_turns=5)
        mgr.set("standing goal wins")

        cli = HermesCLI.__new__(HermesCLI)
        cli._pending_input = queue.Queue()
        cli.session_id = sid
        cli.agent = MagicMock()
        cli.agent.session_id = sid
        cli._goal_manager = mgr

        printed: list[str] = []
        with patch("cli._cprint", side_effect=lambda msg: printed.append(str(msg))):
            cli._handle_goal_command("/goal status")

        output = "\n".join(printed)
        assert "standing goal wins" in output
        assert "Kanban goal" not in output


class TestInterruptFlagLifecycle:
    def test_chat_resets_flag_at_entry(self, hermes_home):
        """chat() must reset _last_turn_interrupted at the top of each turn.

        This guards against stale flag state: if turn N was interrupted and
        turn N+1 runs clean, the hook must not see True from N.
        """
        # We can't run chat() end-to-end here, but we can assert the reset
        # is the first thing after the secret-capture registration by
        # inspecting the source shape.
        from cli import HermesCLI
        import inspect

        src = inspect.getsource(HermesCLI.chat)
        # Look for an explicit reset near the top of chat().
        head = src.split("if not self._ensure_runtime_credentials", 1)[0]
        assert "self._last_turn_interrupted = False" in head, (
            "chat() must reset _last_turn_interrupted before run_conversation "
            "runs — otherwise a prior turn's interrupt state leaks into the "
            "next turn's goal hook decision."
        )
