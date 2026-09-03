"""Orchestra v1 专用的 Codex app-server 单轮执行。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Callable, Optional


ORCHESTRA_APP_SERVER_ARGS = ("--enable", "remote_compaction_v2")


@dataclass
class CodexTurnResult:
    """一次 Orchestra Codex worker turn 的结果。"""

    thread_id: Optional[str] = None
    turn_id: Optional[str] = None
    status: str = "failed"
    final_text: str = ""
    error: Optional[str] = None


def _orchestra_response_id(response: dict[str, Any], kind: str) -> Optional[str]:
    nested = response.get(kind)
    if not isinstance(nested, dict):
        nested = {}
    value = nested.get("id") or response.get(f"{kind}Id")
    return str(value) if value else None



def _run_codex_turn(
    *,
    prompt: str,
    workspace: str | Path,
    resume_thread_id: Optional[str] = None,
    model: Optional[str] = None,
    sandbox: str = "workspace-write",
    approval: str = "never",
    codex_bin: str = "codex",
    codex_home: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    app_server_extra_args: Optional[list[str]] = None,
    compact_before_turn: bool = False,
    timeout_seconds: float = 3600,
    poll_interval: float = 0.1,
    on_notification: Optional[Callable[[dict[str, Any]], None]] = None,
    on_thread_ready: Optional[Callable[[str], None]] = None,
    client_factory: Optional[Callable[..., Any]] = None,
) -> CodexTurnResult:
    """新建或恢复一个 Codex thread，并在前台完成一次业务 turn。"""

    from agent.transports.codex_app_server import CodexAppServerClient

    result = CodexTurnResult()
    factory = client_factory or CodexAppServerClient
    client: Any = None
    active_turn_id: Optional[str] = None
    deadline: Optional[float] = None

    def interrupt() -> None:
        if client is None or not result.thread_id or not active_turn_id:
            return
        try:
            client.request(
                "turn/interrupt",
                {"threadId": result.thread_id, "turnId": active_turn_id},
                timeout=5,
            )
        except Exception:
            pass

    def remaining_request_timeout(stage: str) -> float:
        if deadline is None:
            raise RuntimeError("worker deadline is not initialized")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"{stage} timed out after {timeout_seconds:.0f}s")
        return min(20.0, remaining)

    def next_notification(stage: str) -> dict[str, Any]:
        if deadline is None:
            raise RuntimeError("worker deadline is not initialized")
        current_client = client
        if current_client is None:
            raise RuntimeError("codex app-server is not initialized")
        while time.monotonic() < deadline:
            server_request = current_client.take_server_request(timeout=0)
            if server_request is not None:
                current_client.respond_error(
                    server_request.get("id"),
                    code=-32601,
                    message="Orchestra worker does not allow interactive server requests",
                )
                continue

            note = current_client.take_notification(
                timeout=min(0.25, poll_interval)
            )
            if note is not None:
                if on_notification is not None:
                    on_notification(note)
                return note
            if not current_client.is_alive():
                raise RuntimeError(f"codex app-server exited before {stage} completion")
        raise TimeoutError(f"{stage} timed out after {timeout_seconds:.0f}s")

    try:
        client = factory(
            codex_bin=codex_bin,
            codex_home=codex_home,
            env=env or {},
            extra_args=list(app_server_extra_args or []),
        )
        client.initialize(
            client_name="hermes-orchestra-v1",
            client_title="Hermes Orchestra v1",
            client_version="1",
            capabilities={"experimentalApi": True},
        )
        thread_method = "thread/resume" if resume_thread_id else "thread/start"
        thread_params: dict[str, Any] = {
            "cwd": str(Path(workspace).resolve()),
            "approvalPolicy": approval,
            "sandbox": sandbox,
        }
        if model:
            thread_params["model"] = model
        if resume_thread_id:
            thread_params["threadId"] = resume_thread_id
        thread_response = client.request(thread_method, thread_params, timeout=20)
        observed_thread_id = _orchestra_response_id(thread_response, "thread")
        if (
            resume_thread_id
            and observed_thread_id
            and observed_thread_id != resume_thread_id
        ):
            raise RuntimeError(
                "thread/resume returned a different thread id: "
                f"expected {resume_thread_id}, got {observed_thread_id}"
            )
        result.thread_id = observed_thread_id or resume_thread_id
        if not result.thread_id:
            raise RuntimeError(f"{thread_method} returned no thread id")
        if on_thread_ready is not None:
            on_thread_ready(result.thread_id)

        deadline = time.monotonic() + max(1.0, timeout_seconds)
        if compact_before_turn:
            if not resume_thread_id:
                raise ValueError("context compaction requires a resumed thread")
            client.request(
                "thread/compact/start",
                {"threadId": result.thread_id},
                timeout=remaining_request_timeout("context compaction"),
            )
            compaction_turn_id: Optional[str] = None
            compaction_item_id: Optional[str] = None
            compaction_item_completed = False

            while True:
                note = next_notification("context compaction")
                method = str(note.get("method") or "")
                params = note.get("params")
                if not isinstance(params, dict):
                    params = {}

                if method == "turn/started":
                    turn = params.get("turn")
                    if not isinstance(turn, dict):
                        turn = {}
                    note_thread_id = str(
                        turn.get("threadId") or params.get("threadId") or ""
                    )
                    if note_thread_id and note_thread_id != result.thread_id:
                        continue
                    turn_id = str(turn.get("id") or params.get("turnId") or "")
                    if not turn_id:
                        raise RuntimeError(
                            "context compaction lifecycle missing turn id"
                        )
                    if compaction_turn_id and compaction_turn_id != turn_id:
                        raise RuntimeError(
                            "context compaction lifecycle has conflicting turn ids"
                        )
                    compaction_turn_id = turn_id
                    active_turn_id = turn_id
                    continue

                if method in {"item/started", "item/completed"}:
                    item = params.get("item")
                    if not isinstance(item, dict) or item.get("type") != "contextCompaction":
                        continue
                    note_thread_id = str(
                        item.get("threadId") or params.get("threadId") or ""
                    )
                    if note_thread_id and note_thread_id != result.thread_id:
                        continue
                    turn_id = str(
                        item.get("turnId") or params.get("turnId") or ""
                    )
                    if not compaction_turn_id:
                        raise RuntimeError(
                            "context compaction item arrived before turn/started"
                        )
                    if turn_id and turn_id != compaction_turn_id:
                        raise RuntimeError(
                            "context compaction lifecycle has conflicting turn ids"
                        )
                    item_id = str(item.get("id") or params.get("itemId") or "")
                    if not item_id:
                        raise RuntimeError(
                            "context compaction lifecycle missing item id"
                        )
                    if method == "item/started":
                        if compaction_item_id and compaction_item_id != item_id:
                            raise RuntimeError(
                                "context compaction lifecycle has conflicting item ids"
                            )
                        compaction_item_id = item_id
                    else:
                        if not compaction_item_id:
                            raise RuntimeError(
                                "context compaction item completed before item/started"
                            )
                        if compaction_item_id != item_id:
                            raise RuntimeError(
                                "context compaction lifecycle has conflicting item ids"
                            )
                        compaction_item_completed = True
                    continue

                if method == "turn/completed":
                    turn = params.get("turn")
                    if not isinstance(turn, dict):
                        turn = {}
                    turn_id = str(turn.get("id") or params.get("turnId") or "")
                    if not compaction_turn_id or turn_id != compaction_turn_id:
                        continue
                    active_turn_id = None
                    error = turn.get("error")
                    status = str(turn.get("status") or "failed")
                    if status != "completed" or error:
                        error_text = (
                            str(error.get("message") or error)
                            if isinstance(error, dict)
                            else str(error or status)
                        )
                        raise RuntimeError(f"context compaction failed: {error_text}")
                    if not compaction_item_id or not compaction_item_completed:
                        raise RuntimeError(
                            "context compaction lifecycle incomplete before turn completion"
                        )
                    break

        turn_response = client.request(
            "turn/start",
            {
                "threadId": result.thread_id,
                "input": [{"type": "text", "text": prompt}],
            },
            timeout=remaining_request_timeout("turn"),
        )
        result.turn_id = _orchestra_response_id(turn_response, "turn")
        if not result.turn_id:
            raise RuntimeError("turn/start returned no turn id")
        active_turn_id = result.turn_id

        while True:
            note = next_notification("turn")
            method = str(note.get("method") or "")
            params = note.get("params")
            if not isinstance(params, dict):
                params = {}
            if method == "item/completed":
                item = params.get("item")
                if isinstance(item, dict) and item.get("type") == "agentMessage":
                    result.final_text = str(item.get("text") or "")
                    if any(
                        marker in result.final_text
                        for marker in ("<turn_aborted>", "<turn_aborted/>")
                    ):
                        result.status = "interrupted"
                        result.error = "codex reported turn_aborted"
                        return result
            elif method == "turn/completed":
                turn = params.get("turn")
                if not isinstance(turn, dict):
                    turn = {}
                turn_id = str(turn.get("id") or params.get("turnId") or "")
                if turn_id and turn_id != result.turn_id:
                    continue
                result.status = str(turn.get("status") or "failed")
                error = turn.get("error")
                if error:
                    result.error = (
                        str(error.get("message") or error)
                        if isinstance(error, dict)
                        else str(error)
                    )
                active_turn_id = None
                return result
    except TimeoutError as exc:
        result.status = "timed_out"
        result.error = str(exc)
        interrupt()
        return result
    except KeyboardInterrupt:
        result.status = "interrupted"
        result.error = "worker interrupted by user"
        interrupt()
        return result
    except Exception as exc:
        result.error = str(exc)
        interrupt()
        return result
    finally:
        if client is not None:
            client.close()


def run_codex_turn(**kwargs: Any) -> CodexTurnResult:
    """启用兼容的远端压缩协议运行一个 Orchestra Codex turn。"""

    kwargs["app_server_extra_args"] = list(ORCHESTRA_APP_SERVER_ARGS)
    return _run_codex_turn(**kwargs)
