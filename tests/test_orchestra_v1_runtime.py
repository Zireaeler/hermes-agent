"""自动运行的项目边界与 worker 交互测试。"""

import json
from pathlib import Path

import pytest

from hermes_cli.orchestra_v1_codex import CodexTurnResult
from hermes_cli.orchestra_v1_codex import _run_codex_turn
from hermes_cli.orchestra_v1_control import atomic_write, initialize_project
from hermes_cli.orchestra_v1_runtime import run_project
from tests.test_orchestra_v1 import FakeAgent, init_git_repo, orchestra_output
from tests.test_orchestra_v1_codex import FakeClient


@pytest.fixture
def project_env(tmp_path):
    project = tmp_path / "project"
    init_git_repo(project)
    home = tmp_path / "hermes"
    control = initialize_project(project, "完成项目目标", hermes_home=home)
    return project, home, control


def scripted_agents(*decisions):
    outputs = iter(decisions)
    requests = []

    def factory(**kwargs):
        return FakeAgent(orchestra_output(next(outputs)), requests, **kwargs)

    return factory, requests


def test_automatically_reassesses_and_resumes_or_replaces_thread(project_env):
    project, home, control = project_env
    factory, requests = scripted_agents(
        "开始新任务", "继续当前任务", "开始新任务", "停止"
    )
    resumes = []

    def worker(**kwargs):
        resumes.append(kwargs["resume_thread_id"])
        thread = kwargs["resume_thread_id"] or f"thread-{len(resumes)}"
        kwargs["on_thread_ready"](thread)
        kwargs["on_notification"]({
            "method": "item/completed",
            "params": {"evidence": "原始工具结果"},
        })
        return CodexTurnResult(
            thread_id=thread, status="completed", final_text=f"本轮结果 {len(resumes)}"
        )

    result = run_project(
        project, hermes_home=home, agent_factory=factory, worker_runner=worker
    )

    assert result.reason == "停止"
    assert result.worker_turns == 3
    assert resumes == [None, "thread-1", None]
    assert "本轮结果 1" in requests[1]
    assert "本轮结果 3" in requests[3]
    assert "worker_completed" in requests[1]
    assert "原始工具结果" in requests[1]
    assert (control / "intent.md").read_text() == "完成项目目标\n"
    assert len(list((control / "history").glob("*-worker-*/notifications.jsonl"))) == 3


@pytest.mark.parametrize("decision", ["等待", "询问人类", "停止"])
def test_terminal_decision_returns_without_worker(project_env, decision):
    project, home, _ = project_env
    factory, _ = scripted_agents(decision)

    def worker(**_kwargs):
        raise AssertionError("不应运行")

    result = run_project(
        project, hermes_home=home, agent_factory=factory, worker_runner=worker
    )
    assert result.reason == decision
    assert result.worker_turns == 0


def test_turn_budget_absorbs_last_result_without_starting_more_work(project_env):
    project, home, _ = project_env
    factory, requests = scripted_agents("开始新任务", "开始新任务")
    calls = []

    def worker(**_kwargs):
        calls.append(True)
        return CodexTurnResult(
            thread_id="t", status="completed", final_text="最后一次结果"
        )

    result = run_project(
        project,
        hermes_home=home,
        agent_factory=factory,
        worker_runner=worker,
        max_worker_turns=1,
    )
    assert result.reason == "budget_exhausted"
    assert len(calls) == 1
    assert "最后一次结果" in requests[-1]
    assert "已执行 worker turn：1；上限：1" in requests[-1]


def test_periodic_observation_never_overwrites_live_task(project_env):
    project, home, control = project_env
    factory, requests = scripted_agents(
        "开始新任务", "继续当前任务", "开始新任务", "停止"
    )
    now = [0.0]

    def worker(**kwargs):
        kwargs["on_thread_ready"]("thread-live")
        before = {
            name: (control / name).read_text()
            for name in ("state.md", "task.md", "decision.txt")
        }
        now[0] = 10
        assert kwargs["on_poll"]() is None
        now[0] = 20
        assert kwargs["on_poll"]() == "review_requested"
        assert {name: (control / name).read_text() for name in before} == before
        return CodexTurnResult(
            thread_id="thread-live",
            status="interrupted",
            control_reason="review_requested",
            final_text="实际中断结果",
        )

    result = run_project(
        project,
        hermes_home=home,
        agent_factory=factory,
        worker_runner=worker,
        check_interval=10,
        clock=lambda: now[0],
    )
    assert result.reason == "停止"
    assert result.worker_turns == 1
    assert len(requests) == 4
    assert "本次仅观察" in requests[1]
    assert "实际中断结果" in requests[-1]
    assert "review_requested" in requests[-1]


def test_wall_budget_interrupts_active_worker_and_absorbs_result(project_env):
    project, home, _ = project_env
    factory, requests = scripted_agents("开始新任务", "开始新任务")
    now = [0.0]

    def worker(**kwargs):
        assert kwargs["timeout_seconds"] == 5
        now[0] = 6
        assert kwargs["on_poll"]() == "budget_exhausted"
        return CodexTurnResult(
            thread_id="t",
            status="interrupted",
            control_reason="budget_exhausted",
            final_text="预算结束现场",
        )

    result = run_project(
        project,
        hermes_home=home,
        agent_factory=factory,
        worker_runner=worker,
        max_seconds=5,
        clock=lambda: now[0],
    )
    assert result.reason == "budget_exhausted"
    assert result.worker_turns == 1
    assert "预算结束现场" in requests[-1]


def test_changed_intent_returns_control_and_is_reloaded(project_env):
    project, home, control = project_env
    factory, requests = scripted_agents("开始新任务", "停止")

    def worker(**kwargs):
        atomic_write(control / "intent.md", "人类改变目标\n")
        assert kwargs["on_poll"]() == "intent_changed"
        return CodexTurnResult(
            thread_id="t", status="interrupted", control_reason="intent_changed"
        )

    result = run_project(
        project, hermes_home=home, agent_factory=factory, worker_runner=worker
    )
    assert result.reason == "停止"
    assert "人类改变目标" in requests[-1]


@pytest.mark.parametrize("status", ["failed", "timed_out"])
def test_failures_are_reassessed_but_retries_are_bounded(project_env, status):
    project, home, _ = project_env
    factory, requests = scripted_agents(*(["开始新任务"] * 4))
    result = run_project(
        project,
        hermes_home=home,
        agent_factory=factory,
        worker_runner=lambda **_: CodexTurnResult(status=status, error="明确故障"),
    )
    assert result.reason == "worker_failure_limit"
    assert result.worker_turns == 3
    assert "明确故障" in requests[-1]


def test_unconfirmed_termination_cannot_start_replacement_worker(project_env):
    project, home, _ = project_env
    factory, requests = scripted_agents("开始新任务", "开始新任务")
    result = run_project(
        project,
        hermes_home=home,
        agent_factory=factory,
        worker_runner=lambda **_: CodexTurnResult(
            status="failed",
            control_reason="review_requested",
            termination_confirmed=False,
        ),
    )
    assert result.reason == "worker_unconfirmed"
    assert result.worker_turns == 1
    assert "worker_unconfirmed" in requests[-1]


def test_user_interrupt_does_not_wake_orchestra_again(project_env):
    project, home, _ = project_env
    factory, requests = scripted_agents("开始新任务")
    result = run_project(
        project,
        hermes_home=home,
        agent_factory=factory,
        worker_runner=lambda **_: CodexTurnResult(status="interrupted"),
    )
    assert result.reason == "interrupted"
    assert len(requests) == 1
    assert (
        json.loads((Path(result.history_dir) / "metadata.json").read_text())["status"]
        == "interrupted"
    )


def test_slow_decision_consumes_budget_without_starting_worker(project_env):
    project, home, _ = project_env
    now = [0.0]

    class SlowAgent(FakeAgent):
        def chat(self, request):
            now[0] = 6
            return super().chat(request)

    def worker(**_kwargs):
        raise AssertionError("决策已用尽预算，不应启动 worker")

    result = run_project(
        project,
        hermes_home=home,
        max_seconds=5,
        clock=lambda: now[0],
        worker_runner=worker,
        agent_factory=lambda **kw: SlowAgent(orchestra_output("开始新任务"), [], **kw),
    )
    assert result.reason == "budget_exhausted"
    assert result.worker_turns == 0


def test_invalid_observation_stops_active_worker_and_does_not_retry(project_env):
    project, home, control = project_env
    now = [0.0]
    outputs = iter([orchestra_output("开始新任务"), "无效观察输出"])
    client = FakeClient()

    def worker(**kwargs):
        now[0] = 2
        return _run_codex_turn(**kwargs, client_factory=lambda **_: client)

    result = run_project(
        project,
        hermes_home=home,
        check_interval=1,
        clock=lambda: now[0],
        worker_runner=worker,
        agent_factory=lambda **kw: FakeAgent(next(outputs), [], **kw),
    )
    assert result.reason == "observer_failed"
    assert result.worker_turns == 1
    assert "turn/interrupt" in [method for method, _ in client.requests]
    assert (control / "task.md").read_text() == "当前完整任务\n"


@pytest.mark.parametrize(
    "options",
    [
        {"max_seconds": 0},
        {"max_seconds": float("nan")},
        {"max_worker_turns": 0},
        {"check_interval": -1},
        {"worker_timeout": float("inf")},
    ],
)
def test_invalid_runtime_limits_do_not_call_models(project_env, options):
    project, home, _ = project_env
    with pytest.raises(ValueError):
        run_project(project, hermes_home=home, **options)
