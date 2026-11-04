"""活跃 turn 的观察、中断及完成竞态。"""

from types import SimpleNamespace

import hermes_cli.orchestra_v1_codex as codex_module

from hermes_cli.orchestra_v1_codex import _run_codex_turn
from tests.test_orchestra_v1_codex import FakeClient, completed_notifications


def test_idle_worker_can_be_interrupted_and_waits_for_terminal_notification(tmp_path):
    class InterruptClient(FakeClient):
        def request(self, method, params=None, timeout=30):
            response = super().request(method, params, timeout)
            if method == "turn/interrupt":
                self.notifications.append({
                    "method": "turn/completed",
                    "params": {"turn": {"id": "turn-1", "status": "interrupted"}},
                })
            return response

    client = InterruptClient()
    result = _run_codex_turn(
        prompt="任务",
        workspace=tmp_path,
        client_factory=lambda **_: client,
        on_poll=lambda: "review_requested",
        timeout_seconds=2,
    )
    assert result.status == "interrupted"
    assert result.termination_confirmed
    assert result.control_reason == "review_requested"
    assert sum(method == "turn/interrupt" for method, _ in client.requests) == 1
    assert client.closed


def test_completion_during_observation_wins_over_interrupt_request(tmp_path):
    client = FakeClient()

    def poll():
        client.notifications.extend(completed_notifications())
        return "review_requested"

    result = _run_codex_turn(
        prompt="任务",
        workspace=tmp_path,
        client_factory=lambda **_: client,
        on_poll=poll,
        timeout_seconds=2,
    )
    assert result.status == "completed"
    assert result.final_text == "worker result"
    assert result.termination_confirmed
    assert not any(method == "turn/interrupt" for method, _ in client.requests)


def test_interrupt_ack_without_terminal_notification_is_not_confirmation(tmp_path):
    client = FakeClient()
    result = _run_codex_turn(
        prompt="任务",
        workspace=tmp_path,
        client_factory=lambda **_: client,
        on_poll=lambda: "review_requested",
        timeout_seconds=2,
        interrupt_timeout_seconds=0.01,
    )
    assert result.status == "failed"
    assert not result.termination_confirmed
    assert client.closed


def test_observation_runs_even_when_worker_keeps_producing_notifications(tmp_path):
    client = FakeClient(
        notifications=[{"method": "item/started", "params": {}}] * 3
        + completed_notifications()
    )
    polls = []

    def poll():
        polls.append(True)
        return None

    result = _run_codex_turn(
        prompt="任务",
        workspace=tmp_path,
        client_factory=lambda **_: client,
        on_poll=poll,
        timeout_seconds=2,
    )
    assert result.status == "completed"
    assert len(polls) >= 3


def test_unrelated_thread_cannot_replace_worker_final_answer(tmp_path):
    notes = completed_notifications()
    notes.insert(
        1,
        {
            "method": "item/completed",
            "params": {
                "threadId": "unrelated-child",
                "item": {"type": "agentMessage", "text": "子任务答案"},
            },
        },
    )
    client = FakeClient(notifications=notes)
    result = _run_codex_turn(
        prompt="任务",
        workspace=tmp_path,
        client_factory=lambda **_: client,
        timeout_seconds=2,
    )
    assert result.final_text == "worker result"


def test_worker_timeout_waits_for_confirmed_interruption(tmp_path, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(codex_module, "time", SimpleNamespace(monotonic=lambda: now[0]))

    class TimeoutClient(FakeClient):
        def take_notification(self, timeout=0):
            now[0] = 2
            return super().take_notification(timeout)

        def request(self, method, params=None, timeout=30):
            response = super().request(method, params, timeout)
            if method == "turn/interrupt":
                self.notifications.append({
                    "method": "turn/completed",
                    "params": {"turn": {"id": "turn-1", "status": "interrupted"}},
                })
            return response

    client = TimeoutClient()
    result = _run_codex_turn(
        prompt="任务",
        workspace=tmp_path,
        client_factory=lambda **_: client,
        timeout_seconds=1,
    )
    assert result.status == "timed_out"
    assert result.control_reason == "worker_timeout"
    assert result.termination_confirmed


def test_turn_start_timeout_does_not_claim_worker_is_terminated(tmp_path):
    class StartTimeoutClient(FakeClient):
        def request(self, method, params=None, timeout=30):
            if method == "turn/start":
                raise TimeoutError("服务端可能已开始执行")
            return super().request(method, params, timeout)

    client = StartTimeoutClient()
    result = _run_codex_turn(
        prompt="任务", workspace=tmp_path, client_factory=lambda **_: client
    )
    assert result.status == "timed_out"
    assert not result.termination_confirmed
