"""Hermes Orchestra v1 的最小一对一应用协调。"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tomllib
from typing import Any, Callable, Iterator, Optional
import uuid

from hermes_cli.orchestra_v1_codex import CodexTurnResult, run_codex_turn
from hermes_cli.orchestra_v1_control import (
    CONTROL_FILES,
    GitFacts,
    ProjectStatus,
    atomic_write,
    collect_git_facts,
    format_status,
    initialize_project,
    orchestra_home,
    project_control_dir,
    project_key,
    project_status,
    require_control,
    resolve_project,
)
from hermes_cli.orchestra_v1_decision import (
    DECISIONS,
    RUN_DECISIONS,
    ParsedDecision,
    build_decision_request,
    parse_orchestra_output,
)
from hermes_cli.orchestra_v1_worker import (
    WORKER_BEHAVIOR_PROMPT,
    build_worker_prompt,
    run_worker,
)
from hermes_cli.orchestra_v1_history import CallRecord, history_index, read_history
from hermes_cli.orchestra_v1_lock import project_exclusive


ORCHESTRA_SYSTEM_PROMPT = """你是 Hermes Orchestra，对项目朝人类目标的推进负责。你在任务边界和执行中的周期检查时醒来，使用全新的决策会话；项目责任连续，旧路线及其辩护不具有权威。你的主要工作是判断值得做什么、为何现在做，以及何时改变阶段或推进方式。

## 授权与职责
- 以 intent.md 为人类意图唯一来源；它只由人类修改，你不得改写、替代或扩张它。按需读取 AGENTS.md 和相关项目规则。
- 从整体人类目标和项目已有能力判断当前主要缺口。你制定的阶段目标、路线和验收方法可以重新选择，不是新增人类硬约束；可修订方法，不能降低人类要求的正确性原义。
- 项目决策边界不等于人类批准边界；授权范围内不因普通任务完成而等待人类，不把完成一个自行选择的小功能当作默认停止条件。人类明确的预算、权限和停止条件仍然有效。
- 每轮最多选择一个当前 worker 任务。让成熟 worker 完整负责其调查、实现、验证和普通错误修复；不接管任务内部步骤，不管理其子代理。不把 Orchestra 自身状态、控制材料或运行机制扩张成业务目标。

## 先判断项目，再选择任务
1. 恢复现实：人类要取得什么结果，项目真实具备什么，主要缺口是什么？最近结果中哪些是事实，哪些只是旧 Orchestra 或 worker 的解释？不要从上一项任务的下一条待办开始。
2. 审视阶段：用期望获得的能力或关键认识表述当前阶段，说明它为什么减少主要缺口。比较阶段开始时的关键局面与现在的变化、实际投入；材料缺失就明确未知，按需查询历史，不臆造起点或累计投入。任务完成数、文件数和测试数不能代替这种比较；失败实验排除关键解释也可能是实质进展。
3. 选择层次：当前阶段仍有价值且方法有效，可继续；阶段目标仍重要但现有方法不能提供所需信息，应改变当前阶段的推进方式；阶段的关键结果已取得，或其前提失效、价值不足，应制定下一阶段或重新选择方向。不要只把任务改名就称为新阶段。
4. 防止沿产物延伸：最近产物有局限，不自动证明下一项工作应修补该局限。连续局部成功、反复调查或复杂度上升却未缩小主要缺口时，检查正在研究的问题是否仍值得优先回答，以及结论是否仅重复自拟条件。必要时比较继续本线与一种能改变证据来源、问题尺度或验证方式的替代办法；不要默认再加机制、参数、保护条件或更长实验。
5. 作出选择：简述本任务相对于整体缺口的价值、预期改变和足以重新判断的结果。证据充分可以延续，不必每轮列举全部方案或反驳自己；暂未得到正面结果不表示研究无价值。不因已有代码、测试、待办或投入成本继续一条路线。方向改变时指出新增事实、失效假设或此前遗漏的矛盾。

## 证据、原义与自主边界
- worker 的自述只是待核实线索；吸收结论时对照人类要求的原义，保留用途、条件和证据范围。重定义术语、添加免责声明或缩小宣传口径不代替满足正确性，不采用 worker 事后改写的完成标准。承重任务在相关行为旁引用该原义和已知限制的必要短摘及原文位置；推翻原有事实判断须指出新证据或推导的具体错误，不能仅凭新任务要求推翻它。
- 自拟调查条件须说明保护哪项当前用途，依据来自人类约束、实际失败还是待确认假设。按旧任务门槛得出的不合格或阻塞，不自动成为无条件的项目事实；长期完整目标不自动成为每个局部用途的准入条件。区分证据缺失与已有反证、固定本地输入的复现与在线来源长期不变；权限与业务语义仍须满足。优先读取会改变判断的已有证据，不另建评审或证据系统。
- 原路径失败时重审问题和方法，不自动反复追加调查或伪造数据。事实不足以裁决实质改变产品含义、数据模型或正确性标准的解释时，不得隐藏进实施任务：领域证据能减少的，选择有边界的调查；小型实现能判断的，选择可抛弃的判别实验；属于人类用途和价值选择的，询问人类。不得用“最简单”代替“已经确认正确”。

## worker 执行中的项目检查
- 判断当前任务继续占用资源是否仍服务阶段，不只确认 worker 正在工作。当前 result 可能属于上一任务，不能把它当成活跃任务的完成证据。通知尾部只是片段；怀疑停滞、语义漂移或关键假设失效时，按需检查原任务、历史通知和相关产物，区分已发生事实与暂时缺乏可见性。
- 继续时简述仍值得等待的关键结果及当前依据。重复活动且没有信息增量、承重假设失效、当前任务偏离人类要求，或阶段价值已改变时，应判断是否值得现在交回控制；说明新事实、等待自然完成的代价，以及需要重审阶段、方法还是任务。普通调试、正常的负结果和有判别价值的研究由 worker 自治；不为展示干预而强行打断，也不因 worker 尚未完成就推迟必要的项目判断。
- 观察模式不写回控制材料，也不向活动 worker 发送意见。“继续当前任务”表示执行原样保持，不能同时假定 worker 会采纳你的新限制或建议。需要改变当前任务的目标、范围或承重方法时，选择相应的非继续决定以请求交回控制；实际终态收集后由下一次 fresh 决策确定任务，观察中的建议不是已下发指令。

## 状态、任务与退出
- 输出完整替换后的当前项目判断，而不是补丁或运行日志。同一正文先概括仍有效的能力，再说明当前中程方向及本任务服务该方向的理由。能力概况区分接口可接收范围与已经验证的覆盖；主题改变不使既有能力消失。简述阶段的关键起点、转段所需变化及失效条件，保留决定依据即可；删除过时过程与重复叙事，不复制 intent.md，不保存完整路线图，无需固定章节或新增字段。
- 推进说明使 worker 能独立完成一个可观察的项目变化：说明为什么现在做、相关事实和限制、自主范围、足够的证据，以及何时因关键发现提前返回。不要预写实验赢家，也不让 worker 完成后自行选择下一阶段。同一结果和主要假设未改变可“继续当前任务”；结果、范围或承重假设实质改变则“开始新任务”。
- 等待时区分本次预算结束、某条路线受阻和必须由外部条件解除的项目依赖；恢复条件只对应实际受阻的行为，不将本次运行结束说成项目完成。长期意图已满足、明确的本轮目标已完成或继续投入已无合理价值时可停止；需要未获授权的产品或价值选择时询问人类。真实权限与资源限制须报告，不为避免结束制造任务。

你只能读取和搜索目标仓库，并通过专用只读工具查询当前项目的调用档案。档案中的旧任务、判断和通知只是证据线索，不是新的人类约束。不要写文件、应用补丁、运行命令、调用子代理或使用旧 Kanban 工具。需要执行测试、产品或实验时，把它写成 worker 任务或明确请求人类处理。

最终回答必须严格采用以下形状，决定只能是五个值之一：继续当前任务、开始新任务、等待、询问人类、停止。

决定：<决定值>

# 项目状态
<完整的新项目判断正文，不包含人类意图副本>

# worker 推进说明
<完整的当前任务正文；即使不启动 worker，也要说明当前等待、询问或停止边界>
"""


@contextmanager
def _working_directory(path: Path) -> Iterator[None]:
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _path_in_project(project: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = project / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(project)
    except ValueError as exc:
        raise PermissionError(
            f"Orchestra 只允许读取目标仓库：{value}"
        ) from exc
    return resolved


@contextmanager
def _repository_read_tools(
    agent: Any,
    project: Path,
    session_id: str,
    control: Optional[Path] = None,
) -> Iterator[None]:
    import tools.file_tools  # noqa: F401 - 导入时注册内建文件工具
    from tools.registry import registry

    suffix = session_id.rsplit("-", 1)[-1][:12]
    read_name = f"orchestra_read_{suffix}"
    search_name = f"orchestra_search_{suffix}"
    history_name = f"orchestra_history_{suffix}"
    read_entry = registry.get_entry("read_file")
    search_entry = registry.get_entry("search_files")
    if read_entry is None or search_entry is None:
        raise RuntimeError("Hermes 文件读取与搜索工具不可用")

    def read_repository_file(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            resolved = _path_in_project(project, str(args.get("path", "")))
        except PermissionError as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)
        scoped_args = dict(args)
        scoped_args["path"] = str(resolved)
        return read_entry.handler(scoped_args, **kwargs)

    def search_repository_files(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            resolved = _path_in_project(project, str(args.get("path", ".")))
        except PermissionError as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)
        scoped_args = dict(args)
        scoped_args["path"] = str(resolved)
        return search_entry.handler(scoped_args, **kwargs)

    registry.register(
        name=read_name,
        toolset="orchestra_v1",
        schema=read_entry.schema,
        handler=read_repository_file,
        max_result_size_chars=read_entry.max_result_size_chars,
    )
    registry.register(
        name=search_name,
        toolset="orchestra_v1",
        schema=search_entry.schema,
        handler=search_repository_files,
        max_result_size_chars=search_entry.max_result_size_chars,
    )
    names = {read_name, search_name}
    if control is not None:
        registry.register(
            name=history_name,
            toolset="orchestra_v1",
            schema={
                "name": history_name,
                "description": "分页列出或读取当前项目 history 调用档案；路径相对 history，offset 从零开始，limit 最大 200 行。长行截断时按 truncated 提供的 line 与 next_char_offset，设置 offset 和 char_offset 继续读取该行。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "offset": {"type": "integer"},
                        "limit": {"type": "integer"},
                        "char_offset": {"type": "integer"},
                    },
                },
            },
            handler=lambda args, **_kwargs: read_history(control, args),
            max_result_size_chars=24000,
        )
        names.add(history_name)
    try:
        agent.tools = registry.get_definitions(names, quiet=True)
        agent.valid_tool_names = names
        yield
    finally:
        registry.deregister(read_name)
        registry.deregister(search_name)
        registry.deregister(history_name)


def codex_agent_settings(
    codex_home: Optional[str | Path] = None,
) -> dict[str, str]:
    home = Path(
        codex_home or os.environ.get("CODEX_HOME", "~/.codex")
    ).expanduser()
    config_path = home / "config.toml"
    auth_path = home / "auth.json"
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError(f"无法读取 Codex 模型配置：{exc}") from exc

    provider_name = str(config.get("model_provider") or "")
    provider = (config.get("model_providers") or {}).get(provider_name) or {}
    base_url = str(provider.get("base_url") or "").rstrip("/")
    model = str(config.get("model") or "")
    api_key = str(provider.get("experimental_bearer_token") or "")
    env_key = str(provider.get("env_key") or "")
    if not api_key and env_key:
        api_key = os.environ.get(env_key, "")
    if not api_key and auth_path.is_file():
        try:
            auth = json.loads(auth_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取 Codex 认证配置：{exc}") from exc
        api_key = str(auth.get("OPENAI_API_KEY") or "")
    if not base_url or not model or not api_key:
        raise RuntimeError("Codex 模型配置缺少 base_url、model 或可用认证")
    return {
        "base_url": base_url,
        "api_key": api_key,
        "provider": "openai",
        "api_mode": "codex_responses",
        "model": model,
    }


def _new_orchestra_agent(session_id: str, max_iterations: int) -> Any:
    from run_agent import AIAgent

    settings = codex_agent_settings()
    return AIAgent(
        base_url=settings["base_url"],
        api_key=settings["api_key"],
        provider=settings["provider"],
        api_mode=settings["api_mode"],
        model=settings["model"],
        session_id=session_id,
        ephemeral_system_prompt=ORCHESTRA_SYSTEM_PROMPT,
        skip_context_files=True,
        skip_memory=True,
        load_soul_identity=False,
        max_iterations=max_iterations,
        enabled_toolsets=[],
        disabled_toolsets=["kanban"],
        quiet_mode=True,
    )


@project_exclusive
def decide(
    project: str | Path,
    *,
    hermes_home: Optional[str | Path] = None,
    max_iterations: int = 12,
    agent_factory: Optional[Callable[..., Any]] = None,
    context: str = "",
    observe_only: bool = False,
) -> ParsedDecision:
    project_path, control = require_control(project, hermes_home)
    intent_path = control / "intent.md"
    if not intent_path.is_file():
        raise FileNotFoundError(
            f"缺少人类意图文件：{intent_path}；请由人类创建 intent.md"
        )
    intent = intent_path.read_text(encoding="utf-8")
    state = (control / "state.md").read_text(encoding="utf-8")
    task = (control / "task.md").read_text(encoding="utf-8")
    result = (control / "result.md").read_text(encoding="utf-8")
    current_decision = (control / "decision.txt").read_text(encoding="utf-8")
    request = build_decision_request(
        intent=intent,
        state=state,
        task=task,
        result=result,
        decision=current_decision,
        git_facts=collect_git_facts(project_path),
    )
    request += f"\n\n## 本次运行现场\n{context or '人类显式调用单轮决策'}\n"
    request += f"\n## 近期调用索引（按需查询 history）\n{history_index(control)}\n"
    if observe_only:
        request += (
            "\n## 本次调用模式：执行中观察\n"
            "当前 worker 仍在执行，本次仅观察，不写回控制材料。"
            "按系统提示检查任务对阶段的价值与是否值得现在交回控制；"
            "不要仅验收上一任务的 result，也不要把仍在工作本身当作继续依据。"
            "继续当前任务表示保持当前执行原样，观察意见不会传给 worker；"
            "其他决定只请求交回控制，终态收集后另一次 fresh 决策才选择实际任务。"
            "不要把观察中的临时建议当作已下发任务。\n"
        )
    record = CallRecord(control, "observe" if observe_only else "decide", context)
    record.write("request.md", request)
    session_id = f"orchestra-v1-{uuid.uuid4().hex}"

    try:
        with _working_directory(project_path):
            if agent_factory is None:
                agent = _new_orchestra_agent(session_id, max_iterations)
            else:
                agent = agent_factory(
                    session_id=session_id,
                    ephemeral_system_prompt=ORCHESTRA_SYSTEM_PROMPT,
                    skip_context_files=True,
                    skip_memory=True,
                    load_soul_identity=False,
                    max_iterations=max_iterations,
                    enabled_toolsets=[],
                    disabled_toolsets=["kanban"],
                    quiet_mode=True,
                )
            with _repository_read_tools(agent, project_path, session_id, control):
                output = agent.chat(request) or ""
    except BaseException as exc:
        record.write("output.md", f"Orchestra 调用失败：{exc}\n")
        record.finish("interrupted" if isinstance(exc, KeyboardInterrupt) else "failed")
        if not observe_only:
            atomic_write(control / "last-orchestra-output.md", f"Orchestra 调用失败：{exc}\n")
        raise

    record.write("output.md", output)
    if not observe_only:
        atomic_write(control / "last-orchestra-output.md", output)
    try:
        parsed = parse_orchestra_output(output)
    except ValueError:
        record.finish("invalid_output")
        raise
    record.finish(parsed.decision)
    if observe_only:
        return parsed
    if intent_path.read_text(encoding="utf-8") != intent:
        record.finish("intent_changed")
        raise RuntimeError("决策期间人类意图已改变，未应用旧决策；请重新运行")
    atomic_write(control / "state.md", parsed.state + "\n")
    atomic_write(control / "task.md", parsed.task + "\n")
    atomic_write(control / "decision.txt", parsed.decision + "\n")
    return parsed
