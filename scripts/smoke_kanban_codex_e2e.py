#!/usr/bin/env python3
"""Real Codex CLI Kanban worker-lane end-to-end smoke test.

This is intentionally not part of the normal test suite: it starts real Codex
CLI processes and therefore needs local Codex auth/network access. It uses a
temporary HERMES_HOME and workspace, preserves the operator's existing
~/.codex/CODEX_HOME, and waits only for task state written by this smoke run.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "gpt-5.4-mini"
DEFAULT_TIMEOUT = 900.0
DEFAULT_WORKER_TIMEOUT = 420


def _die(message: str, *, details: dict[str, Any] | None = None) -> "NoReturn":
    if details:
        print(json.dumps({"ok": False, "error": message, **details}, indent=2, sort_keys=True))
    else:
        print(json.dumps({"ok": False, "error": message}, indent=2, sort_keys=True))
    raise SystemExit(1)


def _run(argv: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and proc.returncode != 0:
        _die(
            "command failed",
            details={
                "argv": argv,
                "cwd": str(cwd) if cwd else None,
                "returncode": proc.returncode,
                "stdout": proc.stdout[-2000:],
                "stderr": proc.stderr[-2000:],
            },
        )
    return proc


def _write_config(home: Path, *, model: str | None, worker_timeout: int) -> None:
    model_line = f"      model: {model}\n" if model else ""
    config = f"""\
kanban:
  worker_lanes:
    codex-impl:
      type: codex_cli
{model_line}      sandbox: workspace-write
      approval: never
      max_concurrency: 1
      timeout_seconds: {worker_timeout}
      success_policy: block_for_review
    codex-review:
      type: codex_cli
{model_line}      sandbox: read-only
      approval: never
      max_concurrency: 1
      timeout_seconds: {worker_timeout}
      success_policy: block_for_review
    codex-test:
      type: codex_cli
{model_line}      sandbox: workspace-write
      approval: never
      max_concurrency: 1
      timeout_seconds: {worker_timeout}
      success_policy: block_for_review
  acceptance_checks:
    smoke-exact-file:
      argv:
        - python3
        - -c
        - 'from pathlib import Path; assert Path("smoke_result.txt").read_text() == "hermes codex e2e ok\\n"'
      timeout_seconds: 30
"""
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(config, encoding="utf-8")


def _init_workspace(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "README.md").write_text("Hermes Codex E2E smoke workspace\n", encoding="utf-8")
    _run(["git", "init"], cwd=workspace)
    _run(["git", "config", "user.email", "hermes-smoke@example.invalid"], cwd=workspace)
    _run(["git", "config", "user.name", "Hermes Smoke"], cwd=workspace)
    _run(["git", "add", "README.md"], cwd=workspace)
    _run(["git", "commit", "-m", "initial smoke workspace"], cwd=workspace)


def _setup_environment(home: Path) -> None:
    repo = str(REPO_ROOT)
    if repo not in sys.path:
        sys.path.insert(0, repo)
    os.environ["HERMES_HOME"] = str(home)
    os.environ.pop("HERMES_KANBAN_DB", None)
    os.environ.pop("HERMES_KANBAN_HOME", None)
    os.environ.pop("HERMES_KANBAN_WORKSPACES_ROOT", None)
    os.environ.pop("HERMES_KANBAN_BOARD", None)
    os.environ["PYTHONPATH"] = (
        repo
        if not os.environ.get("PYTHONPATH")
        else repo + os.pathsep + os.environ["PYTHONPATH"]
    )


def _create_task(workspace: Path, *, worker_timeout: int) -> str:
    from hermes_cli import kanban_db as kb

    body = """\
Create `smoke_result.txt` in this repository containing exactly this single
line, including the trailing newline:

hermes codex e2e ok

Using Python, this exact content is:

"hermes codex e2e ok\\n"

Do not change any other tracked file. Run a verification command with python3
that proves the file content is exactly `hermes codex e2e ok\\n`. Include the
required structured receipt with a Progress checklist, Changed files,
Verification, Remaining risks, and Recommended reviewer action.
"""
    with kb.connect() as conn:
        return kb.create_task(
            conn,
            title="real Codex Kanban e2e smoke implementation",
            body=body,
            assignee="codex-impl",
            created_by="codex-e2e-smoke",
            workspace_kind="dir",
            workspace_path=str(workspace),
            max_runtime_seconds=int(worker_timeout) + 60,
            acceptance_check_requests={
                "name": "smoke-file-content",
                "type": "file_content",
                "path": "smoke_result.txt",
                "equals": "hermes codex e2e ok\n",
                "reason": "smoke verifies the implementation artifact",
            },
        )


def _create_goal_root_and_child(workspace: Path, *, worker_timeout: int) -> tuple[str, str]:
    """Create a top-level Kanban goal and deterministic Codex child task."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.goals import create_kanban_task_from_goal

    root_id = create_kanban_task_from_goal(
        "real Codex Kanban e2e smoke goal",
        session_id="codex-e2e-smoke-session",
        assignee="orchestrator",
        workspace_kind="dir",
        workspace_path=str(workspace),
        max_runtime_seconds=int(worker_timeout) + 60,
        created_by="codex-e2e-smoke",
        idempotency_key="codex-e2e-smoke-goal",
    )
    body = """\
Implement the smoke goal by creating `smoke_result.txt` in this repository
containing exactly this single line, including the trailing newline:

hermes codex e2e ok

Using Python, this exact content is:

"hermes codex e2e ok\\n"

Do not change any other tracked file. Run a verification command with python3
that proves the file content is exactly `hermes codex e2e ok\\n`. Include the
required structured receipt with a Progress checklist, Changed files,
Verification, Remaining risks, and Recommended reviewer action.
"""
    with kb.connect() as conn:
        child_ids = kb.decompose_triage_task(
            conn,
            root_id,
            root_assignee="orchestrator",
            children=[
                {
                    "title": "real Codex Kanban e2e smoke goal implementation",
                    "body": body,
                    "assignee": "codex-impl",
                    "acceptance_check_requests": {
                        "name": "smoke-file-content",
                        "type": "file_content",
                        "path": "smoke_result.txt",
                        "equals": "hermes codex e2e ok\n",
                        "reason": "smoke verifies the implementation artifact",
                    },
                }
            ],
            author="codex-e2e-smoke",
        )
    if not child_ids:
        _die("failed to decompose smoke goal into a worker child", details={"root_task_id": root_id})
    return root_id, child_ids[0]


def _dispatch_one(task_id: str, *, max_spawn: int = 1) -> dict[str, Any]:
    from hermes_cli import kanban_db as kb

    with kb.connect() as conn:
        result = kb.dispatch_once(conn, max_spawn=max_spawn)
        snapshot = kb.task_progress_snapshot(conn, task_id, log_tail_bytes=2048)
    return {
        "dispatch": result.to_dict(),
        "snapshot": snapshot.to_dict() if snapshot else None,
    }


def _snapshot(task_id: str) -> dict[str, Any]:
    from hermes_cli import kanban_db as kb

    with kb.connect() as conn:
        snap = kb.task_progress_snapshot(
            conn,
            task_id,
            log_tail_bytes=4096,
            include_children=True,
        )
        if snap is None:
            raise RuntimeError(f"unknown task {task_id}")
        return snap.to_dict()


def _acceptance(task_id: str) -> dict[str, Any] | None:
    from hermes_cli import kanban_db as kb

    with kb.connect() as conn:
        return kb.task_acceptance_snapshot(
            conn,
            task_id,
            log_tail_bytes=2048,
            followup_log_tail_bytes=2048,
        )


def _advance(task_id: str, *, dispatch_max: int = 3) -> dict[str, Any]:
    from hermes_cli import kanban_db as kb

    with kb.connect() as conn:
        return kb.advance_acceptance_workflow_until_idle(
            conn,
            task_id,
            review_assignee="codex-review",
            test_assignee="codex-test",
            dispatch=True,
            dispatch_max=dispatch_max,
            reviewer="codex-e2e-smoke-controller",
            summary="real Codex implementation, review/test follow-ups, and acceptance checks passed",
            max_iterations=6,
        )


def _advance_goal(root_task_id: str, *, dispatch_max: int = 3) -> dict[str, Any]:
    from hermes_cli import kanban_db as kb

    with kb.connect() as conn:
        return kb.advance_goal_acceptance_workflow_until_idle(
            conn,
            root_task_id,
            review_assignee="codex-review",
            test_assignee="codex-test",
            dispatch=True,
            dispatch_max=dispatch_max,
            reviewer="codex-e2e-smoke-controller",
            summary="real Codex goal, review/test follow-ups, and acceptance checks passed",
            max_iterations=8,
        )


def _task_events(task_id: str) -> list[dict[str, Any]]:
    from hermes_cli import kanban_db as kb

    with kb.connect() as conn:
        return [
            {
                "id": event.id,
                "kind": event.kind,
                "run_id": event.run_id,
                "payload": event.payload,
                "created_at": event.created_at,
            }
            for event in kb.list_events(conn, task_id)
        ]


def _wait_for_implementation(task_id: str, *, deadline: float) -> dict[str, Any]:
    observed_running = False
    observed_progress = False
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        _dispatch_one(task_id, max_spawn=0)
        last = _snapshot(task_id)
        task = last["task"]
        if task["status"] == "running":
            observed_running = True
        if last.get("worker_progress"):
            observed_progress = True
        review = ((last.get("evidence") or {}).get("review") or {})
        worker_lane = last.get("worker_lane") or {}
        if task["status"] == "blocked" and (
            last.get("review_required") or review.get("required")
        ):
            if worker_lane.get("exit_code") != 0:
                _die("implementation worker did not exit cleanly", details={"snapshot": last})
            if not observed_running:
                _die("implementation completed before running state was observed", details={"snapshot": last})
            if not observed_progress:
                _die("implementation did not write worker_progress task_events", details={"snapshot": last})
            return last
        if task["status"] in {"done", "archived"}:
            _die("implementation finished without review-required handoff", details={"snapshot": last})
        time.sleep(2)
    _die("timed out waiting for implementation worker", details={"last_snapshot": last or {}})


def _wait_for_acceptance(task_id: str, *, deadline: float) -> dict[str, Any]:
    last_payload: dict[str, Any] | None = None
    last_acceptance: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last_payload = _advance(task_id)
        last_acceptance = _acceptance(task_id)
        final = last_payload.get("final") or {}
        task = ((final.get("implementation") or {}).get("task") or {})
        if task.get("status") == "done":
            gate = (last_acceptance or {}).get("review_followup_gate") or {}
            acceptance_gate = (last_acceptance or {}).get("acceptance_check_gate") or {}
            if gate.get("ready") is not True:
                _die("review/test follow-up gate was not ready at completion", details={"acceptance": last_acceptance or {}})
            if acceptance_gate.get("ready") is not True:
                _die("deterministic acceptance gate was not ready at completion", details={"acceptance": last_acceptance or {}})
            return last_payload
        if last_payload.get("stop_reason") == "blocked":
            _die("acceptance workflow blocked", details={"payload": last_payload, "acceptance": last_acceptance or {}})
        time.sleep(3)
    _die(
        "timed out waiting for review/test follow-ups and acceptance approval",
        details={"last_payload": last_payload or {}, "last_acceptance": last_acceptance or {}},
    )


def _wait_for_goal_acceptance(root_task_id: str, child_task_id: str, *, deadline: float) -> dict[str, Any]:
    last_payload: dict[str, Any] | None = None
    last_child_acceptance: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last_payload = _advance_goal(root_task_id)
        last_child_acceptance = _acceptance(child_task_id)
        root_task = ((last_payload.get("final") or {}).get("task") or {})
        if root_task.get("status") == "done":
            gate = (last_child_acceptance or {}).get("review_followup_gate") or {}
            acceptance_gate = (last_child_acceptance or {}).get("acceptance_check_gate") or {}
            if gate.get("ready") is not True:
                _die("goal review/test follow-up gate was not ready at completion", details={"acceptance": last_child_acceptance or {}})
            if acceptance_gate.get("ready") is not True:
                _die("goal deterministic acceptance gate was not ready at completion", details={"acceptance": last_child_acceptance or {}})
            return last_payload
        if last_payload.get("stop_reason") == "blocked":
            _die("goal acceptance workflow blocked", details={"payload": last_payload, "child_acceptance": last_child_acceptance or {}})
        time.sleep(3)
    _die(
        "timed out waiting for goal review/test follow-ups and acceptance approval",
        details={"last_payload": last_payload or {}, "last_child_acceptance": last_child_acceptance or {}},
    )


def _assert_final(task_id: str, workspace: Path) -> dict[str, Any]:
    final_snapshot = _snapshot(task_id)
    final_acceptance = _acceptance(task_id)
    result_path = workspace / "smoke_result.txt"
    if result_path.read_text(encoding="utf-8") != "hermes codex e2e ok\n":
        _die("smoke_result.txt content is wrong", details={"path": str(result_path)})
    if final_snapshot["task"]["status"] != "done":
        _die("implementation task is not done", details={"snapshot": final_snapshot})
    review = ((final_snapshot.get("evidence") or {}).get("review") or {})
    if review.get("required") is not False or review.get("decision") != "approved":
        _die("implementation review metadata was not approved", details={"snapshot": final_snapshot})
    events = _task_events(task_id)
    kinds = {event["kind"] for event in events}
    required_kinds = {
        "worker_started",
        "worker_heartbeat",
        "worker_progress",
        "worker_review_required",
        "worker_review_followups_planned",
        "acceptance_check_completed",
        "completed",
    }
    missing = sorted(required_kinds - kinds)
    if missing:
        _die("missing expected task events", details={"missing": missing, "events": events[-20:]})
    return {
        "snapshot": final_snapshot,
        "acceptance": final_acceptance,
        "events": events,
    }


def _assert_goal_final(root_task_id: str, child_task_id: str, workspace: Path) -> dict[str, Any]:
    root_snapshot = _snapshot(root_task_id)
    child_final = _assert_final(child_task_id, workspace)
    if root_snapshot["task"]["status"] != "done":
        _die("goal root task is not done", details={"snapshot": root_snapshot})
    child_ids = {
        ((child.get("task") or {}).get("id"))
        for child in (root_snapshot.get("children") or [])
        if isinstance(child, dict)
    }
    if child_task_id not in child_ids:
        _die("goal root snapshot does not include implementation child", details={"snapshot": root_snapshot, "child_task_id": child_task_id})
    root_events = _task_events(root_task_id)
    root_kinds = {event["kind"] for event in root_events}
    for kind in {"decomposed", "goal_acceptance_advanced", "completed"}:
        if kind not in root_kinds:
            _die("missing expected goal root event", details={"missing": kind, "events": root_events[-20:]})
    return {
        "root_snapshot": root_snapshot,
        "child_snapshot": child_final["snapshot"],
        "child_acceptance": child_final["acceptance"],
        "child_events": child_final["events"],
        "root_events": root_events,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Codex model for all lanes; use 'default' to omit --model")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="overall smoke timeout in seconds")
    parser.add_argument("--worker-timeout", type=int, default=DEFAULT_WORKER_TIMEOUT, help="per Codex wrapper timeout in seconds")
    parser.add_argument("--goal", action="store_true", help="exercise the /goal-style root task -> Codex child controller path")
    parser.add_argument("--keep", action="store_true", help="keep the temporary HERMES_HOME/workspace after success")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv or sys.argv[1:]))
    if shutil.which("codex") is None:
        _die("codex binary not found on PATH")
    if shutil.which("git") is None:
        _die("git binary not found on PATH")
    if shutil.which("python3") is None:
        _die("python3 binary not found on PATH")

    temp_root = Path(tempfile.mkdtemp(prefix="hermes-codex-e2e."))
    home = temp_root / "home"
    workspace = temp_root / "workspace"
    model = None if str(args.model).strip().lower() in {"", "default", "none"} else str(args.model).strip()
    keep = bool(args.keep)
    try:
        _setup_environment(home)
        _write_config(home, model=model, worker_timeout=int(args.worker_timeout))
        _init_workspace(workspace)

        from hermes_cli import kanban_db as kb
        from hermes_cli.worker_lanes import register_configured_worker_lanes

        kb.init_db()
        register_configured_worker_lanes()

        root_task_id: str | None = None
        if args.goal:
            root_task_id, task_id = _create_goal_root_and_child(
                workspace,
                worker_timeout=int(args.worker_timeout),
            )
        else:
            task_id = _create_task(workspace, worker_timeout=int(args.worker_timeout))

        initial = _dispatch_one(task_id, max_spawn=1)
        if not initial["dispatch"].get("spawned"):
            _die("dispatcher did not spawn implementation worker", details={"initial": initial})

        deadline = time.monotonic() + float(args.timeout)
        implementation = _wait_for_implementation(task_id, deadline=deadline)
        if root_task_id:
            acceptance_payload = _wait_for_goal_acceptance(
                root_task_id,
                task_id,
                deadline=deadline,
            )
            final = _assert_goal_final(root_task_id, task_id, workspace)
            final_snapshot = final["child_snapshot"]
            final_acceptance = final["child_acceptance"]
            recent_events = final["child_events"][-12:]
            goal_final_status = final["root_snapshot"]["task"]["status"]
        else:
            acceptance_payload = _wait_for_acceptance(task_id, deadline=deadline)
            final = _assert_final(task_id, workspace)
            final_snapshot = final["snapshot"]
            final_acceptance = final["acceptance"]
            recent_events = final["events"][-12:]
            goal_final_status = None

        out = {
            "ok": True,
            "task_id": task_id,
            "root_task_id": root_task_id,
            "home": str(home),
            "workspace": str(workspace),
            "model": model or "codex default",
            "implementation_run": (implementation.get("run") or {}).get("id"),
            "final_status": final_snapshot["task"]["status"],
            "goal_final_status": goal_final_status,
            "review_decision": (
                ((final_snapshot.get("evidence") or {}).get("review") or {})
                .get("decision")
            ),
            "advance_stop_reason": acceptance_payload.get("stop_reason"),
            "review_followup_gate": (final_acceptance or {}).get("review_followup_gate"),
            "acceptance_check_gate": (final_acceptance or {}).get("acceptance_check_gate"),
            "recent_events": recent_events,
        }
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    except KeyboardInterrupt:
        keep = True
        raise
    finally:
        if keep:
            print(f"[smoke] kept temporary root: {temp_root}", file=sys.stderr)
        else:
            shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
