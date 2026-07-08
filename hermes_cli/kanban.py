"""CLI for the Hermes Kanban board — ``hermes kanban …`` subcommand.

Exposes the full Kanban command surface documented in the design spec
(``docs/hermes-kanban-v1-spec.pdf``).  All DB work is delegated to
``kanban_db``.  This module adds:

  * Argparse subcommand construction (``build_parser``).
  * Argument dispatch (``kanban_command``).
  * Output formatting (plain text + ``--json``).
  * A short shared helper that parses a single slash-style string
    (used by ``/kanban …`` in CLI and gateway) and forwards it to the
    argparse surface.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Any, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_swarm as ks
from hermes_cli.profiles import get_active_profile_name, get_profile_dir, seed_profile_skills

try:
    import yaml
except Exception:  # pragma: no cover - yaml is a declared runtime dep.
    yaml = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------

_STATUS_ICONS = {
    "todo":     "◻",
    "ready":    "▶",
    "running":  "●",
    "scheduled":"⏱",
    "blocked":  "⊘",
    "done":     "✓",
    "archived": "—",
}


def _fmt_ts(ts: Optional[int]) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _fmt_task_line(t: kb.Task) -> str:
    icon = _STATUS_ICONS.get(t.status, "?")
    assignee = t.assignee or "(unassigned)"
    tenant = f" [{t.tenant}]" if t.tenant else ""
    return f"{icon} {t.id}  {t.status:8s}  {assignee:20s}{tenant}  {t.title}"


def _task_to_dict(t: kb.Task) -> dict[str, Any]:
    return {
        "id": t.id,
        "title": t.title,
        "body": t.body,
        "assignee": t.assignee,
        "status": t.status,
        "priority": t.priority,
        "tenant": t.tenant,
        "workspace_kind": t.workspace_kind,
        "workspace_path": t.workspace_path,
        "branch_name": t.branch_name,
        "created_by": t.created_by,
        "created_at": t.created_at,
        "started_at": t.started_at,
        "completed_at": t.completed_at,
        "result": t.result,
        "max_runtime_seconds": t.max_runtime_seconds,
        "skills": list(t.skills) if t.skills else [],
        "max_retries": t.max_retries,
        "session_id": t.session_id,
        "workflow_template_id": t.workflow_template_id,
        "current_step_key": t.current_step_key,
    }


def _run_state_kwargs(args: argparse.Namespace) -> Optional[dict[str, str]]:
    st = getattr(args, "state_type", None)
    sn = getattr(args, "state_name", None)
    if (st is None) != (sn is None):
        return None
    if st is None:
        return {}
    return {"state_type": st, "state_name": sn}


def _snapshot_to_dict(snapshot: kb.TaskProgressSnapshot) -> dict[str, Any]:
    return snapshot.to_dict()


def _attach_progress_diagnostics(conn, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        from hermes_cli.kanban_progress import attach_progress_diagnostics

        return attach_progress_diagnostics(conn, payload)
    except Exception:
        return payload


def _session_goal_snapshot_from_env() -> tuple[Optional[kb.TaskProgressSnapshot], Optional[str]]:
    """Return latest /goal create root for HERMES_SESSION_ID, if any."""
    session_id = os.environ.get("HERMES_SESSION_ID")
    if not session_id:
        return None, None
    try:
        from hermes_cli.goals import latest_kanban_goal_snapshot_for_session

        return latest_kanban_goal_snapshot_for_session(session_id), session_id
    except Exception:
        return None, session_id


def _resolve_optional_goal_task_id(task_id: Optional[str]) -> tuple[Optional[str], bool, Optional[str]]:
    """Resolve an optional root task id through the current session goal."""
    if task_id:
        return str(task_id), False, None
    snapshot, session_id = _session_goal_snapshot_from_env()
    if snapshot is None:
        return None, False, session_id
    return snapshot.task.id, True, session_id


def _render_progress_items(items: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in items:
        status = str(item.get("status") or "").strip() or "unknown"
        text = str(item.get("text") or "").strip()
        index = item.get("index")
        prefix = f"{index}. " if index is not None else "- "
        lines.append(f"{prefix}[{status}] {text}")
    return lines


def _render_codex_activity(events: list[dict[str, Any]], *, limit: int = 5) -> list[str]:
    lines: list[str] = []
    for event in events[-max(1, limit):]:
        if not isinstance(event, dict):
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
        title = item.get("type") or payload.get("event_type") or "codex_event"
        bits = [str(title)]
        if item.get("status"):
            bits.append(str(item.get("status")))
        if item.get("exit_code") is not None:
            bits.append(f"exit={item.get('exit_code')}")
        run_id = payload.get("run_id", event.get("run_id"))
        if run_id is not None:
            bits.append(f"run={run_id}")

        summary = ""
        if item.get("type") == "command_execution":
            summary = str(item.get("command") or "")
        elif item.get("type") == "file_change":
            changes = item.get("changes") if isinstance(item.get("changes"), list) else []
            summary = ", ".join(
                str(change.get("path"))
                for change in changes[:3]
                if isinstance(change, dict) and change.get("path")
            )
        else:
            summary = str(item.get("text_tail") or payload.get("thread_id") or "")

        line = " · ".join(bits)
        if summary:
            line += f": {summary}"
        lines.append(line[:200])
    return lines


def _parse_workspace_flag(value: str) -> tuple[str, Optional[str]]:
    """Parse ``--workspace`` into ``(kind, path|None)``.

    Accepts: ``scratch``, ``worktree``, ``worktree:<path>``, ``dir:<path>``.
    """
    if not value:
        return ("scratch", None)
    v = value.strip()
    if v in {"scratch", "worktree"}:
        return (v, None)
    for prefix, kind in (("dir:", "dir"), ("worktree:", "worktree")):
        if not v.startswith(prefix):
            continue
        path = v[len(prefix):].strip()
        if not path:
            raise argparse.ArgumentTypeError(
                f"--workspace {prefix} requires a path after the colon"
            )
        return (kind, os.path.expanduser(path))
    raise argparse.ArgumentTypeError(
        f"unknown --workspace value {value!r}: use scratch, worktree, "
        "worktree:<path>, or dir:<path>"
    )


def _parse_branch_flag(value: Optional[str]) -> Optional[str]:
    """Normalize an optional branch name from ``kanban create --branch``."""
    if value is None:
        return None
    branch = value.strip()
    if not branch:
        raise argparse.ArgumentTypeError("--branch requires a non-empty name")
    if branch.startswith("-"):
        raise argparse.ArgumentTypeError("--branch must not start with '-'")
    if any(ch.isspace() for ch in branch):
        raise argparse.ArgumentTypeError("--branch must not contain whitespace")
    return branch


def _check_dispatcher_presence() -> tuple[bool, str]:
    """Return ``(running, message)``.

    - ``running=True``: a gateway is alive for this HERMES_HOME and its
      config has ``kanban.dispatch_in_gateway`` on (default). Message
      is a short status line.
    - ``running=False``: either no gateway is running, or the gateway
      is running but the config flag is off. Message is human guidance
      explaining the next step.

    Used by ``hermes kanban create`` (and callers) to warn when a task
    will sit in ``ready`` because nothing is there to pick it up.
    Defensive against import failures and config-read errors — if the
    probe itself errors, we return ``(True, "")`` so we don't spam
    false warnings (better to miss a warning than to cry wolf).
    """
    try:
        from gateway.status import get_running_pid  # type: ignore
    except Exception:
        return (True, "")  # can't probe — silent
    try:
        pid = get_running_pid()
    except Exception:
        return (True, "")  # probe errored — silent

    # Even if the gateway is up, dispatch_in_gateway may be off.
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        dispatch_on = bool(cfg.get("kanban", {}).get("dispatch_in_gateway", True))
    except Exception:
        dispatch_on = True  # can't tell — assume default

    if pid and dispatch_on:
        return (True, f"gateway pid={pid}, dispatch enabled")
    if pid and not dispatch_on:
        return (
            False,
            "Gateway is running but kanban.dispatch_in_gateway=false in "
            "config.yaml — the task will sit in 'ready' until you flip it "
            "back on and restart the gateway, OR run the legacy "
            "standalone daemon (`hermes kanban daemon --force`)."
        )
    return (
        False,
        "No gateway is running — the task will sit in 'ready' until you "
        "start it. Run:\n"
        "    hermes gateway start\n"
        "The gateway hosts an embedded dispatcher (tick interval 60s by "
        "default); your task will be picked up on the next tick after "
        "the gateway comes up."
    )


# ---------------------------------------------------------------------------
# Argparse builder
# ---------------------------------------------------------------------------

def build_parser(parent_subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Attach the ``kanban`` subcommand tree under an existing subparsers.

    Returns the top-level ``kanban`` parser so caller can ``set_defaults``.
    """
    kanban_parser = parent_subparsers.add_parser(
        "kanban",
        help="Multi-profile collaboration board (tasks, links, comments)",
        description=(
            "Durable SQLite-backed task board shared across Hermes profiles. "
            "Tasks are claimed atomically, can depend on other tasks, and "
            "are executed by a named profile in an isolated workspace. "
            "See https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban "
            "or docs/hermes-kanban-v1-spec.pdf for the full design."
        ),
    )
    # --- global --board flag ---
    # Applies to every subcommand below. When set, scopes all reads and
    # writes to that board's DB. When omitted, resolves via the
    # HERMES_KANBAN_BOARD env var, then the persisted current-board
    # file, then "default". See kanban_db.get_current_board().
    kanban_parser.add_argument(
        "--board",
        default=None,
        metavar="<slug>",
        help=(
            "Board slug to operate on. Defaults to the current board "
            "(set via `hermes kanban boards switch <slug>` or the "
            "HERMES_KANBAN_BOARD env var). Use `hermes kanban boards list` "
            "to see all boards."
        ),
    )
    sub = kanban_parser.add_subparsers(dest="kanban_action")

    # --- init ---
    sub.add_parser("init", help="Create kanban.db if missing (idempotent)")

    # --- boards (new in v2: multi-project support) ---
    p_boards = sub.add_parser(
        "boards",
        help="Manage kanban boards (one board per project / workstream)",
        description=(
            "Boards let you separate unrelated streams of work "
            "(projects, repos, domains) into isolated queues. Each "
            "board has its own DB, workspaces directory, and dispatcher "
            "loop — tasks on one board cannot collide with tasks on "
            "another. The first board is 'default' and always exists."
        ),
    )
    boards_sub = p_boards.add_subparsers(dest="boards_action")

    b_list = boards_sub.add_parser(
        "list", aliases=["ls"],
        help="List all boards with task counts",
    )
    b_list.add_argument("--json", action="store_true")
    b_list.add_argument("--all", action="store_true",
                        help="Include archived boards too")

    b_create = boards_sub.add_parser(
        "create", aliases=["new"],
        help="Create a new board",
    )
    b_create.add_argument("slug",
                          help="Board slug (kebab-case, e.g. atm10-server)")
    b_create.add_argument("--name", default=None,
                          help="Human-readable display name (defaults to Title Case of slug)")
    b_create.add_argument("--description", default=None,
                          help="Optional description")
    b_create.add_argument("--icon", default=None,
                          help="Optional emoji or single-character icon for the dashboard")
    b_create.add_argument("--color", default=None,
                          help="Optional hex color (e.g. '#8b5cf6') for the dashboard")
    b_create.add_argument("--switch", action="store_true",
                          help="Switch to the new board after creating it")
    b_create.add_argument("--default-workdir", default=None,
                          help="Default workspace path for tasks created on this board")

    b_rm = boards_sub.add_parser(
        "rm", aliases=["remove", "delete"],
        help="Archive (default) or delete a board",
    )
    b_rm.add_argument("slug")
    b_rm.add_argument("--delete", action="store_true",
                      help="Hard-delete the board directory instead of archiving it. "
                           "Default is to move it to boards/_archived/ so it's recoverable.")

    b_switch = boards_sub.add_parser(
        "switch", aliases=["use"],
        help="Set the active board for subsequent CLI calls",
    )
    b_switch.add_argument("slug")

    boards_sub.add_parser(
        "show", aliases=["current"],
        help="Print the currently-active board slug",
    )

    b_rename = boards_sub.add_parser(
        "rename",
        help="Change a board's human-readable display name (slug is immutable)",
    )
    b_rename.add_argument("slug")
    b_rename.add_argument("name", help="New display name")

    b_set_wd = boards_sub.add_parser(
        "set-default-workdir",
        help="Set the default workspace path for tasks on a board",
    )
    b_set_wd.add_argument("slug")
    b_set_wd.add_argument("path", nargs="?", default=None,
                          help="Absolute path to use as default workdir. Omit to clear.")

    # --- create ---
    p_create = sub.add_parser("create", help="Create a new task")
    p_create.add_argument("title", help="Task title")
    p_create.add_argument("--body", default=None, help="Optional opening post")
    p_create.add_argument("--assignee", default=None, help="Profile name to assign")
    p_create.add_argument("--parent", action="append", default=[],
                          help="Parent task id (repeatable)")
    p_create.add_argument("--workspace", default="scratch",
                          help="scratch | worktree | worktree:<path> | dir:<path> "
                               "(default: scratch)")
    p_create.add_argument("--branch", default=None,
                          help="Branch name for worktree tasks, e.g. wt/t6-wire")
    p_create.add_argument("--tenant", default=None, help="Tenant namespace")
    p_create.add_argument("--priority", type=int, default=0, help="Priority tiebreaker")
    p_create.add_argument("--triage", action="store_true",
                          help="Park in triage — a specifier will flesh out the spec and promote to todo")
    p_create.add_argument("--idempotency-key", default=None,
                          help="Dedup key. If a non-archived task with this key exists, "
                               "its id is returned instead of creating a duplicate.")
    p_create.add_argument("--max-runtime", default=None,
                          help="Per-task runtime cap. Accepts seconds (300) or "
                               "durations (90s, 30m, 2h, 1d). When exceeded, "
                               "the dispatcher SIGTERMs (then SIGKILLs) the worker "
                               "and re-queues the task.")
    p_create.add_argument("--created-by", default="user",
                          help="Author name recorded on the task (default: user)")
    p_create.add_argument("--skill", action="append", default=[], dest="skills",
                          help="Skill to force-load into the worker "
                               "(repeatable). Appended to the built-in "
                               "kanban-worker skill. Example: "
                               "--skill translation --skill github-code-review")
    p_create.add_argument("--max-retries", type=int, default=None,
                          metavar="N",
                          help="Per-task override for the consecutive-failure "
                               "circuit breaker. Trip on the Nth failure — "
                               "e.g. --max-retries 1 blocks on the first "
                               "failure (no retries), --max-retries 3 allows "
                               "two retries. Omit to use the dispatcher's "
                               "kanban.failure_limit config "
                               f"(default {kb.DEFAULT_FAILURE_LIMIT}).")
    p_create.add_argument("--initial-status",
                          choices=sorted(kb.VALID_INITIAL_STATUSES),
                          default="running",
                          help="Initial card status. Use 'blocked' for cards "
                               "that require immediate human ops (R3 gate) "
                               "to skip the brief running-to-blocked transition.")
    p_create.add_argument(
        "--acceptance-check-request",
        "--acceptance-request",
        action="append",
        default=[],
        dest="acceptance_check_requests",
        metavar="PATH",
        help=(
            "JSON/YAML file containing a declarative acceptance_check_request "
            "to attach at task creation time. Repeatable. Use '-' for stdin. "
            "Safe types: file_content or trusted command_template."
        ),
    )
    p_create.add_argument("--json", action="store_true", help="Emit JSON output")

    # --- goal bridge ---
    p_goal = sub.add_parser(
        "goal",
        help="Create or inspect a Kanban top-level task for a standing goal",
    )
    p_goal.add_argument("goal", help="Goal text / complex objective")
    p_goal.add_argument("--session", default=None, help="Originating session id")
    p_goal.add_argument("--assignee", default=None, help="Orchestrator assignee")
    p_goal.add_argument("--workspace", default="scratch",
                        help="scratch | worktree | worktree:<path> | dir:<path> "
                             "(inherited by decomposed child tasks)")
    p_goal.add_argument("--branch", default=None,
                        help="Branch name for worktree goal tasks, e.g. wt/t6-wire")
    p_goal.add_argument("--tenant", default=None, help="Tenant namespace")
    p_goal.add_argument("--priority", type=int, default=0, help="Priority tiebreaker")
    p_goal.add_argument("--max-runtime", default=None,
                        help="Per-task runtime cap inherited by decomposed child tasks")
    p_goal.add_argument("--max-retries", type=int, default=None,
                        help="Per-task retry breaker inherited by decomposed child tasks")
    p_goal.add_argument("--created-by", default="goal", help="Creator recorded on the task")
    p_goal.add_argument("--idempotency-key", default=None, help="Dedup key for the top-level task")
    p_goal.add_argument(
        "--decompose",
        action="store_true",
        help="Immediately run the Kanban decomposer on the new triage task",
    )
    p_goal.add_argument(
        "--advance",
        action="store_true",
        help=(
            "After decomposition, run one scoped goal-controller pass to "
            "dispatch ready children or advance review/test gates"
        ),
    )
    p_goal.add_argument(
        "--loop",
        action="store_true",
        help=(
            "With --advance, repeat bounded controller passes until done, "
            "waiting on workers, blocked, idle, or the iteration cap"
        ),
    )
    p_goal.add_argument(
        "--max-iterations",
        type=int,
        default=8,
        metavar="N",
        help="With --advance --loop, cap controller passes (1-64, default 8)",
    )
    p_goal.add_argument(
        "--advance-dry-run",
        action="store_true",
        help="With --advance, report scoped dispatches without claiming tasks",
    )
    p_goal.add_argument(
        "--dispatch-max",
        type=int,
        default=None,
        metavar="N",
        help="With --advance, cap scoped child/follow-up worker spawns",
    )
    p_goal.add_argument(
        "--review-assignee",
        default="codex-review",
        help="With --advance, review worker lane for child follow-ups",
    )
    p_goal.add_argument(
        "--test-assignee",
        default="codex-test",
        help="With --advance, test worker lane for child follow-ups",
    )
    p_goal.add_argument(
        "--no-verify",
        action="store_true",
        help="With --advance, do not run configured acceptance checks",
    )
    p_goal.add_argument(
        "--no-approve",
        action="store_true",
        help="With --advance, stop before approving children or completing the root",
    )
    p_goal.add_argument(
        "--no-request-changes",
        action="store_true",
        help="With --advance, report failed gates instead of requesting changes",
    )
    p_goal.add_argument(
        "--author",
        default=None,
        help="Author name recorded on decomposition audit comments",
    )
    p_goal.add_argument("--json", action="store_true", help="Emit JSON output")

    # --- swarm ---
    p_swarm = sub.add_parser(
        "swarm",
        help="Create a Kanban Swarm v1 graph (parallel workers → verifier → synthesizer)",
    )
    p_swarm.add_argument("goal", help="Swarm goal / final outcome")
    p_swarm.add_argument(
        "--worker",
        action="append",
        default=[],
        metavar="PROFILE:TITLE[:SKILL,SKILL]",
        help="Parallel worker card (repeatable)",
    )
    p_swarm.add_argument("--verifier", required=True, help="Verifier profile")
    p_swarm.add_argument("--synthesizer", required=True, help="Synthesizer/writer profile")
    p_swarm.add_argument("--tenant", default=None, help="Tenant namespace")
    p_swarm.add_argument("--priority", type=int, default=0, help="Priority tiebreaker")
    p_swarm.add_argument("--created-by", default=None, help="Creator/anchor profile")
    p_swarm.add_argument("--idempotency-key", default=None, help="Dedup key for the root card")
    p_swarm.add_argument("--json", action="store_true", help="Emit JSON output")

    # --- list ---
    p_list = sub.add_parser("list", aliases=["ls"], help="List tasks")
    p_list.add_argument("--mine", action="store_true",
                        help="Filter by $HERMES_PROFILE as assignee")
    p_list.add_argument("--assignee", default=None)
    p_list.add_argument("--status", default=None,
                        choices=sorted(kb.VALID_STATUSES))
    p_list.add_argument("--tenant", default=None)
    p_list.add_argument("--session", default=None,
                        help="Filter by originating chat/agent session id "
                             "(set on tasks created from inside an ACP loop)")
    p_list.add_argument("--archived", action="store_true",
                        help="Include archived tasks")
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument(
        "--sort",
        default=None,
        choices=sorted(kb.VALID_SORT_ORDERS.keys()),
        help="Sort order for listed tasks (default: priority)",
    )
    p_list.add_argument(
        "--workflow-template-id",
        default=None,
        metavar="ID",
        help="Restrict to tasks with this workflow_template_id",
    )
    p_list.add_argument(
        "--step-key",
        default=None,
        dest="current_step_key",
        metavar="KEY",
        help="Restrict to tasks with this current_step_key",
    )

    # --- show ---
    p_show = sub.add_parser("show", help="Show a task with comments + events")
    p_show.add_argument("task_id")
    p_show.add_argument("--json", action="store_true")
    p_show.add_argument(
        "--state-type",
        choices=("status", "outcome"),
        default=None,
        help="With --state-name: filter listed runs by task_runs column",
    )
    p_show.add_argument(
        "--state-name",
        default=None,
        metavar="VALUE",
        help="With --state-type: keep runs whose column equals this value",
    )

    # --- progress ---
    p_progress = sub.add_parser(
        "progress",
        help="Read a task progress/evidence snapshot without interrupting its worker",
    )
    p_progress.add_argument(
        "task_id",
        nargs="?",
        help=(
            "Task id. Omit to inspect the current HERMES_SESSION_ID "
            "session's latest /goal create Kanban root."
        ),
    )
    p_progress.add_argument("--json", action="store_true")
    p_progress.add_argument(
        "--log-tail",
        type=int,
        default=None,
        metavar="BYTES",
        help="Include the last N bytes of the worker log in the snapshot",
    )
    p_progress.add_argument(
        "--children",
        action="store_true",
        help="Include related child/dependency worker progress summaries for goal/root tasks",
    )

    # --- acceptance ---
    p_acceptance = sub.add_parser(
        "acceptance",
        help="Read bounded implementation and review/test follow-up evidence",
    )
    p_acceptance.add_argument("task_id")
    p_acceptance.add_argument("--json", action="store_true")
    p_acceptance.add_argument(
        "--log-tail",
        type=int,
        default=None,
        metavar="BYTES",
        help="Include the last N bytes of the implementation worker log",
    )
    p_acceptance.add_argument(
        "--followup-log-tail",
        type=int,
        default=None,
        metavar="BYTES",
        help="Include the last N bytes of each follow-up worker log",
    )

    # --- verify ---
    p_verify = sub.add_parser(
        "verify",
        help="Run configured deterministic acceptance checks for a task",
    )
    p_verify.add_argument("task_id")
    p_verify.add_argument(
        "checks",
        nargs="*",
        help="Configured check names; omitted means all configured checks",
    )
    p_verify.add_argument(
        "--source-run-id",
        type=int,
        default=None,
        help="Implementation run id this verification belongs to",
    )
    p_verify.add_argument("--json", action="store_true")

    # --- acceptance-check-request ---
    p_acceptance_req = sub.add_parser(
        "acceptance-check-request",
        aliases=["acceptance-request"],
        help="Validate and attach a safe task-scoped acceptance check request",
    )
    p_acceptance_req.add_argument("task_id")
    p_acceptance_req.add_argument(
        "path",
        nargs="?",
        default="-",
        help="JSON/YAML file containing acceptance_check_request, or '-' for stdin",
    )
    p_acceptance_req.add_argument(
        "--source-run-id",
        type=int,
        default=None,
        help="Implementation run id this request belongs to",
    )
    p_acceptance_req.add_argument(
        "--requested-by",
        default=None,
        help="Controller/skill identity for the request event",
    )
    p_acceptance_req.add_argument("--json", action="store_true")

    # --- advance-acceptance ---
    p_advance = sub.add_parser(
        "advance-acceptance",
        help="Advance review/test/verify/approve workflow to the next safe point",
    )
    p_advance.add_argument("task_id")
    p_advance.add_argument("--review-assignee", default="codex-review")
    p_advance.add_argument("--test-assignee", default="codex-test")
    p_advance.add_argument(
        "--no-dispatch",
        action="store_true",
        help="Create follow-ups but do not dispatch external workers",
    )
    p_advance.add_argument(
        "--dispatch-max",
        type=int,
        default=None,
        metavar="N",
        help="Cap scoped follow-up worker spawns",
    )
    p_advance.add_argument(
        "--dry-run",
        action="store_true",
        help="With dispatch enabled, report follow-up spawns without claiming tasks",
    )
    p_advance.add_argument(
        "--loop",
        action="store_true",
        help=(
            "Repeat bounded controller passes until done, waiting on workers, "
            "blocked, idle, or the iteration cap"
        ),
    )
    p_advance.add_argument(
        "--max-iterations",
        type=int,
        default=8,
        metavar="N",
        help="With --loop, cap controller passes (1-64, default 8)",
    )
    p_advance.add_argument(
        "--no-verify",
        action="store_true",
        help="Do not run configured Hermes acceptance checks",
    )
    p_advance.add_argument(
        "--no-approve",
        action="store_true",
        help="Stop before approving even if all gates are satisfied",
    )
    p_advance.add_argument(
        "--no-request-changes",
        action="store_true",
        help=(
            "When review/test or acceptance gates fail, report the blocked "
            "gate instead of requesting changes on the implementation task"
        ),
    )
    p_advance.add_argument(
        "--reviewer",
        default=None,
        help="Reviewer/controller name for planned tasks and approval",
    )
    p_advance.add_argument(
        "--summary",
        default=None,
        help="Approval summary when the workflow reaches approve",
    )
    p_advance.add_argument(
        "--result",
        default=None,
        help="Task result when the workflow reaches approve",
    )
    p_advance.add_argument("--json", action="store_true")

    # --- advance-goal ---
    p_advance_goal = sub.add_parser(
        "advance-goal",
        help="Advance a decomposed goal/root task and its worker children",
    )
    p_advance_goal.add_argument(
        "task_id",
        nargs="?",
        help=(
            "Goal/root task id. Omit to advance the current HERMES_SESSION_ID "
            "session's latest /goal create Kanban root."
        ),
    )
    p_advance_goal.add_argument("--review-assignee", default="codex-review")
    p_advance_goal.add_argument("--test-assignee", default="codex-test")
    p_advance_goal.add_argument(
        "--no-dispatch",
        action="store_true",
        help="Do not dispatch ready child or follow-up workers",
    )
    p_advance_goal.add_argument(
        "--dispatch-max",
        type=int,
        default=None,
        metavar="N",
        help="Cap scoped child/follow-up worker spawns",
    )
    p_advance_goal.add_argument(
        "--dry-run",
        action="store_true",
        help="With dispatch enabled, report spawns without claiming tasks",
    )
    p_advance_goal.add_argument(
        "--loop",
        action="store_true",
        help=(
            "Repeat bounded controller passes until done, waiting on workers, "
            "blocked, idle, or the iteration cap"
        ),
    )
    p_advance_goal.add_argument(
        "--max-iterations",
        type=int,
        default=8,
        metavar="N",
        help="With --loop, cap controller passes (1-64, default 8)",
    )
    p_advance_goal.add_argument(
        "--no-verify",
        action="store_true",
        help="Do not run configured Hermes acceptance checks for children",
    )
    p_advance_goal.add_argument(
        "--no-approve",
        action="store_true",
        help="Stop before approving children or completing the root",
    )
    p_advance_goal.add_argument(
        "--no-request-changes",
        action="store_true",
        help=(
            "When child review/test or acceptance gates fail, report the "
            "blocked gate instead of requesting changes"
        ),
    )
    p_advance_goal.add_argument(
        "--reviewer",
        default=None,
        help="Reviewer/controller name for planned tasks and approvals",
    )
    p_advance_goal.add_argument(
        "--summary",
        default=None,
        help="Approval summary when child/root workflows reach approve",
    )
    p_advance_goal.add_argument(
        "--result",
        default=None,
        help="Task result when child/root workflows reach approve",
    )
    p_advance_goal.add_argument("--json", action="store_true")

    # --- advance-controller ---
    p_controller = sub.add_parser(
        "advance-controller",
        aliases=["controller-tick"],
        help="Run one autonomous Kanban controller tick across ready workflows",
    )
    p_controller.add_argument("--review-assignee", default="codex-review")
    p_controller.add_argument("--test-assignee", default="codex-test")
    p_controller.add_argument(
        "--no-dispatch",
        action="store_true",
        help="Inspect/plan only; do not dispatch child or follow-up workers",
    )
    p_controller.add_argument(
        "--dispatch-max",
        type=int,
        default=None,
        metavar="N",
        help="Cap worker spawns across this controller tick",
    )
    p_controller.add_argument(
        "--dry-run",
        action="store_true",
        help="With dispatch enabled, report spawns without claiming tasks",
    )
    p_controller.add_argument(
        "--max-items",
        type=int,
        default=8,
        metavar="N",
        help="Max goal/review-required workflows to inspect this tick (1-128)",
    )
    p_controller.add_argument(
        "--max-iterations",
        type=int,
        default=8,
        metavar="N",
        help="Max bounded passes per workflow (1-64)",
    )
    p_controller.add_argument(
        "--no-goals",
        action="store_true",
        help="Do not scan decomposed goal roots",
    )
    p_controller.add_argument(
        "--no-review-required",
        action="store_true",
        help="Do not scan standalone review-required implementation tasks",
    )
    p_controller.add_argument(
        "--no-verify",
        action="store_true",
        help="Do not run configured Hermes acceptance checks",
    )
    p_controller.add_argument(
        "--no-approve",
        action="store_true",
        help="Stop before approving even if all gates pass",
    )
    p_controller.add_argument(
        "--no-request-changes",
        action="store_true",
        help="Report failed gates instead of requesting bounded changes",
    )
    p_controller.add_argument(
        "--reviewer",
        default=None,
        help="Reviewer/controller name for planned tasks and approvals",
    )
    p_controller.add_argument(
        "--summary",
        default=None,
        help="Approval summary when a workflow reaches approve",
    )
    p_controller.add_argument(
        "--result",
        default=None,
        help="Task result when a workflow reaches approve",
    )
    p_controller.add_argument("--json", action="store_true")

    # --- reviews ---
    p_reviews = sub.add_parser(
        "reviews",
        help="List tasks with external-worker evidence waiting for Hermes review",
    )
    p_reviews.add_argument("--assignee", default=None)
    p_reviews.add_argument("--tenant", default=None)
    p_reviews.add_argument("--lane", default=None, help="Filter by worker lane name")
    p_reviews.add_argument("--limit", type=int, default=100)
    p_reviews.add_argument(
        "--log-tail",
        type=int,
        default=None,
        metavar="BYTES",
        help="Include the last N bytes of each worker log in JSON output",
    )
    p_reviews.add_argument(
        "--include-followups",
        action="store_true",
        help=(
            "Include review/test follow-up task evidence. By default the "
            "review queue lists only implementation handoffs."
        ),
    )
    p_reviews.add_argument("--json", action="store_true")

    # --- review ---
    p_review = sub.add_parser(
        "review",
        help="Approve or request changes for review-required worker evidence",
    )
    p_review.add_argument("task_id")
    p_review.add_argument(
        "decision",
        choices=("approve", "request-changes", "request_changes"),
    )
    p_review.add_argument(
        "--reviewer",
        default=None,
        help="Reviewer name (default: active profile or user)",
    )
    p_review.add_argument(
        "--comment",
        default=None,
        help="Review comment; required for request-changes",
    )
    p_review.add_argument(
        "--result",
        default=None,
        help="Result text to store when approving. Defaults to the approval summary.",
    )
    p_review.add_argument(
        "--summary",
        default=None,
        help="Approval summary stored as the completed review run summary.",
    )
    p_review.add_argument("--json", action="store_true")

    # --- plan-review ---
    p_plan_review = sub.add_parser(
        "plan-review",
        help="Create independent review/test worker tasks from review-required evidence",
    )
    p_plan_review.add_argument("task_id")
    p_plan_review.add_argument("--review-assignee", default="codex-review")
    p_plan_review.add_argument("--test-assignee", default="codex-test")
    p_plan_review.add_argument(
        "--review-only",
        action="store_true",
        help="Create only the review follow-up task",
    )
    p_plan_review.add_argument(
        "--test-only",
        action="store_true",
        help="Create only the test follow-up task",
    )
    p_plan_review.add_argument(
        "--created-by",
        default="hermes-review-planner",
        help="created_by value for the follow-up tasks",
    )
    p_plan_review.add_argument(
        "--dispatch",
        action="store_true",
        help="Immediately run one dispatcher pass for the planned follow-up tasks only",
    )
    p_plan_review.add_argument(
        "--dry-run",
        action="store_true",
        help="With --dispatch, report which follow-up tasks would be spawned without claiming",
    )
    p_plan_review.add_argument(
        "--dispatch-max",
        type=int,
        default=None,
        metavar="N",
        help="With --dispatch, cap the number of follow-up workers spawned",
    )
    p_plan_review.add_argument("--json", action="store_true")

    # --- assign ---
    p_assign = sub.add_parser("assign", help="Assign or reassign a task")
    p_assign.add_argument("task_id")
    p_assign.add_argument("profile", help="Profile name (or 'none' to unassign)")

    # --- reclaim / reassign (recovery) ---
    p_reclaim = sub.add_parser(
        "reclaim",
        help="Release an active worker claim on a running task",
    )
    p_reclaim.add_argument("task_id")
    p_reclaim.add_argument(
        "--reason", default=None,
        help="Human-readable reason (recorded on the reclaimed event)",
    )

    p_reassign = sub.add_parser(
        "reassign",
        help="Reassign a task to a different profile, optionally reclaiming first",
    )
    p_reassign.add_argument("task_id")
    p_reassign.add_argument(
        "profile",
        help="New profile name (or 'none' to unassign)",
    )
    p_reassign.add_argument(
        "--reclaim", action="store_true",
        help="Release any active claim before reassigning (required if task is running)",
    )
    p_reassign.add_argument(
        "--reason", default=None,
        help="Human-readable reason (recorded on the reclaimed event)",
    )

    # --- diagnostics (board-wide health) ---
    p_diag = sub.add_parser(
        "diagnostics",
        aliases=["diag"],
        help="List active diagnostics on the current board",
    )
    p_diag.add_argument(
        "--severity",
        choices=["warning", "error", "critical"],
        default=None,
        help="Only show diagnostics at or above this severity",
    )
    p_diag.add_argument(
        "--task",
        default=None,
        help="Only show diagnostics for one task id",
    )
    p_diag.add_argument(
        "--json", action="store_true",
        help="Emit JSON (structured) instead of the default human table",
    )

    # --- link / unlink ---
    p_link = sub.add_parser("link", help="Add a parent->child dependency")
    p_link.add_argument("parent_id")
    p_link.add_argument("child_id")
    p_unlink = sub.add_parser("unlink", help="Remove a parent->child dependency")
    p_unlink.add_argument("parent_id")
    p_unlink.add_argument("child_id")

    # --- claim ---
    p_claim = sub.add_parser(
        "claim",
        help="Atomically claim a ready task (prints resolved workspace path)",
    )
    p_claim.add_argument("task_id")
    p_claim.add_argument("--ttl", type=int, default=kb.DEFAULT_CLAIM_TTL_SECONDS,
                         help="Claim TTL in seconds (default: 900)")

    # --- comment / complete / block / unblock / archive ---
    p_comment = sub.add_parser("comment", help="Append a comment")
    p_comment.add_argument("task_id")
    p_comment.add_argument("text", nargs="+", help="Comment body")
    p_comment.add_argument("--author", default=None,
                           help="Author name (default: $HERMES_PROFILE or 'user')")
    p_comment.add_argument("--max-len", type=int, default=None,
                           help="Trim the stored comment body to this many characters")

    p_complete = sub.add_parser("complete", help="Mark one or more tasks done")
    p_complete.add_argument("task_ids", nargs="+",
                            help="One or more task ids (only --result applies to all of them)")
    p_complete.add_argument("--result", default=None, help="Result summary")
    p_complete.add_argument("--summary", default=None,
                            help="Structured handoff summary for downstream tasks. "
                                 "Falls back to --result if omitted.")
    p_complete.add_argument("--metadata", default=None,
                            help='JSON dict of structured facts (e.g. \'{"changed_files": [...], '
                                 '"tests_run": 12}\'). Stored on the closing run.')

    p_edit = sub.add_parser(
        "edit",
        help="Edit recovery fields on an already-completed task",
    )
    p_edit.add_argument("task_id")
    p_edit.add_argument(
        "--result",
        required=True,
        help="Backfilled task result text for a done task",
    )
    p_edit.add_argument(
        "--summary",
        default=None,
        help="Structured handoff summary. Falls back to --result if omitted.",
    )
    p_edit.add_argument(
        "--metadata",
        default=None,
        help="JSON dict of structured facts to store on the latest completed run.",
    )

    p_block = sub.add_parser("block", help="Mark one or more tasks blocked")
    p_block.add_argument("task_id")
    p_block.add_argument("reason", nargs="*", help="Reason (also appended as a comment)")
    p_block.add_argument("--ids", nargs="+", default=None,
                         help="Additional task ids to block with the same reason (bulk mode)")

    p_schedule = sub.add_parser("schedule", help="Park one or more tasks in Scheduled (waiting on time, not human input)")
    p_schedule.add_argument("task_id")
    p_schedule.add_argument("reason", nargs="*", help="Reason/timing note (also appended as a comment)")
    p_schedule.add_argument("--ids", nargs="+", default=None,
                            help="Additional task ids to schedule with the same reason (bulk mode)")

    p_unblock = sub.add_parser("unblock", help="Return one or more blocked/scheduled tasks to ready")
    p_unblock.add_argument("task_ids", nargs="+")

    p_archive = sub.add_parser("archive", help="Archive one or more tasks")
    p_archive.add_argument("task_ids", nargs="*",
                           help="Task ids to archive (default mode)")
    p_archive.add_argument(
        "--rm",
        dest="purge_ids",
        nargs="+",
        default=None,
        help="Permanently delete already-archived task ids from the board",
    )

    # --- tail ---
    p_tail = sub.add_parser("tail", help="Follow a task's event stream")
    p_tail.add_argument("task_id")
    p_tail.add_argument("--interval", type=float, default=1.0)

    # --- dispatch ---
    p_disp = sub.add_parser(
        "dispatch",
        help="One dispatcher pass: reclaim stale, promote ready, spawn workers",
    )
    p_disp.add_argument("--dry-run", action="store_true",
                        help="Don't actually spawn processes; just print what would happen")
    p_disp.add_argument("--max", type=int, default=None,
                        help="Cap number of spawns this pass")
    p_disp.add_argument("--failure-limit", type=int,
                        default=kb.DEFAULT_SPAWN_FAILURE_LIMIT,
                        help=f"Auto-block a task after this many consecutive non-success attempts "
                             f"(spawn_failed, timed_out, or crashed; default: {kb.DEFAULT_SPAWN_FAILURE_LIMIT})")
    p_disp.add_argument("--json", action="store_true")

    # --- daemon (deprecated) ---
    p_daemon = sub.add_parser(
        "daemon",
        help="DEPRECATED — dispatcher now runs in the gateway. Use `hermes gateway start`.",
    )
    p_daemon.add_argument("--interval", type=float, default=60.0,
                          help="Seconds between dispatch ticks (default: 60)")
    p_daemon.add_argument("--max", type=int, default=None,
                          help="Cap number of spawns per tick")
    p_daemon.add_argument("--failure-limit", type=int,
                          default=kb.DEFAULT_SPAWN_FAILURE_LIMIT)
    p_daemon.add_argument("--pidfile", default=None,
                          help="Write the daemon's PID to this file on start")
    p_daemon.add_argument("--verbose", "-v", action="store_true",
                          help="Log each tick's outcome to stdout")
    # Undocumented escape hatch for users who truly cannot run the gateway.
    # Intentionally excluded from --help so nobody discovers it casually and
    # keeps the old double-dispatcher pattern alive.
    p_daemon.add_argument("--force", action="store_true",
                          help=argparse.SUPPRESS)

    # --- watch ---
    p_watch = sub.add_parser(
        "watch",
        help="Live-stream task_events to the terminal (Ctrl+C to exit)",
    )
    p_watch.add_argument("--assignee", default=None,
                         help="Only show events for tasks assigned to this profile")
    p_watch.add_argument("--tenant", default=None,
                         help="Only show events from tasks in this tenant")
    p_watch.add_argument("--kinds", default=None,
                         help="Comma-separated event kinds to include "
                              "(e.g. 'completed,blocked,gave_up,crashed,timed_out')")
    p_watch.add_argument("--interval", type=float, default=0.5,
                         help="Poll interval in seconds (default: 0.5)")

    # --- stats ---
    p_stats = sub.add_parser(
        "stats", help="Per-status + per-assignee counts + oldest-ready age",
    )
    p_stats.add_argument("--json", action="store_true")

    # --- notify subscribe / list / remove ---
    p_nsub = sub.add_parser(
        "notify-subscribe",
        help="Subscribe a gateway source to a task's terminal events "
             "(used by /kanban subscribe in the gateway adapter)",
    )
    p_nsub.add_argument("task_id")
    p_nsub.add_argument("--platform", required=True)
    p_nsub.add_argument("--chat-id", required=True)
    p_nsub.add_argument("--thread-id", default=None)
    p_nsub.add_argument("--user-id", default=None)
    p_nsub.add_argument(
        "--notifier-profile", default=None,
        help="Profile gateway that owns/delivers this subscription (default: active profile)",
    )

    p_nlist = sub.add_parser(
        "notify-list",
        help="List notification subscriptions (optionally for a single task)",
    )
    p_nlist.add_argument("task_id", nargs="?", default=None)
    p_nlist.add_argument("--json", action="store_true")

    p_nrm = sub.add_parser(
        "notify-unsubscribe",
        help="Remove a gateway subscription from a task",
    )
    p_nrm.add_argument("task_id")
    p_nrm.add_argument("--platform", required=True)
    p_nrm.add_argument("--chat-id", required=True)
    p_nrm.add_argument("--thread-id", default=None)

    # --- log ---
    p_log = sub.add_parser(
        "log",
        help="Print the worker log for a task (from <kanban-root>/kanban/logs/)",
    )
    p_log.add_argument("task_id")
    p_log.add_argument("--tail", type=int, default=None,
                       help="Only print the last N bytes")

    # --- runs (per-attempt history for a task) ---
    p_runs = sub.add_parser(
        "runs",
        help="Show attempt history for a task (one row per run: profile, "
             "outcome, elapsed, summary)",
    )
    p_runs.add_argument("task_id")
    p_runs.add_argument("--json", action="store_true")
    p_runs.add_argument(
        "--state-type",
        choices=("status", "outcome"),
        default=None,
        help="With --state-name: filter runs by task_runs column",
    )
    p_runs.add_argument(
        "--state-name",
        default=None,
        metavar="VALUE",
        help="With --state-type: keep runs whose column equals this value",
    )

    # --- heartbeat (worker liveness signal) ---
    p_hb = sub.add_parser(
        "heartbeat",
        help="Emit a heartbeat event for a running task (worker liveness signal)",
    )
    p_hb.add_argument("task_id")
    p_hb.add_argument("--note", default=None,
                      help="Optional short note attached to the heartbeat event")

    # --- assignees ---
    p_asg = sub.add_parser(
        "assignees",
        help="List known profiles + per-profile task counts "
             "(union of ~/.hermes/profiles/ and current assignees on the board)",
    )
    p_asg.add_argument("--json", action="store_true")

    # --- worker-lanes ---
    p_lanes = sub.add_parser(
        "worker-lanes",
        aliases=["lanes"],
        help="List registered external worker lanes and active worker instances",
    )
    p_lanes.add_argument("--json", action="store_true")

    # --- worker-lane-request ---
    p_lane_req = sub.add_parser(
        "worker-lane-request",
        aliases=["lane-request"],
        help="Validate and optionally enable a skill-generated worker lane request",
    )
    p_lane_req.add_argument(
        "path",
        nargs="?",
        default="-",
        help="JSON/YAML file containing worker_lane_request, or '-' for stdin",
    )
    p_lane_req.add_argument(
        "--enable",
        action="store_true",
        help="Enable the validated request in the current process registry",
    )
    p_lane_req.add_argument(
        "--persist",
        action="store_true",
        help="Persist the sanitized lane under kanban.worker_lanes in config.yaml",
    )
    p_lane_req.add_argument(
        "--replace",
        action="store_true",
        help="Replace an existing lane with the same name",
    )
    p_lane_req.add_argument(
        "--task-id",
        default=None,
        help="Optional task id to receive a lane request audit event",
    )
    p_lane_req.add_argument(
        "--source-event-id",
        type=int,
        default=None,
        help="Optional worker_lane_request_intent event id being resolved",
    )
    p_lane_req.add_argument(
        "--requested-by",
        default=None,
        help="Controller/skill/operator identity for the audit event",
    )
    p_lane_req.add_argument("--json", action="store_true")

    # --- context --- (for spawned workers)
    p_ctx = sub.add_parser(
        "context",
        help="Print the full context a worker sees for a task "
             "(title + body + parent results + comments).",
    )
    p_ctx.add_argument("task_id")

    # --- specify --- (triage → todo via auxiliary LLM)
    p_specify = sub.add_parser(
        "specify",
        help="Flesh out a triage-column task into a concrete spec "
             "(title + body) and promote it to todo. Uses the auxiliary "
             "LLM configured under auxiliary.triage_specifier.",
    )
    p_specify.add_argument(
        "task_id",
        nargs="?",
        default=None,
        help="Task id to specify (required unless --all is given)",
    )
    p_specify.add_argument(
        "--all",
        dest="all_triage",
        action="store_true",
        help="Specify every task currently in the triage column",
    )
    p_specify.add_argument(
        "--tenant",
        default=None,
        help="When used with --all, restrict the sweep to this tenant",
    )
    p_specify.add_argument(
        "--author",
        default=None,
        help="Author name recorded on the audit comment "
             "(default: $HERMES_PROFILE or 'specifier')",
    )
    p_specify.add_argument(
        "--json",
        action="store_true",
        help="Emit one JSON object per task on stdout",
    )

    # --- decompose --- (triage → fan-out via auxiliary LLM + orchestrator)
    p_decompose = sub.add_parser(
        "decompose",
        help="Decompose a triage-column task into a graph of child tasks "
             "routed to specialist profiles by description. Falls back to "
             "specify-style single-task promotion when the task doesn't "
             "benefit from fan-out. Uses auxiliary.kanban_decomposer.",
    )
    p_decompose.add_argument(
        "task_id",
        nargs="?",
        default=None,
        help="Task id to decompose (required unless --all is given)",
    )
    p_decompose.add_argument(
        "--all",
        dest="all_triage",
        action="store_true",
        help="Decompose every task currently in the triage column",
    )
    p_decompose.add_argument(
        "--tenant",
        default=None,
        help="When used with --all, restrict the sweep to this tenant",
    )
    p_decompose.add_argument(
        "--author",
        default=None,
        help="Author name recorded on the audit comment "
             "(default: $HERMES_PROFILE or 'decomposer')",
    )
    p_decompose.add_argument(
        "--json",
        action="store_true",
        help="Emit one JSON object per task on stdout",
    )

    # --- runtime kernel control plane ---
    p_runtime = sub.add_parser(
        "runtime",
        help="Create and advance goal-driven runtime kernel jobs",
    )
    runtime_sub = p_runtime.add_subparsers(dest="runtime_action")

    rt_create = runtime_sub.add_parser("create", help="Create a runtime job from an objective")
    rt_create.add_argument("objective")
    rt_create.add_argument("--assignee", default=None, help="Assignee/worker lane for the initial runtime node")
    rt_create.add_argument("--workspace", default="scratch",
                           help="scratch | worktree | worktree:<path> | dir:<path>")
    rt_create.add_argument("--goal-item", action="append", default=[],
                           metavar="KEY:DESCRIPTION",
                           help="Required goal item; repeatable. Defaults to Phase 1 fixture item.")
    rt_create.add_argument("--created-by", default="runtime", help="Creator recorded on the root Kanban task")
    rt_create.add_argument("--idempotency-key", default=None, help="Dedup key for the root Kanban task")
    rt_create.add_argument("--json", action="store_true")

    rt_promote = runtime_sub.add_parser("promote", help="Promote an existing Kanban root task into a runtime job")
    rt_promote.add_argument("task_id")
    rt_promote.add_argument("--objective", default=None, help="Override objective; defaults to task body/title")
    rt_promote.add_argument("--assignee", default=None, help="Assignee/worker lane for the initial runtime node")
    rt_promote.add_argument("--workspace-path", default=None, help="Override runtime workspace path")
    rt_promote.add_argument("--goal-item", action="append", default=[], metavar="KEY:DESCRIPTION")
    rt_promote.add_argument("--json", action="store_true")

    rt_status = runtime_sub.add_parser("status", help="Show runtime job status")
    rt_status.add_argument("job_id")
    rt_status.add_argument("--json", action="store_true")

    rt_list = runtime_sub.add_parser("list", aliases=["ls"], help="List runtime jobs")
    rt_list.add_argument("--state", default=None)
    rt_list.add_argument("--limit", type=int, default=50)
    rt_list.add_argument("--json", action="store_true")

    rt_advance = runtime_sub.add_parser("advance", help="Advance a runtime job")
    rt_advance.add_argument("job_id")
    rt_advance.add_argument("--loop", action="store_true", help="Run a bounded supervisor loop")
    rt_advance.add_argument("--max-steps", type=int, default=8)
    rt_advance.add_argument("--no-create-tasks", action="store_true")
    rt_advance.add_argument("--fake-provider", action="store_true",
                            help="Use deterministic fixture provider; not a real LLM")
    rt_advance.add_argument("--provider", choices=["none", "fake", "real"], default=None,
                            help="Decision provider mode. 'real' requires --model-provider and --model")
    rt_advance.add_argument("--model-provider", default=None,
                            help="Hermes model source provider used when --provider real")
    rt_advance.add_argument("--model", default=None,
                            help="Model used when --provider real")
    rt_advance.add_argument("--codex-config", action="store_true",
                            help="Use ~/.codex config.toml/auth.json model source for --provider real")
    rt_advance.add_argument("--profile", default="graph_patch_decision",
                            help="Decision profile used when --provider real")
    rt_advance.add_argument("--max-retries", type=int, default=1,
                            help="Parse retry count for real decision provider")
    rt_advance.add_argument("--timeout", type=float, default=None,
                            help="Provider request timeout in seconds for --provider real")
    rt_advance.add_argument("--json", action="store_true")

    rt_decision = runtime_sub.add_parser("decision", help="Show recent runtime decision records")
    rt_decision.add_argument("job_id")
    rt_decision.add_argument("--limit", type=int, default=10)
    rt_decision.add_argument("--json", action="store_true")

    rt_checkpoint = runtime_sub.add_parser("checkpoint", help="Create or show a decision checkpoint")
    rt_checkpoint.add_argument("job_id")
    rt_checkpoint.add_argument("--create", action="store_true", help="Create a new DB-derived checkpoint")
    rt_checkpoint.add_argument("--reason", default="manual", help="Reason recorded when creating a checkpoint")
    rt_checkpoint.add_argument("--json", action="store_true")

    rt_prompt = runtime_sub.add_parser("prompt", help="Render provider request/prompt input")
    rt_prompt.add_argument("job_id")
    rt_prompt.add_argument("--profile", default="graph_patch_decision")
    rt_prompt.add_argument("--json", action="store_true")

    rt_provider_smoke = runtime_sub.add_parser("provider-smoke", help="Dry-run or execute a runtime provider request without applying a patch")
    rt_provider_smoke.add_argument("job_id")
    rt_provider_smoke.add_argument("--model-provider", default=None)
    rt_provider_smoke.add_argument("--model", default=None)
    rt_provider_smoke.add_argument("--codex-config", action="store_true",
                                   help="Use ~/.codex config.toml/auth.json model source")
    rt_provider_smoke.add_argument("--profile", default="graph_patch_decision")
    rt_provider_smoke.add_argument("--max-retries", type=int, default=1)
    rt_provider_smoke.add_argument("--timeout", type=float, default=None)
    rt_provider_smoke.add_argument("--execute", action="store_true",
                                   help="Call the real provider but do not apply the returned patch")
    rt_provider_smoke.add_argument("--json", action="store_true")

    rt_context = runtime_sub.add_parser("context", help="Show decision session segment/checkpoint context")
    rt_context.add_argument("job_id")
    rt_context.add_argument("--json", action="store_true")

    rt_compact = runtime_sub.add_parser("compact", help="Compact a runtime decision session")
    rt_compact.add_argument("job_id")
    rt_compact.add_argument("--profile", default="token_budget_compaction")
    rt_compact.add_argument("--reason", default="manual")
    rt_compact.add_argument("--json", action="store_true")

    # --- gc ---
    p_gc = sub.add_parser(
        "gc", help="Garbage-collect archived-task workspaces, old events, and old logs",
    )
    p_gc.add_argument("--event-retention-days", type=int, default=30,
                      help="Delete task_events older than N days for terminal tasks (default: 30)")
    p_gc.add_argument("--log-retention-days", type=int, default=30,
                      help="Delete worker log files older than N days (default: 30)")

    kanban_parser.set_defaults(_kanban_parser=kanban_parser)
    return kanban_parser


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------

def kanban_command(args: argparse.Namespace) -> int:
    """Entry point from ``hermes kanban …`` argparse dispatch.

    Returns a shell-style exit code (0 on success, non-zero on error).
    """
    action = getattr(args, "kanban_action", None)
    if not action:
        # No subaction given: print help via the stored parser reference.
        parser = getattr(args, "_kanban_parser", None)
        if parser is not None:
            parser.print_help()
        else:
            print(
                "usage: hermes kanban <action> [options]\n"
                "Run 'hermes kanban --help' for the full list of actions.",
                file=sys.stderr,
            )
        return 0

    # Board-management commands operate on board metadata and the persisted
    # current-board pointer itself. They must ignore the shared `--board`
    # task-routing override; otherwise `/kanban --board beta boards show`
    # reports beta as the current board even when the on-disk pointer is
    # alpha.
    if action == "boards":
        return _dispatch_boards(args)

    # `--board <slug>` applies to every subcommand below by way of an
    # env-var pin for the duration of this call. Using HERMES_KANBAN_BOARD
    # (rather than threading `board=` through 50+ kb.connect() sites)
    # keeps the patch small and inherits the exact same resolution the
    # dispatcher uses for workers — consistency is a feature here.
    board_override = getattr(args, "board", None)
    prev_board_env = os.environ.get("HERMES_KANBAN_BOARD")
    restore_board_env = False

    def _restore_board_env() -> None:
        if not restore_board_env:
            return
        if prev_board_env is None:
            os.environ.pop("HERMES_KANBAN_BOARD", None)
        else:
            os.environ["HERMES_KANBAN_BOARD"] = prev_board_env
    if board_override:
        try:
            normed = kb._normalize_board_slug(board_override)
        except ValueError as exc:
            print(f"kanban: {exc}", file=sys.stderr)
            return 2
        if not normed:
            print("kanban: --board requires a slug", file=sys.stderr)
            return 2
        # Boards other than 'default' must already exist — typoed slugs
        # would otherwise silently create an empty board.
        if normed != kb.DEFAULT_BOARD and not kb.board_exists(normed):
            print(
                f"kanban: board {normed!r} does not exist. "
                f"Create it with `hermes kanban boards create {normed}`.",
                file=sys.stderr,
            )
            return 1
        os.environ["HERMES_KANBAN_BOARD"] = normed
        restore_board_env = True

    # Auto-initialize the DB before dispatching any subcommand. init_db
    # is idempotent, so running it every invocation is cheap (one
    # SELECT against sqlite_master when tables already exist) and
    # prevents "no such table: tasks" on first use from a fresh
    # HERMES_HOME. Previously only `init` and `daemon` triggered
    # schema creation; `create` / `list` / every other command would
    # error out on a fresh install.
    try:
        kb.init_db()
    except Exception as exc:
        print(f"kanban: could not initialize database: {exc}", file=sys.stderr)
        _restore_board_env()
        return 1

    handlers = {
        "init":     _cmd_init,
        "create":   _cmd_create,
        "goal":     _cmd_goal,
        "swarm":    _cmd_swarm,
        "list":     _cmd_list,
        "ls":       _cmd_list,
        "show":     _cmd_show,
        "progress": _cmd_progress,
        "acceptance": _cmd_acceptance,
        "verify": _cmd_verify,
        "acceptance-check-request": _cmd_acceptance_check_request,
        "acceptance-request": _cmd_acceptance_check_request,
        "advance-acceptance": _cmd_advance_acceptance,
        "advance-goal": _cmd_advance_goal,
        "advance-controller": _cmd_advance_controller,
        "controller-tick": _cmd_advance_controller,
        "reviews":  _cmd_reviews,
        "review":   _cmd_review,
        "plan-review": _cmd_plan_review,
        "assign":   _cmd_assign,
        "reclaim":  _cmd_reclaim,
        "reassign": _cmd_reassign,
        "diagnostics": _cmd_diagnostics,
        "diag":     _cmd_diagnostics,
        "link":     _cmd_link,
        "unlink":   _cmd_unlink,
        "claim":    _cmd_claim,
        "comment":  _cmd_comment,
        "complete": _cmd_complete,
        "edit":     _cmd_edit,
        "block":    _cmd_block,
        "schedule": _cmd_schedule,
        "unblock":  _cmd_unblock,
        "archive":  _cmd_archive,
        "tail":     _cmd_tail,
        "dispatch": _cmd_dispatch,
        "daemon":   _cmd_daemon,
        "watch":    _cmd_watch,
        "stats":    _cmd_stats,
        "log":      _cmd_log,
        "runs":     _cmd_runs,
        "heartbeat": _cmd_heartbeat,
        "assignees": _cmd_assignees,
        "worker-lanes": _cmd_worker_lanes,
        "lanes": _cmd_worker_lanes,
        "worker-lane-request": _cmd_worker_lane_request,
        "lane-request": _cmd_worker_lane_request,
        "notify-subscribe":   _cmd_notify_subscribe,
        "notify-list":        _cmd_notify_list,
        "notify-unsubscribe": _cmd_notify_unsubscribe,
        "context":  _cmd_context,
        "specify":  _cmd_specify,
        "decompose":  _cmd_decompose,
        "runtime": _dispatch_runtime,
        "gc":       _cmd_gc,
    }
    handler = handlers.get(action)
    if not handler:
        print(f"kanban: unknown action {action!r}", file=sys.stderr)
        _restore_board_env()
        return 2
    try:
        return int(handler(args) or 0)
    except (ValueError, RuntimeError) as exc:
        print(f"kanban: {exc}", file=sys.stderr)
        _restore_board_env()
        return 1
    finally:
        _restore_board_env()


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _profile_author() -> str:
    """Best-effort author name for an interactive CLI call."""
    for env in ("HERMES_PROFILE_NAME", "HERMES_PROFILE"):
        v = os.environ.get(env)
        if v:
            return v
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name() or "user"
    except Exception:
        return "user"


# ---------------------------------------------------------------------------
# Boards management (hermes kanban boards …)
# ---------------------------------------------------------------------------

def _dispatch_boards(args: argparse.Namespace) -> int:
    """Handle ``hermes kanban boards <action>``.

    Boards management is deliberately separate from the task-level
    commands: it operates on the filesystem (board directories,
    ``current`` pointer, ``board.json``), not on the per-board SQLite
    DB, so a fresh HERMES_HOME that has never called ``kanban init``
    can still run ``boards create`` / ``boards list``.
    """
    sub = getattr(args, "boards_action", None) or "list"
    if sub in {"list", "ls"}:
        return _cmd_boards_list(args)
    if sub in {"create", "new"}:
        return _cmd_boards_create(args)
    if sub in {"rm", "remove", "delete"}:
        return _cmd_boards_rm(args)
    if sub in {"switch", "use"}:
        return _cmd_boards_switch(args)
    if sub in {"show", "current"}:
        return _cmd_boards_show(args)
    if sub == "rename":
        return _cmd_boards_rename(args)
    if sub == "set-default-workdir":
        return _cmd_boards_set_default_workdir(args)
    print(f"kanban boards: unknown action {sub!r}", file=sys.stderr)
    return 2


def _board_task_counts(slug: str) -> dict[str, int]:
    """Return ``{status: count}`` for a board. Safe to call on an empty DB."""
    try:
        path = kb.kanban_db_path(board=slug)
        if not path.exists():
            return {}
        with kb.connect(board=slug) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
            ).fetchall()
        return {r["status"]: int(r["n"]) for r in rows}
    except Exception:
        return {}


def _cmd_boards_list(args: argparse.Namespace) -> int:
    include_archived = bool(getattr(args, "all", False))
    boards = kb.list_boards(include_archived=include_archived)
    # Enrich each entry with task counts + whether it's the current board.
    current = kb.get_current_board()
    for b in boards:
        b["is_current"] = (b["slug"] == current)
        b["counts"] = _board_task_counts(b["slug"])
        b["total"] = sum(b["counts"].values())
    if getattr(args, "json", False):
        print(json.dumps(boards, indent=2, ensure_ascii=False))
        return 0
    # Human table: marker (•) for current, slug, display name, counts.
    if not boards:
        print("(no boards — create one with `hermes kanban boards create <slug>`)")
        return 0
    print(f"{'':2s}  {'SLUG':24s}  {'NAME':28s}  COUNTS")
    for b in boards:
        marker = "●" if b["is_current"] else " "
        counts = b["counts"] or {}
        counts_str = (
            ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            or "(empty)"
        )
        name = b.get("name") or ""
        if b.get("archived"):
            name += " [archived]"
        print(f"{marker:2s}  {b['slug']:24s}  {name:28s}  {counts_str}")
    print()
    print(f"Current board: {current}")
    if len(boards) > 1:
        print("Switch boards with `hermes kanban boards switch <slug>`.")
    return 0


def _cmd_boards_create(args: argparse.Namespace) -> int:
    try:
        normed = kb._normalize_board_slug(args.slug)
    except ValueError as exc:
        print(f"kanban boards create: {exc}", file=sys.stderr)
        return 2
    if not normed:
        print("kanban boards create: slug is required", file=sys.stderr)
        return 2
    already = kb.board_exists(normed) and normed != kb.DEFAULT_BOARD
    meta = kb.create_board(
        normed,
        name=args.name,
        description=args.description,
        icon=args.icon,
        color=args.color,
        default_workdir=args.default_workdir,
    )
    verb = "already exists" if already else "created"
    print(f"Board {meta['slug']!r} {verb}.")
    print(f"  Display name: {meta.get('name', '')}")
    print(f"  DB path:      {meta['db_path']}")
    if getattr(args, "switch", False):
        kb.set_current_board(meta["slug"])
        print(f"  Switched to {meta['slug']!r}.")
    else:
        print(f"  Use `hermes kanban boards switch {meta['slug']}` to make it current.")
    return 0


def _cmd_boards_rm(args: argparse.Namespace) -> int:
    # When the user runs `hermes kanban boards delete <slug>` (alias), the
    # boards_action is 'delete' but args.delete is never set to True because
    # the --delete flag belongs to the 'rm' subparser only.  Detect the alias
    # and treat it identically to `boards rm --delete` (fixes #23139).
    force_delete = getattr(args, "delete", False) or getattr(args, "boards_action", "") == "delete"
    try:
        res = kb.remove_board(args.slug, archive=not force_delete)
    except ValueError as exc:
        print(f"kanban boards rm: {exc}", file=sys.stderr)
        return 1
    if res["action"] == "archived":
        print(f"Board {res['slug']!r} archived → {res['new_path']}")
        print("Recover by moving the directory back to "
              "<root>/kanban/boards/<slug>/.")
    else:
        print(f"Board {res['slug']!r} deleted.")
    return 0


def _cmd_boards_switch(args: argparse.Namespace) -> int:
    try:
        normed = kb._normalize_board_slug(args.slug)
    except ValueError as exc:
        print(f"kanban boards switch: {exc}", file=sys.stderr)
        return 2
    if not normed:
        print("kanban boards switch: slug is required", file=sys.stderr)
        return 2
    if not kb.board_exists(normed):
        print(
            f"kanban boards switch: board {normed!r} does not exist. "
            f"Create it with `hermes kanban boards create {normed}`.",
            file=sys.stderr,
        )
        return 1
    kb.set_current_board(normed)
    print(f"Active board is now {normed!r}.")
    return 0


def _cmd_boards_show(args: argparse.Namespace) -> int:
    current = kb.get_current_board()
    meta = kb.read_board_metadata(current)
    counts = _board_task_counts(current)
    total = sum(counts.values())
    print(f"Current board: {current}")
    print(f"  Display name: {meta.get('name', '')}")
    if meta.get("description"):
        print(f"  Description:  {meta['description']}")
    print(f"  DB path:      {meta['db_path']}")
    print(f"  Tasks:        {total} total"
          + (f" ({', '.join(f'{k}={v}' for k, v in sorted(counts.items()))})"
             if counts else ""))
    return 0


def _cmd_boards_rename(args: argparse.Namespace) -> int:
    try:
        normed = kb._normalize_board_slug(args.slug)
    except ValueError as exc:
        print(f"kanban boards rename: {exc}", file=sys.stderr)
        return 2
    if not normed or not kb.board_exists(normed):
        print(f"kanban boards rename: board {args.slug!r} does not exist",
              file=sys.stderr)
        return 1
    meta = kb.write_board_metadata(normed, name=args.name)
    print(f"Board {normed!r} renamed to {meta['name']!r}.")
    return 0


def _cmd_boards_set_default_workdir(args: argparse.Namespace) -> int:
    try:
        normed = kb._normalize_board_slug(args.slug)
    except ValueError as exc:
        print(f"kanban boards set-default-workdir: {exc}", file=sys.stderr)
        return 2
    if not normed or not kb.board_exists(normed):
        print(f"kanban boards set-default-workdir: board {args.slug!r} does not exist",
              file=sys.stderr)
        return 1
    meta = kb.write_board_metadata(normed, default_workdir=args.path)
    new_val = meta.get("default_workdir")
    if new_val:
        print(f"Board {normed!r} default workdir set to {new_val!r}.")
    else:
        print(f"Board {normed!r} default workdir cleared.")
    return 0


# ---------------------------------------------------------------------------


def _parse_runtime_goal_items(values: list[str]) -> Optional[list[dict[str, Any]]]:
    if not values:
        return None
    items: list[dict[str, Any]] = []
    for raw in values:
        key, sep, desc = str(raw).partition(":")
        key = key.strip()
        desc = desc.strip()
        if not sep or not key or not desc:
            raise ValueError("--goal-item must use KEY:DESCRIPTION")
        items.append(
            {
                "item_key": key,
                "description": desc,
                "required": True,
                "verifier_required": True,
            }
        )
    return items


def _runtime_summary(status: dict[str, Any]) -> dict[str, Any]:
    job = status["job"]
    return {
        "id": job["id"],
        "state": job["state"],
        "root_task_id": job.get("root_task_id"),
        "objective": job["objective"],
        "graph_revision": job["graph_revision"],
        "goal_items": [
            {
                "item_key": item["item_key"],
                "state": item["state"],
                "required": bool(item["required"]),
            }
            for item in status.get("goal_items", [])
        ],
        "open_gaps": [
            {
                "gap_key": gap["gap_key"],
                "gap_type": gap["gap_type"],
                "summary": gap["summary"],
            }
            for gap in status.get("goal_gaps", [])
            if gap.get("state") == "open"
        ],
        "frontier": [
            {
                "node_key": node["node_key"],
                "node_type": node["node_type"],
                "state": node["state"],
                "task_id": node.get("latest_task_id"),
                "assignee": node.get("assignee"),
            }
            for node in status.get("nodes", [])
            if node.get("state") in {"ready", "running", "waiting_human", "failed", "succeeded"}
        ],
        "ledger_summary": status.get("ledger_summary", []),
        "frontier_summary": status.get("frontier_summary", {}),
        "liveness": status.get("liveness", {}),
    }


def _dispatch_runtime(args: argparse.Namespace) -> int:
    sub = getattr(args, "runtime_action", None) or "list"
    if sub in {"list", "ls"}:
        return _cmd_runtime_list(args)
    if sub == "create":
        return _cmd_runtime_create(args)
    if sub == "promote":
        return _cmd_runtime_promote(args)
    if sub == "status":
        return _cmd_runtime_status(args)
    if sub == "advance":
        return _cmd_runtime_advance(args)
    if sub == "decision":
        return _cmd_runtime_decision(args)
    if sub == "checkpoint":
        return _cmd_runtime_checkpoint(args)
    if sub == "prompt":
        return _cmd_runtime_prompt(args)
    if sub == "provider-smoke":
        return _cmd_runtime_provider_smoke(args)
    if sub == "context":
        return _cmd_runtime_context(args)
    if sub == "compact":
        return _cmd_runtime_compact(args)
    print(f"kanban runtime: unknown action {sub!r}", file=sys.stderr)
    return 2


def _runtime_decision_provider_from_args(args: argparse.Namespace):
    from hermes_cli import kanban_runtime_decision as rd
    from hermes_cli import kanban_runtime_kernel as rk

    mode = getattr(args, "provider", None)
    fake_flag = bool(getattr(args, "fake_provider", False))
    if fake_flag and mode == "real":
        raise ValueError("--fake-provider cannot be combined with --provider real")
    if fake_flag or mode == "fake":
        return rk.fixture_decision_provider
    if mode in {None, "none"}:
        return None
    if mode != "real":
        raise ValueError(f"unknown runtime decision provider mode {mode!r}")

    source = _runtime_model_source_from_args(args, require_for_real=True)
    return rd.RuntimeDecisionProvider(
        provider_name=source["provider_name"],
        model=source["model"],
        profile_name=getattr(args, "profile", None) or "graph_patch_decision",
        max_retries=getattr(args, "max_retries", 1),
        timeout_seconds=getattr(args, "timeout", None),
        explicit_base_url=source.get("explicit_base_url"),
        explicit_api_key=source.get("explicit_api_key"),
    )


def _runtime_model_source_from_args(args: argparse.Namespace, *, require_for_real: bool = False) -> dict[str, Any]:
    if getattr(args, "codex_config", False):
        return _runtime_model_source_from_codex_config(args)

    model_provider = (getattr(args, "model_provider", None) or "").strip()
    model = (getattr(args, "model", None) or "").strip()
    if require_for_real and (not model_provider or not model):
        raise ValueError("--provider real requires --model-provider and --model, or --codex-config")
    return {
        "source": "explicit",
        "provider_name": model_provider,
        "model": model,
        "display_provider": model_provider or None,
        "explicit_base_url": None,
        "explicit_api_key": None,
    }


def _runtime_model_source_from_codex_config(args: argparse.Namespace) -> dict[str, Any]:
    import tomllib
    from pathlib import Path

    config_path = Path.home() / ".codex" / "config.toml"
    auth_path = Path.home() / ".codex" / "auth.json"
    if not config_path.exists():
        raise ValueError("missing ~/.codex/config.toml")
    if not auth_path.exists():
        raise ValueError("missing ~/.codex/auth.json")
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"could not read .codex model source: {type(exc).__name__}") from exc

    codex_provider = str(config.get("model_provider") or "").strip()
    providers = config.get("model_providers") if isinstance(config.get("model_providers"), dict) else {}
    provider_cfg = providers.get(codex_provider) if isinstance(providers, dict) else None
    if not isinstance(provider_cfg, dict):
        raise ValueError(f".codex model_provider {codex_provider!r} is not configured")
    base_url = str(provider_cfg.get("base_url") or "").strip()
    api_key = str(auth.get("OPENAI_API_KEY") or "").strip()
    model = (getattr(args, "model", None) or config.get("model") or "").strip()
    if not base_url:
        raise ValueError(".codex provider base_url is missing")
    if not api_key:
        raise ValueError(".codex auth OPENAI_API_KEY is missing")
    if not model:
        raise ValueError(".codex model is missing; pass --model")
    return {
        "source": "codex_config",
        "codex_provider": codex_provider,
        "provider_name": "custom",
        "display_provider": f"codex:{codex_provider}",
        "model": model,
        "explicit_base_url": base_url,
        "explicit_api_key": api_key,
    }


def _cmd_runtime_create(args: argparse.Namespace) -> int:
    from hermes_cli import kanban_runtime_kernel as rk

    workspace_kind, workspace_path = _parse_workspace_flag(args.workspace)
    goal_items = _parse_runtime_goal_items(getattr(args, "goal_item", []))
    board = kb.get_current_board()
    with kb.connect() as conn:
        job_id = rk.create_runtime_job_from_objective(
            conn,
            args.objective,
            board=board,
            workspace_kind=workspace_kind,
            workspace_path=workspace_path,
            assignee=args.assignee,
            created_by=args.created_by,
            goal_items=goal_items,
            idempotency_key=args.idempotency_key,
        )
        status = rk.status_runtime_job(conn, job_id)
    if getattr(args, "json", False):
        print(json.dumps(_runtime_summary(status), indent=2, ensure_ascii=False))
    else:
        print(f"Created runtime job {job_id}")
        print(f"  Root task: {status['job']['root_task_id']}")
        print(f"  State:     {status['job']['state']}")
    return 0


def _cmd_runtime_promote(args: argparse.Namespace) -> int:
    from hermes_cli import kanban_runtime_kernel as rk

    goal_items = _parse_runtime_goal_items(getattr(args, "goal_item", []))
    board = kb.get_current_board()
    with kb.connect() as conn:
        job_id = rk.promote_runtime_job(
            conn,
            args.task_id,
            objective=args.objective,
            board=board,
            workspace_path=args.workspace_path,
            goal_items=goal_items,
            initial_assignee=args.assignee,
        )
        status = rk.status_runtime_job(conn, job_id)
    if getattr(args, "json", False):
        print(json.dumps(_runtime_summary(status), indent=2, ensure_ascii=False))
    else:
        print(f"Promoted {args.task_id} to runtime job {job_id}")
        print(f"  State: {status['job']['state']}")
    return 0


def _cmd_runtime_status(args: argparse.Namespace) -> int:
    from hermes_cli import kanban_runtime_kernel as rk

    with kb.connect() as conn:
        status = rk.status_runtime_job(conn, args.job_id)
    if getattr(args, "json", False):
        print(json.dumps(status, indent=2, ensure_ascii=False))
        return 0
    summary = _runtime_summary(status)
    print(f"Runtime job {summary['id']} [{summary['state']}] rev={summary['graph_revision']}")
    print(f"Objective: {summary['objective']}")
    print("Goal items:")
    for item in summary["goal_items"]:
        req = "required" if item["required"] else "optional"
        print(f"  - {item['item_key']}: {item['state']} ({req})")
    if summary["open_gaps"]:
        print("Open gaps:")
        for gap in summary["open_gaps"]:
            print(f"  - {gap['gap_key']}: {gap['summary']}")
    if summary["frontier"]:
        print("Frontier:")
        for node in summary["frontier"]:
            print(
                f"  - {node['node_key']} "
                f"[{node['node_type']}/{node['state']}] task={node['task_id'] or '-'}"
            )
    if summary["liveness"]:
        print(
            "Liveness: "
            f"ready={summary['liveness'].get('ready_count', 0)} "
            f"running={summary['liveness'].get('running_count', 0)} "
            f"waiting_human={summary['liveness'].get('waiting_human_count', 0)} "
            f"illegal_idle={summary['liveness'].get('illegal_idle', False)}"
        )
    return 0


def _cmd_runtime_list(args: argparse.Namespace) -> int:
    from hermes_cli import kanban_runtime_kernel as rk

    with kb.connect() as conn:
        jobs = rk.list_runtime_jobs(conn, state=args.state, limit=args.limit)
    if getattr(args, "json", False):
        print(json.dumps(jobs, indent=2, ensure_ascii=False))
        return 0
    if not jobs:
        print("(no runtime jobs)")
        return 0
    print(f"{'JOB':18s} {'STATE':16s} {'REV':>4s} {'ROOT':12s} OBJECTIVE")
    for job in jobs:
        objective = str(job.get("objective") or "")
        if len(objective) > 80:
            objective = objective[:77] + "..."
        print(
            f"{job['id']:18s} {job['state']:16s} "
            f"{int(job['graph_revision']):4d} {str(job.get('root_task_id') or '-'):12s} {objective}"
        )
    return 0


def _cmd_runtime_advance(args: argparse.Namespace) -> int:
    from hermes_cli import kanban_runtime_kernel as rk

    provider = _runtime_decision_provider_from_args(args)
    create_tasks = not getattr(args, "no_create_tasks", False)
    board = kb.get_current_board()
    with kb.connect() as conn:
        if getattr(args, "loop", False):
            result = rk.advance_runtime_job_until_idle(
                conn,
                args.job_id,
                board=board,
                create_tasks=create_tasks,
                decision_provider=provider,
                max_steps=args.max_steps,
            )
        else:
            tick = rk.advance_runtime_job(
                conn,
                args.job_id,
                board=board,
                create_tasks=create_tasks,
                decision_provider=provider,
            )
            result = {
                "job_id": tick.job_id,
                "state": tick.job_state,
                "reason": tick.job_state,
                "step": {
                    "materialized_nodes": tick.materialized_nodes,
                    "ingested_nodes": tick.ingested_nodes,
                    "decision_requested": tick.decision_requested,
                    "patch_status": tick.patch_status,
                },
            }
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"Advanced runtime job {result['job_id']}: {result['state']} ({result['reason']})")
        if "step" in result:
            step = result["step"]
            if step["materialized_nodes"]:
                print(f"  Materialized: {', '.join(step['materialized_nodes'])}")
            if step["ingested_nodes"]:
                print(f"  Ingested:     {', '.join(step['ingested_nodes'])}")
            if step["patch_status"]:
                print(f"  Patch:        {step['patch_status']}")
        else:
            print(f"  Steps: {len(result['steps'])}")
    return 0


def _cmd_runtime_decision(args: argparse.Namespace) -> int:
    with kb.connect() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, job_id, db_revision, decision_session_id, delta_json,
                       decision_json, status, validator_result_json, error,
                       created_at, completed_at
                  FROM kernel_decisions
                 WHERE job_id = ?
                 ORDER BY created_at DESC, id DESC
                 LIMIT ?
                """,
                (args.job_id, max(1, int(args.limit))),
            ).fetchall()
        ]
    for row in rows:
        for key in ("delta_json", "decision_json", "validator_result_json"):
            if row.get(key):
                row[key[:-5]] = json.loads(row[key])
        decision = row.get("decision")
        if isinstance(decision, dict):
            row["provider"] = decision.get("provider_name")
            row["model"] = decision.get("model")
            row["profile_name"] = decision.get("profile_name")
            row["profile_hash"] = decision.get("profile_hash")
            row["request_ref"] = decision.get("request_ref")
            row["response_ref"] = decision.get("response_ref")
            row["parse_status"] = decision.get("parse_status")
            row["retry_count"] = decision.get("retry_count")
            row["provider_latency_ms"] = decision.get("provider_latency_ms")
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("(no runtime decisions)")
        return 0
    for row in rows:
        err = f" error={row['error']}" if row.get("error") else ""
        provider_bits = []
        if row.get("provider"):
            provider_bits.append(f"provider={row['provider']}")
        if row.get("model"):
            provider_bits.append(f"model={row['model']}")
        if row.get("profile_name"):
            provider_bits.append(f"profile={row['profile_name']}")
        provider_text = f" {' '.join(provider_bits)}" if provider_bits else ""
        print(f"{row['id']} rev={row['db_revision']} status={row['status']}{provider_text}{err}")
    return 0


def _cmd_runtime_checkpoint(args: argparse.Namespace) -> int:
    from hermes_cli import kanban_runtime_decision as rd
    from hermes_cli import kanban_runtime_kernel as rk

    with kb.connect() as conn:
        rk.ensure_runtime_schema(conn)
        if getattr(args, "create", False):
            checkpoint = rd.create_decision_checkpoint(conn, args.job_id, reason=args.reason)
        else:
            checkpoint = rd.latest_decision_checkpoint(conn, args.job_id)
            if checkpoint is None:
                checkpoint = rd.create_decision_checkpoint(conn, args.job_id, reason="initial")
    if getattr(args, "json", False):
        print(json.dumps(checkpoint, indent=2, ensure_ascii=False))
        return 0
    print(
        f"Decision checkpoint {checkpoint['id']} "
        f"job={checkpoint['job_id']} revision={checkpoint['revision']} reason={checkpoint['reason']}"
    )
    return 0


def _cmd_runtime_prompt(args: argparse.Namespace) -> int:
    from hermes_cli import kanban_runtime_decision as rd
    from hermes_cli import kanban_runtime_kernel as rk

    with kb.connect() as conn:
        rk.ensure_runtime_schema(conn)
        delta = rk.build_decision_delta(conn, args.job_id)
        request = rd.build_decision_provider_request(conn, args.job_id, delta)
        rendered = rd.render_decision_prompt(request)
        messages, _, profile = rd.render_decision_messages(request, profile_name=args.profile)
    payload = {
        "request": request.to_dict(),
        "rendered": rendered,
        "profile": {
            "profile_name": profile["profile_name"],
            "profile_version": profile["profile_version"],
            "profile_hash": profile["profile_hash"],
            "profile_path": profile["profile_path"],
        },
        "messages": messages,
        "provider_call": {
            "no_tools": True,
            "single_shot": True,
            "message_count": len(messages),
            "input_token_estimate": rd.estimate_decision_input_tokens(rendered, profile["content"]),
        },
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _cmd_runtime_provider_smoke(args: argparse.Namespace) -> int:
    from hermes_cli import kanban_runtime_decision as rd
    from hermes_cli import kanban_runtime_kernel as rk

    execute = bool(getattr(args, "execute", False))
    source = _runtime_model_source_from_args(args, require_for_real=execute)

    with kb.connect() as conn:
        rk.ensure_runtime_schema(conn)
        delta = rk.build_decision_delta(conn, args.job_id)
        request = rd.build_decision_provider_request(conn, args.job_id, delta)
        messages, rendered, profile = rd.render_decision_messages(request, profile_name=args.profile)

    payload = {
        "job_id": args.job_id,
        "dry_run": not execute,
        "request": request.to_dict(),
        "rendered": rendered,
        "profile": {
            "profile_name": profile["profile_name"],
            "profile_version": profile["profile_version"],
            "profile_hash": profile["profile_hash"],
            "profile_path": profile["profile_path"],
        },
        "provider_call": {
            "mode": "real" if execute else "dry_run",
            "source": source["source"],
            "model_provider": source["display_provider"],
            "model": source["model"] or None,
            "explicit_base_url": bool(source.get("explicit_base_url")),
            "explicit_api_key": bool(source.get("explicit_api_key")),
            "no_tools": True,
            "single_shot": True,
            "message_count": len(messages),
            "input_token_estimate": rd.estimate_decision_input_tokens(rendered, profile["content"]),
            "max_retries": int(getattr(args, "max_retries", 1)),
            "timeout_seconds": getattr(args, "timeout", None),
        },
    }
    if execute:
        provider = rd.RuntimeDecisionProvider(
            provider_name=source["provider_name"],
            model=source["model"],
            profile_name=args.profile,
            max_retries=getattr(args, "max_retries", 1),
            timeout_seconds=getattr(args, "timeout", None),
            explicit_base_url=source.get("explicit_base_url"),
            explicit_api_key=source.get("explicit_api_key"),
        )
        result = provider.decide(request)
        payload["provider_result"] = result.to_dict()
        if result.patch is not None:
            with kb.connect() as conn:
                rk.ensure_runtime_schema(conn)
                payload["validation"] = rk.validate_graph_patch(conn, args.job_id, result.patch)
        else:
            payload["validation"] = {
                "status": "skipped",
                "would_apply": False,
                "reason": result.error or result.parse_status,
            }
        payload["applied"] = False

    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _cmd_runtime_context(args: argparse.Namespace) -> int:
    from hermes_cli import kanban_runtime_decision as rd
    from hermes_cli import kanban_runtime_kernel as rk

    with kb.connect() as conn:
        rk.ensure_runtime_schema(conn)
        payload = rd.decision_context_status(conn, args.job_id)
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        active = payload["active_segment"]
        checkpoint = payload.get("latest_checkpoint")
        print(f"Decision context job={args.job_id} active_segment={active['id']} tokens={payload['active_segment_tokens']}")
        if checkpoint:
            print(f"  checkpoint={checkpoint['id']} revision={checkpoint.get('revision')}")
    return 0


def _cmd_runtime_compact(args: argparse.Namespace) -> int:
    from hermes_cli import kanban_runtime_decision as rd
    from hermes_cli import kanban_runtime_kernel as rk

    with kb.connect() as conn:
        rk.ensure_runtime_schema(conn)
        payload = rd.compact_decision_session(
            conn,
            args.job_id,
            profile_name=args.profile,
            reason=args.reason,
        )
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        if payload["status"] == "compacted":
            print(
                f"Compacted runtime job {args.job_id}: "
                f"checkpoint={payload['checkpoint_id']} new_segment={payload['new_segment_id']}"
            )
        else:
            print(f"Compaction rejected for {args.job_id}: {payload.get('reason')}")
    return 0


def _parse_duration(val) -> Optional[int]:
    """Parse ``30s`` / ``5m`` / ``2h`` / ``1d`` or a raw integer → seconds.

    Returns None for empty input. Raises ValueError on malformed input so
    the CLI can surface a usage error cleanly.
    """
    if val is None or val == "":
        return None
    s = str(val).strip().lower()
    # Bare integer → seconds.
    try:
        return int(s)
    except ValueError:
        pass
    # Suffixed form.
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if s and s[-1] in units:
        try:
            n = float(s[:-1])
        except ValueError as exc:
            raise ValueError(f"malformed duration {val!r}") from exc
        return int(n * units[s[-1]])
    raise ValueError(f"malformed duration {val!r} (expected 30s, 5m, 2h, 1d, or a number)")


def _cmd_init(args: argparse.Namespace) -> int:
    path = kb.init_db()
    print(f"Kanban DB initialized at {path}")

    # Seed bundled skills (e.g. kanban-worker) into the active profile so
    # the kanban dispatcher can use them without a separate `hermes profile
    # create` step.  This is best-effort — a missing or broken profile is
    # not fatal to `kanban init`.
    try:
        profile_name = get_active_profile_name() or "default"
        profile_dir = get_profile_dir(profile_name)
        result = seed_profile_skills(profile_dir, quiet=True)
        if result:
            copied = result.get("copied", [])
            if copied:
                print(f"Seeded skill(s) into profile {profile_name}: {', '.join(copied)}")
    except Exception:
        pass  # best-effort

    print()
    # Enumerate profiles on disk so the user knows what assignees are
    # already addressable. Multica does this auto-detection on its
    # daemon start; we do it here at init time instead because our
    # dispatcher doesn't need to enumerate — we just pass the name
    # through to `hermes -p <name>`.
    try:
        profiles = kb.list_profiles_on_disk()
    except Exception:
        profiles = []
    if profiles:
        print(f"Discovered {len(profiles)} profile(s) on disk; any of these can "
              f"be an --assignee:")
        for name in profiles:
            print(f"  {name}")
    else:
        print("No profiles found under ~/.hermes/profiles/.")
        print("Create one with `hermes -p <name> setup` before assigning tasks.")
    print()
    print("Next step: start the gateway so ready tasks actually get picked up.")
    print("  hermes gateway start")
    print()
    print(
        "The gateway hosts an embedded dispatcher that ticks every 60 seconds\n"
        "by default (config: kanban.dispatch_interval_seconds). Without a\n"
        "running gateway, tasks stay in 'ready' forever."
    )
    return 0


def _cmd_heartbeat(args: argparse.Namespace) -> int:
    with kb.connect() as conn:
        ok = kb.heartbeat_worker(
            conn,
            args.task_id,
            note=getattr(args, "note", None),
            expected_run_id=_worker_run_id_for(args.task_id),
        )
    if not ok:
        print(f"cannot heartbeat {args.task_id} (not running?)", file=sys.stderr)
        return 1
    print(f"Heartbeat recorded for {args.task_id}")
    return 0


def _cmd_assignees(args: argparse.Namespace) -> int:
    with kb.connect() as conn:
        data = kb.known_assignees(conn)
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    if not data:
        print("(no assignees — create a profile with `hermes -p <name> setup`)")
        return 0
    # Header
    print(f"{'NAME':20s}  {'TYPE':14s}  COUNTS")
    for entry in data:
        if entry.get("worker_lane"):
            entry_type = entry.get("worker_kind") or "worker_lane"
        else:
            entry_type = "profile" if entry["on_disk"] else "unknown"
        counts = entry["counts"] or {}
        count_str = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "(idle)"
        print(f"{entry['name']:20s}  {entry_type:14s}  {count_str}")
    return 0


def _cmd_worker_lanes(args: argparse.Namespace) -> int:
    with kb.connect() as conn:
        lanes = [lane.to_dict() for lane in kb.worker_lane_statuses(conn)]
    if getattr(args, "json", False):
        print(json.dumps(lanes, indent=2, ensure_ascii=False))
        return 0
    if not lanes:
        print("(no worker lanes registered)")
        return 0
    print(f"{'LANE':20s}  {'KIND':12s}  {'ACTIVE':>8s}  {'CAP':>5s}  COUNTS")
    for lane in lanes:
        counts = lane.get("counts") or {}
        count_str = ", ".join(
            f"{k}={v}" for k, v in sorted(counts.items())
        ) or "(idle)"
        max_concurrency = lane.get("max_concurrency")
        active_count = int(lane.get("active_count") or 0)
        cap = "-" if max_concurrency is None else str(max_concurrency)
        print(
            f"{lane.get('name', '-')[:20]:20s}  "
            f"{lane.get('kind', '-')[:12]:12s}  "
            f"{active_count:8d}  {cap:>5s}  {count_str}"
        )
        for inst in lane.get("active") or []:
            run_id = inst.get("run_id") or "-"
            pid = inst.get("worker_pid") or "-"
            hb = _fmt_ts(inst["last_heartbeat_at"]) if inst.get("last_heartbeat_at") else "-"
            model = inst.get("model") or (lane.get("config") or {}).get("model") or "-"
            print(
                f"  - {inst.get('task_id', '-'):<10s} "
                f"run={run_id} pid={pid} model={model} heartbeat={hb}"
            )
    return 0


def _load_lane_request_payload(path: str) -> dict[str, Any]:
    raw_path = (path or "-").strip()
    if raw_path == "-":
        text = sys.stdin.read()
    else:
        text = Path(raw_path).expanduser().read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError("worker lane request input is empty")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        if yaml is None:
            raise ValueError("worker lane request is not JSON and PyYAML is unavailable")
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("worker lane request input must be a mapping")
    req = data.get("worker_lane_request", data)
    if not isinstance(req, dict):
        raise ValueError("worker_lane_request must be a mapping")
    return req


def _load_acceptance_check_request_payload(path: str) -> dict[str, Any]:
    raw_path = (path or "-").strip()
    if raw_path == "-":
        text = sys.stdin.read()
    else:
        text = Path(raw_path).expanduser().read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError("acceptance check request input is empty")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        if yaml is None:
            raise ValueError(
                "acceptance check request is not JSON and PyYAML is unavailable"
            )
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("acceptance check request input must be a mapping")
    req = data.get("acceptance_check_request", data)
    if not isinstance(req, dict):
        raise ValueError("acceptance_check_request must be a mapping")
    return req


def _cmd_acceptance_check_request(args: argparse.Namespace) -> int:
    try:
        req = _load_acceptance_check_request_payload(args.path)
        valid = kb.validate_acceptance_check_request(req)
        with kb.connect() as conn:
            payload = kb.add_acceptance_check_request(
                conn,
                args.task_id,
                valid,
                source_run_id=getattr(args, "source_run_id", None),
                requested_by=(
                    getattr(args, "requested_by", None)
                    or os.environ.get("HERMES_PROFILE")
                    or "agent"
                ),
            )
            gate = kb.acceptance_check_gate_status(
                conn,
                args.task_id,
                source_run_id=payload.get("source_run_id"),
            )
        out = {
            "valid": True,
            "task_id": args.task_id,
            "source_run_id": payload.get("source_run_id"),
            "request": valid,
            "acceptance_check_gate": gate,
        }
    except Exception as exc:
        print(f"kanban acceptance-check-request: {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    print(f"Acceptance check request attached to {args.task_id}: {valid['name']}")
    print(f"  type: {valid['type']}")
    if valid.get("description"):
        print(f"  description: {valid['description']}")
    if valid.get("path"):
        print(f"  path: {valid['path']}")
    gate = out.get("acceptance_check_gate") or {}
    if gate:
        print(
            "  gate: "
            f"ready={gate.get('ready')} "
            f"satisfied={gate.get('satisfied', 0)}/{gate.get('required', 0)}"
        )
    return 0


def _cmd_worker_lane_request(args: argparse.Namespace) -> int:
    from hermes_cli.worker_lanes import (
        enable_worker_lane_request,
        validate_worker_lane_request,
    )

    try:
        req = _load_lane_request_payload(args.path)
        valid = validate_worker_lane_request(req)
    except Exception as exc:
        print(f"kanban worker-lane-request: {exc}", file=sys.stderr)
        return 1

    enabled = False
    lane_info = None
    if getattr(args, "enable", False) or getattr(args, "persist", False):
        try:
            lane = enable_worker_lane_request(
                req,
                persist=bool(getattr(args, "persist", False)),
                replace=bool(getattr(args, "replace", False)),
            )
        except Exception as exc:
            print(f"kanban worker-lane-request: {exc}", file=sys.stderr)
            return 1
        enabled = True
        lane_info = {
            "name": lane.name,
            "kind": lane.kind,
            "source": lane.source,
            "success_policy": lane.success_policy,
            "max_concurrency": lane.max_concurrency,
        }

    task_id = getattr(args, "task_id", None)
    source_event_id = getattr(args, "source_event_id", None)
    requested_by = (
        getattr(args, "requested_by", None)
        or os.environ.get("HERMES_PROFILE")
        or "cli"
    )
    if task_id:
        try:
            with kb.connect() as conn:
                if kb.get_task(conn, task_id) is None:
                    raise ValueError(f"task {task_id} not found")
                kb.record_task_event(
                    conn,
                    task_id,
                    (
                        "worker_lane_request_approved"
                        if enabled
                        else "worker_lane_request_validated"
                    ),
                    {
                        "requested_by": requested_by,
                        "source_event_id": source_event_id,
                        "enabled": enabled,
                        "persisted": bool(getattr(args, "persist", False)),
                        "replace": bool(getattr(args, "replace", False)),
                        "config": valid,
                        "lane": lane_info,
                    },
                )
        except Exception as exc:
            print(f"kanban worker-lane-request: {exc}", file=sys.stderr)
            return 1

    payload = {
        "valid": True,
        "enabled": enabled,
        "persisted": bool(getattr(args, "persist", False)),
        "lane": lane_info,
        "config": valid,
    }
    if task_id:
        payload.update({
            "task_id": task_id,
            "source_event_id": source_event_id,
            "requested_by": requested_by,
            "audit_event": (
                "worker_lane_request_approved"
                if enabled
                else "worker_lane_request_validated"
            ),
        })
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    status = "enabled" if enabled else "validated"
    if payload["persisted"]:
        status += " and persisted"
    print(f"Worker lane request {status}: {valid['name']}")
    print(f"  type: {valid['type']}")
    if valid.get("model"):
        print(f"  model: {valid['model']}")
    print(f"  sandbox: {valid['sandbox']}")
    print(f"  approval: {valid['approval']}")
    print(f"  max_concurrency: {valid['max_concurrency']}")
    print(f"  success_policy: {valid['success_policy']}")
    if not enabled:
        print("  pass --enable to register it, or --persist to write config.yaml")
    return 0


def _cmd_create(args: argparse.Namespace) -> int:
    try:
        ws_kind, ws_path = _parse_workspace_flag(args.workspace)
        branch_name = _parse_branch_flag(getattr(args, "branch", None))
    except argparse.ArgumentTypeError as exc:
        print(f"kanban: {exc}", file=sys.stderr)
        return 2
    if branch_name and ws_kind != "worktree":
        print("kanban: --branch is only valid with --workspace worktree", file=sys.stderr)
        return 2
    try:
        max_runtime = _parse_duration(getattr(args, "max_runtime", None))
    except ValueError as exc:
        print(f"kanban: --max-runtime: {exc}", file=sys.stderr)
        return 2
    max_retries = getattr(args, "max_retries", None)
    if max_retries is not None and max_retries < 1:
        print(
            f"kanban: --max-retries must be >= 1 (got {max_retries}); "
            "use 1 to trip on the first failure.",
            file=sys.stderr,
        )
        return 2
    acceptance_requests: list[dict[str, Any]] = []
    try:
        for path in getattr(args, "acceptance_check_requests", None) or []:
            acceptance_requests.append(_load_acceptance_check_request_payload(path))
        if acceptance_requests:
            acceptance_requests = kb.validate_acceptance_check_requests(
                acceptance_requests
            )
    except ValueError as exc:
        print(f"kanban create: acceptance-check-request: {exc}", file=sys.stderr)
        return 2
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title=args.title,
            body=args.body,
            assignee=args.assignee,
            created_by=args.created_by or _profile_author(),
            workspace_kind=ws_kind,
            workspace_path=ws_path,
            branch_name=branch_name,
            tenant=args.tenant,
            priority=args.priority,
            parents=tuple(args.parent or ()),
            triage=bool(getattr(args, "triage", False)),
            idempotency_key=getattr(args, "idempotency_key", None),
            max_runtime_seconds=max_runtime,
            skills=getattr(args, "skills", None) or None,
            max_retries=max_retries,
            initial_status=getattr(args, "initial_status", "running"),
            acceptance_check_requests=acceptance_requests,
        )
        task = kb.get_task(conn, task_id)
        acceptance_gate = (
            kb.acceptance_check_gate_status(conn, task_id, source_run_id=None)
            if acceptance_requests
            else None
        )
    if getattr(args, "json", False):
        payload = _task_to_dict(task)
        if acceptance_gate is not None:
            payload["acceptance_check_gate"] = acceptance_gate
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Created {task_id}  ({task.status}, assignee={task.assignee or '-'})")
        if acceptance_gate is not None:
            names = [
                str(item.get("name"))
                for item in acceptance_gate.get("items", [])
                if item.get("requested") and item.get("name")
            ]
            if names:
                print("Acceptance requests: " + ", ".join(names))

        # Warn when the task would sit in `ready` because no dispatcher is
        # present. Only warn on ready+assigned tasks — triage/todo are
        # expected to sit idle until promoted, and unassigned tasks
        # can't be dispatched. Skipped in --json mode so the stdout
        # stream stays strictly machine-parseable for callers (the JSON
        # response itself carries enough info for them to decide if
        # they want to check dispatcher presence separately).
        if task.status == "ready" and task.assignee:
            running, message = _check_dispatcher_presence()
            if not running and message:
                print(f"\n⚠  {message}", file=sys.stderr)
    return 0


def _cmd_goal(args: argparse.Namespace) -> int:
    from hermes_cli.goals import create_kanban_task_from_goal

    def _goal_child_ids_from_db(conn, root_id: str) -> list[str]:
        linked_ids = kb.child_ids(conn, root_id)
        if linked_ids:
            return linked_ids
        events = kb.list_events(conn, root_id)
        for event in reversed(events):
            if event.kind != "decomposed" or not isinstance(event.payload, dict):
                continue
            raw_ids = event.payload.get("child_ids")
            if isinstance(raw_ids, list):
                return [item for item in raw_ids if isinstance(item, str)]
        return []

    try:
        ws_kind, ws_path = _parse_workspace_flag(getattr(args, "workspace", "scratch"))
        branch_name = _parse_branch_flag(getattr(args, "branch", None))
    except argparse.ArgumentTypeError as exc:
        print(f"kanban goal: {exc}", file=sys.stderr)
        return 2
    if branch_name and ws_kind != "worktree":
        print("kanban goal: --branch is only valid with --workspace worktree", file=sys.stderr)
        return 2
    try:
        max_runtime = _parse_duration(getattr(args, "max_runtime", None))
    except ValueError as exc:
        print(f"kanban goal: --max-runtime: {exc}", file=sys.stderr)
        return 2
    max_retries = getattr(args, "max_retries", None)
    if max_retries is not None and max_retries < 1:
        print(
            f"kanban goal: --max-retries must be >= 1 (got {max_retries})",
            file=sys.stderr,
        )
        return 2

    try:
        task_id = create_kanban_task_from_goal(
            args.goal,
            session_id=getattr(args, "session", None),
            assignee=getattr(args, "assignee", None),
            tenant=getattr(args, "tenant", None),
            priority=int(getattr(args, "priority", 0) or 0),
            workspace_kind=ws_kind,
            workspace_path=ws_path,
            branch_name=branch_name,
            max_runtime_seconds=max_runtime,
            max_retries=max_retries,
            board=getattr(args, "board", None),
            created_by=getattr(args, "created_by", None) or "goal",
            idempotency_key=getattr(args, "idempotency_key", None),
        )
    except ValueError as exc:
        print(f"kanban goal: {exc}", file=sys.stderr)
        return 2

    outcome = None
    advance_payload = None
    decompose_blocked_advance = False
    if getattr(args, "decompose", False):
        try:
            from hermes_cli.kanban_decompose import decompose_task

            outcome = decompose_task(
                task_id,
                author=getattr(args, "author", None) or getattr(args, "created_by", None) or "goal",
            )
        except Exception as exc:
            print(f"kanban goal: decomposer error: {exc}", file=sys.stderr)
            return 1

    if getattr(args, "advance", False):
        should_try_advance = True
        if outcome is not None and not outcome.ok:
            reason = outcome.reason or ""
            # Idempotent /goal create calls can return an existing root that
            # was already decomposed. In that case the decomposer correctly
            # refuses to run again because the root is no longer in triage,
            # but the controller pass is still valid and should continue.
            should_try_advance = reason.startswith("task is not in triage")
            decompose_blocked_advance = not should_try_advance
        if not should_try_advance:
            advance_payload = {
                "advanced": False,
                "steps": [
                    {
                        "kind": "blocked",
                        "reason": "decomposition did not complete",
                    }
                ],
            }
        else:
            try:
                with kb.connect(board=getattr(args, "board", None)) as conn:
                    common_kwargs = {
                        "review_assignee": getattr(args, "review_assignee", None),
                        "test_assignee": getattr(args, "test_assignee", None),
                        "dispatch": True,
                        "dry_run": bool(getattr(args, "advance_dry_run", False)),
                        "dispatch_max": getattr(args, "dispatch_max", None),
                        "verify": not bool(getattr(args, "no_verify", False)),
                        "approve": not bool(getattr(args, "no_approve", False)),
                        "request_changes_on_failure": not bool(
                            getattr(args, "no_request_changes", False)
                        ),
                        "reviewer": (
                            getattr(args, "author", None)
                            or getattr(args, "created_by", None)
                            or _profile_author()
                        ),
                        "board": getattr(args, "board", None),
                    }
                    if getattr(args, "loop", False):
                        advance_payload = kb.advance_goal_acceptance_workflow_until_idle(
                            conn,
                            task_id,
                            max_iterations=getattr(args, "max_iterations", 8),
                            **common_kwargs,
                        )
                    else:
                        advance_payload = kb.advance_goal_acceptance_workflow(
                            conn,
                            task_id,
                            **common_kwargs,
                        )
            except ValueError as exc:
                print(f"kanban goal: advance failed: {exc}", file=sys.stderr)
                return 2

    with kb.connect(board=getattr(args, "board", None)) as conn:
        task = kb.get_task(conn, task_id)
        linked_child_ids = _goal_child_ids_from_db(conn, task_id)
    created_child_ids = (
        list(outcome.child_ids or [])
        if outcome is not None and outcome.child_ids
        else linked_child_ids
    )
    payload = {
        "task_id": task_id,
        "task": _task_to_dict(task) if task else None,
        "decompose": (
            None
            if outcome is None
            else {
                "ok": outcome.ok,
                "reason": outcome.reason,
                "fanout": outcome.fanout,
                "child_ids": outcome.child_ids or [],
                "new_title": outcome.new_title,
            }
        ),
        "advance": advance_payload,
        "child_ids": created_child_ids,
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        if outcome is not None and not outcome.ok and decompose_blocked_advance:
            return 1
        return 0

    if task is None:
        print(f"Goal task {task_id} created, but could not be reloaded.")
    else:
        print(f"Goal task: {task_id}  ({task.status}, assignee={task.assignee or '-'})")
    if outcome is not None:
        status = "decomposed" if outcome.ok else "decompose failed"
        print(f"Decompose: {status} — {outcome.reason}")
        if outcome.child_ids:
            print("Children: " + ", ".join(outcome.child_ids))
        elif not outcome.ok and getattr(args, "advance", False) and not decompose_blocked_advance:
            print("Decompose: existing non-triage root; continuing with advance")
    else:
        print("Next: run `hermes kanban decompose {}` to create child tasks.".format(task_id))
    if advance_payload is not None:
        final = advance_payload.get("final") or {}
        if advance_payload.get("iterations"):
            print(
                "Advance loop: "
                f"{advance_payload.get('iteration_count')} iteration(s), "
                f"stop={advance_payload.get('stop_reason')}"
            )
        child_summary = final.get("child_summary") or {}
        if advance_payload.get("iterations"):
            iterations = advance_payload.get("iterations") or []
            steps = (
                (iterations[-1].get("steps") or [])
                if iterations and isinstance(iterations[-1], dict)
                else []
            )
        else:
            steps = advance_payload.get("steps") or []
        print("Advance: " + (", ".join(str(step.get("kind")) for step in steps) or "no action"))
        if child_summary:
            print(
                "Child status: "
                f"{child_summary.get('done', 0)}/{child_summary.get('total', 0)} done"
            )
    if outcome is not None and not outcome.ok and decompose_blocked_advance:
        return 1
    return 0


def _cmd_swarm(args: argparse.Namespace) -> int:
    try:
        workers = [ks.parse_worker_arg(raw) for raw in (args.worker or [])]
    except ValueError as exc:
        print(f"kanban swarm: {exc}", file=sys.stderr)
        return 2
    if not workers:
        print("kanban swarm: at least one --worker is required", file=sys.stderr)
        return 2
    with kb.connect() as conn:
        created = ks.create_swarm(
            conn,
            goal=args.goal,
            workers=workers,
            verifier_assignee=args.verifier,
            synthesizer_assignee=args.synthesizer,
            tenant=args.tenant,
            created_by=args.created_by or _profile_author(),
            priority=args.priority,
            idempotency_key=getattr(args, "idempotency_key", None),
        )
    if getattr(args, "json", False):
        print(json.dumps(created.as_dict(), indent=2, ensure_ascii=False))
    else:
        print(f"Swarm root: {created.root_id}")
        print("Workers: " + ", ".join(created.worker_ids))
        print(f"Verifier: {created.verifier_id}")
        print(f"Synthesizer: {created.synthesizer_id}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    assignee = args.assignee
    if args.mine and not assignee:
        assignee = _profile_author()
    with kb.connect() as conn:
        # Cheap "mini-dispatch": recompute ready so list output reflects
        # dependencies that may have cleared since the last dispatcher tick.
        kb.recompute_ready(conn)
        tasks = kb.list_tasks(
            conn,
            assignee=assignee,
            status=args.status,
            tenant=args.tenant,
            session_id=args.session,
            include_archived=args.archived,
            order_by=getattr(args, "sort", None),
            workflow_template_id=args.workflow_template_id,
            current_step_key=args.current_step_key,
        )
    if getattr(args, "json", False):
        print(json.dumps([_task_to_dict(t) for t in tasks], indent=2, ensure_ascii=False))
        return 0
    # Passive discoverability: when the user has multiple boards, surface
    # which one they're looking at in the list header. Single-board users
    # never see this — the feature stays invisible until you opt in.
    try:
        all_boards = kb.list_boards(include_archived=False)
    except Exception:
        all_boards = []
    if len(all_boards) > 1:
        current = kb.get_current_board()
        other_count = len(all_boards) - 1
        print(
            f"Board: {current} "
            f"({other_count} other board{'s' if other_count != 1 else ''} — "
            f"`hermes kanban boards list`)\n"
        )
    if not tasks:
        print("(no matching tasks)")
        return 0
    for t in tasks:
        print(_fmt_task_line(t))
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    rsk = _run_state_kwargs(args)
    if rsk is None:
        print(
            "kanban show: pass both --state-type and --state-name, or omit both",
            file=sys.stderr,
        )
        return 2
    with kb.connect() as conn:
        task = kb.get_task(conn, args.task_id)
        if not task:
            print(f"no such task: {args.task_id}", file=sys.stderr)
            return 1
        comments = kb.list_comments(conn, args.task_id)
        events = kb.list_events(conn, args.task_id)
        parents = kb.parent_ids(conn, args.task_id)
        children = kb.child_ids(conn, args.task_id)
        runs = kb.list_runs(conn, args.task_id, **rsk)
        # Workers hand off via ``task_runs.summary`` (kanban-worker skill);
        # ``tasks.result`` is left NULL unless the caller explicitly passed
        # ``result=``. Surfacing the latest summary here keeps ``show`` from
        # looking like a no-op when the worker actually did real work.
        latest_summary = kb.latest_summary(conn, args.task_id)

    if getattr(args, "json", False):
        payload = {
            "task": _task_to_dict(task),
            "latest_summary": latest_summary,
            "parents": parents,
            "children": children,
            "comments": [
                {"author": c.author, "body": c.body, "created_at": c.created_at}
                for c in comments
            ],
            "events": [
                {
                    "kind": e.kind,
                    "payload": e.payload,
                    "created_at": e.created_at,
                    "run_id": e.run_id,
                }
                for e in events
            ],
            "runs": [
                {
                    "id": r.id,
                    "profile": r.profile,
                    "step_key": r.step_key,
                    "status": r.status,
                    "outcome": r.outcome,
                    "summary": r.summary,
                    "error": r.error,
                    "metadata": r.metadata,
                    "worker_pid": r.worker_pid,
                    "started_at": r.started_at,
                    "ended_at": r.ended_at,
                }
                for r in runs
            ],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    print(f"Task {task.id}: {task.title}")
    print(f"  status:    {task.status}")
    print(f"  assignee:  {task.assignee or '-'}")
    if task.tenant:
        print(f"  tenant:    {task.tenant}")
    print(f"  workspace: {task.workspace_kind}" +
          (f" @ {task.workspace_path}" if task.workspace_path else ""))
    if task.branch_name:
        print(f"  branch:    {task.branch_name}")
    if task.skills:
        print(f"  skills:    {', '.join(task.skills)}")
    if task.model_override:
        print(f"  model:     {task.model_override}")
    # Effective retry threshold. Show the per-task override if set,
    # otherwise the dispatcher's resolved value from config (or the
    # default if config doesn't set it either). Helps operators see
    # why a task auto-blocked earlier/later than they expected.
    if task.max_retries is not None:
        print(f"  max-retries: {task.max_retries} (task)")
    else:
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
            cfg_val = (cfg.get("kanban", {}) or {}).get("failure_limit")
        except Exception:
            cfg_val = None
        if cfg_val is not None and int(cfg_val) != kb.DEFAULT_FAILURE_LIMIT:
            print(f"  max-retries: {int(cfg_val)} (config kanban.failure_limit)")
        else:
            print(f"  max-retries: {kb.DEFAULT_FAILURE_LIMIT} (default)")
    print(f"  created:   {_fmt_ts(task.created_at)} by {task.created_by or '-'}")

    # Diagnostics section — surface active distress signals at the top
    # of show output so CLI users see them before scrolling through
    # comments / runs.
    from hermes_cli import kanban_diagnostics as kd
    diags = kd.compute_task_diagnostics(task, events, runs)
    if diags:
        sev_marker = {"warning": "⚠", "error": "!!", "critical": "!!!"}
        print(f"\n  Diagnostics ({len(diags)}):")
        for d in diags:
            print(f"    {sev_marker.get(d.severity, '?')} [{d.severity}] {d.title}")
            if d.data:
                bits = []
                for k, v in d.data.items():
                    if isinstance(v, list):
                        bits.append(f"{k}={','.join(str(x) for x in v)}")
                    else:
                        bits.append(f"{k}={v}")
                if bits:
                    print(f"       data: {' | '.join(bits)}")
            # Only show suggested actions in show output to keep it tight;
            # full list is available via `kanban diagnostics --task <id>`.
            for a in d.actions:
                if a.suggested:
                    print(f"       → {a.label}")
    if task.started_at:
        print(f"  started:   {_fmt_ts(task.started_at)}")
    if task.completed_at:
        print(f"  completed: {_fmt_ts(task.completed_at)}")
    if parents:
        print(f"  parents:   {', '.join(parents)}")
    if children:
        print(f"  children:  {', '.join(children)}")
    if task.body:
        print()
        print("Body:")
        print(task.body)
    if task.result:
        print()
        print("Result:")
        print(task.result)
    elif latest_summary:
        # Worker handoff lives on the latest run, not on tasks.result.
        # Surface it at top-level so a glance at ``hermes kanban show <id>``
        # tells you what the worker did even if tasks.result is empty.
        print()
        print("Latest summary:")
        print(latest_summary)
    if comments:
        print()
        print(f"Comments ({len(comments)}):")
        for c in comments:
            print(f"  [{_fmt_ts(c.created_at)}] {c.author}: {c.body}")
    if events:
        print()
        print(f"Events ({len(events)}):")
        for e in events[-20:]:
            pl = f" {e.payload}" if e.payload else ""
            run_tag = f" [run {e.run_id}]" if e.run_id else ""
            print(f"  [{_fmt_ts(e.created_at)}]{run_tag} {e.kind}{pl}")
    if runs:
        print()
        print(f"Runs ({len(runs)}):")
        for r in runs:
            # Clamp to 0 so NTP backward-jumps don't print negative seconds.
            elapsed = (max(0, r.ended_at - r.started_at)
                       if r.ended_at else None)
            el = f"{elapsed}s" if elapsed is not None else "active"
            outcome = r.outcome or r.status or "active"
            print(f"  #{r.id:<3} {outcome:<12} @{r.profile or '-'}  {el}  "
                  f"{_fmt_ts(r.started_at)}")
            if r.summary:
                print(f"        → {r.summary.splitlines()[0][:160]}")
            if r.error:
                print(f"        ! {r.error.splitlines()[0][:160]}")
    return 0


def _cmd_progress(args: argparse.Namespace) -> int:
    tid, resolved_from_session_goal, session_id = _resolve_optional_goal_task_id(
        getattr(args, "task_id", None)
    )
    if not tid:
        print(
            "kanban progress: task_id is required unless HERMES_SESSION_ID "
            "has a matching /goal create Kanban root",
            file=sys.stderr,
        )
        return 2
    with kb.connect() as conn:
        snapshot = kb.task_progress_snapshot(
            conn,
            tid,
            log_tail_bytes=getattr(args, "log_tail", None),
            include_children=bool(getattr(args, "children", False)),
        )
        payload = (
            _attach_progress_diagnostics(conn, _snapshot_to_dict(snapshot))
            if snapshot is not None
            else None
        )
    if snapshot is None:
        print(f"no such task: {tid}", file=sys.stderr)
        return 1
    assert payload is not None
    if resolved_from_session_goal:
        payload["resolved_from_session_goal"] = True
        payload["session_id"] = session_id
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    task = payload["task"]
    run = payload.get("run") or {}
    print(f"Task {task['id']}: {task['title']}")
    if resolved_from_session_goal:
        print(f"  resolved:  current session goal ({session_id})")
    print(f"  status:    {task['status']}")
    print(f"  assignee:  {task.get('assignee') or '-'}")
    print(f"  run:       {run.get('id') or '-'} ({run.get('outcome') or run.get('status') or 'no run'})")
    if task.get("worker_pid"):
        print(f"  worker:    pid={task['worker_pid']}")
    if payload.get("last_heartbeat_event"):
        hb = payload["last_heartbeat_event"]
        print(f"  heartbeat: {_fmt_ts(hb['created_at'])}")
    progress = payload.get("worker_progress") or {}
    items = progress.get("items") if isinstance(progress, dict) else None
    if items:
        print("\nProgress:")
        for line in _render_progress_items(items):
            print(f"  {line}")
    codex_events = payload.get("worker_codex_events") or []
    if codex_events:
        print("\nRecent Codex activity:")
        for line in _render_codex_activity(codex_events):
            print(f"  - {line}")
    if payload.get("review_required"):
        print("\nReview: required")
    verification = payload.get("verification") or {}
    commands = verification.get("commands") if isinstance(verification, dict) else None
    if commands:
        print("\nVerification:")
        for cmd in commands:
            print(f"  - {cmd}")
    worker_lane = payload.get("worker_lane") or {}
    if worker_lane:
        lane = worker_lane.get("name") or worker_lane.get("worker_lane")
        exit_code = worker_lane.get("exit_code")
        timed_out = worker_lane.get("timed_out")
        print(f"\nWorker lane: {lane or '-'} exit_code={exit_code} timed_out={timed_out}")
    diagnostics = payload.get("diagnostics") or []
    if diagnostics:
        print("\nDiagnostics:")
        for diag in diagnostics[:6]:
            if not isinstance(diag, dict):
                continue
            print(
                f"  - [{diag.get('severity', '-')}] "
                f"{diag.get('kind', '-')}: {diag.get('title', '')}"
            )
    last_event = payload.get("last_event")
    if last_event:
        print(f"Last event: {last_event['kind']} at {_fmt_ts(last_event['created_at'])}")
    if payload.get("worker_log_tail"):
        print("\nWorker log tail:")
        sys.stdout.write(payload["worker_log_tail"])
        if not payload["worker_log_tail"].endswith("\n"):
            sys.stdout.write("\n")
    child_summary = payload.get("child_summary") or {}
    children = payload.get("children") or []
    if child_summary:
        print("\nChildren:")
        print(
            f"  total={child_summary.get('total', 0)} "
            f"done={child_summary.get('done', 0)} "
            f"running={child_summary.get('running', 0)} "
            f"review_required={child_summary.get('review_required', 0)}"
        )
        actions = child_summary.get("recommended_actions") or {}
        if actions:
            rendered_actions = ", ".join(
                f"{name}={count}" for name, count in sorted(actions.items())
            )
            print(f"  next: {rendered_actions}")
        if child_summary.get("auto_retry_exhausted"):
            print(f"  auto_retry_exhausted={child_summary.get('auto_retry_exhausted')}")
        for child in children:
            ctask = child.get("task") or {}
            lane_meta = child.get("worker_lane") or {}
            lane = lane_meta.get("name") or ctask.get("assignee") or "-"
            run = child.get("run") or {}
            summary = (run.get("summary") or "").splitlines()[0]
            acceptance = child.get("acceptance") or {}
            action = acceptance.get("recommended_action") if isinstance(acceptance, dict) else None
            print(
                f"  - {ctask.get('id', '-')} "
                f"{ctask.get('status', '-')} @{lane}: "
                f"{ctask.get('title', '')[:80]}"
            )
            if action:
                print(f"      next: {action}")
            progress = child.get("worker_progress") or {}
            items = progress.get("items") if isinstance(progress, dict) else None
            if items:
                rendered = "; ".join(_render_progress_items(items[:3]))
                print(f"      progress: {rendered[:160]}")
            if child.get("review_required"):
                print("      review: required")
            diagnostics = child.get("diagnostics") or []
            if diagnostics:
                kinds = ", ".join(
                    str(diag.get("kind") or "-")
                    for diag in diagnostics[:4]
                    if isinstance(diag, dict)
                )
                if kinds:
                    print(f"      diagnostics: {kinds}")
            auto_request_changes = (
                acceptance.get("auto_request_changes")
                if isinstance(acceptance, dict)
                else None
            )
            if (
                isinstance(auto_request_changes, dict)
                and auto_request_changes.get("exhausted")
            ):
                used = auto_request_changes.get("used")
                limit = auto_request_changes.get("limit")
                print(f"      auto-retry: exhausted ({used}/{limit})")
            if summary:
                print(f"      summary: {summary[:160]}")
    return 0


def _cmd_acceptance(args: argparse.Namespace) -> int:
    with kb.connect() as conn:
        payload = kb.task_acceptance_snapshot(
            conn,
            args.task_id,
            log_tail_bytes=getattr(args, "log_tail", None),
            followup_log_tail_bytes=getattr(args, "followup_log_tail", None),
        )
    if payload is None:
        print(f"no such task: {args.task_id}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    task = (payload.get("implementation") or {}).get("task") or {}
    gate = payload.get("review_followup_gate") or {}
    print(f"Acceptance {task.get('id', args.task_id)}: {task.get('title', '')}")
    print(f"  status:             {task.get('status', '-')}")
    print(f"  recommended_action: {payload.get('recommended_action')}")
    print(f"  approval_allowed:   {payload.get('approval_allowed')}")
    if gate:
        print(
            "  followup_gate:      "
            f"ready={gate.get('ready')} "
            f"satisfied={gate.get('satisfied', 0)}/{gate.get('required', 0)}"
        )
        for reason in gate.get("blocking_reasons") or []:
            print(f"    - {reason}")
    followups = payload.get("followups") or []
    if followups:
        print("  followups:")
        for item in followups:
            snap = item.get("snapshot") or {}
            ftask = snap.get("task") or {}
            gate_item = item.get("gate_item") or {}
            print(
                f"    - {item.get('purpose')}: {item.get('task_id')} "
                f"{ftask.get('status', '-')} state={gate_item.get('state', '-')}"
            )
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    try:
        with kb.connect() as conn:
            payload = kb.run_acceptance_checks(
                conn,
                args.task_id,
                check_names=list(getattr(args, "checks", None) or []) or None,
                source_run_id=getattr(args, "source_run_id", None),
            )
    except ValueError as exc:
        print(f"kanban verify: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    gate = payload.get("acceptance_check_gate") or {}
    print(f"Acceptance checks for {args.task_id}:")
    for item in payload.get("checks") or []:
        state = "passed" if item.get("passed") else "failed"
        extra = ""
        if item.get("timed_out"):
            extra = " timed_out"
        elif item.get("error"):
            extra = f" {item.get('error')}"
        print(
            f"  - {item.get('name')}: {state} "
            f"exit={item.get('exit_code')} duration_ms={item.get('duration_ms')}{extra}"
        )
    if gate:
        print(
            "  gate: "
            f"ready={gate.get('ready')} "
            f"satisfied={gate.get('satisfied', 0)}/{gate.get('required', 0)}"
        )
        for reason in gate.get("blocking_reasons") or []:
            print(f"    - {reason}")
    return 0


def _cmd_advance_acceptance(args: argparse.Namespace) -> int:
    reviewer = getattr(args, "reviewer", None) or _profile_author()
    try:
        with kb.connect() as conn:
            common_kwargs = {
                "review_assignee": getattr(args, "review_assignee", None),
                "test_assignee": getattr(args, "test_assignee", None),
                "dispatch": not bool(getattr(args, "no_dispatch", False)),
                "dry_run": bool(getattr(args, "dry_run", False)),
                "dispatch_max": getattr(args, "dispatch_max", None),
                "verify": not bool(getattr(args, "no_verify", False)),
                "approve": not bool(getattr(args, "no_approve", False)),
                "request_changes_on_failure": not bool(
                    getattr(args, "no_request_changes", False)
                ),
                "reviewer": reviewer,
                "summary": getattr(args, "summary", None),
                "result": getattr(args, "result", None),
            }
            if getattr(args, "loop", False):
                payload = kb.advance_acceptance_workflow_until_idle(
                    conn,
                    args.task_id,
                    max_iterations=getattr(args, "max_iterations", 8),
                    **common_kwargs,
                )
            else:
                payload = kb.advance_acceptance_workflow(
                    conn,
                    args.task_id,
                    **common_kwargs,
                )
    except ValueError as exc:
        print(f"kanban advance-acceptance: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    final = payload.get("final") or {}
    print(f"Advanced acceptance for {args.task_id}:")
    if payload.get("iterations"):
        print(
            f"  loop: {payload.get('iteration_count')} iteration(s), "
            f"stop={payload.get('stop_reason')}"
        )
    if not payload.get("steps"):
        print("  - no action")
    for step in payload.get("steps") or []:
        print(f"  - {step.get('kind')}")
    print(f"  recommended_action: {final.get('recommended_action')}")
    print(f"  approval_allowed:   {final.get('approval_allowed')}")
    impl_task = ((final.get("implementation") or {}).get("task") or {})
    print(f"  status:             {impl_task.get('status', '-')}")
    return 0


def _cmd_advance_goal(args: argparse.Namespace) -> int:
    reviewer = getattr(args, "reviewer", None) or _profile_author()
    tid, resolved_from_session_goal, session_id = _resolve_optional_goal_task_id(
        getattr(args, "task_id", None)
    )
    if not tid:
        print(
            "kanban advance-goal: task_id is required unless HERMES_SESSION_ID "
            "has a matching /goal create Kanban root",
            file=sys.stderr,
        )
        return 2
    try:
        with kb.connect() as conn:
            common_kwargs = {
                "review_assignee": getattr(args, "review_assignee", None),
                "test_assignee": getattr(args, "test_assignee", None),
                "dispatch": not bool(getattr(args, "no_dispatch", False)),
                "dry_run": bool(getattr(args, "dry_run", False)),
                "dispatch_max": getattr(args, "dispatch_max", None),
                "verify": not bool(getattr(args, "no_verify", False)),
                "approve": not bool(getattr(args, "no_approve", False)),
                "request_changes_on_failure": not bool(
                    getattr(args, "no_request_changes", False)
                ),
                "reviewer": reviewer,
                "summary": getattr(args, "summary", None),
                "result": getattr(args, "result", None),
            }
            if getattr(args, "loop", False):
                payload = kb.advance_goal_acceptance_workflow_until_idle(
                    conn,
                    tid,
                    max_iterations=getattr(args, "max_iterations", 8),
                    **common_kwargs,
                )
            else:
                payload = kb.advance_goal_acceptance_workflow(
                    conn,
                    tid,
                    **common_kwargs,
                )
    except ValueError as exc:
        print(f"kanban advance-goal: {exc}", file=sys.stderr)
        return 2
    if resolved_from_session_goal:
        payload["resolved_from_session_goal"] = True
        payload["session_id"] = session_id
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    final = payload.get("final") or {}
    root_task = final.get("task") or {}
    child_summary = final.get("child_summary") or {}
    print(f"Advanced goal for {tid}:")
    if resolved_from_session_goal:
        print(f"  resolved:           current session goal ({session_id})")
    if payload.get("iterations"):
        print(
            f"  loop: {payload.get('iteration_count')} iteration(s), "
            f"stop={payload.get('stop_reason')}"
        )
    if not payload.get("steps"):
        print("  - no action")
    for step in payload.get("steps") or []:
        print(f"  - {step.get('kind')}")
    print(f"  status:             {root_task.get('status', '-')}")
    print(f"  children:           {child_summary.get('done', 0)}/{child_summary.get('total', 0)} done")
    if payload.get("incomplete_children"):
        print(f"  incomplete:         {len(payload.get('incomplete_children') or [])}")
    return 0


def _cmd_advance_controller(args: argparse.Namespace) -> int:
    reviewer = getattr(args, "reviewer", None) or _profile_author()
    try:
        with kb.connect() as conn:
            payload = kb.advance_controller_once(
                conn,
                review_assignee=getattr(args, "review_assignee", None),
                test_assignee=getattr(args, "test_assignee", None),
                dispatch=not bool(getattr(args, "no_dispatch", False)),
                dry_run=bool(getattr(args, "dry_run", False)),
                dispatch_max=getattr(args, "dispatch_max", None),
                verify=not bool(getattr(args, "no_verify", False)),
                approve=not bool(getattr(args, "no_approve", False)),
                request_changes_on_failure=not bool(
                    getattr(args, "no_request_changes", False)
                ),
                reviewer=reviewer,
                summary=getattr(args, "summary", None),
                result=getattr(args, "result", None),
                max_iterations=getattr(args, "max_iterations", 8),
                max_items=getattr(args, "max_items", 8),
                include_goals=not bool(getattr(args, "no_goals", False)),
                include_review_required=not bool(
                    getattr(args, "no_review_required", False)
                ),
            )
    except ValueError as exc:
        print(f"kanban advance-controller: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    print("Advanced Kanban controller:")
    print(
        f"  items:        {payload.get('item_count', 0)}/"
        f"{payload.get('max_items', 0)}"
    )
    print(f"  stop:         {payload.get('stop_reason')}")
    print(f"  dispatch:     {payload.get('dispatch_used', 0)}")
    scanned = payload.get("scanned") or {}
    print(
        "  scanned:      "
        f"goals={scanned.get('goals', 0)} "
        f"review_required={scanned.get('review_required', 0)}"
    )
    if not payload.get("items"):
        print("  - no action")
    for item in payload.get("items") or []:
        final = ((item.get("payload") or {}).get("final") or {})
        task = final.get("task") or {}
        print(
            f"  - {item.get('kind')} {item.get('task_id')}: "
            f"stop={item.get('stop_reason')} "
            f"advanced={item.get('advanced')} "
            f"status={task.get('status', '-')}"
        )
    return 0


def _cmd_reviews(args: argparse.Namespace) -> int:
    with kb.connect() as conn:
        snapshots = kb.review_required_snapshots(
            conn,
            assignee=getattr(args, "assignee", None),
            tenant=getattr(args, "tenant", None),
            worker_lane=getattr(args, "lane", None),
            limit=getattr(args, "limit", 100),
            log_tail_bytes=getattr(args, "log_tail", None),
            include_followups=bool(getattr(args, "include_followups", False)),
        )
    payload = [_snapshot_to_dict(snapshot) for snapshot in snapshots]
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    if not payload:
        print("(no review-required worker lane tasks)")
        return 0
    print(f"{'TASK':10s}  {'LANE':16s}  {'ASSIGNEE':16s}  {'RUN':>5s}  SUMMARY")
    for item in payload:
        task = item.get("task") or {}
        run = item.get("run") or {}
        lane_meta = item.get("worker_lane") or {}
        lane = lane_meta.get("name") or lane_meta.get("worker_lane") or "-"
        summary = (run.get("summary") or "").splitlines()[0] if run else ""
        if not summary:
            review = (item.get("evidence") or {}).get("review") or {}
            summary = review.get("reason") if isinstance(review, dict) else ""
        print(
            f"{task.get('id', '-'):10s}  {lane[:16]:16s}  "
            f"{(task.get('assignee') or '-')[:16]:16s}  "
            f"{str(run.get('id') or '-'):>5s}  {summary[:120]}"
        )
    print("\nUse `hermes kanban progress <task_id> --json` for full bounded evidence.")
    return 0


def _cmd_review(args: argparse.Namespace) -> int:
    decision = str(getattr(args, "decision", "")).replace("-", "_")
    reviewer = getattr(args, "reviewer", None) or _profile_author()
    try:
        with kb.connect() as conn:
            snapshot = kb.review_worker_evidence(
                conn,
                args.task_id,
                decision=decision,
                reviewer=reviewer,
                comment=getattr(args, "comment", None),
                result=getattr(args, "result", None),
                summary=getattr(args, "summary", None),
            )
    except ValueError as exc:
        print(f"kanban: {exc}", file=sys.stderr)
        return 2
    payload = _snapshot_to_dict(snapshot)
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    if decision == "approve":
        print(f"Approved {args.task_id}; task is now {snapshot.task.status}")
    else:
        print(f"Requested changes for {args.task_id}; task is now {snapshot.task.status}")
    return 0


def _cmd_plan_review(args: argparse.Namespace) -> int:
    review_only = bool(getattr(args, "review_only", False))
    test_only = bool(getattr(args, "test_only", False))
    if review_only and test_only:
        print("kanban plan-review: pass at most one of --review-only/--test-only", file=sys.stderr)
        return 2
    try:
        with kb.connect() as conn:
            plan = kb.plan_review_followups(
                conn,
                args.task_id,
                review_assignee=getattr(args, "review_assignee", None),
                test_assignee=getattr(args, "test_assignee", None),
                include_review=not test_only,
                include_test=not review_only,
                created_by=getattr(args, "created_by", None) or "hermes-review-planner",
            )
            dispatch_result = None
            if getattr(args, "dispatch", False):
                followup_ids = [
                    tid
                    for tid in (
                        plan.review_task_id,
                        *plan.review_shard_task_ids,
                        plan.test_task_id,
                    )
                    if tid
                ]
                dispatch_result = kb.dispatch_once(
                    conn,
                    dry_run=bool(getattr(args, "dry_run", False)),
                    max_spawn=getattr(args, "dispatch_max", None),
                    only_task_ids=followup_ids,
                )
    except ValueError as exc:
        print(f"kanban plan-review: {exc}", file=sys.stderr)
        return 2
    payload = plan.to_dict()
    if dispatch_result is not None:
        payload["dispatch"] = dispatch_result.to_dict()
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    print(
        f"Planned review follow-ups for {plan.source_task_id} "
        f"(run {plan.source_run_id})"
    )
    if plan.review_task_id:
        state = "existing" if plan.review_task_id in plan.existing else "created"
        print(f"  review: {plan.review_task_id} @{plan.review_assignee} ({state})")
    if plan.test_task_id:
        state = "existing" if plan.test_task_id in plan.existing else "created"
        print(f"  test:   {plan.test_task_id} @{plan.test_assignee} ({state})")
    if dispatch_result is not None:
        print(f"  dispatched: {len(dispatch_result.spawned)}")
        for tid, who, ws in dispatch_result.spawned:
            tag = " (dry)" if getattr(args, "dry_run", False) else ""
            print(f"    - {tid} -> {who} @ {ws or '-'}{tag}")
    return 0


def _cmd_assign(args: argparse.Namespace) -> int:
    profile = None if args.profile.lower() in {"none", "-", "null"} else args.profile
    with kb.connect() as conn:
        ok = kb.assign_task(conn, args.task_id, profile)
    if not ok:
        print(f"no such task: {args.task_id}", file=sys.stderr)
        return 1
    print(f"Assigned {args.task_id} to {profile or '(unassigned)'}")
    return 0


def _cmd_reclaim(args: argparse.Namespace) -> int:
    with kb.connect() as conn:
        ok = kb.reclaim_task(
            conn, args.task_id,
            reason=getattr(args, "reason", None),
        )
    if not ok:
        print(
            f"cannot reclaim {args.task_id} (not running or unknown id)",
            file=sys.stderr,
        )
        return 1
    print(f"Reclaimed {args.task_id}")
    return 0


def _cmd_reassign(args: argparse.Namespace) -> int:
    profile = None if args.profile.lower() in {"none", "-", "null"} else args.profile
    with kb.connect() as conn:
        ok = kb.reassign_task(
            conn, args.task_id, profile,
            reclaim_first=bool(getattr(args, "reclaim", False)),
            reason=getattr(args, "reason", None),
        )
    if not ok:
        print(
            f"cannot reassign {args.task_id} "
            f"(unknown id, or still running — pass --reclaim to release first)",
            file=sys.stderr,
        )
        return 1
    print(
        f"Reassigned {args.task_id} to "
        f"{profile or '(unassigned)'}"
        + (" (claim reclaimed)" if getattr(args, "reclaim", False) else "")
    )
    return 0


def _cmd_diagnostics(args: argparse.Namespace) -> int:
    """List active diagnostics on the board. Wraps the same rule engine
    the dashboard uses, so CLI output matches what the UI shows.
    """
    from hermes_cli import kanban_diagnostics as kd
    from hermes_cli.config import load_config

    diag_config = kd.config_from_runtime_config(load_config())

    with kb.connect() as conn:
        # Either one-task mode or fleet mode.
        if getattr(args, "task", None):
            task = kb.get_task(conn, args.task)
            if task is None:
                print(f"no such task: {args.task}", file=sys.stderr)
                return 1
            diags_by_task = {
                args.task: kd.compute_task_diagnostics(
                    task,
                    kb.list_events(conn, args.task),
                    kb.list_runs(conn, args.task),
                    config=diag_config,
                )
            }
        else:
            # Fleet mode: pull all non-archived tasks + their events/runs.
            rows = list(conn.execute(
                "SELECT * FROM tasks WHERE status != 'archived'"
            ).fetchall())
            ids = [r["id"] for r in rows]
            if not ids:
                diags_by_task = {}
            else:
                placeholders = ",".join(["?"] * len(ids))
                ev_by = {i: [] for i in ids}
                for row in conn.execute(
                    f"SELECT * FROM task_events WHERE task_id IN ({placeholders}) ORDER BY id",
                    tuple(ids),
                ):
                    ev_by.setdefault(row["task_id"], []).append(row)
                run_by = {i: [] for i in ids}
                for row in conn.execute(
                    f"SELECT * FROM task_runs WHERE task_id IN ({placeholders}) ORDER BY id",
                    tuple(ids),
                ):
                    run_by.setdefault(row["task_id"], []).append(row)
                diags_by_task = {}
                for r in rows:
                    tid = r["id"]
                    dl = kd.compute_task_diagnostics(
                        r,
                        ev_by.get(tid, []),
                        run_by.get(tid, []),
                        config=diag_config,
                    )
                    if dl:
                        diags_by_task[tid] = dl

        # Severity filter.
        sev = getattr(args, "severity", None)
        if sev:
            for tid in list(diags_by_task.keys()):
                kept = [d for d in diags_by_task[tid] if kd.SEVERITY_ORDER.index(d.severity) >= kd.SEVERITY_ORDER.index(sev)]
                if kept:
                    diags_by_task[tid] = kept
                else:
                    del diags_by_task[tid]

        # Map task_id → title/status/assignee for the table output.
        meta: dict[str, dict] = {}
        if diags_by_task:
            placeholders = ",".join(["?"] * len(diags_by_task))
            for r in conn.execute(
                f"SELECT id, title, status, assignee FROM tasks WHERE id IN ({placeholders})",
                tuple(diags_by_task.keys()),
            ):
                meta[r["id"]] = {
                    "title": r["title"], "status": r["status"],
                    "assignee": r["assignee"],
                }

    if getattr(args, "json", False):
        out_json = [
            {
                "task_id": tid,
                **meta.get(tid, {}),
                "diagnostics": [d.to_dict() for d in dl],
            }
            for tid, dl in diags_by_task.items()
        ]
        print(json.dumps(out_json, indent=2, ensure_ascii=False))
        return 0

    if not diags_by_task:
        print("No active diagnostics on this board.")
        return 0

    # Human-readable summary: grouped by task, severity-marked, with
    # suggested actions inline.
    sev_marker = {"warning": "⚠", "error": "!!", "critical": "!!!"}
    total = sum(len(dl) for dl in diags_by_task.values())
    print(
        f"{total} active diagnostic(s) across "
        f"{len(diags_by_task)} task(s):\n"
    )
    for tid, dl in diags_by_task.items():
        m = meta.get(tid, {})
        title = m.get("title") or "(untitled)"
        status = m.get("status") or "?"
        assignee = m.get("assignee") or "(unassigned)"
        print(f"  {tid}  {status:8s}  @{assignee:18s}  {title}")
        for d in dl:
            print(f"    {sev_marker.get(d.severity, '?')} [{d.severity}] {d.kind}: {d.title}")
            if d.data:
                # Compact key:value pairs on one line.
                bits = []
                for k, v in d.data.items():
                    if isinstance(v, list):
                        bits.append(f"{k}={','.join(str(x) for x in v)}")
                    else:
                        bits.append(f"{k}={v}")
                if bits:
                    print(f"       data: {' | '.join(bits)}")
            # Suggested actions first.
            for a in d.actions:
                if a.suggested:
                    print(f"       → {a.label}")
        print()
    return 0


def _cmd_link(args: argparse.Namespace) -> int:
    with kb.connect() as conn:
        kb.link_tasks(conn, args.parent_id, args.child_id)
    print(f"Linked {args.parent_id} -> {args.child_id}")
    return 0


def _cmd_unlink(args: argparse.Namespace) -> int:
    with kb.connect() as conn:
        ok = kb.unlink_tasks(conn, args.parent_id, args.child_id)
    if not ok:
        print(f"No such link: {args.parent_id} -> {args.child_id}", file=sys.stderr)
        return 1
    print(f"Unlinked {args.parent_id} -> {args.child_id}")
    return 0


def _cmd_claim(args: argparse.Namespace) -> int:
    with kb.connect() as conn:
        task = kb.claim_task(conn, args.task_id, ttl_seconds=args.ttl)
        if task is None:
            # Report why
            existing = kb.get_task(conn, args.task_id)
            if existing is None:
                print(f"no such task: {args.task_id}", file=sys.stderr)
                return 1
            print(
                f"cannot claim {args.task_id}: status={existing.status} "
                f"lock={existing.claim_lock or '(none)'}",
                file=sys.stderr,
            )
            return 1
        workspace = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, task.id, str(workspace))
    print(f"Claimed {task.id}")
    print(f"Workspace: {workspace}")
    return 0


def _cmd_comment(args: argparse.Namespace) -> int:
    body = " ".join(args.text).strip()
    if args.max_len is not None:
        if args.max_len < 1:
            print("kanban: --max-len must be positive", file=sys.stderr)
            return 2
        if len(body) > args.max_len:
            suffix = f"\n\n[trimmed to {args.max_len} chars by --max-len]"
            body = body[: max(0, args.max_len - len(suffix))].rstrip() + suffix
    author = args.author or _profile_author()
    with kb.connect() as conn:
        kb.add_comment(conn, args.task_id, author, body)
    print(f"Comment added to {args.task_id}")
    return 0


def _worker_run_id_for(task_id: str) -> Optional[int]:
    if os.environ.get("HERMES_KANBAN_TASK") != task_id:
        return None
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _cmd_complete(args: argparse.Namespace) -> int:
    """Mark one or more tasks done. Supports a single id or a list."""
    ids = list(args.task_ids or [])
    if not ids:
        print("at least one task_id is required", file=sys.stderr)
        return 1
    summary = getattr(args, "summary", None)
    raw_meta = getattr(args, "metadata", None)
    # Guard: structured handoff fields are per-run, so they'd be
    # copy-pasted identically across N runs — almost always a footgun.
    # Refuse instead of silently doing the wrong thing.
    if len(ids) > 1 and (summary or raw_meta):
        print(
            "kanban: --summary / --metadata are per-task and can't be used "
            "with multiple ids (would apply the same handoff to every task). "
            "Complete tasks one at a time, or drop the flags for the bulk close.",
            file=sys.stderr,
        )
        return 2
    metadata = None
    if raw_meta:
        try:
            metadata = json.loads(raw_meta)
            if not isinstance(metadata, dict):
                raise ValueError("must be a JSON object")
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"kanban: --metadata: {exc}", file=sys.stderr)
            return 2
    failed: list[str] = []
    with kb.connect() as conn:
        for tid in ids:
            if not kb.complete_task(
                conn, tid,
                result=args.result,
                summary=summary,
                metadata=metadata,
                expected_run_id=_worker_run_id_for(tid),
            ):
                failed.append(tid)
                print(f"cannot complete {tid} (unknown id or terminal state)", file=sys.stderr)
            else:
                print(f"Completed {tid}")
    return 0 if not failed else 1


def _cmd_edit(args: argparse.Namespace) -> int:
    raw_meta = getattr(args, "metadata", None)
    metadata = None
    if raw_meta:
        try:
            metadata = json.loads(raw_meta)
            if not isinstance(metadata, dict):
                raise ValueError("must be a JSON object")
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"kanban: --metadata: {exc}", file=sys.stderr)
            return 2
    with kb.connect() as conn:
        if not kb.edit_completed_task_result(
            conn,
            args.task_id,
            result=args.result,
            summary=getattr(args, "summary", None),
            metadata=metadata,
        ):
            print(
                f"cannot edit {args.task_id} (unknown id or task is not done)",
                file=sys.stderr,
            )
            return 1
    print(f"Edited {args.task_id}")
    return 0


def _cmd_block(args: argparse.Namespace) -> int:
    reason = " ".join(args.reason).strip() if args.reason else None
    author = _profile_author()
    ids = [args.task_id] + list(getattr(args, "ids", None) or [])
    failed: list[str] = []
    with kb.connect() as conn:
        for tid in ids:
            if reason:
                kb.add_comment(conn, tid, author, f"BLOCKED: {reason}")
            if not kb.block_task(
                conn,
                tid,
                reason=reason,
                expected_run_id=_worker_run_id_for(tid),
            ):
                failed.append(tid)
                print(f"cannot block {tid}", file=sys.stderr)
            else:
                print(f"Blocked {tid}" + (f": {reason}" if reason else ""))
    return 0 if not failed else 1


def _cmd_schedule(args: argparse.Namespace) -> int:
    reason = " ".join(args.reason).strip() if args.reason else None
    author = _profile_author()
    ids = [args.task_id] + list(getattr(args, "ids", None) or [])
    failed: list[str] = []
    with kb.connect() as conn:
        for tid in ids:
            if reason:
                kb.add_comment(conn, tid, author, f"SCHEDULED: {reason}")
            if not kb.schedule_task(
                conn,
                tid,
                reason=reason,
                expected_run_id=_worker_run_id_for(tid),
            ):
                failed.append(tid)
                print(f"cannot schedule {tid}", file=sys.stderr)
            else:
                print(f"Scheduled {tid}" + (f": {reason}" if reason else ""))
    return 0 if not failed else 1


def _cmd_unblock(args: argparse.Namespace) -> int:
    ids = list(args.task_ids or [])
    if not ids:
        print("at least one task_id is required", file=sys.stderr)
        return 1
    failed: list[str] = []
    with kb.connect() as conn:
        for tid in ids:
            if not kb.unblock_task(conn, tid):
                failed.append(tid)
                print(f"cannot unblock {tid} (not blocked/scheduled?)", file=sys.stderr)
            else:
                print(f"Unblocked {tid}")
    return 0 if not failed else 1


def _cmd_archive(args: argparse.Namespace) -> int:
    ids = list(args.task_ids or [])
    purge_ids = list(getattr(args, "purge_ids", None) or [])
    if ids and purge_ids:
        print("choose either task_ids to archive or --rm archived task_ids", file=sys.stderr)
        return 1
    if not ids and not purge_ids:
        print("at least one task_id is required", file=sys.stderr)
        return 1
    failed: list[str] = []
    with kb.connect() as conn:
        if purge_ids:
            for tid in purge_ids:
                if not kb.delete_archived_task(conn, tid):
                    failed.append(tid)
                    print(f"cannot delete {tid} (must already be archived)", file=sys.stderr)
                else:
                    print(f"Deleted {tid}")
            return 0 if not failed else 1
        for tid in ids:
            if not kb.archive_task(conn, tid):
                failed.append(tid)
                print(f"cannot archive {tid}", file=sys.stderr)
            else:
                print(f"Archived {tid}")
    return 0 if not failed else 1


def _cmd_tail(args: argparse.Namespace) -> int:
    last_id = 0
    print(f"Tailing events for {args.task_id}. Ctrl-C to stop.")
    try:
        while True:
            with kb.connect() as conn:
                events = kb.list_events(conn, args.task_id)
            for e in events:
                if e.id > last_id:
                    pl = f" {e.payload}" if e.payload else ""
                    print(f"[{_fmt_ts(e.created_at)}] {e.kind}{pl}", flush=True)
                    last_id = e.id
            time.sleep(max(0.1, args.interval))
    except KeyboardInterrupt:
        print("\n(stopped)")
        return 0


def _cmd_dispatch(args: argparse.Namespace) -> int:
    with kb.connect() as conn:
        res = kb.dispatch_once(
            conn,
            dry_run=args.dry_run,
            max_spawn=args.max,
            failure_limit=getattr(args, "failure_limit", kb.DEFAULT_SPAWN_FAILURE_LIMIT),
        )
    if getattr(args, "json", False):
        print(json.dumps(res.to_dict(), indent=2))
        return 0
    print(f"Reclaimed:    {res.reclaimed}")
    print(f"Crashed:      {len(res.crashed)}")
    if res.crashed:
        print(f"  {', '.join(res.crashed)}")
    print(f"Timed out:    {len(res.timed_out)}")
    if res.timed_out:
        print(f"  {', '.join(res.timed_out)}")
    print(f"Stale:        {len(res.stale)}")
    if res.stale:
        print(f"  {', '.join(res.stale)}")
    print(f"Auto-blocked: {len(res.auto_blocked)}")
    if res.auto_blocked:
        print(f"  {', '.join(res.auto_blocked)}")
    print(f"Promoted:     {res.promoted}")
    print(f"Spawned:      {len(res.spawned)}")
    for tid, who, ws in res.spawned:
        tag = " (dry)" if args.dry_run else ""
        print(f"  - {tid}  ->  {who}  @ {ws or '-'}{tag}")
    if res.skipped_unassigned:
        print(f"Skipped (unassigned): {', '.join(res.skipped_unassigned)}")
    if res.skipped_nonspawnable:
        print(
            f"Skipped (non-spawnable assignee — terminal lane, OK): "
            f"{', '.join(res.skipped_nonspawnable)}"
        )
    if res.skipped_concurrency:
        print(f"Skipped (lane concurrency): {', '.join(res.skipped_concurrency)}")
    return 0


def _cmd_daemon(args: argparse.Namespace) -> int:
    """Deprecated — the dispatcher now runs inside the gateway.

    Left in as a stub so users with the old command in scripts/systemd
    units get a clear migration message instead of a cryptic
    "no such command" error. A ``--force`` escape hatch keeps the old
    standalone daemon alive for the rare edge case where someone truly
    cannot run the gateway (e.g. running on a host that forbids
    long-lived background services), but the default path exits 2
    with guidance so nobody accidentally keeps running two dispatchers
    against the same kanban.db.
    """
    # --force lets power users keep the standalone loop for one more
    # release cycle. Undocumented in `--help` so nobody discovers it
    # casually — intentional.
    if not getattr(args, "force", False):
        print(
            "hermes kanban daemon: DEPRECATED — the dispatcher now runs\n"
            "inside the gateway. To use kanban:\n"
            "\n"
            "    hermes gateway start       # starts the gateway + embedded dispatcher\n"
            "\n"
            "Ready tasks will be picked up on the next dispatcher tick\n"
            "(default: every 60 seconds). Configure via config.yaml:\n"
            "\n"
            "    kanban:\n"
            "      dispatch_in_gateway: true      # default\n"
            "      dispatch_interval_seconds: 60\n"
            "      failure_limit: 2              # consecutive non-success attempts before auto-block\n"
            "\n"
            "Running both the gateway AND this standalone daemon will\n"
            "race for claims. If you truly need the old standalone\n"
            "daemon (no gateway available), rerun with --force.",
            file=sys.stderr,
        )
        return 2

    # Legacy path — same logic as before, kept behind --force.
    # Make sure the DB exists before printing "started" so the user sees the
    # correct DB path and any init error surfaces immediately.
    kb.init_db()

    pidfile = getattr(args, "pidfile", None)
    if pidfile:
        try:
            Path(pidfile).parent.mkdir(parents=True, exist_ok=True)
            Path(pidfile).write_text(str(os.getpid()), encoding="utf-8")
        except OSError as exc:
            print(f"warning: could not write pidfile {pidfile}: {exc}", file=sys.stderr)

    verbose = bool(getattr(args, "verbose", False))
    print(
        f"Kanban dispatcher running STANDALONE via --force "
        f"(interval={args.interval}s, pid={os.getpid()}). "
        f"Ctrl-C to stop. NOTE: if a gateway is also running with "
        f"dispatch_in_gateway=true (default), you have two dispatchers "
        f"racing for claims.",
        file=sys.stderr,
    )

    # Health telemetry: warn when every tick finds ready work but fails to
    # spawn any worker. Catches broken profiles, PATH drift, missing venv,
    # credential loss — cases where the per-task circuit breaker auto-blocks
    # each task quietly but the operator has no signal that the dispatcher
    # itself is dysfunctional.
    HEALTH_WINDOW = 6  # ticks (default 30s at interval=5)
    health_state = {"bad_ticks": 0, "last_warn_at": 0}

    def _on_tick(res):
        ready_pending = bool(res.skipped_unassigned) or _ready_queue_nonempty()
        spawned_any = bool(res.spawned)
        if ready_pending and not spawned_any:
            health_state["bad_ticks"] += 1
        else:
            health_state["bad_ticks"] = 0
        # Emit a warning once per HEALTH_WINDOW bad ticks (not every tick)
        # so log volume stays bounded while the problem persists.
        if health_state["bad_ticks"] >= HEALTH_WINDOW:
            now = int(time.time())
            # Rate-limit repeats: at most one warning per 5 minutes.
            if now - health_state["last_warn_at"] >= 300:
                print(
                    f"[{_fmt_ts(now)}] WARN dispatcher stuck: "
                    f"ready queue non-empty for {health_state['bad_ticks']} "
                    f"consecutive ticks but 0 workers spawned successfully. "
                    f"Check profile health (venv, PATH, credentials) and "
                    f"`hermes kanban list --status ready` / "
                    f"`hermes kanban list --status blocked` for recent "
                    f"spawn_failed tasks.",
                    file=sys.stderr, flush=True,
                )
                health_state["last_warn_at"] = now
        if not verbose:
            return
        did_work = (
            res.reclaimed or res.crashed or res.timed_out or res.promoted
            or res.spawned or res.auto_blocked or res.stale
        )
        if did_work:
            print(
                f"[{_fmt_ts(int(time.time()))}] "
                f"reclaimed={res.reclaimed} crashed={len(res.crashed)} "
                f"timed_out={len(res.timed_out)} stale={len(res.stale)} "
                f"promoted={res.promoted} spawned={len(res.spawned)} "
                f"auto_blocked={len(res.auto_blocked)}",
                flush=True,
            )

    def _ready_queue_nonempty() -> bool:
        """Cheap probe — is there at least one ready+assigned+unclaimed
        task whose assignee maps to a real Hermes profile (i.e. one the
        dispatcher would actually try to spawn for)?

        Filters out tasks assigned to control-plane lanes
        (e.g. ``orion-cc``, ``orion-research``) that are pulled by
        terminals via ``claim_task`` directly — those are correctly idle
        from the dispatcher's perspective, not stuck.
        """
        try:
            with kb.connect() as conn:
                return kb.has_spawnable_ready(conn)
        except Exception:
            return False

    try:
        kb.run_daemon(
            interval=args.interval,
            max_spawn=args.max,
            failure_limit=getattr(args, "failure_limit", kb.DEFAULT_SPAWN_FAILURE_LIMIT),
            on_tick=_on_tick,
        )
    finally:
        if pidfile:
            try:
                Path(pidfile).unlink()
            except OSError:
                pass
    print("(dispatcher stopped)")
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    """Live-stream task_events to the terminal."""
    kinds = (
        {k.strip() for k in args.kinds.split(",") if k.strip()}
        if args.kinds else None
    )
    cursor = 0
    print("Watching kanban events. Ctrl-C to stop.", flush=True)
    # Seed cursor at the latest id so we don't replay history.
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM task_events"
        ).fetchone()
        cursor = int(row["m"])

    try:
        while True:
            with kb.connect() as conn:
                rows = conn.execute(
                    "SELECT e.id, e.task_id, e.kind, e.payload, e.created_at, "
                    "       t.assignee, t.tenant "
                    "FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id "
                    "WHERE e.id > ? ORDER BY e.id ASC LIMIT 200",
                    (cursor,),
                ).fetchall()
            for r in rows:
                cursor = max(cursor, int(r["id"]))
                if kinds and r["kind"] not in kinds:
                    continue
                if args.assignee and r["assignee"] != args.assignee:
                    continue
                if args.tenant and r["tenant"] != args.tenant:
                    continue
                try:
                    payload = json.loads(r["payload"]) if r["payload"] else None
                except Exception:
                    payload = None
                pl = f" {payload}" if payload else ""
                print(
                    f"[{_fmt_ts(r['created_at'])}] {r['task_id']:10s} "
                    f"{r['kind']:18s} (@{r['assignee'] or '-'}){pl}",
                    flush=True,
                )
            time.sleep(max(0.1, args.interval))
    except KeyboardInterrupt:
        print("\n(stopped)")
        return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    with kb.connect() as conn:
        stats = kb.board_stats(conn)
    if getattr(args, "json", False):
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return 0
    print("By status:")
    for k in ("triage", "todo", "scheduled", "ready", "running", "blocked", "done"):
        print(f"  {k:8s}  {stats['by_status'].get(k, 0)}")
    if stats["by_assignee"]:
        print("\nBy assignee:")
        for who, counts in sorted(stats["by_assignee"].items()):
            parts = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            print(f"  {who:20s}  {parts}")
    age = stats["oldest_ready_age_seconds"]
    if age is not None:
        print(f"\nOldest ready task age: {int(age)}s")
    return 0


def _cmd_notify_subscribe(args: argparse.Namespace) -> int:
    with kb.connect() as conn:
        if kb.get_task(conn, args.task_id) is None:
            print(f"no such task: {args.task_id}", file=sys.stderr)
            return 1
        kb.add_notify_sub(
            conn, task_id=args.task_id,
            platform=args.platform, chat_id=args.chat_id,
            thread_id=args.thread_id, user_id=args.user_id,
            notifier_profile=args.notifier_profile or _profile_author(),
        )
    print(f"Subscribed {args.platform}:{args.chat_id}"
          + (f":{args.thread_id}" if args.thread_id else "")
          + f" to {args.task_id}")
    return 0


def _cmd_notify_list(args: argparse.Namespace) -> int:
    with kb.connect() as conn:
        subs = kb.list_notify_subs(conn, args.task_id)
    if getattr(args, "json", False):
        print(json.dumps(subs, indent=2, ensure_ascii=False))
        return 0
    if not subs:
        print("(no subscriptions)")
        return 0
    for s in subs:
        thr = f":{s['thread_id']}" if s.get("thread_id") else ""
        owner = f"  owner={s['notifier_profile']}" if s.get("notifier_profile") else ""
        print(f"  {s['task_id']:10s}  {s['platform']}:{s['chat_id']}{thr}"
              f"  (since event {s['last_event_id']}){owner}")
    return 0


def _cmd_notify_unsubscribe(args: argparse.Namespace) -> int:
    with kb.connect() as conn:
        ok = kb.remove_notify_sub(
            conn, task_id=args.task_id,
            platform=args.platform, chat_id=args.chat_id,
            thread_id=args.thread_id,
        )
    if not ok:
        print("(no such subscription)", file=sys.stderr)
        return 1
    print(f"Unsubscribed from {args.task_id}")
    return 0


def _cmd_log(args: argparse.Namespace) -> int:
    content = kb.read_worker_log(args.task_id, tail_bytes=args.tail)
    if content is None:
        print(f"(no log for {args.task_id} — task may not have spawned yet)",
              file=sys.stderr)
        return 1
    sys.stdout.write(content)
    if not content.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def _cmd_runs(args: argparse.Namespace) -> int:
    """Show attempt history for a task."""
    rsk = _run_state_kwargs(args)
    if rsk is None:
        print(
            "kanban runs: pass both --state-type and --state-name, or omit both",
            file=sys.stderr,
        )
        return 2
    with kb.connect() as conn:
        runs = kb.list_runs(conn, args.task_id, **rsk)
    if getattr(args, "json", False):
        print(json.dumps([
            {
                "id": r.id, "profile": r.profile, "status": r.status,
                "outcome": r.outcome, "started_at": r.started_at,
                "ended_at": r.ended_at, "summary": r.summary,
                "error": r.error, "metadata": r.metadata,
                "worker_pid": r.worker_pid, "step_key": r.step_key,
            } for r in runs
        ], indent=2, ensure_ascii=False))
        return 0
    if not runs:
        print(f"(no runs yet for {args.task_id})")
        return 0
    print(f"{'#':3s}  {'OUTCOME':12s}  {'PROFILE':16s}  {'ELAPSED':>8s}  STARTED")
    for i, r in enumerate(runs, 1):
        end = r.ended_at or int(time.time())
        # Clamp to 0 so NTP backward-jumps don't print negative durations.
        elapsed = max(0, end - r.started_at)
        if elapsed < 60:
            el = f"{elapsed}s"
        elif elapsed < 3600:
            el = f"{elapsed // 60}m"
        else:
            el = f"{elapsed / 3600:.1f}h"
        outcome = r.outcome or ("(running)" if not r.ended_at else r.status)
        print(f"{i:3d}  {outcome:12s}  {(r.profile or '-'):16s}  {el:>8s}  {_fmt_ts(r.started_at)}")
        if r.summary:
            # Indent and truncate long summaries to keep the table readable.
            summary = r.summary.splitlines()[0][:100]
            print(f"     → {summary}")
        if r.error:
            print(f"     ✖ {r.error[:100]}")
    return 0


def _cmd_context(args: argparse.Namespace) -> int:
    with kb.connect() as conn:
        text = kb.build_worker_context(conn, args.task_id)
    print(text)
    return 0


def _cmd_specify(args: argparse.Namespace) -> int:
    """Flesh out a triage task (or all of them) via auxiliary LLM,
    then promote to todo. Thin wrapper over ``kanban_specify``."""
    from hermes_cli import kanban_specify as spec

    all_flag = bool(getattr(args, "all_triage", False))
    tenant = getattr(args, "tenant", None)
    author = getattr(args, "author", None) or _profile_author()
    want_json = bool(getattr(args, "json", False))

    if args.task_id and all_flag:
        print(
            "kanban: pass either a task id OR --all, not both",
            file=sys.stderr,
        )
        return 2

    if all_flag:
        ids = spec.list_triage_ids(tenant=tenant)
        if not ids:
            msg = (
                "No triage tasks"
                + (f" for tenant {tenant!r}" if tenant else "")
                + "."
            )
            if want_json:
                print(json.dumps({"specified": 0, "total": 0}))
            else:
                print(msg)
            return 0
    elif args.task_id:
        ids = [args.task_id]
    else:
        print(
            "kanban: specify requires a task id or --all",
            file=sys.stderr,
        )
        return 2

    ok_count = 0
    fail_count = 0
    for tid in ids:
        outcome = spec.specify_task(tid, author=author)
        if outcome.ok:
            ok_count += 1
        else:
            fail_count += 1
        if want_json:
            print(json.dumps({
                "task_id": outcome.task_id,
                "ok": outcome.ok,
                "reason": outcome.reason,
                "new_title": outcome.new_title,
            }))
        elif outcome.ok:
            title_suffix = (
                f" — retitled: {outcome.new_title!r}"
                if outcome.new_title
                else ""
            )
            print(f"Specified {outcome.task_id} → todo{title_suffix}")
        else:
            print(
                f"kanban: specify {outcome.task_id}: {outcome.reason}",
                file=sys.stderr,
            )
    if not all_flag:
        return 0 if ok_count == 1 else 1
    # --all: succeed if at least one promotion landed; exit 1 only when
    # every candidate failed (honest signal for scripts).
    return 0 if (ok_count > 0 or not ids) else 1


def _cmd_decompose(args: argparse.Namespace) -> int:
    """Fan a triage task (or all of them) out into a graph of child
    tasks via the auxiliary LLM, routed to specialist profiles by
    description. Thin wrapper over ``kanban_decompose``."""
    from hermes_cli import kanban_decompose as decomp

    all_flag = bool(getattr(args, "all_triage", False))
    tenant = getattr(args, "tenant", None)
    author = getattr(args, "author", None) or _profile_author()
    want_json = bool(getattr(args, "json", False))

    if args.task_id and all_flag:
        print(
            "kanban: pass either a task id OR --all, not both",
            file=sys.stderr,
        )
        return 2

    if all_flag:
        ids = decomp.list_triage_ids(tenant=tenant)
        if not ids:
            msg = (
                "No triage tasks"
                + (f" for tenant {tenant!r}" if tenant else "")
                + "."
            )
            if want_json:
                print(json.dumps({"decomposed": 0, "total": 0}))
            else:
                print(msg)
            return 0
    elif args.task_id:
        ids = [args.task_id]
    else:
        print(
            "kanban: decompose requires a task id or --all",
            file=sys.stderr,
        )
        return 2

    ok_count = 0
    for tid in ids:
        outcome = decomp.decompose_task(tid, author=author)
        if outcome.ok:
            ok_count += 1
        if want_json:
            print(json.dumps({
                "task_id": outcome.task_id,
                "ok": outcome.ok,
                "reason": outcome.reason,
                "fanout": outcome.fanout,
                "child_ids": outcome.child_ids,
                "new_title": outcome.new_title,
            }))
        elif outcome.ok:
            if outcome.fanout and outcome.child_ids:
                child_summary = ", ".join(outcome.child_ids)
                print(
                    f"Decomposed {outcome.task_id} → {len(outcome.child_ids)} "
                    f"children ({child_summary}); root promoted to todo"
                )
            else:
                title_suffix = (
                    f" — retitled: {outcome.new_title!r}"
                    if outcome.new_title
                    else ""
                )
                print(
                    f"Specified {outcome.task_id} → todo "
                    f"(no fanout){title_suffix}"
                )
        else:
            print(
                f"kanban: decompose {outcome.task_id}: {outcome.reason}",
                file=sys.stderr,
            )
    if not all_flag:
        return 0 if ok_count == 1 else 1
    return 0 if (ok_count > 0 or not ids) else 1


def _cmd_gc(args: argparse.Namespace) -> int:
    """Remove scratch workspaces of archived tasks, prune old events, and
    delete old worker logs."""
    import shutil
    scratch_root = kb.workspaces_root()
    removed_ws = 0
    with kb.connect() as conn:
        rows = conn.execute(
            "SELECT id, workspace_kind, workspace_path FROM tasks WHERE status = 'archived'"
        ).fetchall()
    for row in rows:
        if row["workspace_kind"] != "scratch":
            continue
        path = Path(row["workspace_path"] or (scratch_root / row["id"]))
        try:
            path = path.resolve()
        except OSError:
            continue
        try:
            path.relative_to(scratch_root.resolve())
        except ValueError:
            # Safety: never delete outside the scratch root.
            continue
        if path.exists() and path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            removed_ws += 1

    event_days = getattr(args, "event_retention_days", 30)
    log_days = getattr(args, "log_retention_days", 30)
    with kb.connect() as conn:
        removed_events = kb.gc_events(
            conn, older_than_seconds=event_days * 24 * 3600,
        )
    removed_logs = kb.gc_worker_logs(
        older_than_seconds=log_days * 24 * 3600,
    )
    print(f"GC complete: {removed_ws} workspace(s), "
          f"{removed_events} event row(s), {removed_logs} log file(s) removed")
    return 0


# ---------------------------------------------------------------------------
# Slash-command entry point (used by /kanban from CLI and gateway)
# ---------------------------------------------------------------------------

_SLASH_KANBAN_HELP = """\
**/kanban** — manage the shared task board.

Common subcommands:
  `list` (alias `ls`)   List tasks on the current board
  `show <id>`           Task details + comments + events
  `stats`               Per-status / per-assignee counts
  `create <title>…`     Create a task (auto-subscribes you to events)
  `comment <id> <msg>`  Append a comment
  `complete <id>…`      Mark task(s) done
  `block <id> [reason]` Mark blocked; `schedule <id> [reason]` parks time-delay work; `unblock <id>` to revive
  `assign <id> <profile>`  Reassign
  `boards list`         Show all boards
  `assignees`           Known profiles + counts
  `worker-lanes`        External worker lane capacity + active instances
  `context <id>`        Full worker-context dump
  `runs <id>`           Attempt history
  `log <id>`            Worker log

Run `/kanban <subcommand> -h` for arguments. \
Read-only commands are safe while an agent is running.\
"""


def run_slash(rest: str) -> str:
    """Execute a ``/kanban …`` string and return captured stdout/stderr.

    ``rest`` is everything after ``/kanban`` (may be empty).  Used from
    both the interactive CLI (``self._handle_kanban_command``) and the
    gateway (``_handle_kanban_command``) so formatting is identical.
    """
    import io
    import contextlib

    tokens = shlex.split(rest) if rest and rest.strip() else []

    # Bare ``/kanban`` or ``/kanban help`` / ``--help`` / ``-h`` / ``?``:
    # show the curated short-help block instead of dumping argparse's full
    # usage tree (which is enormous and reads as garbage in a chat
    # bubble).  Per-subcommand help still works via ``/kanban foo -h``.
    if not tokens or tokens[0] in {"help", "--help", "-h", "?"}:
        return _SLASH_KANBAN_HELP

    # Single argparse tree rooted at "/kanban".  build_parser() expects a
    # subparsers action to attach to, so build a throwaway one and pull
    # the kanban_parser back out — then drive it directly so usage/error
    # text reads as ``/kanban`` (not ``/kanban-wrap kanban``).
    _wrap = argparse.ArgumentParser(prog="/kanban-wrap", add_help=False)
    _wrap.exit_on_error = False  # type: ignore[attr-defined]
    _top_sub = _wrap.add_subparsers(dest="_top")
    kanban_parser = build_parser(_top_sub)
    kanban_parser.prog = "/kanban"
    kanban_parser.exit_on_error = False  # type: ignore[attr-defined]
    for _action in kanban_parser._actions:
        if isinstance(_action, argparse._SubParsersAction):
            for _name, _choice in _action.choices.items():
                _choice.prog = f"/kanban {_name}"
                _choice.exit_on_error = False  # type: ignore[attr-defined]

    def _usage_for_error() -> str:
        if tokens:
            for _action in kanban_parser._actions:
                if isinstance(_action, argparse._SubParsersAction):
                    subparser = _action.choices.get(tokens[0])
                    if subparser is not None:
                        return subparser.format_usage().rstrip()
        return kanban_parser.format_usage().rstrip()

    buf_out = io.StringIO()
    buf_err = io.StringIO()
    # ``-h`` / ``--help`` makes argparse print to stdout and SystemExit(0).
    # Capture both streams so neither the help text nor the error text
    # bypasses our buffer.
    try:
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            args = kanban_parser.parse_args(tokens)
    except SystemExit as exc:
        out = buf_out.getvalue().rstrip()
        err = buf_err.getvalue().rstrip()
        # Help dump (exit 0) → return the captured help text directly.
        if exc.code in {0, None} and out:
            return out
        body = err or out
        return f"⚠ /kanban usage error\n{body}" if body else "⚠ /kanban usage error"
    except argparse.ArgumentError as exc:
        return f"⚠ /kanban usage error\n{_usage_for_error()}\n{exc}"

    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        try:
            kanban_command(args)
        except SystemExit:
            pass
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)

    out = buf_out.getvalue().rstrip()
    err = buf_err.getvalue().rstrip()
    if err and out:
        return f"{out}\n{err}"
    return err if err else (out or "(no output)")
