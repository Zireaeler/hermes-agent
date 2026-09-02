from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from hermes_cli.orchestra_v1_control import (
    atomic_write,
    initialize_project,
    project_key,
    project_status,
)


def init_git_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True,
    )
    (path / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "initial"], check=True)


def test_project_key_is_stable_for_same_canonical_path(tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    assert project_key(project) == project_key(project / ".")


def test_initialize_creates_intent_separately_and_refuses_silent_overwrite(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "hermes"

    control = initialize_project(project, "Build the product", hermes_home=home)

    assert {path.name for path in control.iterdir()} == {
        "intent.md",
        "state.md",
        "task.md",
        "result.md",
        "decision.txt",
        "worker-thread.txt",
        "last-orchestra-output.md",
    }
    assert (control / "intent.md").read_text(encoding="utf-8") == "Build the product\n"
    assert (control / "state.md").read_text(encoding="utf-8") == ""

    with pytest.raises(FileExistsError):
        initialize_project(project, "Replace it", hermes_home=home)
    assert (control / "intent.md").read_text(encoding="utf-8") == "Build the product\n"
    assert (control / "state.md").read_text(encoding="utf-8") == ""


def test_control_directory_must_stay_outside_worker_workspace(tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(ValueError, match="目标项目目录之外"):
        initialize_project(
            project,
            "Goal",
            hermes_home=project / ".hermes",
        )


def test_control_directory_rejects_symlink_into_worker_workspace(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "hermes"
    orchestra = home / "orchestra"
    orchestra.mkdir(parents=True)
    inside_control = project / "inside-control"
    inside_control.mkdir()
    link = orchestra / project_key(project)
    try:
        link.symlink_to(inside_control, target_is_directory=True)
    except OSError:
        pytest.skip("当前平台不支持符号链接")

    with pytest.raises(ValueError, match="目标项目目录之外"):
        initialize_project(project, "Goal", hermes_home=home)


def test_atomic_write_replaces_target_without_leaving_temporary_file(tmp_path):
    target = tmp_path / "state.md"
    target.write_text("old", encoding="utf-8")

    atomic_write(target, "new")

    assert target.read_text(encoding="utf-8") == "new"
    assert list(tmp_path.glob(".state.md.*.tmp")) == []


def test_atomic_write_preserves_symlinked_control_file(tmp_path):
    backing = tmp_path / "backing.md"
    backing.write_text("old", encoding="utf-8")
    target = tmp_path / "state.md"
    try:
        target.symlink_to(backing)
    except OSError:
        pytest.skip("当前平台不支持符号链接")

    atomic_write(target, "new")

    assert target.is_symlink()
    assert backing.read_text(encoding="utf-8") == "new"


def test_status_reports_intent_and_missing_debug_output(tmp_path):
    project = tmp_path / "project"
    init_git_repo(project)
    home = tmp_path / "hermes"
    control = initialize_project(project, "Goal", hermes_home=home)
    atomic_write(control / "decision.txt", "等待\n")
    atomic_write(control / "result.md", "状态：completed\n")
    (control / "last-orchestra-output.md").unlink()

    status = project_status(project, hermes_home=home)

    assert status.decision == "等待"
    assert status.worker_thread == "尚无"
    assert status.last_worker_status == "completed"
    assert status.file_times["intent.md"] != "不存在"
    assert status.file_times["last-orchestra-output.md"] == "不存在"
    assert set(status.file_times) == {
        "intent.md",
        "state.md",
        "task.md",
        "result.md",
        "decision.txt",
        "worker-thread.txt",
        "last-orchestra-output.md",
    }
