"""Orchestra v1 的控制材料与项目机械事实。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Optional

from utils import atomic_replace


CONTROL_FILES = (
    "intent.md",
    "state.md",
    "task.md",
    "result.md",
    "decision.txt",
    "worker-thread.txt",
    "last-orchestra-output.md",
)


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
    project_path = resolve_project(project)
    control = (orchestra_home(hermes_home) / project_key(project_path)).resolve()
    try:
        control.relative_to(project_path)
    except ValueError:
        return control
    raise ValueError("Orchestra 控制目录必须位于目标项目目录之外")


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
        "intent.md": goal.strip() + "\n",
        "state.md": "",
        "task.md": "",
        "result.md": "",
        "decision.txt": "",
        "worker-thread.txt": "",
        "last-orchestra-output.md": "",
    }
    for name, content in initial.items():
        atomic_write(control / name, content)
    return control


def require_control(
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


def project_status(
    project: str | Path,
    *,
    hermes_home: Optional[str | Path] = None,
) -> ProjectStatus:
    project_path, control = require_control(project, hermes_home)
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
