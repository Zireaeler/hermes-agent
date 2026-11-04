"""显式授权的有限 worker 团队执行，不替 Orchestra 选择研究方向。"""

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path
import threading
import time
from typing import Any, Callable, Optional

from hermes_cli.orchestra_v1_codex import CodexTurnResult, run_codex_turn
from hermes_cli.orchestra_v1_control import atomic_write, require_control
from hermes_cli.orchestra_v1_history import CallRecord, history_index
from hermes_cli.orchestra_v1_lock import project_exclusive
from hermes_cli.orchestra_v1_team_decision import (
    TeamDecision, TeamDecisionFormatError, TeamDecisionUnavailable,
    decide_team, parse_team_output,
)
from hermes_cli.orchestra_v1_worker import build_worker_prompt, _worker_result_markdown


@dataclass(frozen=True)
class TeamRunResult:
    reason: str
    worker_turns: int
    history_dir: str


@dataclass
class _ActiveWorker:
    slot: int
    task: str
    workspace: Path
    record: CallRecord
    started: float
    cancel: threading.Event = field(default_factory=threading.Event)
    future: Optional[Future] = None
    thread_id: str = ""
    notes: deque = field(default_factory=lambda: deque(maxlen=6))


@project_exclusive
def run_team_project(
    project: str | Path,
    *,
    output_dir: str | Path,
    hermes_home: Optional[str | Path] = None,
    max_workers: int = 4,
    max_subagents: int = 3,
    max_seconds: float = 86400,
    check_interval: float = 900,
    max_iterations: int = 60,
    codex_bin: str = "codex",
    worker_runner: Optional[Callable[..., CodexTurnResult]] = None,
    decision_runner: Optional[Callable[..., TeamDecision]] = None,
    agent_factory: Optional[Callable[..., Any]] = None,
    on_event: Optional[Callable[[dict], None]] = None,
    clock: Callable[[], float] = time.monotonic,
) -> TeamRunResult:
    """串行项目判断、最多四个并行执行，单 worker 没有固定时长上限。"""
    if type(max_workers) is not int or type(max_subagents) is not int or not 1 <= max_workers <= 4 or not 0 <= max_subagents <= 3:
        raise ValueError("本次团队最多4个worker，每个最多3个直接子代理")
    if not math.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError("整次运行秒数必须是有限正数")
    if not math.isfinite(check_interval) or check_interval <= 0:
        raise ValueError("团队项目检查间隔必须是有限正数")
    project_path, control = require_control(project, hermes_home)
    output = Path(output_dir).expanduser()
    if not output.is_absolute():
        output = project_path / output
    output = output.resolve()
    if output == project_path or not output.is_relative_to(project_path) or ".git" in output.relative_to(project_path).parts:
        raise ValueError("团队产物必须位于项目内的独立研究目录，不能写入.git")
    status_path = control / "team-status.json"
    if status_path.is_file():
        previous = json.loads(status_path.read_text())
        if previous.get("status") == "running" or previous.get("reason") == "worker_unconfirmed":
            raise RuntimeError("已有未结束或终态未知的团队现场；先核实旧进程，不自动重放")
    output.mkdir(parents=True, exist_ok=True)
    record = CallRecord(control, "team-run")
    started = clock()
    deadline = started + max_seconds
    active: dict[int, _ActiveWorker] = {}
    finished: list[dict] = []
    turns = 0
    trigger = "team_started"
    reason = ""
    next_check = started
    last_intent = (control / "intent.md").read_text(encoding="utf-8")
    wake = threading.Event()
    pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="orchestra-worker")
    runner = worker_runner or run_codex_turn
    decider = decision_runner or decide_team
    latest_state = (control / "state.md").read_text(encoding="utf-8")
    decision_failures = 0
    retry_after = 0.0
    last_decision_error = ""

    def emit(kind: str, **values):
        event = {"kind": kind, **values}
        record.notification(event)
        if on_event is not None:
            on_event(event)

    def cards() -> list[dict]:
        return [
            {
                "slot": slot, "task": worker.task,
                "workspace": str(worker.workspace), "thread_id": worker.thread_id,
                "elapsed_seconds": round(max(0, clock() - worker.started), 3),
                "return_requested": worker.cancel.is_set(),
                "history": worker.record.path.name,
                "recent_notifications": list(worker.notes),
            }
            for slot, worker in sorted(active.items())
        ]

    def save_status(status: str = "running"):
        atomic_write(status_path, json.dumps({
            "status": status, "reason": reason, "run": record.path.name,
            "elapsed_seconds": round(max(0, clock() - started), 3),
            "max_seconds": max_seconds, "max_workers": max_workers,
            "max_subagents": max_subagents, "worker_turns": turns,
            "decision_failures": decision_failures,
            "last_decision_error": last_decision_error,
            "decision_retry_in_seconds": round(max(0, retry_after-clock()), 3),
            "output_dir": str(output), "active": cards(),
            "finished": [{k: v for k, v in row.items() if k != "result"} for row in finished],
        }, ensure_ascii=False, indent=2))

    def collect() -> bool:
        nonlocal reason
        changed = False
        for slot, worker in list(active.items()):
            if worker.future is None or not worker.future.done():
                continue
            try:
                result = worker.future.result()
            except BaseException as exc:
                result = CodexTurnResult(
                    thread_id=worker.thread_id or None, status="failed",
                    final_text="", error=str(exc), termination_confirmed=False,
                )
            markdown = _worker_result_markdown(result, f"{worker.record.path.name}/task.md")
            worker.record.write("result.md", markdown)
            worker.record.finish(result.status)
            finished.append({
                "slot": slot, "task": worker.task, "workspace": str(worker.workspace),
                "history": worker.record.path.name, "status": result.status,
                "termination_confirmed": result.termination_confirmed,
                "result": markdown,
            })
            del active[slot]
            changed = True
            emit("team_worker_finished", slot=slot, status=result.status,
                 termination_confirmed=result.termination_confirmed)
            if not result.termination_confirmed:
                reason = "worker_unconfirmed"
        if changed:
            atomic_write(control / "result.md", "\n\n".join(
                f"## worker {row['slot']}：{row['workspace']}\n{row['result']}"
                for row in finished[-max_workers:]
            ))
        return changed

    def start_worker(slot: int, task: str):
        nonlocal turns
        turns += 1
        workspace = output / f"worker-{slot}" / f"task-{turns:04d}-{record.path.name[-8:]}"
        workspace.mkdir(parents=True, exist_ok=False)
        worker_record = CallRecord(control, "team-worker", f"slot={slot}; task={turns}")
        worker_record.write("task.md", task)
        worker = _ActiveWorker(slot, task, workspace, worker_record, clock())
        prompt = build_worker_prompt(task) + (
            "\n# 当前团队现场与人类资源授权\n"
            f"项目根目录：{project_path}\n项目规则：{project_path / 'agents.md'}（若存在，同时按需读取AGENTS.md）。\n"
            f"本次人类意图：\n{last_intent}\n"
            f"共享研究产物根：{output}\n你的可写工作目录：{workspace}\n"
            "可读取整个项目和其他worker已有成果，只写自己的工作目录，避免覆盖旧研究或其他活动worker。"
            "需要整合时，读取他人的证据与结论并在此目录形成整合产物，不只拼接摘要。\n"
            f"团队最多{max_workers}个worker；你的全部后代合计最多{max_subagents}个并发subagent，根与后代共用额度。"
            "硬限制由Codex配置执行，不得另启动独立codex、模型API或其他代理进程绕过限额。"
            "继承者也遵守相同边界。子代理数量按需要使用，不要求用满。\n"
            f"启动时整次研究尚余{max(0, deadline-clock()):.1f}秒；没有单worker一小时超时，"
            "不因到一小时而停止。周期项目检查不是执行截止；完成当前完整责任后返回Orchestra。\n"
            "可以调查公开领域资料并制作研究原型，不将公开文档中的旧运行数字声称为本次复现。"
            "实质机制、证据与跨领域整合优先，不为填满时间制造无信息增量的活动。\n"
        )
        worker_record.write("prompt.md", prompt)

        def notification(note):
            worker_record.notification(note)
            worker.notes.append(json.dumps(note, ensure_ascii=False)[-1600:])

        def ready(thread_id):
            worker.thread_id = thread_id
            worker_record.write("worker-thread.txt", thread_id + "\n")

        def poll():
            if worker.cancel.is_set():
                return "team_review_requested"
            if clock() >= deadline:
                return "budget_exhausted"
            if (control / "STOP").exists():
                return "user_stop_requested"
            if (control / "intent.md").read_text(encoding="utf-8") != last_intent:
                return "intent_changed"
            return None

        worker.future = pool.submit(
            runner, prompt=prompt, workspace=workspace, resume_thread_id=None,
            sandbox="workspace-write", approval="never", timeout_seconds=None,
            max_subagents=max_subagents, research_network=True,
            codex_bin=codex_bin,
            env={"HERMES_KANBAN_TASK": ""},
            on_notification=notification, on_thread_ready=ready, on_poll=poll,
        )
        worker.future.add_done_callback(lambda _: wake.set())
        active[slot] = worker
        emit("team_worker_started", slot=slot, task=task, workspace=str(workspace))

    def decision_context(final: bool = False) -> str:
        return (
            f"项目根目录：{project_path}\n共享研究目录：{output}\n"
            "空闲槽启动后，外层会在共享研究目录下创建专属任务目录；任务中的新成果使用其工作目录内相对路径。\n"
            f"唤醒原因：{trigger}\n本次已用时：{max(0, clock()-started):.1f}秒；"
            f"总预算：{max_seconds}秒；剩余：{max(0, deadline-clock()):.1f}秒。\n"
            "没有任务轮数上限，没有单worker一小时超时；执行失败是待判断事实，不按固定次数结束研究。\n"
            f"上次未应用的决策调用错误：{last_decision_error or '(无)'}；调用失败不是项目停止决定。\n"
            f"人类意图（只读唯一来源）：\n{last_intent}\n\n"
            f"当前项目判断（可修订）：\n{latest_state}\n\n"
            f"当前活动worker（必须逐个明确keep或stop）：\n{json.dumps(cards(), ensure_ascii=False)}\n\n"
            f"近期结束的任务与结果（旧结果不是活动任务的完成证明）：\n{json.dumps(finished[-12:], ensure_ascii=False)}\n\n"
            f"全部任务数量：{turns}；早期细节可按需查history：\n{history_index(control)}\n\n"
            + (
                "本次运行已结束执行，禁止start或启动任何worker。只吸收已有结果：在最终state中形成可读研究总评，"
                "给出整合研究主要成果与路径、相对旧报告的实质变化、未解决矛盾和证据边界；"
                "不得把未执行建议说成成果，也不把本次预算结束说成长期项目完成。\n"
                if final else ""
            )
        )

    def apply(decision: TeamDecision):
        nonlocal latest_state
        decision = parse_team_output(
            json.dumps(asdict(decision), ensure_ascii=False),
            max_workers=max_workers, active_slots=set(active),
        )
        for assignment in decision.assignments:
            if assignment.action == "stop" and assignment.slot in active:
                active[assignment.slot].cancel.set()
                emit("team_return_requested", slot=assignment.slot, detail=assignment.task)
        latest_state = decision.state
        atomic_write(control / "state.md", latest_state + "\n")
        atomic_write(control / "task.md", json.dumps([
            {"slot": a.slot, "action": a.action, "task": a.task}
            for a in decision.assignments
        ], ensure_ascii=False, indent=2) + "\n")
        atomic_write(control / "decision.txt", "等待\n")
        if decision.decision == "推进":
            for assignment in decision.assignments:
                if assignment.action == "start":
                    if assignment.slot in active:
                        raise RuntimeError("不能替换尚未确认终态的活动worker")
                    if clock() < deadline and not (control / "STOP").exists():
                        start_worker(assignment.slot, assignment.task)
        emit("team_decision", decision=decision.decision,
             assignments=[{"slot": a.slot, "action": a.action} for a in decision.assignments])

    try:
        save_status()
        next_status = clock()
        while not reason:
            changed = collect()
            if reason:
                break
            if clock() >= deadline:
                reason = "budget_exhausted"
                break
            if (control / "STOP").exists():
                reason = "user_stop_requested"
                break
            current_intent = (control / "intent.md").read_text(encoding="utf-8")
            if current_intent != last_intent:
                last_intent = current_intent
                for worker in active.values():
                    worker.cancel.set()
                trigger = "intent_changed"
                next_check = clock()
            if changed:
                trigger = "worker_returned"
            if (changed or clock() >= next_check) and clock() >= retry_after:
                snapshot_intent = last_intent
                try:
                    decision = decider(
                        project_path, control=control, context=decision_context(),
                        max_workers=max_workers, max_subagents=max_subagents,
                        active_slots=set(active), max_iterations=max_iterations,
                        agent_factory=agent_factory,
                    )
                except (TeamDecisionUnavailable, TeamDecisionFormatError) as exc:
                    decision_failures += 1
                    delay = min(300, 30 * 2 ** min(decision_failures - 1, 4))
                    retry_after = clock() + delay
                    next_check = retry_after
                    last_decision_error = str(exc)[:1000]
                    emit("team_decision_deferred", error=last_decision_error,
                         retry_in_seconds=delay, active_slots=sorted(active))
                    save_status()
                    continue
                decision_failures = 0
                retry_after = 0.0
                last_decision_error = ""
                if collect():
                    emit("team_decision_discarded", reason="worker_returned_during_decision")
                    trigger = "worker_returned"
                    next_check = clock()
                    continue
                if (control / "intent.md").read_text(encoding="utf-8") != snapshot_intent:
                    next_check = clock()
                    continue
                if clock() >= deadline or (control / "STOP").exists():
                    reason = "budget_exhausted" if clock() >= deadline else "user_stop_requested"
                    break
                apply(decision)
                next_check = clock() + check_interval
                trigger = "periodic_check"
                if decision.decision in {"询问人类", "停止"} or (
                    decision.decision == "等待" and not active
                ):
                    reason = decision.decision
                    break
                if not active:
                    reason = "no_active_work"
                    break
            if changed or clock() >= next_status:
                save_status()
                next_status = clock() + 10
            wake.wait(timeout=min(1.0, max(0, next_check-clock()), max(0, deadline-clock())))
            wake.clear()
    except KeyboardInterrupt:
        reason = "interrupted"
    except Exception as exc:
        reason = "failed"
        record.write("error.txt", str(exc))
        emit("team_error", error=str(exc))
    finally:
        for worker in active.values():
            worker.cancel.set()
        pool.shutdown(wait=True, cancel_futures=False)
        collect()
        if reason not in {"failed", "worker_unconfirmed", "interrupted"}:
            try:
                last_intent = (control / "intent.md").read_text(encoding="utf-8")
                final_intent = last_intent
                trigger = f"final_absorption:{reason}"
                final = decider(
                    project_path, control=control, context=decision_context(final=True),
                    max_workers=max_workers, max_subagents=max_subagents,
                    active_slots=set(), max_iterations=max_iterations,
                    agent_factory=agent_factory,
                )
                if any(a.action == "start" for a in final.assignments):
                    raise ValueError("最终吸收不得启动worker，未应用其新任务")
                if (control / "intent.md").read_text(encoding="utf-8") != final_intent:
                    raise RuntimeError("最终吸收期间人类意图改变，未应用旧判断")
                latest_state = final.state
                atomic_write(control / "state.md", latest_state + "\n")
                record.write("final-state.md", latest_state)
                atomic_write(output / "ORCHESTRA_FINAL_ASSESSMENT.md", latest_state + "\n")
            except Exception as exc:
                record.write("final-absorption-error.txt", str(exc))
                emit("team_final_absorption_failed", error=str(exc))
        atomic_write(control / "decision.txt", "停止\n" if reason == "停止" else "等待\n")
        record.write("summary.json", json.dumps({
            "reason": reason, "worker_turns": turns, "output_dir": str(output),
            "finished": [{k: v for k, v in r.items() if k != "result"} for r in finished],
        }, ensure_ascii=False, indent=2))
        record.finish(reason)
        save_status("finished")
        emit("team_finished", reason=reason, worker_turns=turns)
    return TeamRunResult(reason, turns, str(record.path))
