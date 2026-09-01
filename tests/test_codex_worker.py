from __future__ import annotations

import subprocess
from typing import Any, Optional

import pytest

from hermes_cli.codex_worker import run_codex_turn


class FakeClient:
    def __init__(
        self,
        *,
        notifications: Optional[list[dict[str, Any]]] = None,
        alive: bool = True,
        interrupt_on_read: bool = False,
        **_kwargs: Any,
    ) -> None:
        self.notifications = list(notifications or [])
        self.alive = alive
        self.interrupt_on_read = interrupt_on_read
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.initialized = False
        self.closed = False

    def initialize(self, **_kwargs: Any) -> dict[str, Any]:
        self.initialized = True
        return {"userAgent": "fake"}

    def request(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
        timeout: float = 30,
    ) -> dict[str, Any]:
        del timeout
        payload = params or {}
        self.requests.append((method, payload))
        if method == "thread/start":
            return {"thread": {"id": "thread-new"}}
        if method == "thread/resume":
            return {"thread": {"id": payload["threadId"]}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        if method == "turn/interrupt":
            return {}
        raise AssertionError(f"unexpected request: {method}")

    def take_server_request(self, timeout: float = 0) -> None:
        del timeout
        return None

    def take_notification(self, timeout: float = 0) -> Optional[dict[str, Any]]:
        del timeout
        if self.interrupt_on_read:
            raise KeyboardInterrupt
        return self.notifications.pop(0) if self.notifications else None

    def respond_error(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def is_alive(self) -> bool:
        return self.alive

    def stderr_tail(self, _lines: int) -> list[str]:
        return ["fake stderr"]

    def close(self) -> None:
        self.closed = True


def completed_notifications() -> list[dict[str, Any]]:
    return [
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "type": "agentMessage",
                    "id": "message-1",
                    "text": "worker result",
                }
            },
        },
        {
            "method": "turn/completed",
            "params": {
                "turn": {"id": "turn-1", "status": "completed", "error": None}
            },
        },
    ]


def test_starts_thread_collects_events_and_final_message(tmp_path):
    client = FakeClient(notifications=completed_notifications())
    seen: list[str] = []
    ready: list[tuple[str, list[str]]] = []

    result = run_codex_turn(
        prompt="do the task",
        workspace=tmp_path,
        client_factory=lambda **_kwargs: client,
        on_notification=lambda note: seen.append(note["method"]),
        on_thread_ready=lambda thread_id: ready.append(
            (thread_id, [method for method, _params in client.requests])
        ),
        timeout_seconds=2,
        poll_interval=0.01,
    )

    assert result.status == "completed"
    assert result.thread_id == "thread-new"
    assert result.turn_id == "turn-1"
    assert result.final_text == "worker result"
    assert seen == ["item/completed", "turn/completed"]
    assert ready == [("thread-new", ["thread/start"])]
    assert [method for method, _params in client.requests] == [
        "thread/start",
        "turn/start",
    ]
    assert client.initialized
    assert client.closed


def test_resumes_existing_thread_in_new_client(tmp_path):
    client = FakeClient(notifications=completed_notifications())

    result = run_codex_turn(
        prompt="continue",
        workspace=tmp_path,
        resume_thread_id="thread-existing",
        client_factory=lambda **_kwargs: client,
        timeout_seconds=2,
        poll_interval=0.01,
    )

    assert result.status == "completed"
    assert result.thread_id == "thread-existing"
    assert client.requests[0] == (
        "thread/resume",
        {
            "cwd": str(tmp_path.resolve()),
            "approvalPolicy": "never",
            "sandbox": "workspace-write",
            "threadId": "thread-existing",
        },
    )


def test_rejects_resume_that_returns_a_different_thread(tmp_path):
    class MismatchedResumeClient(FakeClient):
        def request(
            self,
            method: str,
            params: Optional[dict[str, Any]] = None,
            timeout: float = 30,
        ) -> dict[str, Any]:
            if method == "thread/resume":
                del timeout
                payload = params or {}
                self.requests.append((method, payload))
                return {"thread": {"id": "thread-different"}}
            return super().request(method, params, timeout)

    client = MismatchedResumeClient()

    result = run_codex_turn(
        prompt="continue",
        workspace=tmp_path,
        resume_thread_id="thread-existing",
        client_factory=lambda **_kwargs: client,
        timeout_seconds=2,
        poll_interval=0.01,
    )

    assert result.status == "failed"
    assert "returned a different thread id" in (result.error or "")
    assert [method for method, _params in client.requests] == ["thread/resume"]
    assert client.closed


def test_turn_aborted_marker_finishes_without_waiting_for_completion(tmp_path):
    client = FakeClient(
        notifications=[
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "agentMessage",
                        "text": "<turn_aborted>",
                    }
                },
            }
        ]
    )

    result = run_codex_turn(
        prompt="task",
        workspace=tmp_path,
        client_factory=lambda **_kwargs: client,
        timeout_seconds=2,
        poll_interval=0.01,
    )

    assert result.status == "interrupted"
    assert result.error == "codex reported turn_aborted"
    assert client.closed


def test_interrupts_active_turn_on_keyboard_interrupt(tmp_path):
    client = FakeClient(interrupt_on_read=True)

    result = run_codex_turn(
        prompt="long task",
        workspace=tmp_path,
        client_factory=lambda **_kwargs: client,
        timeout_seconds=2,
        poll_interval=0.01,
    )

    assert result.status == "interrupted"
    assert result.error == "worker interrupted by user"
    assert client.requests[-1] == (
        "turn/interrupt",
        {"threadId": "thread-new", "turnId": "turn-1"},
    )
    assert client.closed


def test_reports_app_server_exit_before_completion(tmp_path):
    client = FakeClient(alive=False)

    result = run_codex_turn(
        prompt="task",
        workspace=tmp_path,
        client_factory=lambda **_kwargs: client,
        timeout_seconds=2,
        poll_interval=0.01,
    )

    assert result.status == "failed"
    assert result.error == "codex app-server exited before turn completion"
    assert client.closed


@pytest.mark.integration
@pytest.mark.timeout(600)
def test_real_codex_starts_and_resumes_in_new_process(tmp_path):
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)

    first = run_codex_turn(
        prompt="Create first.txt containing exactly first followed by a newline, then report completion.",
        workspace=tmp_path,
        timeout_seconds=180,
    )
    assert first.status == "completed", first.error
    assert first.thread_id
    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "first\n"

    second = run_codex_turn(
        prompt="Continue the same task by creating second.txt containing exactly second followed by a newline, then report completion.",
        workspace=tmp_path,
        resume_thread_id=first.thread_id,
        timeout_seconds=180,
    )
    assert second.status == "completed", second.error
    assert second.thread_id == first.thread_id
    assert (tmp_path / "second.txt").read_text(encoding="utf-8") == "second\n"
    assert second.final_text
