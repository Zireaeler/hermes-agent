"""调用档案的范围、分页和控制材料保护。"""

import json

import pytest

from hermes_cli.orchestra_v1 import decide
from hermes_cli.orchestra_v1_control import atomic_write, initialize_project
from hermes_cli.orchestra_v1_history import history_index, read_history
from tests.test_orchestra_v1 import FakeAgent, init_git_repo, orchestra_output


def test_observation_preserves_control_and_records_exact_input(tmp_path):
    project = tmp_path / "project"
    init_git_repo(project)
    home = tmp_path / "hermes"
    control = initialize_project(project, "原始意图", hermes_home=home)
    atomic_write(control / "task.md", "原始任务\n")
    before = {p.name: p.read_text() for p in control.iterdir() if p.is_file()}
    requests = []
    decide(
        project,
        hermes_home=home,
        observe_only=True,
        context="periodic_check",
        agent_factory=lambda **kw: FakeAgent(orchestra_output("停止"), requests, **kw),
    )
    assert {p.name: p.read_text() for p in control.iterdir() if p.is_file()} == before
    record = next((control / "history").iterdir())
    assert (record / "input" / "task.md").read_text() == "原始任务\n"
    assert (record / "request.md").read_text() == requests[0]
    assert "决定：停止" in (record / "output.md").read_text()
    assert json.loads((record / "metadata.json").read_text())["elapsed_seconds"] >= 0


def test_history_tool_rejects_external_paths_and_symlinks(tmp_path):
    control = tmp_path / "control"
    history = control / "history"
    history.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("外部内容")
    (history / "link").symlink_to(outside)
    for path in ("../../outside.txt", str(outside), "link"):
        assert "error" in json.loads(read_history(control, {"path": path}))
    assert "外部内容" not in read_history(control, {"path": "link"})


def test_history_reads_pages_and_limits_large_logs(tmp_path):
    history = tmp_path / "history"
    history.mkdir()
    (history / "log.txt").write_text("\n".join(str(n) for n in range(8)))
    page = json.loads(
        read_history(tmp_path, {"path": "log.txt", "offset": 2, "limit": 3})
    )
    assert page["lines"] == ["2", "3", "4"]
    assert page["more"]
    (history / "large.txt").write_text(("x" * 10000 + "\n") * 5)
    assert len(read_history(tmp_path, {"path": "large.txt"})) < 24000
    first = json.loads(read_history(tmp_path, {"path": "large.txt", "limit": 1}))
    assert first["truncated"] == [{"line": 0, "next_char_offset": 8000}]
    rest = json.loads(
        read_history(tmp_path, {"path": "large.txt", "limit": 1, "char_offset": 8000})
    )
    assert rest["lines"] == ["x" * 2000]
    assert not rest["truncated"]


def test_unfinished_archive_does_not_block_future_decisions(tmp_path):
    (tmp_path / "history" / "unfinished").mkdir(parents=True)
    assert "档案元信息未完成" in history_index(tmp_path)


def test_intent_change_during_decision_does_not_apply_stale_task(tmp_path):
    project = tmp_path / "project"
    init_git_repo(project)
    home = tmp_path / "hermes"
    control = initialize_project(project, "旧意图", hermes_home=home)

    class ChangingAgent(FakeAgent):
        def chat(self, request):
            atomic_write(control / "intent.md", "新意图\n")
            return super().chat(request)

    with pytest.raises(RuntimeError, match="意图已改变"):
        decide(
            project,
            hermes_home=home,
            agent_factory=lambda **kw: ChangingAgent(
                orchestra_output("开始新任务"), [], **kw
            ),
        )
    assert (control / "task.md").read_text() == ""
    assert (control / "intent.md").read_text() == "新意图\n"


def test_decision_agent_can_query_its_project_archive_through_registered_tool(tmp_path):
    project = tmp_path / "project"
    init_git_repo(project)
    home = tmp_path / "hermes"
    control = initialize_project(project, "目标", hermes_home=home)
    archived = control / "history" / "previous" / "input"
    archived.mkdir(parents=True)
    (archived / "task.md").write_text("旧任务证据")
    atomic_write(
        archived.parent / "metadata.json", '{"kind": "worker", "elapsed_seconds": 4}'
    )

    class ReadingAgent(FakeAgent):
        def chat(self, request):
            from tools.registry import registry

            name = next(
                name
                for name in self.valid_tool_names
                if name.startswith("orchestra_history_")
            )
            response = registry.get_entry(name).handler({
                "path": "previous/input/task.md"
            })
            assert json.loads(response)["lines"] == ["旧任务证据"]
            assert "4.000 秒" in request
            return super().chat(request)

    decide(
        project,
        hermes_home=home,
        agent_factory=lambda **kw: ReadingAgent(orchestra_output("停止"), [], **kw),
    )
