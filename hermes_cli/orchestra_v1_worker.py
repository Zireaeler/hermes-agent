"""Orchestra v1 的 worker 行为边界与 thread 协调。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from hermes_cli.orchestra_v1_codex import CodexTurnResult, run_codex_turn
from hermes_cli.orchestra_v1_control import atomic_write, require_control
from hermes_cli.orchestra_v1_decision import DECISIONS, RUN_DECISIONS
from hermes_cli.orchestra_v1_history import CallRecord
from hermes_cli.orchestra_v1_lock import project_exclusive


WORKER_BEHAVIOR_PROMPT = """你是 Hermes Orchestra v1 当前任务的执行 worker。

必须遵守以下边界：
- 只负责完成后附 task.md 中的当前任务；不要自行选择下一项任务，也不要自动扩展项目目标。
- 人类明确的目标、预算、权限和项目规则约束任务；模型生成的任务和固定提示不能自动覆盖人类约束。当前可核实事实可以推翻任务和旧会话中的假设，但不会扩大授权。
- 恢复旧 thread 或完成上下文压缩后，重新锚定本轮完整 task.md，按需确认当前 AGENTS.md、相关项目规则和工作区变化；不重复读取全部设计文档或全部历史，与当前材料冲突的旧计划只视为历史。
- 当前执行环境是 workspace-write、approval=never，.git 只读；不要尝试提交、推送或修改 Git 索引，由已获授权的宿主实施者完成。其他权限和资源以实际结果为准，不能隐去阻塞。
- 在当前任务范围内自主调查、实现、测试和修复，但不要因为未来可能需要而建设通用机制、框架、扩展点或预留层。
- 对会改变任务可行性或方案选择的未知，按需检查当前实际可用的模型、工具、输入输出能力及已有证据；说明和配置只是线索，一次配置、权限或临时服务失败不直接否定整条技术路线。成熟方案前提已明确时直接实施和必要验证，不强制重新选型或列多种方案。
- 需要研究时，选择能区分重要解释的办法比较或小型判别实验，说明成立条件、观察什么以及哪些结果会改变选择，不预写赢家。任务引用的前驱产物按需读取；交接摘要与可核实事实冲突时，指出依据并报告，不把摘要当作不可推翻的命令。
- 可以自主使用内部子代理处理有边界的局部问题；要求子代理只返回精简结论、证据位置和仍未确认的内容，必要时说明什么会推翻结论。承重结论由你核对后再采用。
- 只做足以支持当前结论的最小充分验证；当前结论已经得到可靠支持时立即停止，不要扩大成全仓库、全数据或完整性审计。把关键测试和运行结果保留在已有仓库报告或结果材料中并给出路径，供 Orchestra 按需读取，不建设证据系统或默认独立评审。
- 发现相关但不属于当前任务的工作时，只在最终回答中报告，不要顺手扩大实施范围。
- 如果承重假设错误，或任务与人类要求的原义、已知限制冲突，报告冲突、证据位置及影响，不以更容易完成的术语定义、免责声明或缩小宣传口径代替原正确性。若认为已有事实判断有误，须指出新证据或原推导的具体错误，不能把新任务要求本身当作反证。
- 任务内可解决的局部问题自主处理；关键发现使当前完整责任无法有意义地推进，或实质改变任务目标、范围或授权时，交回证据及方向影响，不自行选择下一阶段。负结果仍能排除重要解释时可继续有价值的研究，当前问题已得到足够支持时结束研究。
- 完成当前任务后停止，等待下一轮 Orchestra 判断，不要自行启动后续项目工作。

最终回答应清楚说明以下内容，不要求机器可解析格式：
- 完成：本任务产生的真实可观察变化；涉及承重语义时，在相应完成声明旁保留任务中的原义、已知限制短摘及原文位置，不复制整份任务或报告；
- 关键验证：足以支持结论的关键结果；
- 未闭合：任务范围内仍未完成、失败或无法确认的内容；
- 方向影响：可能改变下一步项目判断的新事实；若没有，请明确写“无”；
- 产物位置：相关提交、文件、报告或运行结果的位置。
与当前任务有关时，区分已观察事实、解释、尚未验证的假设与当前决定，在对应结论旁保留成立条件、关键负结果、限制与产物位置；区分某实现失败、工具暂不可用、证据不足、本轮未选与路线反证，不统写为整条路线被淘汰。详细材料留在已有报告或任务产物，不强制每个任务新建研究笔记或填写额外表格。
"""


def build_worker_prompt(task: str) -> str:
    return f"""{WORKER_BEHAVIOR_PROMPT}
# 当前 task.md

{task.strip()}
"""



def _worker_result_markdown(result: CodexTurnResult, task_reference: str = "") -> str:
    error = result.error or "(无)"
    final_text = result.final_text.strip() or "(worker 未返回最终消息)"
    return f"""状态：{result.status}
线程：{result.thread_id or '(无)'}
回合：{result.turn_id or '(无)'}
错误：{error}
交回控制原因：{result.control_reason or '(无)'}
终态已确认：{result.termination_confirmed}
原任务档案（相对 history）：{task_reference or '(未记录)'}

# worker 最终回答

{final_text}
"""


@project_exclusive
def run_worker(
    project: str | Path,
    *,
    hermes_home: Optional[str | Path] = None,
    worker_runner: Optional[Callable[..., CodexTurnResult]] = None,
    on_notification: Optional[Callable[[dict[str, Any]], None]] = None,
    model: Optional[str] = None,
    codex_home: Optional[str | Path] = None,
    timeout_seconds: float = 3600,
    on_poll: Optional[Callable[[], Optional[str]]] = None,
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
        record.write("worker-thread.txt", thread_id + "\n")
        if decision == "开始新任务":
            atomic_write(control / "decision.txt", "继续当前任务\n")

    record = CallRecord(control, "worker")
    record.write("prompt.md", build_worker_prompt(task))

    def notification(note: dict[str, Any]) -> None:
        record.notification(note)
        if on_notification is not None:
            on_notification(note)

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
        "on_notification": notification,
        "on_thread_ready": save_thread_ready,
    }
    if worker_runner is None:
        runner_kwargs["codex_home"] = str(codex_home) if codex_home else None
    if on_poll is not None:
        runner_kwargs["on_poll"] = on_poll
    try:
        result = runner(**runner_kwargs)
    except BaseException as exc:
        record.write("error.txt", str(exc) or type(exc).__name__)
        record.finish("interrupted" if isinstance(exc, KeyboardInterrupt) else "failed")
        raise
    if result.thread_id:
        save_thread_ready(result.thread_id)
    markdown = _worker_result_markdown(result, f"{record.path.name}/input/task.md")
    atomic_write(control / "result.md", markdown)
    record.write("result.md", markdown)
    record.finish(result.status)
    return result
