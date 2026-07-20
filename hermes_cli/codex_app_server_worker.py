"""Codex app-server turn driver for Kanban worker lanes.

The wire protocol is provided by ``agent.transports.codex_app_server``. This
module adds only the Kanban-specific polling hook needed for live Runtime
directives; it does not own graph or receipt semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import time
from typing import Any, Callable, Optional

from agent.transports.codex_app_server import (
    CodexAppServerClient,
    CodexAppServerError,
)


@dataclass
class AppServerWorkerResult:
    thread_id: Optional[str] = None
    turn_id: Optional[str] = None
    final_text: str = ""
    status: str = "failed"
    error: Optional[str] = None
    notifications: list[dict[str, Any]] = field(default_factory=list)
    accepted_delivery_ids: list[str] = field(default_factory=list)


def _thread_id(result: dict[str, Any]) -> Optional[str]:
    thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
    value = (
        thread.get("id")
        or thread.get("sessionId")
        or result.get("threadId")
        or result.get("sessionId")
    )
    return str(value) if value else None


def _turn_id(result: dict[str, Any]) -> Optional[str]:
    turn = result.get("turn") if isinstance(result.get("turn"), dict) else {}
    value = turn.get("id") or result.get("turnId")
    return str(value) if value else None


def live_directive_prompt(delivery: dict[str, Any]) -> str:
    directive = delivery.get("directive") or {}
    return (
        "Hermes Runtime live directive. This message is a validated control "
        "update for your current durable responsibility, not a new user goal.\n\n"
        f"Directive ID: {directive.get('directive_id') or delivery.get('directive_id')}\n"
        f"Action: {directive.get('action') or delivery.get('action')}\n"
        f"Summary: {directive.get('summary') or ''}\n"
        "Instructions:\n- "
        + "\n- ".join(str(value) for value in directive.get("instructions") or [])
        + "\nEvidence refs:\n- "
        + "\n- ".join(str(value) for value in directive.get("evidence_refs") or [])
        + "\n\nApply this update before continuing obsolete work. In your canonical "
        "checkpoint or terminal receipt, include this exact directive ID in "
        "consumed_directive_ids only after you have incorporated it."
    )


def run_app_server_turn(
    *,
    prompt: str,
    workspace: str,
    model: Optional[str],
    sandbox: str,
    approval: str,
    output_schema: Optional[dict[str, Any]],
    resume_thread_id: Optional[str],
    codex_bin: str,
    codex_home: Optional[str],
    env: dict[str, str],
    timeout_seconds: float,
    poll_interval: float,
    on_notification: Optional[Callable[[dict[str, Any]], None]] = None,
    on_tick: Optional[Callable[[], None]] = None,
    register_turn: Optional[
        Callable[[str, str], list[dict[str, Any]]]
    ] = None,
    poll_live_directives: Optional[
        Callable[[str, str], list[dict[str, Any]]]
    ] = None,
    record_live_delivery: Optional[
        Callable[[dict[str, Any], bool, Optional[str], Optional[str]], None]
    ] = None,
    complete_turn: Optional[Callable[[str, str], None]] = None,
    client_factory: Callable[..., CodexAppServerClient] = CodexAppServerClient,
) -> AppServerWorkerResult:
    result = AppServerWorkerResult()
    turn_closed = False
    client = client_factory(
        codex_bin=codex_bin,
        codex_home=codex_home,
        env=env,
    )
    try:
        client.initialize(
            client_name="hermes-kanban-runtime",
            client_title="Hermes Kanban Runtime",
            client_version="4g15",
            capabilities={"experimentalApi": True},
        )
        thread_method = "thread/resume" if resume_thread_id else "thread/start"
        thread_params: dict[str, Any] = {
            "cwd": workspace,
            "model": model,
            "approvalPolicy": approval,
            "sandbox": sandbox,
        }
        if resume_thread_id:
            thread_params["threadId"] = resume_thread_id
        thread_response = client.request(thread_method, thread_params, timeout=20)
        result.thread_id = _thread_id(thread_response) or resume_thread_id
        if not result.thread_id:
            raise RuntimeError(f"{thread_method} returned no thread identity")
        turn_params: dict[str, Any] = {
            "threadId": result.thread_id,
            "input": [{"type": "text", "text": prompt}],
        }
        if output_schema is not None:
            turn_params["outputSchema"] = output_schema
        turn_response = client.request("turn/start", turn_params, timeout=20)
        result.turn_id = _turn_id(turn_response)
        if not result.turn_id:
            raise RuntimeError("turn/start returned no turn identity")

        pending = (
            register_turn(result.thread_id, result.turn_id)
            if register_turn is not None
            else []
        )
        deadline = time.monotonic() + max(1.0, timeout_seconds)
        next_poll = 0.0
        while time.monotonic() < deadline:
            server_request = client.take_server_request(timeout=0)
            if server_request is not None:
                client.respond_error(
                    server_request.get("id"),
                    code=-32601,
                    message="Kanban worker lane does not allow interactive server requests",
                )
                continue

            now = time.monotonic()
            if now >= next_poll:
                if on_tick is not None:
                    on_tick()
                if poll_live_directives is not None:
                    pending.extend(
                        poll_live_directives(result.thread_id, result.turn_id)
                    )
                unique: dict[str, dict[str, Any]] = {}
                for delivery in pending:
                    unique[str(delivery.get("id") or delivery.get("delivery_id"))] = delivery
                pending = []
                for delivery_id, delivery in unique.items():
                    try:
                        steer_response = client.request(
                            "turn/steer",
                            {
                                "threadId": result.thread_id,
                                "expectedTurnId": result.turn_id,
                                "input": [
                                    {
                                        "type": "text",
                                        "text": live_directive_prompt(delivery),
                                    }
                                ],
                                "clientUserMessageId": f"hermes-{delivery_id}",
                            },
                            timeout=10,
                        )
                        observed_turn = str(
                            steer_response.get("turnId") or result.turn_id
                        )
                        accepted = observed_turn == result.turn_id
                        if accepted:
                            result.accepted_delivery_ids.append(delivery_id)
                        if record_live_delivery is not None:
                            record_live_delivery(
                                delivery,
                                accepted,
                                None if accepted else "stale_turn",
                                None if accepted else "turn/steer returned another turn",
                            )
                    except (CodexAppServerError, TimeoutError) as exc:
                        code = (
                            str(exc.code)
                            if isinstance(exc, CodexAppServerError)
                            else "transport_timeout"
                        )
                        if record_live_delivery is not None:
                            record_live_delivery(delivery, False, code, str(exc))
                next_poll = now + max(0.05, poll_interval)

            note = client.take_notification(timeout=min(0.25, poll_interval))
            if note is None:
                if not client.is_alive():
                    result.error = "codex app-server exited before turn completion"
                    break
                continue
            result.notifications.append(note)
            if on_notification is not None:
                on_notification(note)
            method = str(note.get("method") or "")
            params = note.get("params") if isinstance(note.get("params"), dict) else {}
            if method == "item/completed":
                item = params.get("item") if isinstance(params.get("item"), dict) else {}
                if item.get("type") == "agentMessage":
                    result.final_text = str(item.get("text") or "")
            if method == "turn/completed":
                turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                result.status = str(turn.get("status") or "failed")
                error = turn.get("error")
                if error:
                    result.error = (
                        str(error.get("message") or error)
                        if isinstance(error, dict)
                        else str(error)
                    )
                if complete_turn is not None:
                    complete_turn(result.thread_id, result.turn_id)
                    turn_closed = True
                return result
        if result.error is None:
            result.error = f"turn timed out after {timeout_seconds:.0f}s"
        if result.thread_id and result.turn_id:
            try:
                client.request(
                    "turn/interrupt",
                    {"threadId": result.thread_id, "turnId": result.turn_id},
                    timeout=5,
                )
            except Exception:
                pass
        return result
    except Exception as exc:
        result.error = str(exc)
        return result
    finally:
        if (
            complete_turn is not None
            and not turn_closed
            and result.thread_id
            and result.turn_id
        ):
            try:
                complete_turn(result.thread_id, result.turn_id)
            except Exception:
                pass
        client.close()


def load_output_schema(path: Optional[str]) -> Optional[dict[str, Any]]:
    if not path:
        return None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None
