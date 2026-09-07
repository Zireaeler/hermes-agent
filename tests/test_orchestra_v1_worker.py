from __future__ import annotations

from typing import Any

import pytest

import hermes_cli.orchestra_v1_worker as worker_module
from hermes_cli.orchestra_v1_codex import CodexTurnResult
from hermes_cli.orchestra_v1_control import atomic_write, initialize_project
from hermes_cli.orchestra_v1_worker import build_worker_prompt, run_worker


def test_worker_prompt_keeps_task_and_fixed_scope_boundaries():
    task = "完成唯一任务 MARKER-ONLY-TASK"

    prompt = build_worker_prompt(task)

    assert prompt.count(task) == 1
    assert "只负责完成后附 task.md 中的当前任务" in prompt
    assert "不要自动扩展项目目标" in prompt
    assert "不要因为未来可能需要而建设通用机制" in prompt
    assert "承重假设错误" in prompt
    assert "不能自动覆盖人类约束" in prompt
    assert "当前可核实事实可以推翻任务和旧会话中的假设" in prompt
    assert "按需确认当前 AGENTS.md" in prompt
    assert ".git 只读" in prompt
    assert "由已获授权的宿主实施者完成" in prompt
    assert "关键测试和运行结果保留在已有仓库报告或结果材料中" in prompt
    assert "恢复旧 thread 或完成上下文压缩后" in prompt
    assert "重新锚定本轮完整 task.md" in prompt
    assert "子代理只返回精简结论、证据位置和仍未确认的内容" in prompt
    assert "承重结论由你核对后再采用" in prompt
    assert "足以支持当前结论的最小充分验证" in prompt
    assert "完成：本任务产生的真实可观察变化" in prompt
    assert "关键验证：足以支持结论的关键结果" in prompt
    assert "未闭合：任务范围内仍未完成" in prompt
    assert "方向影响：可能改变下一步项目判断的新事实" in prompt
    assert "产物位置：相关提交、文件、报告或运行结果的位置" in prompt



def test_default_runner_uses_orchestra_codex_entry(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "hermes"
    control = initialize_project(project, "Goal", hermes_home=home)
    atomic_write(control / "decision.txt", "开始新任务\n")
    atomic_write(control / "task.md", "Do work\n")
    calls: list[dict[str, Any]] = []

    def runner(**kwargs: Any) -> CodexTurnResult:
        calls.append(kwargs)
        kwargs["on_thread_ready"]("thread-new")
        return CodexTurnResult(
            thread_id="thread-new",
            turn_id="turn-1",
            status="completed",
            final_text="done",
        )

    monkeypatch.setattr(worker_module, "run_codex_turn", runner)

    result = run_worker(
        project,
        hermes_home=home,
        codex_home="/tmp/test-codex-home",
    )

    assert result is not None and result.status == "completed"
    assert calls[0]["codex_home"] == "/tmp/test-codex-home"
    assert calls[0]["compact_before_turn"] is False


def test_new_task_keeps_old_thread_until_new_thread_is_ready(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "hermes"
    control = initialize_project(project, "Goal", hermes_home=home)
    atomic_write(control / "decision.txt", "开始新任务\n")
    atomic_write(control / "task.md", "Do new work\n")
    atomic_write(control / "worker-thread.txt", "thread-old\n")
    calls: list[dict[str, Any]] = []

    def runner(**kwargs: Any) -> CodexTurnResult:
        assert (control / "worker-thread.txt").read_text(encoding="utf-8") == "thread-old\n"
        calls.append(kwargs)
        kwargs["on_thread_ready"]("thread-new")
        assert (control / "worker-thread.txt").read_text(encoding="utf-8") == "thread-new\n"
        return CodexTurnResult(
            thread_id="thread-new",
            turn_id="turn-1",
            status="completed",
            final_text="done",
        )

    result = run_worker(project, hermes_home=home, worker_runner=runner)

    assert result is not None and result.status == "completed"
    assert calls[0]["resume_thread_id"] is None
    assert calls[0]["compact_before_turn"] is False
    assert "Do new work" in calls[0]["prompt"]
    assert "只负责完成后附 task.md 中的当前任务" in calls[0]["prompt"]
    assert "方向影响：可能改变下一步项目判断的新事实" in calls[0]["prompt"]
    assert (control / "worker-thread.txt").read_text(encoding="utf-8") == "thread-new\n"
    assert (control / "decision.txt").read_text(encoding="utf-8") == "继续当前任务\n"
    saved = (control / "result.md").read_text(encoding="utf-8")
    assert "状态：completed" in saved
    assert "done" in saved


def test_new_task_failure_before_thread_ready_preserves_old_thread(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "hermes"
    control = initialize_project(project, "Goal", hermes_home=home)
    atomic_write(control / "decision.txt", "开始新任务\n")
    atomic_write(control / "task.md", "Try work\n")
    atomic_write(control / "worker-thread.txt", "thread-old\n")

    result = run_worker(
        project,
        hermes_home=home,
        worker_runner=lambda **_kwargs: CodexTurnResult(
            status="failed",
            error="start failed",
        ),
    )

    assert result is not None and result.status == "failed"
    assert (control / "worker-thread.txt").read_text(encoding="utf-8") == "thread-old\n"
    assert (control / "decision.txt").read_text(encoding="utf-8") == "开始新任务\n"


def test_new_task_cancellation_before_thread_ready_preserves_old_thread(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "hermes"
    control = initialize_project(project, "Goal", hermes_home=home)
    atomic_write(control / "decision.txt", "开始新任务\n")
    atomic_write(control / "task.md", "Try work\n")
    atomic_write(control / "worker-thread.txt", "thread-old\n")

    def interrupt(**_kwargs: Any) -> CodexTurnResult:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_worker(project, hermes_home=home, worker_runner=interrupt)

    assert (control / "worker-thread.txt").read_text(encoding="utf-8") == "thread-old\n"
    assert (control / "decision.txt").read_text(encoding="utf-8") == "开始新任务\n"


def test_interrupted_turn_after_thread_ready_can_resume_new_thread(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "hermes"
    control = initialize_project(project, "Goal", hermes_home=home)
    atomic_write(control / "decision.txt", "开始新任务\n")
    atomic_write(control / "task.md", "Start work\n")
    atomic_write(control / "worker-thread.txt", "thread-old\n")

    def runner(**kwargs: Any) -> CodexTurnResult:
        kwargs["on_thread_ready"]("thread-new")
        return CodexTurnResult(
            thread_id="thread-new",
            turn_id="turn-1",
            status="interrupted",
            error="worker interrupted by user",
        )

    result = run_worker(project, hermes_home=home, worker_runner=runner)

    assert result is not None and result.status == "interrupted"
    assert (control / "worker-thread.txt").read_text(encoding="utf-8") == "thread-new\n"
    assert (control / "decision.txt").read_text(encoding="utf-8") == "继续当前任务\n"
    assert "状态：interrupted" in (control / "result.md").read_text(encoding="utf-8")

    resumed: list[str | None] = []

    def resume_runner(**kwargs: Any) -> CodexTurnResult:
        resumed.append(kwargs["resume_thread_id"])
        return CodexTurnResult(
            thread_id="thread-new",
            turn_id="turn-2",
            status="completed",
            final_text="resumed",
        )

    run_worker(project, hermes_home=home, worker_runner=resume_runner)

    assert resumed == ["thread-new"]


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
    seen: list[tuple[str | None, bool, str]] = []

    def runner(**kwargs: Any) -> CodexTurnResult:
        seen.append(
            (
                kwargs["resume_thread_id"],
                kwargs["compact_before_turn"],
                kwargs["prompt"],
            )
        )
        return CodexTurnResult(
            thread_id="thread-existing",
            turn_id="turn-2",
            status="completed",
            final_text="continued",
        )

    run_worker(project, hermes_home=home, worker_runner=runner)

    assert seen[0][:2] == ("thread-existing", True)
    assert seen[0][2].count("Continue work") == 1
    assert "重新锚定本轮完整 task.md" in seen[0][2]
    assert (control / "worker-thread.txt").read_text(encoding="utf-8") == "thread-existing\n"


@pytest.mark.parametrize("decision", ["等待", "询问人类", "停止"])
def test_non_running_decisions_do_not_start_worker_or_change_thread(tmp_path, decision):
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "hermes"
    control = initialize_project(project, "Goal", hermes_home=home)
    atomic_write(control / "decision.txt", decision + "\n")
    atomic_write(control / "task.md", "No worker now\n")
    atomic_write(control / "worker-thread.txt", "thread-existing\n")

    def should_not_run(**_kwargs: Any) -> CodexTurnResult:
        raise AssertionError("不应启动 worker")

    assert run_worker(project, hermes_home=home, worker_runner=should_not_run) is None
    assert (control / "worker-thread.txt").read_text(encoding="utf-8") == "thread-existing\n"
