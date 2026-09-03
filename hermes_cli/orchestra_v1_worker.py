"""Orchestra v1 的 worker 行为边界与 thread 协调。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from hermes_cli.orchestra_v1_codex import CodexTurnResult, run_codex_turn
from hermes_cli.orchestra_v1_control import atomic_write, require_control
from hermes_cli.orchestra_v1_decision import DECISIONS, RUN_DECISIONS


WORKER_BEHAVIOR_PROMPT = """你是 Hermes Orchestra v1 当前任务的执行 worker。

必须遵守以下边界：
- 只负责完成后附 task.md 中的当前任务；不要自行选择下一项任务，也不要自动扩展项目目标。
- 在当前任务范围内自主调查、实现、测试和修复，但不要因为未来可能需要而建设通用机制、框架、扩展点或预留层。
- 发现相关但不属于当前任务的工作时，只在最终回答中报告，不要顺手扩大实施范围。
- 如果当前任务依赖的承重假设错误，停止用扩大范围来补偿；报告错误假设、可复核证据以及它对当前任务和项目方向的影响。
- 完成当前任务后停止，等待下一轮 Orchestra 判断，不要自行启动后续项目工作。

最终回答应清楚说明以下内容，不要求机器可解析格式：
- 实际完成的变化；
- 可复核的证据；
- 仍未完成的问题；
- 可能影响项目方向的新事实；若没有，请明确写“无”。
"""


def build_worker_prompt(task: str) -> str:
    return f"""{WORKER_BEHAVIOR_PROMPT}
# 当前 task.md

{task.strip()}
"""



def _worker_result_markdown(result: CodexTurnResult) -> str:
    error = result.error or "(无)"
    final_text = result.final_text.strip() or "(worker 未返回最终消息)"
    return f"""状态：{result.status}
线程：{result.thread_id or '(无)'}
回合：{result.turn_id or '(无)'}
错误：{error}

# worker 最终回答

{final_text}
"""


def run_worker(
    project: str | Path,
    *,
    hermes_home: Optional[str | Path] = None,
    worker_runner: Optional[Callable[..., CodexTurnResult]] = None,
    on_notification: Optional[Callable[[dict[str, Any]], None]] = None,
    model: Optional[str] = None,
    codex_home: Optional[str | Path] = None,
    timeout_seconds: float = 3600,
) -> Optional[CodexTurnResult]:
    project_path, control = require_control(project, hermes_home)
    decision = (control / "decision.txt").read_text(encoding="utf-8").strip()
    if decision not in DECISIONS:
        raise ValueError(f"当前决定无效：{decision or '(空)'}")
    if decision not in RUN_DECISIONS:
        return None

    task = (control / "task.md").read_text(encoding="utf-8").strip()
    if not task:
        raise ValueError("当前 worker 推进说明为空")

    resume_thread_id: Optional[str] = None
    if decision == "继续当前任务":
        resume_thread_id = (
            (control / "worker-thread.txt").read_text(encoding="utf-8").strip()
        )
        if not resume_thread_id:
            raise RuntimeError("继续当前任务需要已有 worker thread")

    def save_thread_ready(thread_id: str) -> None:
        atomic_write(control / "worker-thread.txt", thread_id + "\n")
        if decision == "开始新任务":
            atomic_write(control / "decision.txt", "继续当前任务\n")

    runner = worker_runner or run_codex_turn
    runner_kwargs: dict[str, Any] = {
        "prompt": build_worker_prompt(task),
        "workspace": project_path,
        "resume_thread_id": resume_thread_id,
        "model": model,
        "sandbox": "workspace-write",
        "approval": "never",
        "compact_before_turn": resume_thread_id is not None,
        "timeout_seconds": timeout_seconds,
        "on_notification": on_notification,
        "on_thread_ready": save_thread_ready,
    }
    if worker_runner is None:
        runner_kwargs["codex_home"] = str(codex_home) if codex_home else None
    result = runner(**runner_kwargs)
    if result.thread_id:
        save_thread_ready(result.thread_id)
    atomic_write(control / "result.md", _worker_result_markdown(result))
    return result
