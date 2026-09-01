"""Minimal one-to-one Orchestra v1 control loop."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib
from typing import Any, Callable, Iterator, Optional
import uuid

from hermes_cli.codex_worker import CodexTurnResult, run_codex_turn
from utils import atomic_replace


DECISIONS = (
    "继续当前任务",
    "开始新任务",
    "等待",
    "询问人类",
    "停止",
)
RUN_DECISIONS = {"继续当前任务", "开始新任务"}
CONTROL_FILES = (
    "state.md",
    "task.md",
    "result.md",
    "decision.txt",
    "worker-thread.txt",
    "last-orchestra-output.md",
)

ORCHESTRA_SYSTEM_PROMPT = """你是 Hermes Orchestra，是项目决策边界上的间歇式战略 Agent。

你的职责：
- 重新确认当前人类意图，独立判断项目现实与主要收敛缺口；
- 只做任务之间的项目级判断，不接管任务内部实现；
- worker 的自述只是待核实线索，不自动视为事实；
- 只核实会改变路线的承重信息，不完整规划整个项目；
- 每轮最多选择一个当前 worker 任务；
- 不因已有代码、测试、待办或投入成本继续一条路线；
- 不把 Orchestra 自身状态、控制材料或运行机制扩张成业务目标；
- 方向改变时指出新增事实、失效假设或此前遗漏的矛盾；
- 输出完整替换后的项目状态，而不是补丁或运行日志。

你只能读取和搜索目标仓库。不要写文件、应用补丁、运行命令、调用子代理或使用旧 Kanban 工具。

最终回答必须严格采用以下形状，决定只能是五个值之一：继续当前任务、开始新任务、等待、询问人类、停止。

决定：<决定值>

# 项目状态
<完整的新状态正文>

# worker 推进说明
<完整的当前任务正文；即使不启动 worker，也要说明当前等待、询问或停止边界>
"""

_DECISION_RE = re.compile(r"^决定\s*[:：]\s*(.+?)\s*$", re.MULTILINE)
_STATE_HEADING_RE = re.compile(r"^#\s+项目状态\s*$", re.MULTILINE)
_TASK_HEADING_RE = re.compile(r"^#\s+worker 推进说明\s*$", re.MULTILINE | re.IGNORECASE)


@dataclass(frozen=True)
class ParsedDecision:
    decision: str
    state: str
    task: str


@dataclass(frozen=True)
class GitFacts:
    root: str
    branch: str
    commit: str
    status_short: str
    diff_stat: str
    recent_commits: str


@dataclass(frozen=True)
class ProjectStatus:
    project: str
    control_dir: str
    decision: str
    worker_thread: str
    git_branch: str
    git_commit: str
    last_worker_status: str
    file_times: dict[str, str]


def resolve_project(project: str | Path) -> Path:
    path = Path(project).expanduser().resolve()
    if not path.is_dir():
        raise FileNotFoundError(f"项目目录不存在：{path}")
    return path


def project_key(project: str | Path) -> str:
    canonical = str(resolve_project(project))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def orchestra_home(hermes_home: Optional[str | Path] = None) -> Path:
    if hermes_home is None:
        hermes_home = os.environ.get("HERMES_HOME", "~/.hermes")
    return Path(hermes_home).expanduser().resolve() / "orchestra"


def project_control_dir(
    project: str | Path,
    hermes_home: Optional[str | Path] = None,
) -> Path:
    return orchestra_home(hermes_home) / project_key(project)


def atomic_write(path: str | Path, content: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        atomic_replace(temporary_path, target)
    finally:
        temporary_path.unlink(missing_ok=True)


def initialize_project(
    project: str | Path,
    goal: str,
    *,
    hermes_home: Optional[str | Path] = None,
    overwrite: bool = False,
) -> Path:
    project_path = resolve_project(project)
    control = project_control_dir(project_path, hermes_home)
    if control.exists() and not overwrite:
        raise FileExistsError(f"项目已经初始化：{control}")
    if not goal.strip():
        raise ValueError("初始目标不能为空")

    control.mkdir(parents=True, exist_ok=True)
    initial = {
        "state.md": f"# 人类意图\n\n{goal.strip()}\n",
        "task.md": "",
        "result.md": "",
        "decision.txt": "",
        "worker-thread.txt": "",
        "last-orchestra-output.md": "",
    }
    for name, content in initial.items():
        atomic_write(control / name, content)
    return control


def _require_control(
    project: str | Path,
    hermes_home: Optional[str | Path] = None,
) -> tuple[Path, Path]:
    project_path = resolve_project(project)
    control = project_control_dir(project_path, hermes_home)
    if not control.is_dir():
        raise FileNotFoundError(f"项目尚未初始化：{control}")
    return project_path, control


def _git(project: Path, *args: str) -> str:
    git_bin = shutil.which("git")
    if git_bin is None:
        raise RuntimeError("未找到 git 可执行文件")
    completed = subprocess.run(
        [git_bin, "-C", str(project), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)} 失败：{detail}")
    return completed.stdout.strip()


def collect_git_facts(project: str | Path) -> GitFacts:
    project_path = resolve_project(project)
    root = _git(project_path, "rev-parse", "--show-toplevel")
    branch = _git(project_path, "branch", "--show-current") or "(detached HEAD)"
    return GitFacts(
        root=root,
        branch=branch,
        commit=_git(project_path, "rev-parse", "HEAD"),
        status_short=_git(project_path, "status", "--short"),
        diff_stat=_git(project_path, "diff", "--stat"),
        recent_commits=_git(project_path, "log", "-5", "--pretty=format:%h %s"),
    )


def parse_orchestra_output(output: str) -> ParsedDecision:
    state_match = _STATE_HEADING_RE.search(output)
    task_match = _TASK_HEADING_RE.search(output)
    if state_match is None or task_match is None:
        raise ValueError("Orchestra 输出缺少必要标题")
    if state_match.start() >= task_match.start():
        raise ValueError("Orchestra 输出段落顺序错误")
    decision_match = _DECISION_RE.fullmatch(output[: state_match.start()].strip())
    if decision_match is None:
        raise ValueError("Orchestra 输出开头必须只包含一行决定")

    decision = decision_match.group(1).strip()
    if decision not in DECISIONS:
        raise ValueError(f"未知决定：{decision}")
    state = output[state_match.end() : task_match.start()].strip()
    task = output[task_match.end() :].strip()
    if not state or not task:
        raise ValueError("Orchestra 输出的项目状态或 worker 推进说明为空")
    return ParsedDecision(decision=decision, state=state, task=task)


def _render_git_facts(facts: GitFacts) -> str:
    return f"""仓库根目录：{facts.root}
当前分支：{facts.branch}
当前提交：{facts.commit}

git status --short：
{facts.status_short or '(clean)'}

git diff --stat：
{facts.diff_stat or '(empty)'}

最近提交：
{facts.recent_commits or '(none)'}"""


def build_decision_request(
    *,
    state: str,
    task: str,
    result: str,
    human: Optional[str],
    git_facts: GitFacts,
) -> str:
    return f"""请完成一次全新的 Orchestra 项目决策轮。

## 当前项目状态
{state.strip()}

## 本轮人类变化
{human.strip() if human and human.strip() else '(无)'}

## 当前 worker 推进说明
{task.strip() or '(尚无当前任务)'}

## 最近一次 worker 结果
以下只是线索，不自动视为事实：
{result.strip() or '(尚无 worker 结果)'}

## Git 机械事实
{_render_git_facts(git_facts)}

请按需使用只读文件与搜索工具核实会改变路线的信息，然后严格按系统提示规定的形状输出。不要提及或依赖任何上一轮 Orchestra 原始输出。"""


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
    import tools.file_tools  # noqa: F401 - registers built-in file tools
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
    human: Optional[str] = None,
    hermes_home: Optional[str | Path] = None,
    max_iterations: int = 12,
    agent_factory: Optional[Callable[..., Any]] = None,
) -> ParsedDecision:
    project_path, control = _require_control(project, hermes_home)
    state = (control / "state.md").read_text(encoding="utf-8")
    task = (control / "task.md").read_text(encoding="utf-8")
    result = (control / "result.md").read_text(encoding="utf-8")
    request = build_decision_request(
        state=state,
        task=task,
        result=result,
        human=human,
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
    if parsed.decision == "开始新任务":
        atomic_write(control / "worker-thread.txt", "")
    return parsed


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
    worker_runner: Callable[..., CodexTurnResult] = run_codex_turn,
    on_notification: Optional[Callable[[dict[str, Any]], None]] = None,
    model: Optional[str] = None,
    timeout_seconds: float = 3600,
) -> Optional[CodexTurnResult]:
    project_path, control = _require_control(project, hermes_home)
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
    else:
        atomic_write(control / "worker-thread.txt", "")

    result = worker_runner(
        prompt=task,
        workspace=project_path,
        resume_thread_id=resume_thread_id,
        model=model,
        sandbox="workspace-write",
        approval="never",
        timeout_seconds=timeout_seconds,
        on_notification=on_notification,
        on_thread_ready=lambda thread_id: atomic_write(
            control / "worker-thread.txt", thread_id + "\n"
        ),
    )
    if result.thread_id:
        atomic_write(control / "worker-thread.txt", result.thread_id + "\n")
    atomic_write(control / "result.md", _worker_result_markdown(result))
    return result


def project_status(
    project: str | Path,
    *,
    hermes_home: Optional[str | Path] = None,
) -> ProjectStatus:
    project_path, control = _require_control(project, hermes_home)
    facts = collect_git_facts(project_path)
    file_times = {
        name: (
            datetime.fromtimestamp((control / name).stat().st_mtime).isoformat(
                timespec="seconds"
            )
            if (control / name).is_file()
            else "不存在"
        )
        for name in CONTROL_FILES
    }
    result = (control / "result.md").read_text(encoding="utf-8").strip()
    status_match = re.match(r"状态[:：]\s*(\S+)", result)
    last_worker_status = status_match.group(1) if status_match else "尚无结果"
    return ProjectStatus(
        project=str(project_path),
        control_dir=str(control),
        decision=(control / "decision.txt").read_text(encoding="utf-8").strip()
        or "尚未决定",
        worker_thread=(control / "worker-thread.txt")
        .read_text(encoding="utf-8")
        .strip()
        or "尚无",
        git_branch=facts.branch,
        git_commit=facts.commit,
        last_worker_status=last_worker_status,
        file_times=file_times,
    )


def format_status(status: ProjectStatus) -> str:
    lines = [
        f"项目：{status.project}",
        f"控制目录：{status.control_dir}",
        f"当前决定：{status.decision}",
        f"worker thread：{status.worker_thread}",
        f"Git：{status.git_branch} @ {status.git_commit}",
        f"最近 worker 状态：{status.last_worker_status}",
        "控制文件更新时间：",
    ]
    lines.extend(f"  {name}: {value}" for name, value in status.file_times.items())
    return "\n".join(lines)
