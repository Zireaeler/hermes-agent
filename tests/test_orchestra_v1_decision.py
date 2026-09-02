from __future__ import annotations

import pytest

from hermes_cli.orchestra_v1_control import GitFacts
from hermes_cli.orchestra_v1_decision import (
    DECISIONS,
    build_decision_request,
    parse_orchestra_output,
)


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


def git_facts() -> GitFacts:
    return GitFacts(
        root="/tmp/project",
        branch="main",
        commit="abc123",
        status_short="",
        diff_stat="",
        recent_commits="abc123 initial",
    )


@pytest.mark.parametrize("decision", DECISIONS)
def test_parser_accepts_each_mechanical_decision(decision):
    parsed = parse_orchestra_output(orchestra_output(decision))

    assert parsed.decision == decision
    assert parsed.state == "当前完整状态"
    assert parsed.task == "当前完整任务"


def test_parser_rejects_text_before_decision():
    with pytest.raises(ValueError, match="开头"):
        parse_orchestra_output("说明\n" + orchestra_output("等待"))


def test_decision_request_separates_authoritative_intent_from_state():
    request = build_decision_request(
        intent="唯一人类意图标记",
        state="Orchestra 当前判断标记",
        task="当前任务",
        result="worker 结果",
        decision="开始新任务",
        git_facts=git_facts(),
    )

    assert "## 人类意图（唯一来源，只读）" in request
    assert "唯一人类意图标记" in request
    assert "## 当前项目判断" in request
    assert "Orchestra 当前判断标记" in request
    assert "## 当前机械决定" in request
    assert "开始新任务" in request
    assert "本轮人类变化" not in request
    assert "以下只是线索，不自动视为事实" in request


def test_decision_request_marks_empty_state_without_inventing_intent():
    request = build_decision_request(
        intent="保持真实目标",
        state="",
        task="",
        result="",
        decision="",
        git_facts=git_facts(),
    )

    assert "保持真实目标" in request
    assert "(尚无当前项目判断)" in request
    assert "(尚无当前任务)" in request
    assert "(尚无 worker 结果)" in request
