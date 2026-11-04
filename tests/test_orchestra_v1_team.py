"""有限团队的并发、交回控制、预算和终态边界。"""

import json
from pathlib import Path
import threading
import time

import pytest

from hermes_cli.orchestra_v1_codex import CodexTurnResult
from hermes_cli.orchestra_v1_control import atomic_write, initialize_project
from hermes_cli.orchestra_v1_team import run_team_project
from hermes_cli.orchestra_v1_team_decision import (
    TeamAssignment, TeamDecision, TeamDecisionFormatError, TeamDecisionUnavailable,
)
from tests.test_orchestra_v1 import init_git_repo


@pytest.fixture
def env(tmp_path):
    project = tmp_path / "project"
    init_git_repo(project)
    home = tmp_path / "hermes"
    control = initialize_project(project, "整合研究，允许4个worker各3个子代理", hermes_home=home)
    return project, home, control


def assignment(slot, action="start", task=None):
    return TeamAssignment(slot, action, task if task is not None else f"完整研究任务{slot}" if action == "start" else "")


def decision(*assignments, kind="推进", state="当前完整项目判断"):
    return TeamDecision(kind, state, tuple(assignments))


def wait_cancel(kwargs):
    while not kwargs["on_poll"]():
        time.sleep(0.002)
    return CodexTurnResult(status="interrupted", control_reason="team_review_requested")


def run(env, decider, worker, **kwargs):
    project, home, _ = env
    return run_team_project(
        project, hermes_home=home, output_dir="new-research",
        decision_runner=decider, worker_runner=worker,
        max_seconds=15, check_interval=0.02, **kwargs,
    )


def test_four_workers_overlap_and_early_return_is_absorbed_without_waiting_all(env):
    barrier = threading.Barrier(4)
    busy = set()
    lock = threading.Lock()
    peak = 0
    rounds = 0
    observed = []

    def worker(**kwargs):
        nonlocal peak
        assert kwargs["timeout_seconds"] is None
        assert kwargs["max_subagents"] == 3
        assert kwargs["research_network"] is True
        assert kwargs["env"] == {"HERMES_KANBAN_TASK": ""}
        workspace = Path(kwargs["workspace"])
        slot = int(workspace.parent.name.rsplit("-", 1)[1])
        with lock:
            busy.add(slot)
            peak = max(peak, len(busy))
        barrier.wait(timeout=3)
        if slot != 1:
            result = wait_cancel(kwargs)
        else:
            result = CodexTurnResult(status="completed", final_text="关键新认识")
        with lock:
            busy.remove(slot)
        return result

    def decider(*args, **kwargs):
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            return decision(*(assignment(i) for i in range(1, 5)))
        observed.append(kwargs["context"])
        active = kwargs["active_slots"]
        if "关键新认识" not in kwargs["context"]:
            return decision(*(assignment(i, "keep") for i in active))
        return decision(*(assignment(i, "stop") for i in active), kind="停止")

    result = run(env, decider, worker)
    assert result.reason == "停止"
    assert result.worker_turns == 4
    assert peak == 4
    assert any("关键新认识" in context for context in observed)
    status = json.loads((env[2] / "team-status.json").read_text())
    assert status["active"] == []
    assert len(status["finished"]) == 4
    assert all("result" not in row for row in status["finished"])
    assert (env[2] / "intent.md").read_text().startswith("整合研究")


def test_stop_replacement_waits_for_real_terminal(env):
    starts = 0
    old_finished = threading.Event()
    requested = False

    def worker(**kwargs):
        nonlocal starts
        starts += 1
        if starts == 1:
            result = wait_cancel(kwargs)
            time.sleep(0.02)
            old_finished.set()
            return result
        assert old_finished.is_set()
        return CodexTurnResult(status="completed", final_text="替换任务结果")

    def decider(*args, **kwargs):
        nonlocal requested
        active = kwargs["active_slots"]
        if "替换任务结果" in kwargs["context"]:
            return decision(*(assignment(i, "stop") for i in active), kind="停止")
        if active:
            requested = True
            return decision(assignment(1, "stop", "关键假设失效，换方法"))
        return decision(assignment(1))

    result = run(env, decider, worker)
    assert requested
    assert result.worker_turns == 2
    assert result.reason == "停止"


def test_unknown_terminal_during_decision_prevents_dispatch_and_reentry(env):
    release = threading.Event()
    ended = threading.Event()
    calls = 0
    starts = []

    def worker(**kwargs):
        starts.append(kwargs["workspace"])
        release.wait(timeout=3)
        ended.set()
        return CodexTurnResult(status="failed", termination_confirmed=False)

    def decider(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return decision(assignment(1))
        release.set()
        assert ended.wait(timeout=3)
        time.sleep(0.01)
        return decision(assignment(1, "keep"), assignment(2))

    result = run(env, decider, worker)
    assert result.reason == "worker_unconfirmed"
    assert len(starts) == 1
    assert calls == 2
    with pytest.raises(RuntimeError, match="终态未知"):
        run(env, decider, worker)


def test_budget_during_decision_never_launches_worker(env):
    current = [0.0]
    contexts = []

    def decider(*args, **kwargs):
        contexts.append(kwargs["context"])
        if "唤醒原因：final_absorption:" in kwargs["context"]:
            return decision(kind="等待")
        current[0] = 100
        return decision(assignment(1))

    def worker(**kwargs):
        raise AssertionError("预算到期后不得启动")

    result = run(env, decider, worker, clock=lambda: current[0])
    assert result.reason == "budget_exhausted"
    assert result.worker_turns == 0
    assert any("禁止start" in context for context in contexts)


def test_final_absorption_rejects_changed_intent(env):
    control = env[2]

    def decider(*args, **kwargs):
        if "唤醒原因：final_absorption:" in kwargs["context"]:
            atomic_write(control / "intent.md", "用户最新方向\n")
            return decision(kind="停止", state="不能应用的旧判断")
        return decision(kind="停止", state="已应用判断")

    result = run(env, decider, lambda **kwargs: None)
    assert result.reason == "停止"
    assert (control / "state.md").read_text() == "已应用判断\n"
    assert (Path(result.history_dir) / "final-absorption-error.txt").is_file()
    assert (control / "intent.md").read_text() == "用户最新方向\n"


def test_stop_file_collects_workers_and_never_restarts(env):
    calls = 0

    def worker(**kwargs):
        (env[2] / "STOP").touch()
        return wait_cancel(kwargs)

    def decider(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return decision(assignment(1))
        return decision(kind="等待")

    result = run(env, decider, worker)
    assert result.reason == "user_stop_requested"
    assert result.worker_turns == 1
    assert json.loads((env[2] / "team-status.json").read_text())["active"] == []


def test_wait_keeps_active_research_and_absorbs_its_natural_result(env):
    waited = threading.Event()
    returned = threading.Event()
    wait_count = 0
    contexts = []

    def worker(**kwargs):
        assert waited.wait(timeout=3)
        assert kwargs["on_poll"]() is None
        returned.set()
        return CodexTurnResult(status="completed", final_text="等待后自然完成的整合证据")

    def decider(*args, **kwargs):
        nonlocal wait_count
        contexts.append(kwargs["context"])
        if not kwargs["active_slots"]:
            if not returned.is_set():
                return decision(assignment(1))
            return decision(kind="停止")
        wait_count += 1
        waited.set()
        return decision(assignment(1, "keep"), kind="等待", state="等待既有研究，不是运行结束")

    result = run(env, decider, worker)
    assert wait_count >= 1
    assert result.reason == "停止"
    assert result.worker_turns == 1
    assert any("等待后自然完成的整合证据" in c for c in contexts)
    status = json.loads((env[2] / "team-status.json").read_text())
    assert status["finished"][0]["status"] == "completed"


def test_wait_without_active_work_exits_without_starting_worker(env):
    def worker(**kwargs):
        raise AssertionError("没有活动研究的等待不得启动worker")

    result = run(env, lambda *args, **kwargs: decision(kind="等待"), worker)
    assert result.reason == "等待"
    assert result.worker_turns == 0


@pytest.mark.parametrize("error_type", [TeamDecisionUnavailable, TeamDecisionFormatError])
def test_unavailable_decision_does_not_cancel_active_worker(env, error_type):
    project, home, control = env
    current = [0.0]
    deferred = threading.Event()
    events = []
    calls = 0

    def worker(**kwargs):
        while not deferred.wait(timeout=0.002):
            current[0] += 0.1
        assert kwargs["on_poll"]() is None
        current[0] += 60
        return CodexTurnResult(status="completed", final_text="服务中断期间自然完成的研究")

    def decider(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return decision(assignment(1))
        if calls == 2:
            raise error_type("临时模型服务或格式失败")
        if "服务中断期间自然完成的研究" in kwargs["context"]:
            return decision(kind="停止")
        return decision(*(assignment(i, "keep") for i in kwargs["active_slots"]))

    def event(value):
        events.append(value)
        if value["kind"] == "team_decision_deferred":
            deferred.set()

    result = run_team_project(
        project, hermes_home=home, output_dir="new-research",
        decision_runner=decider, worker_runner=worker,
        max_seconds=3600, check_interval=0.02,
        clock=lambda: current[0], on_event=event,
    )
    assert result.reason == "停止"
    assert result.worker_turns == 1
    assert any(e["kind"] == "team_decision_deferred" for e in events)
    status = json.loads((control / "team-status.json").read_text())
    assert status["finished"][0]["status"] == "completed"
    assert status["decision_failures"] == 0


def test_budget_remains_effective_while_model_service_unavailable(env):
    current = [0.0]

    def decider(*args, **kwargs):
        if "唤醒原因：final_absorption:" in kwargs["context"]:
            return decision(kind="等待")
        raise TeamDecisionUnavailable("HTTP 503: server_is_overloaded")

    def event(value):
        if value["kind"] == "team_decision_deferred":
            current[0] = 100

    def worker(**kwargs):
        raise AssertionError("不可用时不能制造worker启动")

    result = run(env, decider, worker, clock=lambda: current[0], on_event=event)
    assert result.reason == "budget_exhausted"
    assert result.worker_turns == 0


@pytest.mark.parametrize("limits", [{"max_workers": 5}, {"max_subagents": 4}, {"max_workers": True}])
def test_invalid_limits_never_start(env, limits):
    with pytest.raises(ValueError):
        run(env, lambda **kwargs: None, lambda **kwargs: None, **limits)
