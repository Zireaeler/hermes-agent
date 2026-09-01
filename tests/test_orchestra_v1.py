from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

from hermes_cli.codex_worker import CodexTurnResult
from hermes_cli.orchestra_v1 import (
    DECISIONS,
    atomic_write,
    codex_agent_settings,
    decide,
    initialize_project,
    parse_orchestra_output,
    project_control_dir,
    project_key,
    project_status,
    run_worker,
    _path_in_project,
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


def orchestra_output(
    decision: str,
    *,
    state: str = "当前完整状态",
    task: str = "当前完整任务",
) -> str:
    return f"""决定：{decision}

# 项目状态
{state}

# worker 推进说明
{task}
"""


class FakeAgent:
    def __init__(self, output: str, requests: list[str], **kwargs: Any) -> None:
        self.output = output
        self.requests = requests
        self.kwargs = kwargs
        self.tools = [
            {"function": {"name": "read_file"}},
            {"function": {"name": "write_file"}},
            {"function": {"name": "patch"}},
            {"function": {"name": "search_files"}},
        ]
        self.valid_tool_names = {
            tool["function"]["name"] for tool in self.tools
        }

    def chat(self, request: str) -> str:
        self.requests.append(request)
        tool_names = {
            tool["function"]["name"] for tool in self.tools
        }
        assert tool_names == self.valid_tool_names
        assert len(tool_names) == 2
        assert any(name.startswith("orchestra_read_") for name in tool_names)
        assert any(name.startswith("orchestra_search_") for name in tool_names)
        return self.output


def test_project_key_is_stable_for_same_canonical_path(tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    assert project_key(project) == project_key(project / ".")


def test_codex_agent_settings_reuses_codex_model_source(tmp_path):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'model = "gpt-test"\n'
        'model_provider = "local"\n'
        '[model_providers.local]\n'
        'base_url = "http://localhost:9999/v1"\n',
        encoding="utf-8",
    )
    (codex_home / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "test-key"}),
        encoding="utf-8",
    )

    assert codex_agent_settings(codex_home) == {
        "base_url": "http://localhost:9999/v1",
        "api_key": "test-key",
        "provider": "openai",
        "api_mode": "codex_responses",
        "model": "gpt-test",
    }


def test_repository_path_rejects_absolute_and_symlink_escapes(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    inside = project / "inside.txt"
    inside.write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")

    assert _path_in_project(project.resolve(), "inside.txt") == inside.resolve()
    with pytest.raises(PermissionError):
        _path_in_project(project.resolve(), str(outside))

    link = project / "outside-link"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(PermissionError):
        _path_in_project(project.resolve(), str(link))


def test_initialize_creates_minimal_materials_and_refuses_silent_overwrite(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "hermes"

    control = initialize_project(project, "Build the product", hermes_home=home)

    assert {path.name for path in control.iterdir()} == {
        "state.md",
        "task.md",
        "result.md",
        "decision.txt",
        "worker-thread.txt",
        "last-orchestra-output.md",
    }
    assert "Build the product" in (control / "state.md").read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        initialize_project(project, "Replace it", hermes_home=home)
    assert "Build the product" in (control / "state.md").read_text(encoding="utf-8")


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
        pytest.skip("symlinks unavailable")

    atomic_write(target, "new")

    assert target.is_symlink()
    assert backing.read_text(encoding="utf-8") == "new"


@pytest.mark.parametrize("decision", DECISIONS)
def test_parser_accepts_each_mechanical_decision(decision):
    parsed = parse_orchestra_output(orchestra_output(decision))

    assert parsed.decision == decision
    assert parsed.state == "当前完整状态"
    assert parsed.task == "当前完整任务"


def test_parser_rejects_text_before_decision():
    with pytest.raises(ValueError, match="开头"):
        parse_orchestra_output("说明\n" + orchestra_output("等待"))


def test_decide_uses_fresh_sessions_and_omits_previous_raw_output(tmp_path):
    project = tmp_path / "project"
    init_git_repo(project)
    home = tmp_path / "hermes"
    control = initialize_project(project, "Goal", hermes_home=home)
    requests: list[str] = []
    creations: list[FakeAgent] = []
    outputs = iter(
        [
            orchestra_output(
                "开始新任务",
                state="状态一",
                task="任务一",
            ),
            orchestra_output("继续当前任务", state="状态二", task="任务二"),
        ]
    )

    def factory(**kwargs: Any) -> FakeAgent:
        agent = FakeAgent(next(outputs), requests, **kwargs)
        creations.append(agent)
        return agent

    first = decide(project, hermes_home=home, agent_factory=factory)
    atomic_write(control / "last-orchestra-output.md", "RAW_ONLY_MARKER\n")
    second = decide(project, hermes_home=home, agent_factory=factory)

    assert first.decision == "开始新任务"
    assert second.decision == "继续当前任务"
    assert creations[0].kwargs["session_id"] != creations[1].kwargs["session_id"]
    for agent in creations:
        assert agent.kwargs["skip_context_files"] is True
        assert agent.kwargs["skip_memory"] is True
        assert agent.kwargs["load_soul_identity"] is False
        assert agent.kwargs["enabled_toolsets"] == []
        assert "parent_session_id" not in agent.kwargs
    assert "RAW_ONLY_MARKER" not in requests[1]
    assert "状态一" in requests[1]
    assert "任务一" in requests[1]
    assert (control / "state.md").read_text(encoding="utf-8") == "状态二\n"
    assert (control / "task.md").read_text(encoding="utf-8") == "任务二\n"
    from tools.registry import registry

    assert registry.get_tool_names_for_toolset("orchestra_v1") == []


def test_parse_failure_saves_raw_output_without_overwriting_state_or_task(tmp_path):
    project = tmp_path / "project"
    init_git_repo(project)
    home = tmp_path / "hermes"
    control = initialize_project(project, "Original goal", hermes_home=home)
    atomic_write(control / "task.md", "Original task\n")

    def factory(**_kwargs: Any) -> FakeAgent:
        return FakeAgent("malformed orchestra output", [])

    with pytest.raises(ValueError):
        decide(project, hermes_home=home, agent_factory=factory)

    assert "Original goal" in (control / "state.md").read_text(encoding="utf-8")
    assert (control / "task.md").read_text(encoding="utf-8") == "Original task\n"
    assert (
        control / "last-orchestra-output.md"
    ).read_text(encoding="utf-8") == "malformed orchestra output"


def test_new_task_clears_old_thread_then_saves_new_thread_and_result(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "hermes"
    control = initialize_project(project, "Goal", hermes_home=home)
    atomic_write(control / "decision.txt", "开始新任务\n")
    atomic_write(control / "task.md", "Do new work\n")
    atomic_write(control / "worker-thread.txt", "stale-thread\n")
    calls: list[dict[str, Any]] = []

    def runner(**kwargs: Any) -> CodexTurnResult:
        assert (control / "worker-thread.txt").read_text(encoding="utf-8") == ""
        calls.append(kwargs)
        kwargs["on_thread_ready"]("thread-new")
        assert (
            control / "worker-thread.txt"
        ).read_text(encoding="utf-8") == "thread-new\n"
        return CodexTurnResult(
            thread_id="thread-new",
            turn_id="turn-1",
            status="completed",
            final_text="done",
        )

    result = run_worker(project, hermes_home=home, worker_runner=runner)

    assert result is not None and result.status == "completed"
    assert calls[0]["resume_thread_id"] is None
    assert (control / "worker-thread.txt").read_text(encoding="utf-8") == "thread-new\n"
    saved = (control / "result.md").read_text(encoding="utf-8")
    assert "状态：completed" in saved
    assert "done" in saved


def test_continue_task_requires_and_resumes_current_thread(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "hermes"
    control = initialize_project(project, "Goal", hermes_home=home)
    atomic_write(control / "decision.txt", "继续当前任务\n")
    atomic_write(control / "task.md", "Continue work\n")

    with pytest.raises(RuntimeError, match="需要已有 worker thread"):
        run_worker(project, hermes_home=home, worker_runner=lambda **_kwargs: None)

    atomic_write(control / "worker-thread.txt", "thread-existing\n")
    seen: list[str | None] = []

    def runner(**kwargs: Any) -> CodexTurnResult:
        seen.append(kwargs["resume_thread_id"])
        return CodexTurnResult(
            thread_id="thread-existing",
            turn_id="turn-2",
            status="completed",
            final_text="continued",
        )

    run_worker(project, hermes_home=home, worker_runner=runner)
    assert seen == ["thread-existing"]


@pytest.mark.parametrize("decision", ["等待", "询问人类", "停止"])
def test_non_running_decisions_do_not_start_worker(tmp_path, decision):
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "hermes"
    control = initialize_project(project, "Goal", hermes_home=home)
    atomic_write(control / "decision.txt", decision + "\n")
    atomic_write(control / "task.md", "No worker now\n")

    def should_not_run(**_kwargs: Any) -> CodexTurnResult:
        raise AssertionError("worker must not start")

    assert run_worker(project, hermes_home=home, worker_runner=should_not_run) is None


def test_missing_debug_output_does_not_block_status_or_worker(tmp_path):
    project = tmp_path / "project"
    init_git_repo(project)
    home = tmp_path / "hermes"
    control = initialize_project(project, "Goal", hermes_home=home)
    atomic_write(control / "decision.txt", "等待\n")
    (control / "last-orchestra-output.md").unlink()

    assert run_worker(project, hermes_home=home) is None
    assert project_status(project, hermes_home=home).file_times[
        "last-orchestra-output.md"
    ] == "不存在"


def test_status_reports_mechanical_worker_result(tmp_path):
    project = tmp_path / "project"
    init_git_repo(project)
    home = tmp_path / "hermes"
    control = initialize_project(project, "Goal", hermes_home=home)
    atomic_write(control / "decision.txt", "等待\n")
    atomic_write(control / "result.md", "状态：completed\n")

    status = project_status(project, hermes_home=home)

    assert status.decision == "等待"
    assert status.worker_thread == "尚无"
    assert status.last_worker_status == "completed"
    assert set(status.file_times) == {
        "state.md",
        "task.md",
        "result.md",
        "decision.txt",
        "worker-thread.txt",
        "last-orchestra-output.md",
    }
