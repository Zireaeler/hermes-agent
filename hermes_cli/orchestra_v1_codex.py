"""Orchestra v1 专用的 Codex app-server 单轮执行。"""

from __future__ import annotations

from dataclasses import dataclass
import math
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
    control_reason: Optional[str] = None
    termination_confirmed: bool = True


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
    max_subagents: Optional[int] = None,
    research_network: bool = False,
    compact_before_turn: bool = False,
    timeout_seconds: Optional[float] = 3600,
    poll_interval: float = 0.1,
    on_notification: Optional[Callable[[dict[str, Any]], None]] = None,
    on_thread_ready: Optional[Callable[[str], None]] = None,
    on_poll: Optional[Callable[[], Optional[str]]] = None,
    interrupt_timeout_seconds: float = 10,
    client_factory: Optional[Callable[..., Any]] = None,
) -> CodexTurnResult:
    """新建或恢复一个 Codex thread，并在前台完成一次业务 turn。"""

    from agent.transports.codex_app_server import CodexAppServerClient

    if max_subagents is not None and (
        isinstance(max_subagents, bool)
        or not isinstance(max_subagents, int)
        or max_subagents < 0
    ):
        raise ValueError("max_subagents 必须是非负整数或 None")
    if timeout_seconds is not None and (
        not math.isfinite(timeout_seconds) or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds 必须是有限正数或 None")
    extra_args = list(app_server_extra_args or [])
    if max_subagents is not None or research_network:
        # 严格解析确保旧版本不会悄悄忽略并发或网络约束。
        extra_args.append("--strict-config")
    if max_subagents is not None:
        # V2 的并发数包含根线程，所有层级后代共用余下槽位。
        # 模型目录也可能选择 V2，必须同时配置两种后端，不能只依赖 feature 关闭。
        extra_args.extend([
            "-c", f"agents.enabled={'true' if max_subagents else 'false'}",
            "-c", f"features.multi_agent={'true' if max_subagents else 'false'}",
            "-c", "sandbox_workspace_write.writable_roots=[]",
        ])
        if max_subagents:
            extra_args.extend([
                "-c", f"agents.max_concurrent_threads_per_session={max_subagents}",
                "-c", "features.multi_agent_v2={enabled=true,"
                f"max_concurrent_threads_per_session={max_subagents + 1}}}",
            ])
        else:
            extra_args.extend(["-c", "features.multi_agent_v2=false"])
    if research_network:
        extra_args.extend([
            "-c", "sandbox_workspace_write.network_access=true",
            "-c", 'web_search="live"',
        ])

    result = CodexTurnResult()
    factory = client_factory or CodexAppServerClient
    client: Any = None
    active_turn_id: Optional[str] = None
    deadline: Optional[float] = None
    control_interrupt_sent = False
    control_deadline_started = False
    active_descendants: dict[str, Optional[str]] = {}

    def observe_notification(note: dict[str, Any]) -> None:
        params = note.get("params") or {}
        turn = params.get("turn") or {}
        item = params.get("item") or {}
        method = note.get("method")
        thread_id = str(params.get("threadId") or turn.get("threadId") or "")
        if method == "item/completed" and item.get("type") == "subAgentActivity":
            child_id = str(item.get("agentThreadId") or "")
            if item.get("kind") == "started" and child_id:
                active_descendants.setdefault(child_id, None)
        if thread_id and thread_id != result.thread_id:
            if method == "turn/started":
                active_descendants[thread_id] = str(
                    turn.get("id") or params.get("turnId") or ""
                ) or None
            elif method == "turn/completed":
                completed_turn = str(turn.get("id") or params.get("turnId") or "")
                current_turn = active_descendants.get(thread_id)
                if not current_turn or not completed_turn or current_turn == completed_turn:
                    active_descendants.pop(thread_id, None)
        if on_notification is not None:
            on_notification(note)

    def finish_descendants() -> None:
        """根 turn 结束后确认活动后代交回控制，避免外层提前补槽。"""
        if client is None or not active_descendants:
            return
        cleanup_deadline = time.monotonic() + interrupt_timeout_seconds
        interrupted: set[str] = set()
        while active_descendants and time.monotonic() < cleanup_deadline:
            note = client.take_notification(timeout=0)
            if note is not None:
                observe_notification(note)
                continue
            for child_id, child_turn in list(active_descendants.items()):
                remaining = cleanup_deadline - time.monotonic()
                if remaining <= 0:
                    break
                if child_turn and child_id not in interrupted:
                    try:
                        client.request(
                            "turn/interrupt",
                            {"threadId": child_id, "turnId": child_turn},
                            timeout=min(5, remaining),
                        )
                    except Exception:
                        pass
                    interrupted.add(child_id)
            if not client.is_alive():
                break
            note = client.take_notification(timeout=min(0.1, poll_interval))
            if note is not None:
                observe_notification(note)
        if active_descendants:
            result.termination_confirmed = False
            result.error = (
                (result.error + "; " if result.error else "")
                + "descendant termination unconfirmed: "
                + ", ".join(sorted(active_descendants))
            )

    def timeout_message(stage: str) -> str:
        if result.control_reason:
            return f"{stage} interrupt confirmation timed out after {interrupt_timeout_seconds:.0f}s"
        return f"{stage} timed out after {timeout_seconds:.0f}s"

    def poll_control() -> None:
        if on_poll is not None and not result.control_reason:
            reason = on_poll()
            if reason:
                result.control_reason = reason
                result.termination_confirmed = False

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
            raise TimeoutError(timeout_message(stage))
        return min(20.0, remaining)

    def next_notification(stage: str) -> dict[str, Any]:
        nonlocal deadline, control_interrupt_sent, control_deadline_started
        if deadline is None:
            raise RuntimeError("worker deadline is not initialized")
        current_client = client
        if current_client is None:
            raise RuntimeError("codex app-server is not initialized")
        while True:
            if time.monotonic() >= deadline:
                if stage == "turn" and not result.control_reason:
                    poll_control()
                    result.control_reason = result.control_reason or "worker_timeout"
                    result.termination_confirmed = False
                    deadline = time.monotonic() + interrupt_timeout_seconds
                else:
                    raise TimeoutError(timeout_message(stage))
            # 先消化已到达的终态；观察期间结束的 turn 不再被旧建议替换。
            note = current_client.take_notification(timeout=0)
            if note is not None:
                observe_notification(note)
                return note
            if result.control_reason:
                if not control_deadline_started:
                    deadline = time.monotonic() + interrupt_timeout_seconds
                    control_deadline_started = True
                if active_turn_id and not control_interrupt_sent:
                    current_client.request(
                        "turn/interrupt",
                        {"threadId": result.thread_id, "turnId": active_turn_id},
                        timeout=min(5, interrupt_timeout_seconds),
                    )
                    control_interrupt_sent = True
            else:
                # 压缩也受项目总体取消控制，None 不会令压缩绕过总体预算。
                poll_control()
                if result.control_reason:
                    continue
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
                observe_notification(note)
                return note
            if not current_client.is_alive():
                raise RuntimeError(f"codex app-server exited before {stage} completion")

    try:
        client = factory(
            codex_bin=codex_bin,
            codex_home=codex_home,
            env=env or {},
            extra_args=extra_args,
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

        # None 取消单 worker 固定时限；总体预算仍可通过 on_poll 请求交回控制。
        deadline = (
            math.inf if timeout_seconds is None else time.monotonic() + timeout_seconds
        )
        if compact_before_turn:
            if not resume_thread_id:
                raise ValueError("context compaction requires a resumed thread")
            result.termination_confirmed = False
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
                    result.termination_confirmed = False
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
                    result.termination_confirmed = True
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

        # 请求超时也可能已经在服务端启动执行；只有终态通知才能确认结束。
        result.termination_confirmed = False
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
        result.termination_confirmed = False

        while True:
            note = next_notification("turn")
            method = str(note.get("method") or "")
            params = note.get("params")
            if not isinstance(params, dict):
                params = {}
            note_thread = str(params.get("threadId") or "")
            note_turn = str(params.get("turnId") or "")
            if (note_thread and note_thread != result.thread_id) or (note_turn and note_turn != result.turn_id):
                poll_control()
                continue
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
                if result.control_reason == "worker_timeout" and result.status == "interrupted":
                    result.status = "timed_out"
                error = turn.get("error")
                if error:
                    result.error = (
                        str(error.get("message") or error)
                        if isinstance(error, dict)
                        else str(error)
                    )
                active_turn_id = None
                result.termination_confirmed = True
                finish_descendants()
                return result
            poll_control()
    except TimeoutError as exc:
        result.status = "failed" if result.control_reason else "timed_out"
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
