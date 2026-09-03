from __future__ import annotations

from typing import Any, Optional

from hermes_cli.orchestra_v1_codex import (
    CodexTurnResult,
    _run_codex_turn as run_raw_codex_turn,
    run_codex_turn,
)



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
        if method == "thread/compact/start":
            return {}
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


def compaction_notifications(
    *, status: str = "completed", error: Optional[dict[str, str]] = None
) -> list[dict[str, Any]]:
    return [
        {
            "method": "turn/started",
            "params": {
                "threadId": "thread-existing",
                "turn": {"id": "turn-compact"},
            },
        },
        {
            "method": "item/started",
            "params": {
                "threadId": "thread-existing",
                "turnId": "turn-compact",
                "item": {"id": "item-compact", "type": "contextCompaction"},
            },
        },
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-existing",
                "turnId": "turn-compact",
                "item": {"id": "item-compact", "type": "contextCompaction"},
            },
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-existing",
                "turn": {
                    "id": "turn-compact",
                    "status": status,
                    "error": error,
                },
            },
        },
    ]


def test_orchestra_runner_enables_remote_compaction_v2(tmp_path):
    client = FakeClient(notifications=completed_notifications())
    created: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> FakeClient:
        created.append(kwargs)
        return client

    result = run_codex_turn(
        prompt="task",
        workspace=tmp_path,
        client_factory=factory,
        timeout_seconds=2,
        poll_interval=0.01,
    )

    assert result.status == "completed"
    assert created[0]["extra_args"] == [
        "--enable",
        "remote_compaction_v2",
    ]


def test_starts_thread_collects_events_and_final_message(tmp_path):
    client = FakeClient(notifications=completed_notifications())
    seen: list[str] = []
    ready: list[tuple[str, list[str]]] = []

    result = run_raw_codex_turn(
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

    result = run_raw_codex_turn(
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


def test_passes_app_server_extra_args_to_client_factory(tmp_path):
    client = FakeClient(notifications=completed_notifications())
    created: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> FakeClient:
        created.append(kwargs)
        return client

    result = run_raw_codex_turn(
        prompt="task",
        workspace=tmp_path,
        app_server_extra_args=["-c", 'model_providers.provider.name="Local"'],
        client_factory=factory,
        timeout_seconds=2,
        poll_interval=0.01,
    )

    assert result.status == "completed"
    assert created[0]["extra_args"] == [
        "-c",
        'model_providers.provider.name="Local"',
    ]


def test_shared_codex_module_keeps_legacy_orchestra_exports():
    from hermes_cli.codex_worker import (
        CodexTurnResult as LegacyCodexTurnResult,
        run_codex_turn as legacy_run_codex_turn,
    )

    assert LegacyCodexTurnResult is CodexTurnResult
    assert legacy_run_codex_turn is run_codex_turn


def test_compaction_and_turn_start_requests_use_remaining_worker_deadline(tmp_path):
    class TimeoutRecordingClient(FakeClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.timeouts: dict[str, float] = {}

        def request(
            self,
            method: str,
            params: Optional[dict[str, Any]] = None,
            timeout: float = 30,
        ) -> dict[str, Any]:
            self.timeouts[method] = timeout
            return super().request(method, params, timeout)

    client = TimeoutRecordingClient(
        notifications=compaction_notifications() + completed_notifications()
    )

    result = run_raw_codex_turn(
        prompt="continue",
        workspace=tmp_path,
        resume_thread_id="thread-existing",
        compact_before_turn=True,
        client_factory=lambda **_kwargs: client,
        timeout_seconds=1,
        poll_interval=0.01,
    )

    assert result.status == "completed"
    assert 0 < client.timeouts["thread/compact/start"] <= 1
    assert 0 < client.timeouts["turn/start"] <= 1


def test_resumed_thread_compacts_before_starting_worker_turn(tmp_path):
    client = FakeClient(
        notifications=compaction_notifications() + completed_notifications()
    )
    seen: list[str] = []

    result = run_raw_codex_turn(
        prompt="continue",
        workspace=tmp_path,
        resume_thread_id="thread-existing",
        compact_before_turn=True,
        client_factory=lambda **_kwargs: client,
        on_notification=lambda note: seen.append(str(note["method"])),
        timeout_seconds=2,
        poll_interval=0.01,
    )

    assert result.status == "completed"
    assert result.thread_id == "thread-existing"
    assert result.turn_id == "turn-1"
    assert result.final_text == "worker result"
    assert [method for method, _params in client.requests] == [
        "thread/resume",
        "thread/compact/start",
        "turn/start",
    ]
    assert seen == [
        "turn/started",
        "item/started",
        "item/completed",
        "turn/completed",
        "item/completed",
        "turn/completed",
    ]


def test_compaction_failure_does_not_start_worker_turn(tmp_path):
    client = FakeClient(
        notifications=compaction_notifications(
            status="failed", error={"message": "compact failed"}
        )
    )

    result = run_raw_codex_turn(
        prompt="continue",
        workspace=tmp_path,
        resume_thread_id="thread-existing",
        compact_before_turn=True,
        client_factory=lambda **_kwargs: client,
        timeout_seconds=2,
        poll_interval=0.01,
    )

    assert result.status == "failed"
    assert result.turn_id is None
    assert result.error == "context compaction failed: compact failed"
    assert [method for method, _params in client.requests] == [
        "thread/resume",
        "thread/compact/start",
    ]


def test_incomplete_compaction_lifecycle_does_not_start_worker_turn(tmp_path):
    client = FakeClient(
        notifications=[
            compaction_notifications()[0],
            compaction_notifications()[-1],
        ]
    )

    result = run_raw_codex_turn(
        prompt="continue",
        workspace=tmp_path,
        resume_thread_id="thread-existing",
        compact_before_turn=True,
        client_factory=lambda **_kwargs: client,
        timeout_seconds=2,
        poll_interval=0.01,
    )

    assert result.status == "failed"
    assert result.turn_id is None
    assert result.error == (
        "context compaction lifecycle incomplete before turn completion"
    )
    assert [method for method, _params in client.requests] == [
        "thread/resume",
        "thread/compact/start",
    ]


def test_compaction_ignores_other_turn_completion(tmp_path):
    unrelated = {
        "method": "turn/completed",
        "params": {"turn": {"id": "turn-other", "status": "completed"}},
    }
    client = FakeClient(
        notifications=[
            compaction_notifications()[0],
            unrelated,
            *compaction_notifications()[1:],
            *completed_notifications(),
        ]
    )

    result = run_raw_codex_turn(
        prompt="continue",
        workspace=tmp_path,
        resume_thread_id="thread-existing",
        compact_before_turn=True,
        client_factory=lambda **_kwargs: client,
        timeout_seconds=2,
        poll_interval=0.01,
    )

    assert result.status == "completed"
    assert [method for method, _params in client.requests] == [
        "thread/resume",
        "thread/compact/start",
        "turn/start",
    ]


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

    result = run_raw_codex_turn(
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

    result = run_raw_codex_turn(
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

    result = run_raw_codex_turn(
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

    result = run_raw_codex_turn(
        prompt="task",
        workspace=tmp_path,
        client_factory=lambda **_kwargs: client,
        timeout_seconds=2,
        poll_interval=0.01,
    )

    assert result.status == "failed"
    assert result.error == "codex app-server exited before turn completion"
    assert client.closed
