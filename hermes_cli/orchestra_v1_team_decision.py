"""有限并行 worker 的项目决策、分配协议与只读模型调用。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
from typing import Any, Callable, Optional
import uuid

from hermes_cli.orchestra_v1 import (
    ORCHESTRA_SYSTEM_PROMPT,
    _repository_read_tools,
    _working_directory,
    codex_agent_settings,
)
from hermes_cli.orchestra_v1_control import collect_git_facts, resolve_project
from hermes_cli.orchestra_v1_history import CallRecord, history_index


TEAM_DECISIONS = {"推进", "等待", "询问人类", "停止"}
TEAM_ACTIONS = {"start", "keep", "stop"}


class TeamDecisionUnavailable(RuntimeError):
    """本轮模型服务暂不可用，尚未产生项目决定。"""


class TeamDecisionFormatError(ValueError):
    """本轮回答不能作为团队分配应用，现有执行继续有效。"""


@dataclass(frozen=True)
class TeamAssignment:
    slot: int
    action: str
    task: str = ""


@dataclass(frozen=True)
class TeamDecision:
    decision: str
    state: str
    assignments: tuple[TeamAssignment, ...]


def _validate_limits(max_workers: int, max_subagents: int = 3) -> None:
    if type(max_workers) is not int or not 1 <= max_workers <= 4:
        raise ValueError("本次团队最多支持4个 worker，槽位数必须为1至4的整数")
    if type(max_subagents) is not int or not 0 <= max_subagents <= 3:
        raise ValueError("每个 worker 的 subagent 上限必须为0至3的整数")


def parse_team_output(
    output: str,
    *,
    max_workers: int = 4,
    active_slots: Optional[set[int]] = None,
) -> TeamDecision:
    """只解析当前分配，不从任务文本猜测替换、恢复或领域依赖。"""
    _validate_limits(max_workers)
    text = output.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if fenced is not None:
        text = fenced.group(1)
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("Orchestra 团队输出必须是一个完整 JSON 对象") from exc
    if not isinstance(payload, dict):
        raise ValueError("Orchestra 团队输出必须是 JSON 对象")
    decision = payload.get("decision")
    if not isinstance(decision, str) or decision not in TEAM_DECISIONS:
        raise ValueError("未知的团队决定")
    state = payload.get("state")
    if not isinstance(state, str) or not state.strip():
        raise ValueError("团队项目状态不能为空")
    entries = payload.get("assignments")
    if not isinstance(entries, list):
        raise ValueError("assignments 必须是数组")
    if active_slots is not None:
        if any(type(slot) is not int or not 1 <= slot <= max_workers for slot in active_slots):
            raise ValueError("活跃槽位超出当前 worker 范围")
    seen = set()
    assignments = []
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("每个 worker 分配必须是对象")
        slot, action, task = item.get("slot"), item.get("action"), item.get("task", "")
        if type(slot) is not int or not 1 <= slot <= max_workers:
            raise ValueError("worker 槽位超出允许范围")
        if slot in seen:
            raise ValueError("同一轮不能重复分配 worker 槽位")
        seen.add(slot)
        if not isinstance(action, str) or action not in TEAM_ACTIONS:
            raise ValueError("未知的 worker 动作")
        if not isinstance(task, str):
            raise ValueError("worker task 必须是字符串")
        task = task.strip()
        if action == "start" and not task:
            raise ValueError("启动 worker 时必须给出完整任务")
        if action == "keep" and task:
            raise ValueError("keep 只能保持原任务，不能下发或修改任务")
        if action == "start" and decision != "推进":
            raise ValueError("等待、询问或停止时不能启动 worker")
        if active_slots is not None:
            if action == "start" and slot in active_slots:
                raise ValueError("活跃 worker 必须先停止并确认终态，不能直接替换")
            if action in {"keep", "stop"} and slot not in active_slots:
                raise ValueError("空闲槽位不能执行 keep 或 stop")
        assignments.append(TeamAssignment(slot, action, task))
    if active_slots is not None and not active_slots.issubset(seen):
        raise ValueError("每个活跃 worker 都必须明确选择 keep 或 stop")
    return TeamDecision(decision, state.strip(), tuple(assignments))


def build_team_system_prompt(max_workers: int = 4, max_subagents: int = 3) -> str:
    """沿用项目战略职责，替换单 worker 的机械分配与输出边界。"""
    _validate_limits(max_workers, max_subagents)
    base = ORCHESTRA_SYSTEM_PROMPT.split("最终回答必须严格采用以下形状", 1)[0]
    base = base.replace(
        "每轮最多选择一个当前 worker 任务。",
        "本次可分配有限数量的并行 worker，每个 worker 各自完整负责一个明确任务。",
    )
    base = base.replace(
        "- 观察模式不写回控制材料，也不向活动 worker 发送意见。“继续当前任务”表示执行原样保持，不能同时假定 worker 会采纳你的新限制或建议。需要改变当前任务的目标、范围或承重方法时，选择相应的非继续决定以请求交回控制；实际终态收集后由下一次 fresh 决策确定任务，观察中的建议不是已下发指令。",
        "- 本次使用团队分配模式：keep 保持对应活动 worker 原样执行，意见不会传入其会话；需要改变该 worker 的目标、范围或承重方法时选择 stop 请求交回控制，确认实际终态后另一次 fresh 决策才能 start 新任务。其他 worker 可以继续执行，不把停止某个 worker 误解为全团队停止。",
    )
    base = base.replace(
        "同一结果和主要假设未改变可“继续当前任务”；结果、范围或承重假设实质改变则“开始新任务”。",
        "活动任务继续选择 keep；空闲槽位接收完整新任务选择 start；活动任务需要停止选择 stop，不能直接改写。",
    )
    return base + f"""
## 本次团队能力与人类授权
最多同时运行 {max_workers} 个 worker；每个 worker 的全部后代合计最多 {max_subagents} 个并发 subagent，根 worker 加全部后代最多 {1 + max_subagents} 个并发 agent。
worker 执行层最多 {max_workers * (1 + max_subagents)} 个并发 agent（根 worker 与其子代理合计）；Orchestra 是单独的项目决策者。
这些是上限，不是必须填满的配额。没有单 worker 一小时超时或固定任务轮数上限；本次运行的实际剩余时间由现场提供。不要自行把研究切成每小时结束的阶段，不为凑满时长制造任务，也不能因完成第一批任务就默认结束仍有价值的研究。

并行分配服务共同的项目问题。人类要求整合性研究时，不要将四个槽位永久分成四个彼此独立的领域综述；说明各推进面如何共同约束模型、如何交换可复用结论以及由谁整合。领域分工可随证据改变，整合不是报告末尾拼接四份总结。
在研究过程中就根据产物安排交叉检验、共同假设比较及综合模型任务，使新证据及时改变其他推进面的研究问题；不要等运行结束才首次处理跨域矛盾。只有需要执行这些工作时才分配具体任务，不建立每项任务必经的固定评审流水线。
可根据项目需要选择资料调查、模型比较、跨机制原型、整合、反例或验证。明确各 worker 可写的产物范围及共享产物的当前负责人，避免同时覆盖同一文件；这是当前任务协调，不建立固定角色流水线或预制任务图。
现场提供每个槽位的实际可写目录；worker 可读取共享项目和其他槽位产物，但不能越过自身可写范围。整合产物也由被分配的 worker 写入自身目录，不能用任务授权突破运行环境的写入边界。
比较新研究与旧报告时，核对机制假设、跨域因果关系、证据与反证、近似和有效性边界，不能以文档长度、领域数量或测试数量替代实质进展。旧报告是待重新审视的基线，不是必须遵循的路线。

## 唯一输出协议
最终只输出一个 JSON 对象，不加解释性前后缀：
{{"decision":"推进","state":"完整替换后的当前项目判断","assignments":[{{"slot":1,"action":"start","task":"完整 worker 任务"}}]}}
decision 只能是：推进、等待、询问人类、停止。state 必须是非空自然语言，不是增量补丁。
assignments 是数组；slot 是 1 至 {max_workers} 的整数，同一槽位最多出现一次。action 只能是 start、keep、stop：
- start：仅用于现场中的空闲槽位，task 必须是完整任务；不能替换活动 worker。
- keep：仅用于活动槽位，保持任务原样；task 必须省略或为空，不能夹带新指令。
- stop：仅用于活动槽位，要求交回控制；task 可简述停止理由。停止请求不等于终态已确认。
每个现场中的活动槽位必须明确给出 keep 或 stop；空闲且本轮不启用的槽位可省略。
等待、询问人类、停止时不得包含 start；仍活动的 worker 也必须明确列出动作。预算结束或请求整体退出时，应 stop 活动 worker，收集终态，不假装它们已经停止。
等待且仍有活动worker，表示本轮不增加任务并等待所列keep任务返回，运行器保持它们执行；没有活动worker时的等待才结束本次运行。不要为了等待现有研究而选择全局停止。
你只提出本次分配，外层在检查现场变化并执行机械动作后才应用；不要声称观察意见、停止请求或新任务已经被执行。
"""


def decide_team(
    project: str | Path,
    *,
    control: Path,
    context: str,
    max_workers: int = 4,
    max_subagents: int = 3,
    max_iterations: int = 20,
    active_slots: Optional[set[int]] = None,
    agent_factory: Optional[Callable[..., Any]] = None,
) -> TeamDecision:
    """形成一次 fresh 团队分配；当前控制材料由运行循环应用。"""
    prompt = build_team_system_prompt(max_workers, max_subagents)
    project_path = resolve_project(project)
    control = Path(control).resolve()
    request = (
        "请完成一次全新的 Orchestra 团队项目判断。以下现场包含只读人类意图、"
        "当前状态、活动 worker 和近期结果；旧任务及通知不是新的人类授权。\n\n"
        + context
        + "\n\n## 本次决策可用的 Git 机械事实\n"
        + json.dumps(asdict(collect_git_facts(project_path)), ensure_ascii=False, indent=2)
        + "\n\n## 当前项目近期档案（只作查询索引）\n"
        + history_index(control)
        + "\n\n请按系统提示输出团队 JSON 决定。"
    )
    record = CallRecord(control, "team-decide", context)
    record.write("system.md", prompt)
    record.write("request.md", request)
    session_id = f"orchestra-team-{uuid.uuid4().hex}"
    try:
        kwargs = {
            "session_id": session_id,
            "ephemeral_system_prompt": prompt,
            "skip_context_files": True,
            "skip_memory": True,
            "load_soul_identity": False,
            "max_iterations": max_iterations,
            "enabled_toolsets": [],
            "disabled_toolsets": ["kanban"],
            "quiet_mode": True,
        }
        with _working_directory(project_path):
            if agent_factory is None:
                from run_agent import AIAgent

                agent = AIAgent(**codex_agent_settings(), **kwargs)
            else:
                agent = agent_factory(**kwargs)
            with _repository_read_tools(agent, project_path, session_id, control):
                output = agent.chat(request) or ""
        record.write("output.md", output)
        if output.startswith("API call failed after "):
            if re.search(r"HTTP\s+(?:408|429|5\d\d)\b|server_is_overloaded|stream disconnected|timed out|ConnectionError", output, re.IGNORECASE):
                raise TeamDecisionUnavailable(output)
            raise RuntimeError(output)
        try:
            parsed = parse_team_output(
                output, max_workers=max_workers, active_slots=active_slots
            )
        except ValueError as exc:
            raise TeamDecisionFormatError(str(exc)) from exc
    except BaseException as exc:
        record.write("error.txt", str(exc))
        record.finish("interrupted" if isinstance(exc, KeyboardInterrupt) else "failed")
        raise
    record.finish(parsed.decision)
    return parsed
