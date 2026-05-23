from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import yaml


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "smoke_kanban_codex_e2e.py"
)


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location("smoke_kanban_codex_e2e", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_codex_e2e_smoke_config_uses_three_review_gated_lanes(tmp_path):
    smoke = _load_smoke_module()
    home = tmp_path / "home"

    smoke._write_config(home, model="gpt-5.4-mini", worker_timeout=123)

    cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    lanes = cfg["kanban"]["worker_lanes"]
    assert set(lanes) == {"codex-impl", "codex-review", "codex-test"}
    assert lanes["codex-impl"]["sandbox"] == "workspace-write"
    assert lanes["codex-review"]["sandbox"] == "read-only"
    assert lanes["codex-test"]["sandbox"] == "workspace-write"
    assert all(lane["type"] == "codex_cli" for lane in lanes.values())
    assert all(lane["success_policy"] == "block_for_review" for lane in lanes.values())
    assert all(lane["approval"] == "never" for lane in lanes.values())
    assert all(lane["model"] == "gpt-5.4-mini" for lane in lanes.values())
    assert all(lane["timeout_seconds"] == 123 for lane in lanes.values())

    check = cfg["kanban"]["acceptance_checks"]["smoke-exact-file"]
    assert check["argv"][0] == "python3"
    assert "smoke_result.txt" in check["argv"][2]
    assert "\\n" in check["argv"][2]


def test_real_codex_e2e_smoke_environment_is_isolated(tmp_path, monkeypatch):
    smoke = _load_smoke_module()
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_KANBAN_DB", "/tmp/old.db")
    monkeypatch.setenv("HERMES_KANBAN_HOME", "/tmp/old-home")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", "/tmp/old-workspaces")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "old-board")
    monkeypatch.setenv("PYTHONPATH", "/tmp/existing")

    smoke._setup_environment(home)

    assert os.environ["HERMES_HOME"] == str(home)
    assert "HERMES_KANBAN_DB" not in os.environ
    assert "HERMES_KANBAN_HOME" not in os.environ
    assert "HERMES_KANBAN_WORKSPACES_ROOT" not in os.environ
    assert "HERMES_KANBAN_BOARD" not in os.environ
    assert os.environ["PYTHONPATH"].split(os.pathsep)[0] == str(smoke.REPO_ROOT)
    assert os.environ["PYTHONPATH"].endswith("/tmp/existing")


def test_real_codex_e2e_smoke_creates_review_gated_task(tmp_path, monkeypatch):
    smoke = _load_smoke_module()
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    smoke._setup_environment(home)

    from hermes_cli import kanban_db as kb

    kb.init_db()
    task_id = smoke._create_task(workspace, worker_timeout=99)

    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)
        gate = kb.acceptance_check_gate_status(conn, task_id, source_run_id=None)

    assert task is not None
    assert task.assignee == "codex-impl"
    assert task.workspace_kind == "dir"
    assert task.workspace_path == str(workspace)
    assert task.max_runtime_seconds == 159
    assert any(event.kind == "acceptance_check_requested" for event in events)
    assert gate is not None
    assert gate["required"] == 1
    assert gate["items"][0]["name"] == "smoke-file-content"
    assert gate["items"][0]["state"] == "missing"
