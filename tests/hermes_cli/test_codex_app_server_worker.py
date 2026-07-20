from __future__ import annotations

from typing import Any, Optional

from hermes_cli.codex_app_server_worker import run_app_server_turn


class _FakeClient:
    def __init__(self, **_kwargs: Any) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.notifications = [
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {
                        "type": "agentMessage",
                        "id": "message-1",
                        "text": '{"schema":"runtime_worker_receipt_v1"}',
                    },
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {
                        "id": "turn-1",
                        "status": "completed",
                        "error": None,
                    },
                },
            },
        ]

    def initialize(self, **_kwargs: Any) -> dict[str, Any]:
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
            return {"thread": {"id": "thread-1"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        if method == "turn/steer":
            return {"turnId": "turn-1"}
        return {}

    def take_server_request(self, timeout: float = 0) -> None:
        del timeout
        return None

    def take_notification(self, timeout: float = 0) -> Optional[dict[str, Any]]:
        del timeout
        return self.notifications.pop(0) if self.notifications else None

    def respond_error(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def is_alive(self) -> bool:
        return True

    def close(self) -> None:
        return None


def test_app_server_turn_steers_validated_live_directive_before_completion():
    delivery = {
        "id": "rld-1",
        "directive_id": "rdir-1",
        "directive": {
            "directive_id": "rdir-1",
            "action": "narrow_scope",
            "summary": "Do not rebuild the parser.",
            "instructions": ["Consume parser-v2 and continue only renderer work."],
            "evidence_refs": ["event:42"],
        },
    }
    recorded: list[tuple[str, bool, Optional[str]]] = []
    completed: list[tuple[str, str]] = []
    client = _FakeClient()

    result = run_app_server_turn(
        prompt="complete the renderer",
        workspace="/tmp",
        model="gpt-5.6-sol",
        sandbox="workspace-write",
        approval="never",
        output_schema={"type": "object"},
        resume_thread_id=None,
        codex_bin="codex",
        codex_home=None,
        env={},
        timeout_seconds=2,
        poll_interval=0.01,
        register_turn=lambda _thread, _turn: [delivery],
        record_live_delivery=lambda item, accepted, code, _message: recorded.append(
            (str(item["id"]), accepted, code)
        ),
        complete_turn=lambda thread, turn: completed.append((thread, turn)),
        client_factory=lambda **_kwargs: client,
    )

    assert result.status == "completed"
    assert result.accepted_delivery_ids == ["rld-1"]
    assert recorded == [("rld-1", True, None)]
    assert completed == [("thread-1", "turn-1")]
    steer = next(params for method, params in client.requests if method == "turn/steer")
    assert steer["expectedTurnId"] == "turn-1"
    assert "rdir-1" in steer["input"][0]["text"]
    assert result.final_text == '{"schema":"runtime_worker_receipt_v1"}'
