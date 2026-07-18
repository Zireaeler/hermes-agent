"""Tests for the kanban CLI surface (hermes_cli.kanban)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Workspace flag parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        ("scratch",              ("scratch", None)),
        ("worktree",              ("worktree", None)),
        ("worktree:/tmp/wt",       ("worktree", "/tmp/wt")),
        ("dir:/tmp/work",         ("dir", "/tmp/work")),
    ],
)
def test_parse_workspace_flag_valid(value, expected):
    assert kc._parse_workspace_flag(value) == expected


def test_parse_workspace_flag_expands_user():
    kind, path = kc._parse_workspace_flag("dir:~/vault")
    assert kind == "dir"
    assert path.endswith("/vault")
    assert not path.startswith("~")

    kind, path = kc._parse_workspace_flag("worktree:~/trees/t6-wire")
    assert kind == "worktree"
    assert path.endswith("/trees/t6-wire")
    assert not path.startswith("~")

@pytest.mark.parametrize("bad", ["cloud", "dir:", "worktree:", ""])
def test_parse_workspace_flag_rejects(bad):
    if not bad:
        # Empty -> defaults; not an error.
        assert kc._parse_workspace_flag(bad) == ("scratch", None)
        return
    with pytest.raises(argparse.ArgumentTypeError):
        kc._parse_workspace_flag(bad)


def test_parse_branch_flag_rejects_empty_and_option_like():
    assert kc._parse_branch_flag(None) is None
    assert kc._parse_branch_flag(" wt/t6-wire ") == "wt/t6-wire"
    with pytest.raises(argparse.ArgumentTypeError):
        kc._parse_branch_flag("   ")
    with pytest.raises(argparse.ArgumentTypeError):
        kc._parse_branch_flag("-bad")
    with pytest.raises(argparse.ArgumentTypeError):
        kc._parse_branch_flag("bad branch")


def test_runtime_codex_model_source_prefers_isolated_codex_home(tmp_path, monkeypatch):
    isolated = tmp_path / "isolated-codex"
    isolated.mkdir()
    (isolated / "config.toml").write_text(
        'model = "test-model"\nmodel_provider = "test-source"\n'
        '[model_providers.test-source]\nbase_url = "https://model.example.test/v1"\n',
        encoding="utf-8",
    )
    (isolated / "auth.json").write_text('{"OPENAI_API_KEY":"test-secret"}', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(isolated))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home-without-codex")

    source = kc._runtime_model_source_from_codex_config(argparse.Namespace(model=None))

    assert source["display_provider"] == "codex:test-source"
    assert source["model"] == "test-model"
    assert source["explicit_base_url"] == "https://model.example.test/v1"
    assert source["explicit_api_key"] == "test-secret"


def test_parse_runtime_goal_items_defaults_to_worker_owned_verification():
    items = kc._parse_runtime_goal_items(["result:ship the complete result"])

    assert items == [{
        "item_key": "result",
        "description": "ship the complete result",
        "required": True,
        "verifier_required": False,
    }]


def test_runtime_orchestration_request_uses_config_and_cli_overrides(monkeypatch):
    from hermes_cli import config as hc

    monkeypatch.setattr(
        hc,
        "load_config",
        lambda: {
            "kanban": {
                "runtime_orchestration": {
                    "mode": "early_structure_assessment",
                    "worker_lane": "codex-runtime",
                    "max_child_nodes": 3,
                    "artifact_root": "/tmp/runtime-artifacts",
                    "retention": "retain",
                }
            }
        },
    )
    request, assignee = kc._runtime_orchestration_request(
        argparse.Namespace(
            assignee=None,
            orchestration_mode=None,
            orchestration_root=None,
            orchestration_max_children=2,
            orchestration_retention="cleanup_on_terminal",
        )
    )

    assert assignee == "codex-runtime"
    assert request == {
        "mode": "early_structure_assessment",
        "worker_lane": "codex-runtime",
        "max_child_nodes": 2,
        "artifact_root": "/tmp/runtime-artifacts",
        "retention": "cleanup_on_terminal",
    }


# ---------------------------------------------------------------------------
# run_slash smoke tests (end-to-end via the same entry both CLI and gateway use)
# ---------------------------------------------------------------------------

def test_run_slash_no_args_shows_usage(kanban_home):
    out = kc.run_slash("")
    assert "kanban" in out.lower()
    assert "create" in out.lower() or "subcommand" in out.lower() or "action" in out.lower()


def test_run_slash_create_and_list(kanban_home):
    out = kc.run_slash("create 'ship feature' --assignee alice")
    assert "Created" in out
    out = kc.run_slash("list")
    assert "ship feature" in out
    assert "alice" in out


def test_run_slash_create_worktree_path_and_branch(kanban_home, tmp_path):
    target = tmp_path / ".worktrees" / "t6-wire"
    target_arg = target.as_posix()
    out = kc.run_slash(
        f"create 'ship worktree' --workspace worktree:{target_arg} --branch wt/t6-wire"
    )
    assert "Created" in out

    with kb.connect() as conn:
        tasks = kb.list_tasks(conn)
    task = tasks[0]
    assert task.workspace_kind == "worktree"
    assert task.workspace_path == target_arg
    assert task.branch_name == "wt/t6-wire"


def test_run_slash_rejects_branch_without_worktree(kanban_home):
    out = kc.run_slash("create 'bad branch' --workspace scratch --branch wt/bad")
    assert "--branch is only valid with --workspace worktree" in out


def test_run_slash_create_with_parent_and_cascade(kanban_home):
    # Parent then child via --parent
    out1 = kc.run_slash("create 'parent' --assignee alice")
    # Extract the "t_xxxx" id from "Created t_xxxx (ready, ...)"
    import re
    m = re.search(r"(t_[a-f0-9]+)", out1)
    assert m
    p = m.group(1)
    out2 = kc.run_slash(f"create 'child' --assignee bob --parent {p}")
    assert "todo" in out2  # child starts as todo

    # Complete parent; list should promote child to ready
    kc.run_slash(f"complete {p}")
    # Explicit filter: child should now be ready (was todo before complete).
    ready_list = kc.run_slash("list --status ready")
    assert "child" in ready_list


def test_run_slash_show_includes_comments(kanban_home):
    out = kc.run_slash("create 'x'")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    kc.run_slash(f"comment {tid} 'remember to include performance section'")
    show = kc.run_slash(f"show {tid}")
    assert "performance section" in show


def test_run_slash_comment_max_len_trims_long_body(kanban_home):
    out = kc.run_slash("create 'x'")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    kc.run_slash(f"comment {tid} '{'x' * 30}' --max-len 20")
    show = kc.run_slash(f"show {tid}")
    assert "trimmed to 20 chars by --max-len" in show
    assert "x" * 30 not in show


def test_run_slash_block_unblock_cycle(kanban_home):
    out = kc.run_slash("create 'x' --assignee alice")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    # Claim first so block() finds it running
    kc.run_slash(f"claim {tid}")
    assert "Blocked" in kc.run_slash(f"block {tid} 'need decision'")
    assert "Unblocked" in kc.run_slash(f"unblock {tid}")


def test_run_slash_json_output(kanban_home):
    out = kc.run_slash("create 'jsontask' --assignee alice --json")
    payload = json.loads(out)
    assert payload["title"] == "jsontask"
    assert payload["assignee"] == "alice"
    assert payload["status"] == "ready"


def test_run_slash_dispatch_dry_run_counts(kanban_home):
    kc.run_slash("create 'a' --assignee alice")
    kc.run_slash("create 'b' --assignee bob")
    out = kc.run_slash("dispatch --dry-run")
    assert "Spawned:" in out


def test_run_slash_runtime_create_status_and_list_json(kanban_home):
    created = json.loads(kc.run_slash("runtime create 'ship runtime control plane' --json"))
    assert created["state"] == "waiting_decision"
    assert created["root_task_id"].startswith("t_")
    assert created["goal_items"][0]["item_key"] == "initial-runtime-result"
    assert created["frontier"] == []
    assert created["liveness"]["legal_wait"] is True
    assert created["liveness"]["decision_requested"] is True

    status = json.loads(kc.run_slash(f"runtime status {created['id']} --json"))
    assert status["job"]["id"] == created["id"]
    assert status["job"]["metadata"]["initialization_mode"] == "provider_first"
    assert status["orchestration"]["mode"] == "coherent_single_primary"
    assert status["orchestration"]["enabled"] is False
    assert status["nodes"] == []

    jobs = json.loads(kc.run_slash("runtime list --json"))
    assert [job["id"] for job in jobs] == [created["id"]]

    orchestration = json.loads(
        kc.run_slash(f"runtime orchestration {created['id']} --json")
    )
    assert orchestration["mode"] == "coherent_single_primary"
    assert orchestration["child_count"] == 0


def test_run_slash_runtime_promote_existing_root_task(kanban_home):
    root = json.loads(kc.run_slash("create 'existing root' --body 'runtime objective' --json"))
    promoted = json.loads(kc.run_slash(f"runtime promote {root['id']} --json"))
    assert promoted["root_task_id"] == root["id"]
    assert promoted["objective"] == "runtime objective"
    assert promoted["state"] == "waiting_decision"
    assert promoted["frontier"] == []


def test_run_slash_runtime_advance_without_provider_preserves_initial_decision_wait(kanban_home):
    created = json.loads(kc.run_slash("runtime create 'advance runtime' --json"))
    first = json.loads(kc.run_slash(f"runtime advance {created['id']} --json"))
    second = json.loads(kc.run_slash(f"runtime advance {created['id']} --json"))

    assert first["state"] == "waiting_decision"
    assert first["step"]["decision_requested"] is True
    assert first["step"]["materialized_nodes"] == []
    assert second["step"]["materialized_nodes"] == []


def test_run_slash_runtime_reconcile_and_consistency_json(kanban_home):
    created = json.loads(kc.run_slash("runtime create 'reconcile runtime' --json"))
    first = json.loads(kc.run_slash(f"runtime advance {created['id']} --loop --fake-provider --json"))
    assert first["state"] == "waiting_worker"
    status = json.loads(kc.run_slash(f"runtime status {created['id']} --json"))
    node = next(node for node in status["nodes"] if node["node_key"] == "implement-initial-runtime-result")
    task_id = node["latest_task_id"]
    with kb.connect() as conn:
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    reconciled = json.loads(kc.run_slash(f"runtime reconcile {created['id']} --json"))
    consistency = json.loads(kc.run_slash(f"runtime consistency {created['id']} --json"))
    inspected = json.loads(kc.run_slash(f"runtime inspect {created['id']} --json"))

    assert reconciled["events"] == ["materialization_lost"]
    assert reconciled["scheduled_retries"] == ["implement-initial-runtime-result"]
    assert consistency["status"] == "failed"
    assert inspected["legal_waiting_reason"] == "ready_to_materialize"
    assert inspected["recovery"]["open_recovery_events"]


def test_run_slash_runtime_capability_json(kanban_home):
    created = json.loads(kc.run_slash("runtime create 'capability runtime' --json"))
    payload = json.loads(kc.run_slash(f"runtime capability {created['id']} --json"))

    assert payload["policy_revision"] == 1
    assert "workspace_write" in payload["allowed_by_default"]
    assert "secret_access" in payload["require_human"]
    assert "network_access" in payload["denied_by_default"]
    assert payload["blocked_nodes"] == []


def test_run_slash_runtime_memory_json(kanban_home):
    created = json.loads(kc.run_slash("runtime create 'memory runtime' --json"))
    payload = json.loads(kc.run_slash(f"runtime memory {created['id']} --json"))

    assert payload["guidance_loaded"] is False
    assert payload["selected_hints"] == []
    assert payload["recent_usage"] == []


def test_run_slash_runtime_soak_json(kanban_home, tmp_path):
    payload = json.loads(
        kc.run_slash(
            f"runtime soak --scenario phase4g-baseline --max-ticks 20 "
            f"--workspace-path {tmp_path / 'soak-workspace'} --json"
        )
    )

    assert payload["scenario"] == "phase4g-baseline"
    assert payload["final_state"] == "done"
    assert payload["consistency"]["status"] == "passed"
    assert payload["old_segment_excluded_from_provider_input"] is True


def test_run_slash_runtime_active_long_run_soak_json(kanban_home, tmp_path):
    payload = json.loads(
        kc.run_slash(
            f"runtime soak --scenario phase4g6-active-long-run --max-ticks 50 "
            f"--workspace-path {tmp_path / 'active-soak-workspace'} --json"
        )
    )

    assert payload["scenario"] == "phase4g6-active-long-run"
    assert payload["active_tick_count"] >= 50
    assert payload["compactions"] >= 5
    assert payload["historical_sentinels_excluded"] is True
    assert payload["context_chain_validation"]["status"] == "valid"
    assert payload["consistency"]["status"] == "passed"


def test_run_slash_runtime_real_smoke_dry_run_json(kanban_home):
    created = json.loads(kc.run_slash("runtime create 'phase4g1 real smoke dry run' --json"))
    payload = json.loads(kc.run_slash(f"runtime real-smoke {created['id']} --json"))

    assert payload["job_id"] == created["id"]
    assert payload["decision_dry_run"]["called_model"] is False
    assert payload["decision_execute"] is None
    assert payload["real_compaction"] is None
    assert payload["consistency"]["status"] == "passed"


def test_run_slash_runtime_bounded_loop_requires_explicit_model_source(kanban_home):
    created = json.loads(kc.run_slash("runtime create 'phase4g2 bounded loop args' --json"))

    out = kc.run_slash(f"runtime bounded-loop {created['id']} --json")

    assert "--provider real requires --model-provider and --model" in out


def test_run_slash_context_output_format(kanban_home):
    out = kc.run_slash("create 'tech spec' --assignee alice --body 'write an RFC'")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    kc.run_slash(f"comment {tid} 'remember to include performance section'")
    ctx = kc.run_slash(f"context {tid}")
    assert "tech spec" in ctx
    assert "write an RFC" in ctx
    assert "performance section" in ctx


def test_run_slash_create_attaches_acceptance_check_request(
    kanban_home,
    tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    req = tmp_path / "acceptance.yaml"
    req.write_text(
        "acceptance_check_request:\n"
        "  name: expected-file\n"
        "  type: file_content\n"
        "  path: ok.txt\n"
        "  contains: ok\n",
        encoding="utf-8",
    )

    payload = json.loads(kc.run_slash(
        "create 'task with acceptance' "
        "--assignee codex-deep "
        f"--workspace dir:{workspace} "
        f"--acceptance-check-request {req} "
        "--json"
    ))

    with kb.connect() as conn:
        gate = kb.acceptance_check_gate_status(
            conn,
            payload["id"],
            source_run_id=None,
        )
        events = kb.list_events(conn, payload["id"])

    assert payload["acceptance_check_gate"]["items"][0]["name"] == "expected-file"
    assert gate is not None
    assert gate["items"][0]["requested"] is True
    assert any(event.kind == "acceptance_check_requested" for event in events)


def test_run_slash_create_rejects_unsafe_acceptance_request(
    kanban_home,
    tmp_path,
):
    req = tmp_path / "unsafe.yaml"
    req.write_text(
        "acceptance_check_request:\n"
        "  name: bad\n"
        "  type: file_content\n"
        "  path: ok.txt\n"
        "  contains: ok\n"
        "  argv: [pytest, -q]\n",
        encoding="utf-8",
    )

    out = kc.run_slash(
        "create 'unsafe acceptance' "
        "--assignee codex-deep "
        f"--acceptance-check-request {req}"
    )

    assert "acceptance-check-request" in out
    assert "executable command fields" in out
    with kb.connect() as conn:
        assert kb.list_tasks(conn) == []


def test_run_slash_tenant_filter(kanban_home):
    kc.run_slash("create 'biz-a task' --tenant biz-a --assignee alice")
    kc.run_slash("create 'biz-b task' --tenant biz-b --assignee alice")
    a = kc.run_slash("list --tenant biz-a")
    b = kc.run_slash("list --tenant biz-b")
    assert "biz-a task" in a and "biz-b task" not in a
    assert "biz-b task" in b and "biz-a task" not in b


def test_run_slash_session_filter(kanban_home):
    """`hermes kanban list --session <id>` filters by the originating
    chat session id stamped on tasks created from inside an ACP loop."""
    from hermes_cli import kanban_db as kb
    with kb.connect() as conn:
        kb.create_task(
            conn, title="from sess-1 a", assignee="alice", session_id="sess-1"
        )
        kb.create_task(
            conn, title="from sess-1 b", assignee="alice", session_id="sess-1"
        )
        kb.create_task(
            conn, title="from sess-2", assignee="alice", session_id="sess-2"
        )
        kb.create_task(conn, title="cli only", assignee="alice")
    out_1 = kc.run_slash("list --session sess-1")
    out_2 = kc.run_slash("list --session sess-2")
    assert "from sess-1 a" in out_1
    assert "from sess-1 b" in out_1
    assert "from sess-2" not in out_1
    assert "cli only" not in out_1
    assert "from sess-2" in out_2
    assert "from sess-1 a" not in out_2


def test_kanban_list_json_includes_session_id(kanban_home):
    """JSON output exposes `session_id` so external clients (Scarf, web
    dashboards) don't need a side query to filter by chat session."""
    from hermes_cli import kanban_db as kb
    with kb.connect() as conn:
        kb.create_task(
            conn, title="acp task", assignee="alice", session_id="acp-x"
        )
    raw = kc.run_slash("list --json")
    payload = json.loads(raw)
    assert any(
        row.get("title") == "acp task"
        and row.get("session_id") == "acp-x"
        for row in payload
    )


def test_run_slash_usage_error_returns_message(kanban_home):
    # Missing required argument for create
    out = kc.run_slash("create")
    assert "usage" in out.lower() or "error" in out.lower()


def test_run_slash_assign_reassigns(kanban_home):
    out = kc.run_slash("create 'x' --assignee alice")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    assert "Assigned" in kc.run_slash(f"assign {tid} bob")
    show = kc.run_slash(f"show {tid}")
    assert "bob" in show


def test_run_slash_link_unlink(kanban_home):
    a = kc.run_slash("create 'a'")
    b = kc.run_slash("create 'b'")
    import re
    ta = re.search(r"(t_[a-f0-9]+)", a).group(1)
    tb = re.search(r"(t_[a-f0-9]+)", b).group(1)
    assert "Linked" in kc.run_slash(f"link {ta} {tb}")
    # After link, b is todo
    show = kc.run_slash(f"show {tb}")
    assert "todo" in show
    assert "Unlinked" in kc.run_slash(f"unlink {ta} {tb}")


# ---------------------------------------------------------------------------
# Integration with the COMMAND_REGISTRY
# ---------------------------------------------------------------------------

def test_kanban_is_resolvable():
    from hermes_cli.commands import resolve_command

    cmd = resolve_command("kanban")
    assert cmd is not None
    assert cmd.name == "kanban"


def test_kanban_bypasses_active_session_guard():
    from hermes_cli.commands import should_bypass_active_session

    assert should_bypass_active_session("kanban")


def test_kanban_in_autocomplete_table():
    from hermes_cli.commands import COMMANDS, SUBCOMMANDS

    assert "/kanban" in COMMANDS
    subs = SUBCOMMANDS.get("/kanban") or []
    assert "create" in subs
    assert "dispatch" in subs


def test_kanban_autocomplete_includes_live_subcommands():
    from prompt_toolkit.document import Document

    from hermes_cli.commands import SlashCommandCompleter

    completer = SlashCommandCompleter()
    doc = Document("/kanban sp", cursor_position=len("/kanban sp"))
    texts = {c.text for c in completer.get_completions(doc, None)}

    assert "specify" in texts

    doc = Document("/kanban re", cursor_position=len("/kanban re"))
    texts = {c.text for c in completer.get_completions(doc, None)}

    assert "reclaim" in texts
    assert "reassign" in texts


def test_kanban_not_gateway_only():
    # kanban is available in BOTH CLI and gateway surfaces.
    from hermes_cli.commands import COMMAND_REGISTRY

    cmd = next(c for c in COMMAND_REGISTRY if c.name == "kanban")
    assert not cmd.cli_only
    assert not cmd.gateway_only


# ---------------------------------------------------------------------------
# reclaim + reassign CLI smoke tests
# ---------------------------------------------------------------------------

def test_run_slash_reclaim_running_task(kanban_home):
    import re
    import time
    import secrets
    from hermes_cli import kanban_db as kb

    out1 = kc.run_slash("create 'stuck worker task' --assignee broken-model")
    m = re.search(r"(t_[a-f0-9]+)", out1)
    assert m
    tid = m.group(1)

    # Simulate a running claim outside TTL.
    conn = kb.connect()
    try:
        lock = secrets.token_hex(4)
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, int(time.time()) + 3600, 4242, tid),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (tid, lock, int(time.time()) + 3600, 4242, int(time.time())),
        )
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (rid, tid))
        conn.commit()
    finally:
        conn.close()

    out = kc.run_slash(f"reclaim {tid} --reason 'test'")
    assert "Reclaimed" in out, out
    # Status back to ready.
    out2 = kc.run_slash(f"show {tid}")
    assert "ready" in out2.lower()


def test_run_slash_reassign_with_reclaim_flag(kanban_home):
    import re
    import time
    import secrets
    from hermes_cli import kanban_db as kb

    out1 = kc.run_slash("create 'switch model' --assignee orig")
    m = re.search(r"(t_[a-f0-9]+)", out1)
    tid = m.group(1)

    # Simulate a running claim.
    conn = kb.connect()
    try:
        lock = secrets.token_hex(4)
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, int(time.time()) + 3600, 4242, tid),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (tid, lock, int(time.time()) + 3600, 4242, int(time.time())),
        )
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (rid, tid))
        conn.commit()
    finally:
        conn.close()

    out = kc.run_slash(f"reassign {tid} newbie --reclaim --reason 'switch'")
    assert "Reassigned" in out, out
    out2 = kc.run_slash(f"show {tid}")
    assert "newbie" in out2


def test_run_slash_progress_json_is_read_only(kanban_home):
    import re
    import time
    import secrets

    out1 = kc.run_slash("create 'external progress' --assignee codex-deep")
    m = re.search(r"(t_[a-f0-9]+)", out1)
    tid = m.group(1)

    with kb.connect() as conn:
        lock = secrets.token_hex(4)
        now = int(time.time())
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=?, current_run_id=NULL WHERE id=?",
            (lock, now + 3600, 4242, tid),
        )
        cur = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, claim_lock, "
            "claim_expires, worker_pid, started_at) VALUES (?, ?, 'running', ?, ?, ?, ?)",
            (tid, "codex-deep", lock, now + 3600, 4242, now),
        )
        run_id = cur.lastrowid
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, tid))
        kb.record_task_event(
            conn,
            tid,
            "worker_progress",
            {"lane": "codex-deep", "items": [{"index": 1, "status": "done", "text": "mock"}]},
            run_id=run_id,
        )
        kb.record_task_event(
            conn,
            tid,
            "worker_codex_event",
            {
                "worker_lane": "codex-deep",
                "worker_kind": "codex_cli",
                "run_id": run_id,
                "event_type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "status": "completed",
                    "exit_code": 0,
                    "command": "pytest -q",
                },
            },
            run_id=run_id,
        )
        before = kb.get_task(conn, tid)

    payload = json.loads(kc.run_slash(f"progress {tid} --json"))
    text = kc.run_slash(f"progress {tid}")

    with kb.connect() as conn:
        after = kb.get_task(conn, tid)
    assert payload["task"]["status"] == "running"
    assert payload["task"]["worker_pid"] == 4242
    assert payload["worker_progress"]["items"][0]["text"] == "mock"
    assert payload["worker_codex_events"][0]["payload"]["item"]["command"] == "pytest -q"
    assert "Recent Codex activity:" in text
    assert "command_execution" in text
    assert "pytest -q" in text
    assert after.status == "running"
    assert after.claim_lock == before.claim_lock


def test_run_slash_progress_children_json_summarizes_goal_workers(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="goal", triage=True)
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[
                {"title": "implement", "assignee": "codex-fast"},
                {"title": "review", "assignee": "codex-deep"},
            ],
            author="planner",
        )
        assert child_ids is not None
        running_id, review_id = child_ids

        running = kb.claim_task(conn, running_id, claimer="worker:fast")
        assert running is not None
        kb.record_task_event(
            conn,
            running_id,
            "worker_progress",
            {"lane": "codex-fast", "items": [{"index": 1, "status": "running", "text": "mock"}]},
            run_id=running.current_run_id,
        )
        reviewing = kb.claim_task(conn, review_id, claimer="worker:deep")
        assert reviewing is not None
        assert kb.block_task(
            conn,
            review_id,
            reason="review-required: Codex completed; Hermes review required",
            expected_run_id=reviewing.current_run_id,
            metadata={
                "worker_lane": {"name": "codex-deep", "kind": "codex_cli", "exit_code": 0},
                "verification": {"commands": ["pytest -q"], "summary": "passed"},
                "review": {"required": True, "reason": "Codex completed; Hermes review required"},
            },
        )
        before = kb.get_task(conn, running_id)

    payload = json.loads(kc.run_slash(f"progress {root} --children --json"))

    with kb.connect() as conn:
        after = kb.get_task(conn, running_id)

    assert payload["task"]["id"] == root
    assert payload["child_summary"]["total"] == 2
    assert payload["child_summary"]["running"] == 1
    assert payload["child_summary"]["review_required"] == 1
    assert payload["child_summary"]["relationship_counts"]["decomposed_child"] == 2
    assert payload["child_summary"]["recommended_actions"] == {
        "plan_review_followups": 1,
        "wait_for_implementation": 1,
    }
    by_id = {child["task"]["id"]: child for child in payload["children"]}
    assert by_id[running_id]["worker_progress"]["items"][0]["text"] == "mock"
    assert by_id[running_id]["acceptance"]["recommended_action"] == "wait_for_implementation"
    assert by_id[review_id]["worker_lane"]["name"] == "codex-deep"
    assert by_id[review_id]["acceptance"]["recommended_action"] == "plan_review_followups"
    assert by_id[review_id]["verification"]["commands"] == ["pytest -q"]
    assert after.status == "running"
    assert after.claim_lock == before.claim_lock


def test_run_slash_progress_children_surfaces_child_next_action(
    kanban_home,
):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="goal with review child", triage=True)
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[{"title": "implement", "assignee": "codex-deep"}],
            author="planner",
        )
        assert child_ids is not None
        child = child_ids[0]
        claimed = kb.claim_task(conn, child, claimer="worker:codex-deep")
        assert claimed is not None
        assert kb.block_task(
            conn,
            child,
            reason="review-required",
            expected_run_id=claimed.current_run_id,
            metadata={
                "worker_lane": {"name": "codex-deep", "kind": "codex_cli", "exit_code": 0},
                "verification": {"commands": ["python3 -m pytest -q"], "summary": "passed"},
                "review": {"required": True, "reason": "needs review"},
            },
        )

    payload = json.loads(kc.run_slash(f"progress {root} --children --json"))
    text = kc.run_slash(f"progress {root} --children")

    assert payload["child_summary"]["recommended_actions"] == {
        "plan_review_followups": 1,
    }
    child_payload = payload["children"][0]
    assert child_payload["acceptance"]["recommended_action"] == "plan_review_followups"
    assert child_payload["acceptance"]["request_changes_allowed"] is True
    assert "next: plan_review_followups=1" in text
    assert f"{child} blocked @codex-deep" in text
    assert "next: plan_review_followups" in text


def test_run_slash_progress_children_surfaces_child_diagnostics(
    kanban_home,
):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="goal with diagnostic child", triage=True)
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[{"title": "implementation with failed acceptance", "assignee": "codex-deep"}],
            author="planner",
        )
        assert child_ids is not None
        child = child_ids[0]
        claimed = kb.claim_task(conn, child, claimer="worker:codex-deep")
        assert claimed is not None
        assert kb.block_task(
            conn,
            child,
            reason="review-required",
            expected_run_id=claimed.current_run_id,
            metadata={
                "worker_lane": {"name": "codex-deep", "kind": "codex_cli", "exit_code": 0},
                "review": {"required": True, "reason": "needs review"},
            },
        )
        kb.record_task_event(
            conn,
            child,
            "acceptance_check_completed",
            {
                "name": "unit-tests",
                "source_run_id": claimed.current_run_id,
                "passed": False,
                "exit_code": 2,
                "stderr_tail": "assertion failed",
            },
            run_id=claimed.current_run_id,
        )

    payload = json.loads(kc.run_slash(f"progress {root} --children --json"))
    text = kc.run_slash(f"progress {root} --children")
    child_payload = payload["children"][0]

    assert child_payload["diagnostics"][0]["kind"] == "acceptance_check_gate_failed"
    assert child_payload["warnings"]["kinds"]["acceptance_check_gate_failed"] == 1
    assert "diagnostics: acceptance_check_gate_failed" in text


def test_run_slash_progress_without_task_id_reads_session_goal(
    kanban_home,
    tmp_path,
    monkeypatch,
):
    from hermes_cli.goals import create_kanban_task_from_goal

    session_id = "cli-session-progress"
    monkeypatch.setenv("HERMES_SESSION_ID", session_id)
    root = create_kanban_task_from_goal(
        "cli session progress goal",
        session_id=session_id,
        assignee="orchestrator",
        workspace_kind="dir",
        workspace_path=str(tmp_path),
    )
    with kb.connect() as conn:
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[{"title": "implement", "assignee": "codex-deep"}],
            author="planner",
        )
        assert child_ids is not None
        child = child_ids[0]
        running = kb.claim_task(conn, child, claimer="worker:codex-deep")
        assert running is not None
        kb.record_task_event(
            conn,
            child,
            "worker_progress",
            {
                "lane": "codex-deep",
                "items": [{"index": 1, "status": "running", "text": "cli session"}],
            },
            run_id=running.current_run_id,
        )
        before = kb.get_task(conn, child)

    payload = json.loads(kc.run_slash("progress --children --json"))
    text = kc.run_slash("progress --children")

    with kb.connect() as conn:
        after = kb.get_task(conn, child)

    assert payload["resolved_from_session_goal"] is True
    assert payload["session_id"] == session_id
    assert payload["task"]["id"] == root
    assert payload["child_summary"]["running"] == 1
    assert payload["children"][0]["worker_progress"]["items"][0]["text"] == "cli session"
    assert f"current session goal ({session_id})" in text
    assert after.status == before.status == "running"
    assert after.claim_lock == before.claim_lock


def test_run_slash_progress_without_task_id_requires_session_goal(
    kanban_home,
    monkeypatch,
):
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)

    out = kc.run_slash("progress --json")

    assert "task_id is required unless HERMES_SESSION_ID" in out


def test_run_slash_reviews_lists_review_required_evidence(
    kanban_home, tmp_path,
):
    metadata = {
        "worker_lane": {"name": "codex-deep", "kind": "codex_cli", "exit_code": 0},
        "verification": {"commands": ["pytest -q"], "summary": "passed"},
        "git": {"changed_files": ["hermes_cli/kanban.py"], "diff_summary": "+2 -0"},
        "review": {"required": True, "reason": "Codex completed; Hermes review required"},
    }
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="external review",
            assignee="codex-deep",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        task = kb.claim_task(conn, tid, claimer="worker:codex-deep")
        assert task is not None
        assert kb.block_task(
            conn,
            tid,
            reason="review-required: Codex completed; Hermes review required",
            expected_run_id=task.current_run_id,
            metadata=metadata,
        )

    payload = json.loads(kc.run_slash("reviews --json"))
    assert [item["task"]["id"] for item in payload] == [tid]
    assert payload[0]["worker_lane"]["name"] == "codex-deep"
    assert payload[0]["verification"]["commands"] == ["pytest -q"]
    assert payload[0]["evidence"]["review"]["required"] is True

    human = kc.run_slash("reviews --lane codex-deep")
    assert tid in human
    assert "codex-deep" in human
    assert "review-required: Codex completed" in human


def test_run_slash_reviews_hides_followup_evidence_by_default(
    kanban_home,
    tmp_path,
):
    metadata = {
        "worker_lane": {"name": "codex-deep", "kind": "codex_cli", "exit_code": 0},
        "verification": {"commands": ["pytest -q"], "summary": "passed"},
        "review": {"required": True, "reason": "Codex completed; Hermes review required"},
    }
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="implementation review",
            assignee="codex-deep",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        task = kb.claim_task(conn, tid, claimer="worker:codex-deep")
        assert task is not None
        assert kb.block_task(
            conn,
            tid,
            reason="review-required: Codex completed; Hermes review required",
            expected_run_id=task.current_run_id,
            metadata=metadata,
        )
        plan = kb.plan_review_followups(conn, tid)
        for followup_id, lane, verdict in (
            (plan.review_task_id, "codex-review", "approve"),
            (plan.test_task_id, "codex-test", "pass"),
        ):
            follow = kb.claim_task(conn, followup_id, claimer=f"worker:{lane}")
            assert follow is not None
            assert kb.block_task(
                conn,
                followup_id,
                reason="review-required: Codex completed; Hermes review required",
                expected_run_id=follow.current_run_id,
                metadata={
                    "worker_lane": {"name": lane, "kind": "codex_cli", "exit_code": 0},
                    "verification": {"commands": [], "summary": f"Verdict: {verdict}"},
                    "review": {
                        "required": True,
                        "reason": "Codex completed; Hermes review required",
                    },
                },
            )

    default_payload = json.loads(kc.run_slash("reviews --json"))
    followup_payload = json.loads(kc.run_slash("reviews --include-followups --json"))

    assert [item["task"]["id"] for item in default_payload] == [tid]
    assert {
        item["task"]["id"]
        for item in followup_payload
    } == {tid, plan.review_task_id, plan.test_task_id}


def test_run_slash_worker_lanes_lists_active_instances(kanban_home):
    from hermes_cli.worker_lanes import WorkerLane, clear_worker_lanes, register_worker_lane

    clear_worker_lanes()
    register_worker_lane(WorkerLane(
        name="codex-deep",
        kind="codex_cli",
        description="Deep Codex lane",
        spawn_fn=lambda task, workspace, **kwargs: 5100,
        max_concurrency=2,
        source="test",
        config={"type": "codex_cli", "model": "gpt-5.5", "secret": "hidden"},
    ))
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="active lane task", assignee="codex-deep")
        res = kb.dispatch_once(conn, max_spawn=1)
        assert res.spawned[0][0] == tid

    payload = json.loads(kc.run_slash("worker-lanes --json"))

    assert payload[0]["name"] == "codex-deep"
    assert payload[0]["active_count"] == 1
    assert payload[0]["available_capacity"] == 1
    assert payload[0]["active"][0]["task_id"] == tid
    assert payload[0]["active"][0]["worker_pid"] == 5100
    assert payload[0]["config"]["model"] == "gpt-5.5"
    assert "secret" not in payload[0]["config"]

    human = kc.run_slash("worker-lanes")
    assert "codex-deep" in human
    assert tid in human
    assert "ACTIVE" in human


def test_run_slash_review_approve_completes_review_required_task(
    kanban_home, tmp_path,
):
    metadata = {
        "worker_lane": {"name": "codex-deep", "kind": "codex_cli", "exit_code": 0},
        "verification": {"commands": ["pytest -q"], "summary": "passed"},
        "review": {"required": True, "reason": "Codex completed; Hermes review required"},
    }
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="approve via slash",
            assignee="codex-deep",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        task = kb.claim_task(conn, tid, claimer="worker:codex-deep")
        assert task is not None
        assert kb.block_task(
            conn,
            tid,
            reason="review-required: Codex completed; Hermes review required",
            expected_run_id=task.current_run_id,
            metadata=metadata,
        )

    payload = json.loads(kc.run_slash(
        f"review {tid} approve --reviewer ralph --summary 'bounded evidence approved' --json"
    ))

    assert payload["task"]["status"] == "done"
    assert payload["evidence"]["review"]["decision"] == "approved"
    assert payload["evidence"]["review"]["reviewer"] == "ralph"
    assert payload["review_required"] is False


def test_run_slash_review_request_changes_unblocks_for_next_worker(
    kanban_home, tmp_path,
):
    metadata = {
        "worker_lane": {"name": "codex-deep", "kind": "codex_cli", "exit_code": 0},
        "verification": {"commands": ["pytest -q"], "summary": "failed"},
        "review": {"required": True, "reason": "Codex completed; Hermes review required"},
    }
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="request changes via slash",
            assignee="codex-deep",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        task = kb.claim_task(conn, tid, claimer="worker:codex-deep")
        assert task is not None
        assert kb.block_task(
            conn,
            tid,
            reason="review-required: Codex completed; Hermes review required",
            expected_run_id=task.current_run_id,
            metadata=metadata,
        )

    out = kc.run_slash(
        f"review {tid} request-changes --reviewer ralph "
        "--comment 'add a focused regression test'"
    )

    assert f"Requested changes for {tid}" in out
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        comments = kb.list_comments(conn, tid)
        events = kb.list_events(conn, tid)
    assert task.status == "ready"
    assert "focused regression test" in comments[-1].body
    assert any(event.kind == "worker_review_changes_requested" for event in events)


def test_run_slash_plan_review_json_creates_followups(
    kanban_home, tmp_path,
):
    metadata = {
        "worker_lane": {"name": "codex-deep", "kind": "codex_cli", "exit_code": 0},
        "verification": {"commands": ["pytest -q"], "summary": "passed"},
        "git": {"changed_files": ["hermes_cli/kanban.py"], "diff_summary": "+2 -0"},
        "review": {"required": True, "reason": "Codex completed; Hermes review required"},
    }
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="plan review via slash",
            assignee="codex-deep",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        task = kb.claim_task(conn, tid, claimer="worker:codex-deep")
        assert task is not None
        assert kb.block_task(
            conn,
            tid,
            reason="review-required: Codex completed; Hermes review required",
            expected_run_id=task.current_run_id,
            metadata=metadata,
        )

    payload = json.loads(kc.run_slash(
        f"plan-review {tid} --review-assignee codex-review "
        "--test-assignee codex-test --json"
    ))

    with kb.connect() as conn:
        review_task = kb.get_task(conn, payload["review_task_id"])
        test_task = kb.get_task(conn, payload["test_task_id"])
        progress = kb.task_progress_snapshot(conn, tid, include_children=True)
        repeated = json.loads(kc.run_slash(f"plan-review {tid} --json"))

    assert set(payload["created"]) == {payload["review_task_id"], payload["test_task_id"]}
    assert payload["existing"] == []
    assert review_task.status == "ready"
    assert test_task.status == "ready"
    assert review_task.assignee == "codex-review"
    assert test_task.assignee == "codex-test"
    assert "hermes_cli/kanban.py" in review_task.body
    assert "pytest -q" in test_task.body
    assert progress.child_summary["relationship_counts"]["review_followup"] == 1
    assert progress.child_summary["relationship_counts"]["test_followup"] == 1
    assert progress.review_followup_gate["ready"] is False
    assert progress.review_followup_gate["pending"] == 2
    assert repeated["created"] == []
    assert set(repeated["existing"]) == {payload["review_task_id"], payload["test_task_id"]}

    acceptance = json.loads(kc.run_slash(f"acceptance {tid} --json"))
    assert acceptance["recommended_action"] == "wait_for_followups"
    assert acceptance["approval_allowed"] is False
    assert acceptance["review_followup_gate"]["pending"] == 2
    assert [item["purpose"] for item in acceptance["followups"]] == ["review", "test"]

    out = kc.run_slash(
        f"review {tid} approve --reviewer ralph --summary 'too early' --json"
    )
    assert "review follow-up gate is not satisfied" in out


def test_run_slash_plan_review_dispatch_dry_run_scopes_to_followups(
    kanban_home,
    tmp_path,
    all_assignees_spawnable,
):
    changed_files = [f"pkg/module_{index}.py" for index in range(8)]
    metadata = {
        "worker_lane": {"name": "codex-deep", "kind": "codex_cli", "exit_code": 0},
        "verification": {"commands": ["pytest -q"], "summary": "passed"},
        "git": {
            "changed_files": changed_files,
            "diff_summary": "\n".join(f" {path} | 2 +-" for path in changed_files),
        },
        "review": {"required": True, "reason": "Codex completed; Hermes review required"},
    }
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="plan and dispatch followups via slash",
            assignee="codex-deep",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        unrelated = kb.create_task(
            conn,
            title="unrelated",
            assignee="alice",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        task = kb.claim_task(conn, tid, claimer="worker:codex-deep")
        assert task is not None
        assert kb.block_task(
            conn,
            tid,
            reason="review-required: Codex completed; Hermes review required",
            expected_run_id=task.current_run_id,
            metadata=metadata,
        )

    payload = json.loads(kc.run_slash(
        f"plan-review {tid} --dispatch --dry-run --json"
    ))
    spawned_ids = {item["task_id"] for item in payload["dispatch"]["spawned"]}
    expected_ids = {
        payload["review_task_id"],
        payload["test_task_id"],
        *payload["review_shard_task_ids"],
    }

    with kb.connect() as conn:
        unrelated_task = kb.get_task(conn, unrelated)
        review_task = kb.get_task(conn, payload["review_task_id"])
        test_task = kb.get_task(conn, payload["test_task_id"])
        shard_task = kb.get_task(conn, payload["review_shard_task_ids"][0])

    assert len(payload["review_shard_task_ids"]) == 1
    assert spawned_ids == expected_ids
    assert unrelated_task.status == "ready"
    assert review_task.status == "ready"
    assert shard_task.status == "ready"
    assert test_task.status == "ready"


def test_run_slash_verify_runs_configured_acceptance_check(
    kanban_home,
    tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "ok.txt").write_text("ok\n", encoding="utf-8")
    (kanban_home / "config.yaml").write_text(
        "kanban:\n"
        "  acceptance_checks:\n"
        "    exact-file:\n"
        "      argv: [python3, -c, \"from pathlib import Path; "
        "assert Path('ok.txt').read_text() == 'ok\\\\n'\"]\n",
        encoding="utf-8",
    )
    metadata = {
        "worker_lane": {"name": "codex-deep", "kind": "codex_cli", "exit_code": 0},
        "review": {"required": True, "reason": "Codex completed; Hermes review required"},
    }
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="verify via slash",
            assignee="codex-deep",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        task = kb.claim_task(conn, tid, claimer="worker:codex-deep")
        assert task is not None
        assert kb.block_task(
            conn,
            tid,
            reason="review-required: Codex completed; Hermes review required",
            expected_run_id=task.current_run_id,
            metadata=metadata,
        )

    payload = json.loads(kc.run_slash(f"verify {tid} exact-file --json"))
    acceptance = json.loads(kc.run_slash(f"acceptance {tid} --json"))

    assert payload["checks"][0]["name"] == "exact-file"
    assert payload["checks"][0]["passed"] is True
    assert acceptance["acceptance_check_gate"]["ready"] is True


def test_run_slash_acceptance_check_request_runs_task_scoped_file_check(
    kanban_home,
    tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "ok.txt").write_text("ok\n", encoding="utf-8")
    req = tmp_path / "acceptance.yaml"
    req.write_text(
        "acceptance_check_request:\n"
        "  name: expected-file\n"
        "  type: file_content\n"
        "  path: ok.txt\n"
        "  equals: \"ok\\n\"\n",
        encoding="utf-8",
    )
    metadata = {
        "worker_lane": {"name": "codex-deep", "kind": "codex_cli", "exit_code": 0},
        "review": {"required": True, "reason": "Codex completed; Hermes review required"},
    }
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="acceptance request via slash",
            assignee="codex-deep",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        task = kb.claim_task(conn, tid, claimer="worker:codex-deep")
        assert task is not None
        assert kb.block_task(
            conn,
            tid,
            reason="review-required: Codex completed; Hermes review required",
            expected_run_id=task.current_run_id,
            metadata=metadata,
        )

    requested = json.loads(kc.run_slash(
        f"acceptance-check-request {tid} {req} --json"
    ))
    verified = json.loads(kc.run_slash(f"verify {tid} --json"))

    assert requested["request"]["name"] == "expected-file"
    assert requested["acceptance_check_gate"]["missing"] == 1
    assert verified["checks"][0]["type"] == "file_content"
    assert verified["checks"][0]["passed"] is True


def test_run_slash_acceptance_check_request_runs_command_template(
    kanban_home,
    tmp_path,
):
    workspace = tmp_path / "workspace"
    tests_dir = workspace / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_smoke.py").write_text(
        "def test_ok():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    (kanban_home / "config.yaml").write_text(
        "kanban:\n"
        "  acceptance_templates:\n"
        "    pytest-target:\n"
        f"      argv_template: [{json.dumps(sys.executable)}, -m, pytest, \"{{target}}\", -q]\n"
        "      allowed_args: [target]\n"
        "      arg_types:\n"
        "        target: relative_path\n",
        encoding="utf-8",
    )
    req = tmp_path / "acceptance-template.yaml"
    req.write_text(
        "acceptance_check_request:\n"
        "  name: pytest-smoke\n"
        "  type: command_template\n"
        "  template: pytest-target\n"
        "  args:\n"
        "    target: tests/test_smoke.py\n",
        encoding="utf-8",
    )
    metadata = {
        "worker_lane": {"name": "codex-deep", "kind": "codex_cli", "exit_code": 0},
        "review": {"required": True, "reason": "Codex completed; Hermes review required"},
    }
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="command template request via slash",
            assignee="codex-deep",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        task = kb.claim_task(conn, tid, claimer="worker:codex-deep")
        assert task is not None
        assert kb.block_task(
            conn,
            tid,
            reason="review-required: Codex completed; Hermes review required",
            expected_run_id=task.current_run_id,
            metadata=metadata,
        )

    requested = json.loads(kc.run_slash(
        f"acceptance-check-request {tid} {req} --json"
    ))
    verified = json.loads(kc.run_slash(f"verify {tid} --json"))

    assert requested["request"]["type"] == "command_template"
    assert verified["checks"][0]["type"] == "command_template"
    assert verified["checks"][0]["passed"] is True


def test_run_slash_advance_acceptance_dry_run_plans_scoped_followups(
    kanban_home,
    tmp_path,
    all_assignees_spawnable,
):
    changed_files = [f"pkg/module_{index}.py" for index in range(8)]
    metadata = {
        "worker_lane": {"name": "codex-deep", "kind": "codex_cli", "exit_code": 0},
        "verification": {"commands": ["pytest -q"], "summary": "passed"},
        "git": {
            "changed_files": changed_files,
            "diff_summary": "\n".join(f" {path} | 2 +-" for path in changed_files),
        },
        "review": {"required": True, "reason": "Codex completed; Hermes review required"},
    }
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="advance via slash",
            assignee="codex-deep",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        unrelated = kb.create_task(
            conn,
            title="unrelated",
            assignee="alice",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        task = kb.claim_task(conn, tid, claimer="worker:codex-deep")
        assert task is not None
        assert kb.block_task(
            conn,
            tid,
            reason="review-required: Codex completed; Hermes review required",
            expected_run_id=task.current_run_id,
            metadata=metadata,
        )

    payload = json.loads(kc.run_slash(
        f"advance-acceptance {tid} --dry-run --json"
    ))
    plan = payload["steps"][0]["plan"]
    spawned_ids = {item["task_id"] for item in payload["steps"][1]["dispatch"]["spawned"]}
    expected_ids = {
        plan["review_task_id"],
        plan["test_task_id"],
        *plan["review_shard_task_ids"],
    }

    with kb.connect() as conn:
        unrelated_task = kb.get_task(conn, unrelated)
        review_task = kb.get_task(conn, plan["review_task_id"])
        test_task = kb.get_task(conn, plan["test_task_id"])
        shard_task = kb.get_task(conn, plan["review_shard_task_ids"][0])

    assert [step["kind"] for step in payload["steps"]] == [
        "plan_review_followups",
        "dispatch_followups",
    ]
    assert len(plan["review_shard_task_ids"]) == 1
    assert spawned_ids == expected_ids
    assert unrelated_task.status == "ready"
    assert review_task.status == "ready"
    assert shard_task.status == "ready"
    assert test_task.status == "ready"
    assert payload["final"]["recommended_action"] == "wait_for_followups"


def test_run_slash_advance_acceptance_blocks_on_missing_followup_lane(
    kanban_home,
    tmp_path,
    monkeypatch,
    request,
):
    from hermes_cli import profiles
    from hermes_cli.worker_lanes import WorkerLane, clear_worker_lanes, register_worker_lane

    clear_worker_lanes()
    request.addfinalizer(clear_worker_lanes)
    register_worker_lane(WorkerLane(
        name="codex-review",
        kind="codex_cli",
        description="review lane",
        spawn_fn=lambda task, workspace, **kwargs: 123,
        source="test",
    ))
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    metadata = {
        "worker_lane": {"name": "codex-deep", "kind": "codex_cli", "exit_code": 0},
        "verification": {"commands": ["pytest -q"], "summary": "passed"},
        "review": {"required": True, "reason": "Codex completed; Hermes review required"},
    }
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="advance missing followup lane via slash",
            assignee="codex-deep",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        task = kb.claim_task(conn, tid, claimer="worker:codex-deep")
        assert task is not None
        assert kb.block_task(
            conn,
            tid,
            reason="review-required: Codex completed; Hermes review required",
            expected_run_id=task.current_run_id,
            metadata=metadata,
        )

    payload = json.loads(kc.run_slash(f"advance-acceptance {tid} --loop --json"))

    assert payload["stop_reason"] == "blocked"
    assert payload["iterations"][0]["steps"][-1]["kind"] == "blocked"
    assert payload["iterations"][0]["steps"][-1]["missing_lanes"][0]["assignee"] == "codex-test"


def test_run_slash_advance_acceptance_no_request_changes_reports_blocked(
    kanban_home,
    tmp_path,
):
    metadata = {
        "worker_lane": {"name": "codex-deep", "kind": "codex_cli", "exit_code": 0},
        "verification": {"commands": ["pytest -q"], "summary": "passed"},
        "review": {"required": True, "reason": "Codex completed; Hermes review required"},
    }
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="advance via slash no request changes",
            assignee="codex-deep",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        task = kb.claim_task(conn, tid, claimer="worker:codex-deep")
        assert task is not None
        assert kb.block_task(
            conn,
            tid,
            reason="review-required: Codex completed; Hermes review required",
            expected_run_id=task.current_run_id,
            metadata=metadata,
        )
        plan = kb.plan_review_followups(conn, tid)
        review = kb.claim_task(conn, plan.review_task_id, claimer="worker:codex-review")
        assert review is not None
        assert kb.block_task(
            conn,
            plan.review_task_id,
            reason="review-required: Codex completed; Hermes review required",
            expected_run_id=review.current_run_id,
            metadata={
                "worker_lane": {"name": "codex-review", "kind": "codex_cli", "exit_code": 0},
                "verification": {"summary": "Verdict: request_changes"},
                "review": {"required": True},
            },
        )
        test = kb.claim_task(conn, plan.test_task_id, claimer="worker:codex-test")
        assert test is not None
        assert kb.block_task(
            conn,
            plan.test_task_id,
            reason="review-required: Codex completed; Hermes review required",
            expected_run_id=test.current_run_id,
            metadata={
                "worker_lane": {"name": "codex-test", "kind": "codex_cli", "exit_code": 0},
                "verification": {"summary": "Verdict: pass"},
                "review": {"required": True},
            },
        )

    payload = json.loads(kc.run_slash(
        f"advance-acceptance {tid} --no-request-changes --json"
    ))

    with kb.connect() as conn:
        task_after = kb.get_task(conn, tid)
        comments = kb.list_comments(conn, tid)

    assert payload["steps"][0]["kind"] == "blocked"
    assert payload["steps"][0]["review_followup_gate"]["failed"] == 1
    assert task_after.status == "blocked"
    assert comments == []


def test_run_slash_advance_acceptance_loop_approves_ready_gates(
    kanban_home,
    tmp_path,
):
    metadata = {
        "worker_lane": {"name": "codex-deep", "kind": "codex_cli", "exit_code": 0},
        "verification": {"commands": ["pytest -q"], "summary": "passed"},
        "review": {"required": True, "reason": "Codex completed; Hermes review required"},
    }
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="advance loop via slash",
            assignee="codex-deep",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        task = kb.claim_task(conn, tid, claimer="worker:codex-deep")
        assert task is not None
        assert kb.block_task(
            conn,
            tid,
            reason="review-required: Codex completed; Hermes review required",
            expected_run_id=task.current_run_id,
            metadata=metadata,
        )
        plan = kb.plan_review_followups(conn, tid)
        review = kb.claim_task(conn, plan.review_task_id, claimer="worker:codex-review")
        assert review is not None
        assert kb.block_task(
            conn,
            plan.review_task_id,
            reason="review-required: Codex completed; Hermes review required",
            expected_run_id=review.current_run_id,
            metadata={
                "worker_lane": {
                    "name": "codex-review",
                    "kind": "codex_cli",
                    "exit_code": 0,
                },
                "verification": {"summary": "Verdict: approve"},
                "review": {"required": True},
            },
        )
        test = kb.claim_task(conn, plan.test_task_id, claimer="worker:codex-test")
        assert test is not None
        assert kb.block_task(
            conn,
            plan.test_task_id,
            reason="review-required: Codex completed; Hermes review required",
            expected_run_id=test.current_run_id,
            metadata={
                "worker_lane": {
                    "name": "codex-test",
                    "kind": "codex_cli",
                    "exit_code": 0,
                },
                "verification": {"summary": "Verdict: pass"},
                "review": {"required": True},
            },
        )

    payload = json.loads(kc.run_slash(
        f"advance-acceptance {tid} --loop --json"
    ))

    with kb.connect() as conn:
        task_after = kb.get_task(conn, tid)

    assert payload["stop_reason"] == "done"
    assert payload["iteration_count"] == 1
    assert payload["iterations"][0]["steps"][0]["kind"] == "approve"
    assert payload["final"]["recommended_action"] == "done"
    assert task_after.status == "done"


def test_run_slash_advance_goal_dry_run_scopes_child_dispatch(
    kanban_home,
    tmp_path,
    all_assignees_spawnable,
):
    with kb.connect() as conn:
        root = kb.create_task(
            conn,
            title="goal via slash",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
            triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[{"title": "implement", "assignee": "codex-deep"}],
            author="planner",
        )
        assert child_ids is not None
        unrelated = kb.create_task(
            conn,
            title="unrelated",
            assignee="alice",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )

    payload = json.loads(kc.run_slash(
        f"advance-goal {root} --dry-run --json"
    ))
    spawned_ids = {item["task_id"] for item in payload["steps"][0]["dispatch"]["spawned"]}

    with kb.connect() as conn:
        child = kb.get_task(conn, child_ids[0])
        unrelated_task = kb.get_task(conn, unrelated)

    assert payload["steps"][0]["kind"] == "dispatch_goal_children"
    assert spawned_ids == {child_ids[0]}
    assert child.status == "ready"
    assert unrelated_task.status == "ready"
    assert payload["final"]["task"]["status"] == "todo"


def test_run_slash_advance_goal_loop_stops_on_running_child(
    kanban_home,
    tmp_path,
):
    with kb.connect() as conn:
        root = kb.create_task(
            conn,
            title="goal loop via slash",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
            triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[{"title": "implement", "assignee": "codex-deep"}],
            author="planner",
        )
        assert child_ids is not None
        child = child_ids[0]
        running = kb.claim_task(conn, child, claimer="worker:codex-deep")
        assert running is not None
        before = kb.get_task(conn, child)

    payload = json.loads(kc.run_slash(
        f"advance-goal {root} --loop --no-dispatch --json"
    ))

    with kb.connect() as conn:
        after = kb.get_task(conn, child)

    assert payload["stop_reason"] == "waiting"
    assert payload["iteration_count"] == 1
    assert payload["iterations"][0]["steps"][0]["kind"] == "wait_for_child"
    assert after.status == "running"
    assert after.claim_lock == before.claim_lock


def test_run_slash_advance_goal_without_task_id_reads_session_goal(
    kanban_home,
    tmp_path,
    monkeypatch,
    all_assignees_spawnable,
):
    from hermes_cli.goals import create_kanban_task_from_goal

    session_id = "cli-session-advance"
    monkeypatch.setenv("HERMES_SESSION_ID", session_id)
    root = create_kanban_task_from_goal(
        "cli session advance goal",
        session_id=session_id,
        assignee="orchestrator",
        workspace_kind="dir",
        workspace_path=str(tmp_path),
    )
    with kb.connect() as conn:
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[{"title": "implement", "assignee": "codex-deep"}],
            author="planner",
        )
        assert child_ids is not None
        unrelated = kb.create_task(
            conn,
            title="unrelated",
            assignee="alice",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )

    payload = json.loads(kc.run_slash("advance-goal --dry-run --json"))
    text = kc.run_slash("advance-goal --dry-run")
    spawned_ids = {item["task_id"] for item in payload["steps"][0]["dispatch"]["spawned"]}

    with kb.connect() as conn:
        child = kb.get_task(conn, child_ids[0])
        unrelated_task = kb.get_task(conn, unrelated)

    assert payload["resolved_from_session_goal"] is True
    assert payload["session_id"] == session_id
    assert payload["task_id"] == root
    assert spawned_ids == {child_ids[0]}
    assert f"current session goal ({session_id})" in text
    assert child.status == "ready"
    assert unrelated_task.status == "ready"


def test_run_slash_advance_goal_without_task_id_requires_session_goal(
    kanban_home,
    monkeypatch,
):
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)

    out = kc.run_slash("advance-goal --json")

    assert "task_id is required unless HERMES_SESSION_ID" in out


def test_run_slash_advance_controller_advances_goal(
    kanban_home,
    tmp_path,
):
    metadata = {
        "worker_lane": {"name": "codex-deep", "kind": "codex_cli", "exit_code": 0},
        "verification": {"commands": ["pytest -q"], "summary": "passed"},
        "review": {"required": True, "reason": "Codex completed; Hermes review required"},
    }
    with kb.connect() as conn:
        root = kb.create_task(
            conn,
            title="controller slash root",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
            triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[{"title": "implement", "assignee": "codex-deep"}],
            author="planner",
        )
        assert child_ids is not None
        child = child_ids[0]
        claimed = kb.claim_task(conn, child, claimer="worker:codex-deep")
        assert claimed is not None
        assert kb.block_task(
            conn,
            child,
            reason="review-required: Codex completed; Hermes review required",
            expected_run_id=claimed.current_run_id,
            metadata=metadata,
        )
        plan = kb.plan_review_followups(conn, child)
        review = kb.claim_task(conn, plan.review_task_id, claimer="worker:codex-review")
        assert review is not None
        assert kb.block_task(
            conn,
            plan.review_task_id,
            reason="review-required: Codex completed; Hermes review required",
            expected_run_id=review.current_run_id,
            metadata={
                "worker_lane": {"name": "codex-review", "kind": "codex_cli", "exit_code": 0},
                "verification": {"summary": "Verdict: approve"},
                "review": {"required": True},
            },
        )
        test = kb.claim_task(conn, plan.test_task_id, claimer="worker:codex-test")
        assert test is not None
        assert kb.block_task(
            conn,
            plan.test_task_id,
            reason="review-required: Codex completed; Hermes review required",
            expected_run_id=test.current_run_id,
            metadata={
                "worker_lane": {"name": "codex-test", "kind": "codex_cli", "exit_code": 0},
                "verification": {"summary": "Verdict: pass"},
                "review": {"required": True},
            },
        )

    payload = json.loads(kc.run_slash("advance-controller --json"))

    with kb.connect() as conn:
        root_after = kb.get_task(conn, root)
        child_after = kb.get_task(conn, child)

    assert payload["item_count"] == 1
    assert payload["items"][0]["kind"] == "goal"
    assert payload["items"][0]["task_id"] == root
    assert payload["items"][0]["stop_reason"] == "done"
    assert root_after.status == "done"
    assert child_after.status == "done"


def test_run_slash_goal_decompose_advance_dispatches_created_children(
    kanban_home,
    tmp_path,
    monkeypatch,
    all_assignees_spawnable,
):
    from hermes_cli.kanban_decompose import DecomposeOutcome

    def fake_decompose(task_id, *, author=None, timeout=None):
        with kb.connect() as conn:
            child_ids = kb.decompose_triage_task(
                conn,
                task_id,
                root_assignee="orchestrator",
                children=[{"title": "implement goal", "assignee": "codex-deep"}],
                author=author or "test",
            )
        return DecomposeOutcome(
            task_id=task_id,
            ok=bool(child_ids),
            reason="fake decomposed",
            fanout=True,
            child_ids=child_ids or [],
        )

    monkeypatch.setattr("hermes_cli.kanban_decompose.decompose_task", fake_decompose)
    with kb.connect() as conn:
        unrelated = kb.create_task(
            conn,
            title="unrelated ready task",
            assignee="alice",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )

    payload = json.loads(kc.run_slash(
        "goal 'ship via goal advance' "
        f"--workspace dir:{tmp_path} "
        "--assignee orchestrator "
        "--decompose "
        "--advance "
        "--advance-dry-run "
        "--json"
    ))

    advance = payload["advance"]
    spawned_ids = {
        item["task_id"]
        for step in advance["steps"]
        if step["kind"] == "dispatch_goal_children"
        for item in step["dispatch"]["spawned"]
    }

    with kb.connect() as conn:
        root = kb.get_task(conn, payload["task_id"])
        child = kb.get_task(conn, payload["child_ids"][0])
        unrelated_task = kb.get_task(conn, unrelated)

    assert payload["decompose"]["ok"] is True
    assert spawned_ids == {payload["child_ids"][0]}
    assert root.status == "todo"
    assert child.status == "ready"
    assert unrelated_task.status == "ready"


def test_run_slash_goal_decompose_advance_loop_completes_reviewed_codex_lane(
    kanban_home,
    tmp_path,
    monkeypatch,
    request,
):
    """Regression for the real /goal -> Codex smoke path.

    Follow-up review/test workers normally finish by blocking with
    review-required evidence.  The source implementation and root goal become
    done after the gates consume that evidence; the follow-up task rows do not
    need to transition to done.
    """
    from hermes_cli.kanban_decompose import DecomposeOutcome
    from hermes_cli.worker_lanes import (
        WorkerLane,
        clear_worker_lanes,
        register_worker_lane,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "goal_loop.txt"
    target.write_text("initial\n", encoding="utf-8")
    (kanban_home / "config.yaml").write_text(
        "kanban:\n"
        "  acceptance_checks:\n"
        "    exact-goal-file:\n"
        "      argv:\n"
        "        - python3\n"
        "        - -c\n"
        "        - 'from pathlib import Path; "
        "assert Path(\"goal_loop.txt\").read_bytes() == "
        "bytes([100,111,110,101,10])'\n"
        "      timeout_seconds: 30\n",
        encoding="utf-8",
    )

    clear_worker_lanes()
    request.addfinalizer(clear_worker_lanes)

    def fake_spawn(task, workspace_path, board=None):
        lane = task.assignee or ""
        run_id = task.current_run_id
        progress = {
            "worker_lane": lane,
            "lane": lane,
            "worker_kind": "codex_cli",
            "run_id": run_id,
            "items": [{"index": 1, "status": "done", "text": "finish goal file"}],
        }
        if lane == "codex-fast":
            Path(workspace_path, "goal_loop.txt").write_text("done\n", encoding="utf-8")
            verification = {
                "commands": ["python3 exact byte check"],
                "summary": "- command: python3 exact byte check\n  result: passed",
            }
            output_tail = (
                "Progress:\n"
                "- [x] finish goal file\n\n"
                "Changed files:\n"
                "- goal_loop.txt\n\n"
                "Verification:\n"
                "- command: python3 exact byte check\n"
                "  result: passed\n\n"
                "Recommended reviewer action:\n"
                "- approve\n"
            )
        else:
            verdict = "approve" if lane == "codex-review" else "pass"
            verification = {
                "commands": ["python3 exact byte check"],
                "summary": f"Verdict: {verdict}\npassed",
            }
            output_tail = f"Progress:\n- [x] inspect bounded evidence\n\nVerdict: {verdict}\n"
        metadata = {
            "worker_lane": {
                "name": lane,
                "kind": "codex_cli",
                "exit_code": 0,
                "timed_out": False,
                "binary_missing": False,
                "output_tail": output_tail,
            },
            "verification": verification,
            "review": {
                "required": True,
                "reason": "Codex completed; Hermes review required",
            },
        }
        with kb.connect(board=board) as conn:
            kb.record_task_event(conn, task.id, "worker_heartbeat", progress, run_id=run_id)
            kb.record_task_event(conn, task.id, "worker_progress", progress, run_id=run_id)
            assert kb.block_task(
                conn,
                task.id,
                reason="review-required: Codex completed; Hermes review required",
                expected_run_id=run_id,
                metadata=metadata,
            )
        return None

    for lane_name in ("codex-fast", "codex-review", "codex-test"):
        register_worker_lane(WorkerLane(
            name=lane_name,
            kind="codex_cli",
            description=f"Fake {lane_name} lane",
            spawn_fn=fake_spawn,
            success_policy="block_for_review",
            max_concurrency=1,
            source="test",
        ))

    def fake_decompose(task_id, *, author=None, timeout=None):
        with kb.connect() as conn:
            child_ids = kb.decompose_triage_task(
                conn,
                task_id,
                root_assignee="orchestrator",
                children=[{
                    "title": "Implement goal loop file",
                    "assignee": "codex-fast",
                    "body": "Write goal_loop.txt as exactly done newline.",
                }],
                author=author or "test",
            )
        return DecomposeOutcome(
            task_id=task_id,
            ok=bool(child_ids),
            reason="fake decomposed",
            fanout=True,
            child_ids=child_ids or [],
        )

    monkeypatch.setattr("hermes_cli.kanban_decompose.decompose_task", fake_decompose)

    payload = json.loads(kc.run_slash(
        "goal 'ship reviewed codex lane goal' "
        f"--workspace dir:{workspace} "
        "--assignee orchestrator "
        "--decompose "
        "--advance "
        "--loop "
        "--max-iterations 4 "
        "--json"
    ))

    root_id = payload["task_id"]
    child_id = payload["child_ids"][0]
    with kb.connect() as conn:
        root = kb.get_task(conn, root_id)
        child = kb.get_task(conn, child_id)
        root_snapshot = kb.task_progress_snapshot(conn, root_id, include_children=True)
        acceptance = kb.task_acceptance_snapshot(conn, child_id)
        child_run = kb.latest_run(conn, child_id)
        followup_statuses = {
            item["purpose"]: kb.get_task(conn, item["task_id"]).status
            for item in acceptance["followups"]
        }

    assert payload["decompose"]["ok"] is True
    assert payload["advance"]["stop_reason"] == "done"
    assert root.status == "done"
    assert child.status == "done"
    assert child_run.outcome == "completed"
    assert child_run.metadata["worker_lane"]["name"] == "codex-fast"
    assert acceptance["recommended_action"] == "done"
    assert acceptance["review_followup_gate"]["ready"] is True
    assert acceptance["acceptance_check_gate"]["ready"] is True
    assert followup_statuses == {"review": "blocked", "test": "blocked"}
    assert root_snapshot.child_summary["done"] == 1
    assert target.read_text(encoding="utf-8") == "done\n"


def test_run_slash_goal_decompose_fanout_false_dispatches_goal_task(
    kanban_home,
    tmp_path,
    monkeypatch,
    request,
):
    from hermes_cli.kanban_decompose import DecomposeOutcome
    from hermes_cli.worker_lanes import (
        WorkerLane,
        clear_worker_lanes,
        register_worker_lane,
    )

    workspace = tmp_path / "single-workspace"
    workspace.mkdir()
    target = workspace / "single_goal.txt"
    target.write_text("initial\n", encoding="utf-8")
    (kanban_home / "config.yaml").write_text(
        "kanban:\n"
        "  acceptance_checks:\n"
        "    exact-single-goal-file:\n"
        "      argv:\n"
        "        - python3\n"
        "        - -c\n"
        "        - 'from pathlib import Path; "
        "assert Path(\"single_goal.txt\").read_bytes() == "
        "bytes([115,105,110,103,108,101,10])'\n"
        "      timeout_seconds: 30\n",
        encoding="utf-8",
    )

    clear_worker_lanes()
    request.addfinalizer(clear_worker_lanes)

    def fake_spawn(task, workspace_path, board=None):
        lane = task.assignee or ""
        run_id = task.current_run_id
        if lane == "codex-fast":
            Path(workspace_path, "single_goal.txt").write_text("single\n", encoding="utf-8")
            verdict = None
            verification = {
                "commands": ["python3 exact byte check"],
                "summary": "- command: python3 exact byte check\n  result: passed",
            }
            output_tail = (
                "Progress:\n"
                "- [x] finish single goal file\n\n"
                "Changed files:\n"
                "- single_goal.txt\n\n"
                "Verification:\n"
                "- command: python3 exact byte check\n"
                "  result: passed\n"
            )
        else:
            verdict = "approve" if lane == "codex-review" else "pass"
            verification = {
                "commands": ["python3 exact byte check"],
                "summary": f"Verdict: {verdict}\npassed",
            }
            output_tail = f"Verdict: {verdict}\n"
        metadata = {
            "worker_lane": {
                "name": lane,
                "kind": "codex_cli",
                "exit_code": 0,
                "timed_out": False,
                "binary_missing": False,
                "output_tail": output_tail,
            },
            "verification": verification,
            "review": {
                "required": True,
                "reason": "Codex completed; Hermes review required",
            },
        }
        progress = {
            "worker_lane": lane,
            "worker_kind": "codex_cli",
            "run_id": run_id,
            "items": [{
                "index": 1,
                "status": "done",
                "text": f"{verdict or 'finish'} single goal",
            }],
        }
        with kb.connect(board=board) as conn:
            kb.record_task_event(conn, task.id, "worker_heartbeat", progress, run_id=run_id)
            kb.record_task_event(conn, task.id, "worker_progress", progress, run_id=run_id)
            assert kb.block_task(
                conn,
                task.id,
                reason="review-required: Codex completed; Hermes review required",
                expected_run_id=run_id,
                metadata=metadata,
            )
        return None

    for lane_name in ("codex-fast", "codex-review", "codex-test"):
        register_worker_lane(WorkerLane(
            name=lane_name,
            kind="codex_cli",
            description=f"Fake {lane_name} lane",
            spawn_fn=fake_spawn,
            success_policy="block_for_review",
            max_concurrency=1,
            source="test",
        ))

    def fake_decompose(task_id, *, author=None, timeout=None):
        with kb.connect() as conn:
            assert kb.specify_triage_task(
                conn,
                task_id,
                title="Implement single goal file",
                body="Write single_goal.txt as exactly single newline.",
                assignee="codex-fast",
                author=author or "test",
            )
        return DecomposeOutcome(
            task_id=task_id,
            ok=True,
            reason="single task (no fanout)",
            fanout=False,
            child_ids=[],
            new_title="Implement single goal file",
        )

    monkeypatch.setattr("hermes_cli.kanban_decompose.decompose_task", fake_decompose)

    payload = json.loads(kc.run_slash(
        "goal 'ship single codex lane goal' "
        f"--workspace dir:{workspace} "
        "--decompose "
        "--advance "
        "--loop "
        "--max-iterations 4 "
        "--json"
    ))

    task_id = payload["task_id"]
    step_kinds = [
        step["kind"]
        for iteration in payload["advance"]["iterations"]
        for step in iteration["steps"]
    ]
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        snapshot = kb.task_progress_snapshot(conn, task_id, include_children=True)
        acceptance = kb.task_acceptance_snapshot(conn, task_id)
        latest_run = kb.latest_run(conn, task_id)

    assert payload["decompose"]["ok"] is True
    assert payload["decompose"]["fanout"] is False
    assert payload["child_ids"] == []
    assert payload["advance"]["stop_reason"] == "done"
    assert "dispatch_goal_task" in step_kinds
    assert "advance_goal_task_acceptance" in step_kinds
    assert task.status == "done"
    assert task.assignee == "codex-fast"
    assert snapshot.child_summary["total"] == 2
    assert snapshot.child_summary["done"] == 2
    assert snapshot.child_summary["review_required"] == 0
    assert snapshot.child_summary["status_counts"] == {"done": 2}
    assert snapshot.child_summary["recommended_actions"] == {"done": 2}
    assert snapshot.child_summary["relationship_counts"] == {
        "review_followup": 1,
        "test_followup": 1,
    }
    assert {
        child["acceptance"]["followup_gate_item"]["state"]
        for child in snapshot.children
    } == {"satisfied"}
    assert snapshot.worker_progress["items"][0]["text"] == "finish single goal"
    assert acceptance["recommended_action"] == "done"
    assert acceptance["review_followup_gate"]["ready"] is True
    assert acceptance["acceptance_check_gate"]["ready"] is True
    assert latest_run.outcome == "completed"
    assert latest_run.metadata["worker_lane"]["name"] == "codex-fast"
    assert latest_run.metadata["review"]["source_run_id"] == 1
    assert target.read_text(encoding="utf-8") == "single\n"


def test_run_slash_goal_decompose_advance_can_reenter_existing_root(
    kanban_home,
    tmp_path,
    monkeypatch,
    all_assignees_spawnable,
):
    from hermes_cli.kanban_decompose import DecomposeOutcome

    def fake_decompose(task_id, *, author=None, timeout=None):
        with kb.connect() as conn:
            task = kb.get_task(conn, task_id)
            if task and task.status != "triage":
                return DecomposeOutcome(
                    task_id=task_id,
                    ok=False,
                    reason=f"task is not in triage (status={task.status!r})",
                )
            child_ids = kb.decompose_triage_task(
                conn,
                task_id,
                root_assignee="orchestrator",
                children=[{"title": "implement idempotent goal", "assignee": "codex-deep"}],
                author=author or "test",
            )
        return DecomposeOutcome(
            task_id=task_id,
            ok=bool(child_ids),
            reason="fake decomposed",
            fanout=True,
            child_ids=child_ids or [],
        )

    monkeypatch.setattr("hermes_cli.kanban_decompose.decompose_task", fake_decompose)
    command = (
        "goal 'ship idempotent goal advance' "
        f"--workspace dir:{tmp_path} "
        "--assignee orchestrator "
        "--idempotency-key stable-goal "
        "--decompose "
        "--advance "
        "--advance-dry-run "
        "--json"
    )

    first = json.loads(kc.run_slash(command))
    second = json.loads(kc.run_slash(command))

    assert second["task_id"] == first["task_id"]
    assert second["decompose"]["ok"] is False
    assert "task is not in triage" in second["decompose"]["reason"]
    assert second["advance"]["steps"][0]["kind"] == "dispatch_goal_children"
    assert second["child_ids"] == first["child_ids"]


def test_run_slash_worker_lane_request_validates_without_enabling(
    kanban_home, tmp_path,
):
    from hermes_cli.worker_lanes import get_worker_lane

    req = tmp_path / "lane.yaml"
    req.write_text(
        "worker_lane_request:\n"
        "  name: codex-cli-request\n"
        "  type: codex_cli\n"
        "  model: gpt-5.4-mini\n"
        "  sandbox: workspace-write\n"
        "  approval: never\n"
        "  max_concurrency: 1\n"
        "  success_policy: block_for_review\n",
        encoding="utf-8",
    )

    payload = json.loads(kc.run_slash(f"worker-lane-request {req} --json"))

    assert payload["valid"] is True
    assert payload["enabled"] is False
    assert payload["config"]["name"] == "codex-cli-request"
    assert get_worker_lane("codex-cli-request") is None


def test_run_slash_worker_lane_request_records_task_audit_event(
    kanban_home, tmp_path,
):
    from hermes_cli.worker_lanes import clear_worker_lanes, get_worker_lane

    clear_worker_lanes()
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="cli lane request root")
        kb.record_task_event(
            conn,
            task_id,
            "worker_lane_request_intent",
            {
                "requests": [
                    {
                        "config": {
                            "name": "codex-cli-audit",
                            "type": "codex_cli",
                            "model": "gpt-5.4-mini",
                            "sandbox": "workspace-write",
                            "approval": "never",
                            "max_concurrency": 1,
                            "success_policy": "block_for_review",
                        }
                    }
                ],
                "approval_required": True,
            },
        )
        source_event_id = kb.list_events(conn, task_id)[-1].id

    req = tmp_path / "lane.json"
    req.write_text(json.dumps({
        "worker_lane_request": {
            "name": "codex-cli-audit",
            "type": "codex_cli",
            "model": "gpt-5.4-mini",
            "sandbox": "workspace-write",
            "approval": "never",
            "max_concurrency": 1,
            "success_policy": "block_for_review",
        }
    }), encoding="utf-8")

    payload = json.loads(kc.run_slash(
        f"worker-lane-request {req} --enable "
        f"--task-id {task_id} --source-event-id {source_event_id} "
        "--requested-by cli-test --json"
    ))

    assert payload["enabled"] is True
    assert payload["task_id"] == task_id
    assert payload["source_event_id"] == source_event_id
    assert payload["audit_event"] == "worker_lane_request_approved"
    assert get_worker_lane("codex-cli-audit") is not None
    with kb.connect() as conn:
        events = kb.list_events(conn, task_id)
    approved = [event for event in events if event.kind == "worker_lane_request_approved"]
    assert approved
    assert approved[-1].payload["requested_by"] == "cli-test"
    assert approved[-1].payload["source_event_id"] == source_event_id
    assert approved[-1].payload["enabled"] is True
    assert approved[-1].payload["config"]["name"] == "codex-cli-audit"


def test_run_slash_worker_lane_request_persist_enables_config_lane(
    kanban_home, tmp_path,
):
    from hermes_cli.worker_lanes import get_worker_lane
    from hermes_cli.config import read_raw_config

    req = tmp_path / "lane.json"
    req.write_text(json.dumps({
        "worker_lane_request": {
            "name": "codex-persisted",
            "type": "codex_cli",
            "model": "gpt-5.5",
            "sandbox": "workspace-write",
            "approval": "never",
            "max_concurrency": 1,
            "success_policy": "block_for_review",
            "reason": "approved by test",
        }
    }), encoding="utf-8")

    payload = json.loads(kc.run_slash(f"worker-lane-request {req} --persist --json"))

    assert payload["enabled"] is True
    assert payload["persisted"] is True
    lane = get_worker_lane("codex-persisted")
    assert lane is not None
    assert lane.source == "config"
    stored = read_raw_config()["kanban"]["worker_lanes"]["codex-persisted"]
    assert stored["type"] == "codex_cli"
    assert stored["model"] == "gpt-5.5"
    assert "reason" not in stored


def test_run_slash_worker_lane_request_rejects_shell_command(
    kanban_home, tmp_path,
):
    req = tmp_path / "lane.json"
    req.write_text(json.dumps({
        "name": "codex-bad",
        "type": "codex_cli",
        "command": "codex exec -",
    }), encoding="utf-8")

    out = kc.run_slash(f"worker-lane-request {req} --json")

    assert "may not include executable command fields" in out


def test_run_slash_diagnostics_flags_missing_review_followup_lane(
    kanban_home,
    monkeypatch,
):
    from hermes_cli.worker_lanes import WorkerLane, clear_worker_lanes, register_worker_lane

    clear_worker_lanes()
    register_worker_lane(WorkerLane(
        name="codex-review",
        kind="codex_cli",
        description="Review lane",
        spawn_fn=lambda task, workspace, **kwargs: 123,
        source="test",
    ))
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: False)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="implementation", assignee="codex-deep")
        claimed = kb.claim_task(conn, tid, claimer="worker:codex-deep")
        assert claimed is not None
        assert kb.block_task(
            conn,
            tid,
            reason="review-required",
            expected_run_id=claimed.current_run_id,
            metadata={
                "worker_lane": {"name": "codex-deep", "kind": "codex_cli", "exit_code": 0},
                "review": {"required": True, "reason": "needs review"},
            },
        )
        kb.plan_review_followups(
            conn,
            tid,
            review_assignee="codex-review",
            test_assignee="codex-test",
        )

    payload = json.loads(kc.run_slash("diagnostics --json"))

    by_id = {entry["task_id"]: entry for entry in payload}
    kinds = {diag["kind"] for diag in by_id[tid]["diagnostics"]}
    assert "review_followup_lane_missing" in kinds


def test_run_slash_goal_creates_top_level_task(kanban_home):
    payload = json.loads(kc.run_slash(
        "goal 'refactor the worker lane bridge' "
        "--session sess-goal-1 --assignee orchestrator --tenant dev "
        "--priority 3 --workspace dir:/tmp/hermes-goal-repo "
        "--max-runtime 15m --max-retries 2 --json"
    ))

    assert payload["task"]["status"] == "triage"
    assert payload["task"]["assignee"] == "orchestrator"
    assert payload["task"]["session_id"] == "sess-goal-1"
    assert payload["task"]["tenant"] == "dev"
    assert payload["task"]["priority"] == 3
    assert payload["task"]["workspace_kind"] == "dir"
    assert payload["task"]["workspace_path"] == "/tmp/hermes-goal-repo"
    assert payload["task"]["max_runtime_seconds"] == 900
    assert payload["task"]["max_retries"] == 2
    assert payload["decompose"] is None
    assert payload["child_ids"] == []

    payload2 = json.loads(kc.run_slash(
        "goal 'refactor the worker lane bridge' "
        "--session sess-goal-1 --assignee orchestrator --json"
    ))
    assert payload2["task_id"] == payload["task_id"]


def test_run_slash_goal_can_decompose_to_worker_lane(kanban_home, monkeypatch):
    from unittest.mock import MagicMock
    from hermes_cli.worker_lanes import WorkerLane, clear_worker_lanes, register_worker_lane

    clear_worker_lanes()
    register_worker_lane(WorkerLane(
        name="codex-deep",
        kind="codex_cli",
        description="Codex CLI lane for implementation work",
        spawn_fn=lambda *args, **kwargs: None,
        source="test",
    ))

    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = json.dumps({
        "fanout": True,
        "rationale": "implementation can go to codex",
        "tasks": [
            {
                "title": "Implement worker lane bridge",
                "body": "Change code and provide evidence.",
                "assignee": "codex-deep",
                "parents": [],
            }
        ],
    })
    fake_client = MagicMock()
    fake_client.chat.completions.create = MagicMock(return_value=resp)
    monkeypatch.setattr(
        "agent.auxiliary_client.get_text_auxiliary_client",
        lambda *a, **kw: (fake_client, "test-model"),
    )
    monkeypatch.setattr(
        "agent.auxiliary_client.get_auxiliary_extra_body",
        lambda *a, **kw: {},
    )
    monkeypatch.setattr(
        "hermes_cli.kanban_decompose._load_config",
        lambda: {"kanban": {"orchestrator_profile": "orchestrator", "default_assignee": "fallback"}},
    )
    monkeypatch.setattr("hermes_cli.profiles.list_profiles", lambda: [])
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name == "orchestrator")
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "orchestrator")

    payload = json.loads(kc.run_slash(
        "goal 'ship codex worker lane orchestration' "
        "--assignee orchestrator --workspace dir:/tmp/hermes-goal-repo "
        "--max-runtime 20m --max-retries 2 --decompose --json"
    ))

    assert payload["task"]["status"] == "todo"
    assert payload["decompose"]["ok"] is True
    assert payload["decompose"]["fanout"] is True
    assert len(payload["child_ids"]) == 1
    with kb.connect() as conn:
        child = kb.get_task(conn, payload["child_ids"][0])
    assert child.assignee == "codex-deep"
    assert child.status == "ready"
    assert child.workspace_kind == "dir"
    assert child.workspace_path == "/tmp/hermes-goal-repo"
    assert child.max_runtime_seconds == 1200
    assert child.max_retries == 2


# ---------------------------------------------------------------------------
# /kanban specify — slash surface (same entry point CLI + gateway use)
# ---------------------------------------------------------------------------

def test_run_slash_specify_end_to_end(kanban_home, monkeypatch):
    """The /kanban specify slash command routes through run_slash, which
    both the interactive CLI and every gateway platform use. This test
    covers both surfaces."""
    from unittest.mock import MagicMock

    # Create a triage task via the same slash surface.
    create_out = kc.run_slash("create 'rough idea' --triage")
    import re
    m = re.search(r"(t_[a-f0-9]+)", create_out)
    assert m, f"no task id in: {create_out!r}"
    tid = m.group(1)

    # Mock the auxiliary client so we don't hit a real provider.
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = (
        '{"title": "Spec: rough idea", "body": "**Goal**\\nShip it."}'
    )
    fake_client = MagicMock()
    fake_client.chat.completions.create = MagicMock(return_value=resp)
    monkeypatch.setattr(
        "agent.auxiliary_client.get_text_auxiliary_client",
        lambda *a, **kw: (fake_client, "test-model"),
    )

    # Specify via slash.
    out = kc.run_slash(f"specify {tid}")
    assert "Specified" in out
    assert tid in out

    # Task is promoted and retitled.
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.status in {"todo", "ready"}
    assert task.title == "Spec: rough idea"


def test_run_slash_specify_help_is_reachable(kanban_home):
    """`-h`/`--help` on a subcommand returns the actual help text — see
    issue #21794. argparse writes help to stdout and exits 0; run_slash
    must capture both streams and treat exit 0 as success, not error."""
    out = kc.run_slash("specify --help")
    assert "specify" in out.lower()
    # Help dump should NOT come back wrapped as a usage error.
    assert not out.startswith("⚠")


# ---------------------------------------------------------------------------
# /kanban help / no-args / unknown-action UX (issue #21794)
# ---------------------------------------------------------------------------

def test_run_slash_bare_returns_curated_help(kanban_home):
    """Bare `/kanban` returns the curated short-help block — not a 5KB
    argparse usage dump."""
    out = kc.run_slash("")
    assert "/kanban" in out
    assert "list" in out
    assert "show" in out
    # Sanity: should be a chat-friendly size, not the raw usage tree.
    assert len(out) < 2000
    # Shouldn't surface argparse's usage-error sentinel.
    assert "usage error" not in out.lower()


@pytest.mark.parametrize("alias", ["help", "--help", "-h", "?"])
def test_run_slash_help_aliases_match_bare(kanban_home, alias):
    """Every documented help alias produces the same curated output."""
    bare = kc.run_slash("")
    out = kc.run_slash(alias)
    assert out == bare


def test_run_slash_subcommand_help_returns_help_text(kanban_home):
    """`/kanban show -h` returns the actual subcommand help, not a
    fake `(usage error: 0)` sentinel."""
    out = kc.run_slash("show -h")
    assert "task_id" in out
    assert "/kanban show" in out
    assert not out.startswith("⚠")


def test_run_slash_unknown_action_friendly_error(kanban_home):
    """Unknown subcommand surfaces a single-line usage error prefixed
    with our marker — no `(usage error: 2)` wrapping, no doubled
    `kanban kanban` prog string."""
    out = kc.run_slash("frobnicate")
    assert "/kanban" in out
    assert "frobnicate" in out
    assert "/kanban-wrap" not in out
    assert "/kanban kanban" not in out
    assert "(usage error: " not in out


def test_run_slash_missing_required_arg_friendly_error(kanban_home):
    """Missing positional argument shows the subcommand-scoped usage
    line, not the top-level kanban tree."""
    out = kc.run_slash("show")
    assert "/kanban show" in out
    assert "task_id" in out


def test_run_slash_board_override_restores_prior_env(kanban_home, monkeypatch):
    kb.create_board("alpha")
    kb.create_board("beta")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "beta")

    kc.run_slash("--board alpha list")

    assert os.environ.get("HERMES_KANBAN_BOARD") == "beta"


def test_run_slash_board_override_does_not_change_boards_show_current(kanban_home):
    kb.create_board("alpha")
    kb.create_board("beta")
    kb.set_current_board("alpha")

    out = kc.run_slash("--board beta boards show")

    assert "Current board: alpha" in out
