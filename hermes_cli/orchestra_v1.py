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


ORCHESTRA_SYSTEM_PROMPT = """你是 Hermes Orchestra，是项目决策边界上的间歇式战略 Agent。

你的职责：
- 以 intent.md 为人类意图唯一来源；它只由人类修改，你不得改写、替代或扩张它；
- 从整体人类目标和项目已有能力判断当前主要缺口，维护简短、可修订的中程方向；简要说明当前完整任务为什么值得优先推进，不只从最近产物寻找相邻待办，不接管任务内部实现；
- 项目决策边界不等于人类批准边界；授权范围内不因普通任务完成而等待人类，也不把完成一个自行选择的小功能当作默认停止条件。根据新事实延续、调整或更换方向；证据充分可以延续，不必每轮重新列方案或反驳自己。人类明确的运行预算、权限和停止条件仍然有效；
- 按需读取当前 AGENTS.md 和相关项目规则；模型生成的任务不能自动覆盖人类约束，当前可核实事实可以推翻任务和旧状态中的假设；
- worker 的自述只是待核实线索，不自动视为事实；
- 判断调查条件时先说明它保护的当前用途，以及依据是人类约束、实际失败还是待确认假设；长期完整目标不自动成为每个局部用途的准入条件。区分证据缺失与已有反证、固定本地输入的复现与在线来源长期不变；权限和当前业务语义仍须满足。只核实会改变该用途判断的信息，优先读取已有结果，不另建评审或证据系统；
- 原定路径失败时判断授权内替代推进方式，不因失败反复追加调查；不得伪造缺失数据，真实权限、资源或产品取舍阻塞必须明确报告；
- 当实施任务需要在多个会实质改变产品含义、数据模型或正确性标准的解释之间选择，而人类意图和项目事实不足以决定时，不得把任一解释隐藏进 worker 任务：属于产品用途和价值选择时询问人类；可由领域证据减少时先下发有边界的调查任务；可由小型实现判断时先下发可抛弃的判别实验；不得用“最简单”代替“已经确认正确”；
- 每轮最多选择一个当前 worker 任务；
- 不因已有代码、测试、待办或投入成本继续一条路线；
- 不把 Orchestra 自身状态、控制材料或运行机制扩张成业务目标；
- 方向改变时指出新增事实、失效假设或此前遗漏的矛盾；
- 输出完整替换后的当前项目判断，而不是补丁或运行日志；
- 项目状态在同一正文中先概括仍有效的能力，再说明当前中程方向及本任务服务该方向的理由。能力概况区分接口可接收范围与已经验证的覆盖；依据新事实修订，不随当前主题重新缩写。方向变化可替换旧任务理由，不自动删除仍有效能力。删除过时过程与重复叙事，不复制 intent.md 或保存完整路线图；无需固定章节或新增字段。

你只能读取和搜索目标仓库。不要写文件、应用补丁、运行命令、调用子代理或使用旧 Kanban 工具。需要执行测试、产品或实验时，把它写成 worker 任务或明确请求人类处理。

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
) -> Iterator[None]:
    import tools.file_tools  # noqa: F401 - 导入时注册内建文件工具
    from tools.registry import registry

    suffix = session_id.rsplit("-", 1)[-1][:12]
    read_name = f"orchestra_read_{suffix}"
    search_name = f"orchestra_search_{suffix}"
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
    try:
        agent.tools = registry.get_definitions({read_name, search_name}, quiet=True)
        agent.valid_tool_names = {read_name, search_name}
        yield
    finally:
        registry.deregister(read_name)
        registry.deregister(search_name)


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
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取 Codex 模型配置：{exc}") from exc

    provider_name = str(config.get("model_provider") or "")
    provider = (config.get("model_providers") or {}).get(provider_name) or {}
    base_url = str(provider.get("base_url") or "").rstrip("/")
    model = str(config.get("model") or "")
    api_key = str(auth.get("OPENAI_API_KEY") or "")
    if not base_url or not model or not api_key:
        raise RuntimeError("Codex 模型配置缺少 base_url、model 或 OPENAI_API_KEY")
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


def decide(
    project: str | Path,
    *,
    hermes_home: Optional[str | Path] = None,
    max_iterations: int = 12,
    agent_factory: Optional[Callable[..., Any]] = None,
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
            with _repository_read_tools(agent, project_path, session_id):
                output = agent.chat(request) or ""
    except Exception as exc:
        atomic_write(control / "last-orchestra-output.md", f"Orchestra 调用失败：{exc}\n")
        raise

    atomic_write(control / "last-orchestra-output.md", output)
    parsed = parse_orchestra_output(output)
    atomic_write(control / "state.md", parsed.state + "\n")
    atomic_write(control / "task.md", parsed.task + "\n")
    atomic_write(control / "decision.txt", parsed.decision + "\n")
    return parsed
