"""团队分配协议和 fresh 只读项目判断的行为测试。"""

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from hermes_cli.orchestra_v1_control import CONTROL_FILES, initialize_project
from hermes_cli.orchestra_v1_team_decision import (
    TeamAssignment,
    build_team_system_prompt,
    decide_team,
    parse_team_output,
)
from tests.test_orchestra_v1 import FakeAgent, init_git_repo


def output(assignments, decision="推进"):
    return json.dumps(
        {"decision": decision, "state": "当前完整项目判断", "assignments": assignments},
        ensure_ascii=False,
    )


def test_parser_accepts_independent_start_keep_and_stop():
    parsed = parse_team_output(
        output([
            {"slot": 1, "action": "keep"},
            {"slot": 2, "action": "stop", "task": "关键假设失效"},
            {"slot": 4, "action": "start", "task": "整合现有两项结果"},
        ]), active_slots={1, 2},
    )
    assert parsed.assignments == (
        TeamAssignment(1, "keep"), TeamAssignment(2, "stop", "关键假设失效"),
        TeamAssignment(4, "start", "整合现有两项结果"),
    )


@pytest.mark.parametrize("entries, active", [
    ([{"slot": 1, "action": "start", "task": "替换"}], {1}),
    ([{"slot": 1, "action": "keep", "task": "悄悄改任务"}], {1}),
    ([], {1}),
    ([{"slot": 2, "action": "keep"}], set()),
    ([{"slot": 2, "action": "stop"}], set()),
    ([{"slot": 1, "action": "start", "task": ""}], set()),
    ([{"slot": 1, "action": "start", "task": "任务"},
      {"slot": 1, "action": "start", "task": "另一任务"}], set()),
    ([{"slot": 5, "action": "start", "task": "任务"}], set()),
    ([{"slot": True, "action": "start", "task": "任务"}], set()),
    ([{"slot": 1, "action": "resume", "task": "任务"}], set()),
])
def test_parser_rejects_unsafe_or_ambiguous_assignments(entries, active):
    with pytest.raises(ValueError):
        parse_team_output(output(entries), active_slots=active)


@pytest.mark.parametrize("decision", ["等待", "询问人类", "停止"])
def test_terminal_decision_cannot_start_worker(decision):
    with pytest.raises(ValueError):
        parse_team_output(output([
            {"slot": 1, "action": "start", "task": "继续执行"},
        ], decision))
    parsed = parse_team_output(output([
        {"slot": 1, "action": "stop", "task": "结束本次执行"},
    ], decision), active_slots={1})
    assert parsed.decision == decision


def test_parser_accepts_one_json_fence_but_not_extra_prose():
    body = output([])
    assert parse_team_output("```json\n" + body + "\n```").state == "当前完整项目判断"
    with pytest.raises(ValueError):
        parse_team_output("这是我的决定：\n" + body)


@pytest.mark.parametrize("value", ["[]", "{}", '{"decision":[],"state":"状态","assignments":[]}'])
def test_parser_rejects_invalid_top_level(value):
    with pytest.raises(ValueError):
        parse_team_output(value)


def test_team_prompt_replaces_single_worker_protocol_and_limits():
    prompt = build_team_system_prompt(4, 3)
    assert "每轮最多选择一个当前 worker 任务" not in prompt
    assert "决定：<决定值>" not in prompt
    assert "最多同时运行 4 个 worker" in prompt
    assert "全部后代合计最多 3 个并发 subagent" in prompt
    assert "根 worker 加全部后代最多 4 个并发 agent" in prompt
    assert "不能再递归派生" not in prompt
    assert "最多 16 个并发 agent" in prompt
    assert "没有单 worker 一小时超时或固定任务轮数上限" in prompt
    assert "整合不是报告末尾拼接四份总结" in prompt
    assert "人类要求的正确性原义" in prompt


@pytest.fixture
def project_env(tmp_path):
    project = tmp_path / "project"
    init_git_repo(project)
    control = initialize_project(project, "跨领域整合研究", hermes_home=tmp_path / "hermes")
    return project, control


def test_team_decide_is_fresh_scoped_and_does_not_apply_control(project_env):
    project, control = project_env
    before = {name: (control / name).read_text() for name in CONTROL_FILES}
    agents, requests = [], []

    def factory(**kwargs):
        agent = FakeAgent(output([{"slot": 1, "action": "start", "task": "整合研究"}]), requests, **kwargs)
        agents.append(agent)
        return agent

    for _ in range(2):
        parsed = decide_team(project, control=control, context="现场与人类原义", active_slots=set(), agent_factory=factory)
        assert parsed.assignments[0].task == "整合研究"
    assert agents[0].kwargs["session_id"] != agents[1].kwargs["session_id"]
    assert "最多同时运行 4 个 worker" in agents[0].kwargs["ephemeral_system_prompt"]
    assert "现场与人类原义" in requests[0]
    assert before == {name: (control / name).read_text() for name in CONTROL_FILES}
    records = list((control / "history").glob("*-team-decide-*"))
    assert len(records) == 2
    assert all((p / "system.md").is_file() and (p / "output.md").is_file() for p in records)


def test_real_agent_construction_also_receives_team_prompt(project_env, monkeypatch):
    project, control = project_env
    captured = []

    def constructor(**kwargs):
        captured.append(kwargs)
        return FakeAgent(output([]), [], **kwargs)

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=constructor))
    monkeypatch.setattr(
        "hermes_cli.orchestra_v1_team_decision.codex_agent_settings",
        lambda: {"model": "test-model", "api_key": "fake-key"},
    )
    decide_team(project, control=control, context="现场", active_slots=set())
    assert captured[0]["model"] == "test-model"
    assert "最多同时运行 4 个 worker" in captured[0]["ephemeral_system_prompt"]


def test_invalid_output_is_archived_without_replacing_control(project_env):
    project, control = project_env
    before = (control / "state.md").read_text()
    with pytest.raises(ValueError):
        decide_team(
            project, control=control, context="现场", active_slots={1},
            agent_factory=lambda **kwargs: FakeAgent(output([]), [], **kwargs),
        )
    assert (control / "state.md").read_text() == before
    record = next((control / "history").glob("*-team-decide-*"))
    assert (record / "output.md").is_file()
    assert json.loads((record / "metadata.json").read_text())["status"] == "failed"


@pytest.mark.parametrize("status, expected", [(503, "unavailable"), (401, "permanent")])
def test_api_failure_is_not_parsed_as_a_project_decision(project_env, status, expected):
    from hermes_cli.orchestra_v1_team_decision import TeamDecisionUnavailable

    project, control = project_env
    error = f"API call failed after 3 retries: HTTP {status}: upstream error"
    exception = TeamDecisionUnavailable if expected == "unavailable" else RuntimeError
    with pytest.raises(exception, match=f"HTTP {status}"):
        decide_team(
            project, control=control, context="现场", active_slots=set(),
            agent_factory=lambda **kwargs: FakeAgent(error, [], **kwargs),
        )
    assert not (control / "state.md").read_text().strip()
