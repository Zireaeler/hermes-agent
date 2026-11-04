"""一个项目、一个 worker 的前台自动运行闭环。"""

from collections import deque
from dataclasses import dataclass
import json
import math
from pathlib import Path
import time
from typing import Any, Callable, Optional

from hermes_cli.orchestra_v1 import decide
from hermes_cli.orchestra_v1_codex import CodexTurnResult
from hermes_cli.orchestra_v1_control import require_control
from hermes_cli.orchestra_v1_decision import ParsedDecision, RUN_DECISIONS
from hermes_cli.orchestra_v1_history import CallRecord
from hermes_cli.orchestra_v1_lock import project_exclusive
from hermes_cli.orchestra_v1_worker import run_worker


@dataclass(frozen=True)
class RunResult:
    reason: str
    worker_turns: int
    decision: Optional[ParsedDecision]
    history_dir: str


@project_exclusive
def run_project(
    project: str | Path,
    *,
    hermes_home: Optional[str | Path] = None,
    max_worker_turns: int = 10,
    max_seconds: float = 3600,
    check_interval: float = 300,
    worker_timeout: float = 3600,
    max_iterations: int = 12,
    agent_factory: Optional[Callable[..., Any]] = None,
    worker_runner: Optional[Callable[..., CodexTurnResult]] = None,
    on_notification: Optional[Callable[[dict[str, Any]], None]] = None,
    on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    clock: Callable[[], float] = time.monotonic,
) -> RunResult:
    for name, value in (
        ("max_seconds", max_seconds),
        ("worker_timeout", worker_timeout),
    ):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} 必须是有限正数")
    if max_worker_turns < 1 or not math.isfinite(check_interval) or check_interval < 0:
        raise ValueError("worker 轮数必须为正，检查间隔必须是有限非负数")

    project_path, control = require_control(project, hermes_home)
    record = CallRecord(control, "run")
    started = clock()
    turns = 0
    failures = 0
    latest: Optional[ParsedDecision] = None
    notes: deque[str] = deque(maxlen=12)
    trigger = "run_started"

    def emit(kind: str, **values: Any) -> None:
        if on_event is not None:
            on_event({"kind": kind, **values})

    def context(reason: str) -> str:
        elapsed = max(0, clock() - started)
        return (
            f"唤醒原因：{reason}\n本次运行：{record.path.name}\n"
            f"本次已用时：{elapsed:.3f} 秒；上限：{max_seconds} 秒\n"
            f"已执行 worker turn：{turns}；上限：{max_worker_turns}\n"
            f"连续失败：{failures}；上限：3\n"
            "运行上限只约束本次执行，不代表项目存在外部阻塞；达到上限可吸收结果，但不得继续执行。\n"
            "宏观检查请比较阶段原目标、实际能力或认识变化与投入；重审自拟条件是否造成重复调查或全项目阻塞。"
            "普通局部失败和暂未获得正面结果不自动证明研究无价值。\n"
            "近期通知尾部（片段，仅为线索；完整内容可查询当前项目 history）：\n"
            + ("\n".join(notes) or "(尚无通知)")
        )

    def finish(reason: str) -> RunResult:
        record.write(
            "summary.json",
            json.dumps({"reason": reason, "worker_turns": turns}, ensure_ascii=False),
        )
        record.finish(reason)
        emit("finished", reason=reason, worker_turns=turns)
        return RunResult(reason, turns, latest, str(record.path))

    try:
        while True:
            latest = decide(
                project_path,
                hermes_home=hermes_home,
                max_iterations=max_iterations,
                agent_factory=agent_factory,
                context=context(trigger),
            )
            emit("decision", decision=latest.decision, task=latest.task)
            if trigger == "worker_unconfirmed":
                return finish("worker_unconfirmed")
            if latest.decision not in RUN_DECISIONS:
                return finish(latest.decision)
            if clock() - started >= max_seconds or turns >= max_worker_turns:
                return finish("budget_exhausted")
            if failures >= 3:
                return finish("worker_failure_limit")

            intent = (control / "intent.md").read_text(encoding="utf-8")
            next_check = clock() + check_interval
            observer_failed = False

            def notification(note: dict[str, Any]) -> None:
                notes.append(json.dumps(note, ensure_ascii=False)[-1200:])
                if on_notification is not None:
                    on_notification(note)

            def poll() -> Optional[str]:
                nonlocal next_check, observer_failed
                if clock() - started >= max_seconds:
                    return "budget_exhausted"
                if (control / "intent.md").read_text(encoding="utf-8") != intent:
                    return "intent_changed"
                if check_interval == 0 or clock() < next_check:
                    return None
                emit("observing")
                try:
                    observation = decide(
                        project_path,
                        hermes_home=hermes_home,
                        max_iterations=max_iterations,
                        agent_factory=agent_factory,
                        observe_only=True,
                        context=context("periodic_check"),
                    )
                except Exception:
                    observer_failed = True
                    raise
                next_check = clock() + check_interval
                emit(
                    "observation", decision=observation.decision, task=observation.task
                )
                if clock() - started >= max_seconds:
                    return "budget_exhausted"
                return (
                    None
                    if observation.decision == "继续当前任务"
                    else "review_requested"
                )

            remaining = max_seconds - (clock() - started)
            if remaining <= 0:
                return finish("budget_exhausted")
            turns += 1
            result = run_worker(
                project_path,
                hermes_home=hermes_home,
                worker_runner=worker_runner,
                on_notification=notification,
                on_poll=poll,
                timeout_seconds=min(worker_timeout, remaining),
            )
            if result is None:
                raise RuntimeError("自动运行的可执行决定未启动 worker")
            emit(
                "worker_finished",
                status=result.status,
                control_reason=result.control_reason,
            )
            if observer_failed:
                return finish("observer_failed")
            if result.status == "interrupted" and result.control_reason is None:
                return finish("interrupted")
            if result.error == "worker interrupted by user":
                return finish("interrupted")
            failures = failures + 1 if result.status in {"failed", "timed_out"} else 0
            trigger = (
                "worker_unconfirmed"
                if not result.termination_confirmed
                else result.control_reason or f"worker_{result.status}"
            )
    except KeyboardInterrupt:
        return finish("interrupted")
    except Exception as exc:
        record.write("error.txt", str(exc))
        record.finish("failed")
        raise
