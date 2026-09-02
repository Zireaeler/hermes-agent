from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

import hermes_cli.orchestra_v1 as orchestra_v1
from hermes_cli.orchestra_v1 import (
    _path_in_project,
    atomic_write,
    codex_agent_settings,
    decide,
    initialize_project,
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


def test_public_facade_keeps_existing_orchestra_entry_points():
    expected = {
        "CodexTurnResult",
        "DECISIONS",
        "RUN_DECISIONS",
        "atomic_write",
        "collect_git_facts",
        "format_status",
        "initialize_project",
        "parse_orchestra_output",
        "project_control_dir",
        "project_key",
        "project_status",
        "run_codex_turn",
        "run_worker",
    }

    assert all(hasattr(orchestra_v1, name) for name in expected)


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
        pytest.skip("当前平台不支持符号链接")
    with pytest.raises(PermissionError):
        _path_in_project(project.resolve(), str(link))


def test_decide_uses_fresh_sessions_and_reloads_authoritative_intent(tmp_path):
    project = tmp_path / "project"
    init_git_repo(project)
    home = tmp_path / "hermes"
    control = initialize_project(project, "Intent one", hermes_home=home)
    atomic_write(control / "worker-thread.txt", "thread-old\n")
    requests: list[str] = []
    creations: list[FakeAgent] = []
    outputs = iter(
        [
            orchestra_output("开始新任务", state="状态一", task="任务一"),
            orchestra_output("继续当前任务", state="状态二", task="任务二"),
        ]
    )

    def factory(**kwargs: Any) -> FakeAgent:
        agent = FakeAgent(next(outputs), requests, **kwargs)
        creations.append(agent)
        return agent

    first = decide(project, hermes_home=home, agent_factory=factory)
    assert first.decision == "开始新任务"
    assert (control / "worker-thread.txt").read_text(encoding="utf-8") == "thread-old\n"
    assert (control / "intent.md").read_text(encoding="utf-8") == "Intent one\n"

    atomic_write(control / "last-orchestra-output.md", "RAW_ONLY_MARKER\n")
    atomic_write(control / "intent.md", "Intent two\n")
    second = decide(project, hermes_home=home, agent_factory=factory)

    assert second.decision == "继续当前任务"
    assert creations[0].kwargs["session_id"] != creations[1].kwargs["session_id"]
    for agent in creations:
        assert agent.kwargs["skip_context_files"] is True
        assert agent.kwargs["skip_memory"] is True
        assert agent.kwargs["load_soul_identity"] is False
        assert agent.kwargs["enabled_toolsets"] == []
        assert "parent_session_id" not in agent.kwargs
        assert "intent.md 为人类意图唯一来源" in agent.kwargs["ephemeral_system_prompt"]
        assert "不得用“最简单”代替“已经确认正确”" in agent.kwargs[
            "ephemeral_system_prompt"
        ]
    assert "Intent one" in requests[0]
    assert "Intent two" in requests[1]
    assert "RAW_ONLY_MARKER" not in requests[1]
    assert "状态一" in requests[1]
    assert "任务一" in requests[1]
    assert (control / "intent.md").read_text(encoding="utf-8") == "Intent two\n"
    assert (control / "state.md").read_text(encoding="utf-8") == "状态二\n"
    assert (control / "task.md").read_text(encoding="utf-8") == "任务二\n"

    from tools.registry import registry

    assert registry.get_tool_names_for_toolset("orchestra_v1") == []


def test_parse_failure_saves_raw_output_without_overwriting_control_materials(tmp_path):
    project = tmp_path / "project"
    init_git_repo(project)
    home = tmp_path / "hermes"
    control = initialize_project(project, "Original intent", hermes_home=home)
    atomic_write(control / "state.md", "Original judgment\n")
    atomic_write(control / "task.md", "Original task\n")

    def factory(**_kwargs: Any) -> FakeAgent:
        return FakeAgent("malformed orchestra output", [])

    with pytest.raises(ValueError):
        decide(project, hermes_home=home, agent_factory=factory)

    assert (control / "intent.md").read_text(encoding="utf-8") == "Original intent\n"
    assert (control / "state.md").read_text(encoding="utf-8") == "Original judgment\n"
    assert (control / "task.md").read_text(encoding="utf-8") == "Original task\n"
    assert (control / "last-orchestra-output.md").read_text(
        encoding="utf-8"
    ) == "malformed orchestra output"


def test_decide_requires_explicit_intent_file_without_migration(tmp_path):
    project = tmp_path / "project"
    init_git_repo(project)
    home = tmp_path / "hermes"
    control = initialize_project(project, "Intent", hermes_home=home)
    (control / "intent.md").unlink()

    with pytest.raises(FileNotFoundError, match="intent.md"):
        decide(project, hermes_home=home, agent_factory=lambda **_kwargs: None)
