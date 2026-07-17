"""Codex CLI Kanban worker lane adapter.

The dispatcher spawns this module as a wrapper process.  The wrapper owns the
external Codex CLI subprocess, streams stdout/stderr to the Kanban worker log,
periodically heartbeats the claim, writes structured progress events, and
blocks the task for Hermes review when Codex exits successfully.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from hermes_cli.worker_lanes import WorkerLane

CODEX_OUTPUT_TAIL_BYTES = 8192
CODEX_FIELD_MAX_BYTES = 4096
CODEX_EVENT_FIELD_MAX_BYTES = 2048
CODEX_PROGRESS_MAX_ITEMS = 50
CODEX_REVIEW_OUTPUT_TAIL_LINES = 80
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30.0
CODEX_TERMINAL_EVENT_EXIT_GRACE_SECONDS = 5.0

_CHECKBOX_RE = re.compile(r"^\s*[-*]\s*\[([ xX])\]\s+(.+?)\s*$")
_ORDINAL_RE = re.compile(r"^\s*([oxOX])\s*\((\d+)\)\s+(.+?)\s*$")
_CODEX_ITEM_ID_INDEX_RE = re.compile(r"(\d+)$")
_RECEIPT_SECTION_RE = re.compile(r"^\s*([A-Za-z][A-Za-z ]{0,60})\s*:\s*$")
_VERDICT_LINE_RE = re.compile(r"(?i)^\s*verdict\s*:\s*(?:[-*]\s*)?([a-z][a-z_-]*)\s*$")
_VERDICT_HEADER_RE = re.compile(r"(?i)^\s*verdict\s*:\s*$")
_VERDICT_BULLET_RE = re.compile(r"^\s*[-*]?\s*([a-z][a-z_-]*)\s*$")
_JSON_FENCE_RE = re.compile(r"```json[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
_ALLOWED_STRUCTURED_VERDICTS = {
    "approve",
    "approved",
    "request_changes",
    "blocked",
    "pass",
    "passed",
    "fail",
    "failed",
}
_RECEIPT_SECTION_KEYS = {
    "progress",
    "changed_files",
    "verification",
    "remaining_risks",
    "recommended_reviewer_action",
}
_REVIEW_OUTPUT_START_HEADERS = {
    "changed_files",
    "decision",
    "findings",
    "remaining_risks",
    "result",
    "results",
    "review_findings",
    "summary",
    "test_findings",
    "test_results",
    "verification",
}

_MARKDOWN_HEADING_PREFIX_RE = re.compile(r"^\s*(?:#{1,6}\s*)+")


@dataclass(frozen=True)
class CodexLaneConfig:
    name: str
    model: Optional[str] = None
    sandbox: str = "workspace-write"
    approval: str = "never"
    max_concurrency: Optional[int] = 1
    success_policy: str = "block_for_review"
    timeout_seconds: Optional[int] = None
    heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    json_events: bool = False
    network_namespace: Optional[str] = None
    phase4g8_run_id: Optional[str] = None
    network_uid: int = 65534
    network_gid: int = 65534
    isolated_codex_home_seed: Optional[str] = None
    isolated_codex_home_root: Optional[str] = None


class _TailBuffer:
    def __init__(self, max_bytes: int = CODEX_OUTPUT_TAIL_BYTES) -> None:
        self.max_bytes = int(max_bytes)
        self._buf = b""

    def append(self, text: str) -> None:
        if not text:
            return
        self._buf += text.encode("utf-8", errors="replace")
        if len(self._buf) > self.max_bytes:
            self._buf = self._buf[-self.max_bytes :]

    def text(self) -> str:
        return self._buf.decode("utf-8", errors="replace")


def make_codex_worker_lane(config: dict[str, Any], *, source: str = "config") -> WorkerLane:
    cfg = CodexLaneConfig(
        name=str(config["name"]),
        model=(str(config["model"]) if config.get("model") else None),
        sandbox=str(config.get("sandbox") or "workspace-write"),
        approval=str(config.get("approval") or "never"),
        max_concurrency=(
            int(config["max_concurrency"])
            if config.get("max_concurrency") is not None
            else None
        ),
        success_policy=str(config.get("success_policy") or "block_for_review"),
        timeout_seconds=(
            int(config["timeout_seconds"])
            if config.get("timeout_seconds") is not None
            else None
        ),
        json_events=_as_bool(config.get("json_events"), default=False),
        network_namespace=(str(config["network_namespace"]) if config.get("network_namespace") else None),
        phase4g8_run_id=(str(config["phase4g8_run_id"]) if config.get("phase4g8_run_id") else None),
        network_uid=int(config.get("network_uid", 65534)),
        network_gid=int(config.get("network_gid", 65534)),
        isolated_codex_home_seed=(
            str(config["isolated_codex_home_seed"])
            if config.get("isolated_codex_home_seed")
            else None
        ),
        isolated_codex_home_root=(
            str(config["isolated_codex_home_root"])
            if config.get("isolated_codex_home_root")
            else None
        ),
    )

    def _spawn(task, workspace: str, *, board: Optional[str] = None) -> Optional[int]:
        return spawn_codex_worker(task, workspace, cfg, board=board)

    lane_config = {
        "type": "codex_cli",
        "model": cfg.model,
        "sandbox": cfg.sandbox,
        "approval": cfg.approval,
        "timeout_seconds": cfg.timeout_seconds,
        "json_events": cfg.json_events,
    }
    if cfg.network_namespace:
        lane_config["network_namespace"] = cfg.network_namespace
        lane_config["phase4g8_run_id"] = cfg.phase4g8_run_id
        lane_config["network_uid"] = cfg.network_uid
        lane_config["network_gid"] = cfg.network_gid
        lane_config["isolated_codex_home_seed"] = cfg.isolated_codex_home_seed
        lane_config["isolated_codex_home_root"] = cfg.isolated_codex_home_root
    return WorkerLane(
        name=cfg.name,
        kind="codex_cli",
        description=f"Codex CLI worker lane ({cfg.model or 'default model'})",
        spawn_fn=_spawn,
        success_policy=cfg.success_policy,
        max_concurrency=cfg.max_concurrency,
        source=source,
        config=lane_config,
    )


def _as_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _normalize_markdown_block_line(raw_line: str) -> str:
    stripped = raw_line.strip()
    stripped = _MARKDOWN_HEADING_PREFIX_RE.sub("", stripped)
    stripped = stripped.strip("*_`")
    return stripped.strip()


def _safe_env_for_worker(task, workspace: str, cfg: CodexLaneConfig, *, board: Optional[str]) -> dict[str, str]:
    from hermes_cli import kanban_db as kb

    allowed = {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HERMES_HOME",
        "HERMES_KANBAN_HOME",
        "CODEX_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "PHASE4G8_WORKER_TOOLCHAIN",
    }
    env = {k: v for k, v in os.environ.items() if k in allowed and v is not None}
    project_root = str(Path(__file__).resolve().parent.parent)
    existing_pythonpath = env.get("PYTHONPATH") or os.environ.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        project_root
        if not existing_pythonpath
        else project_root + os.pathsep + existing_pythonpath
    )
    env["HERMES_KANBAN_TASK"] = task.id
    env["HERMES_KANBAN_WORKSPACE"] = workspace
    env["HERMES_KANBAN_HOME"] = str(kb.kanban_home())
    env["HERMES_KANBAN_DB"] = str(kb.kanban_db_path(board=board))
    env["HERMES_KANBAN_WORKSPACES_ROOT"] = str(kb.workspaces_root(board=board))
    env["HERMES_KANBAN_BOARD"] = kb._normalize_board_slug(board) or kb.get_current_board()
    env["HERMES_WORKER_LANE"] = cfg.name
    env["HERMES_WORKER_KIND"] = "codex_cli"
    env["HERMES_PROFILE"] = cfg.name
    if task.current_run_id is not None:
        env["HERMES_KANBAN_RUN_ID"] = str(task.current_run_id)
    if task.claim_lock:
        env["HERMES_KANBAN_CLAIM_LOCK"] = task.claim_lock
    if task.tenant:
        env["HERMES_TENANT"] = task.tenant
    if cfg.isolated_codex_home_seed or cfg.isolated_codex_home_root:
        env["CODEX_HOME"] = str(_isolated_codex_home_for_task(task.id, cfg, board=board))
    return env


def _isolated_codex_home_for_task(
    task_id: str,
    cfg: CodexLaneConfig,
    *,
    board: Optional[str],
) -> Path:
    """Return one durable Codex state directory per runtime execution node."""

    from hermes_cli import kanban_db as kb

    if not cfg.isolated_codex_home_seed or not cfg.isolated_codex_home_root:
        raise ValueError("isolated CODEX_HOME requires both seed and root")
    seed = Path(cfg.isolated_codex_home_seed).resolve()
    root = Path(cfg.isolated_codex_home_root).resolve()
    if not seed.is_dir() or not all((seed / name).is_file() for name in ("config.toml", "auth.json")):
        raise ValueError("isolated CODEX_HOME seed is incomplete")
    with kb.connect(board=board) as conn:
        row = conn.execute(
            "SELECT id FROM execution_nodes WHERE latest_task_id = ? LIMIT 1",
            (task_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"runtime execution node not found for task {task_id}")
    node_id = str(row["id"])
    node_key = hashlib.sha256(node_id.encode("utf-8")).hexdigest()[:24]
    target = root / f"node-{node_key}"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o711)
    target.mkdir(mode=0o700, exist_ok=True)
    for name in ("config.toml", "auth.json"):
        destination = target / name
        if not destination.exists():
            shutil.copyfile(seed / name, destination)
        os.chmod(destination, 0o600)
        os.chown(destination, int(cfg.network_uid), int(cfg.network_gid))
    seed_exec_policy = seed / "rules" / "default.rules"
    if seed_exec_policy.is_file():
        target_rules = target / "rules"
        target_rules.mkdir(mode=0o700, exist_ok=True)
        os.chmod(target_rules, 0o700)
        target_exec_policy = target_rules / "default.rules"
        shutil.copyfile(seed_exec_policy, target_exec_policy)
        os.chmod(target_exec_policy, 0o600)
        os.chown(target_rules, int(cfg.network_uid), int(cfg.network_gid))
        os.chown(target_exec_policy, int(cfg.network_uid), int(cfg.network_gid))
    marker = target / ".execution-node"
    marker.write_text(node_id + "\n", encoding="utf-8")
    os.chmod(marker, 0o600)
    os.chown(marker, int(cfg.network_uid), int(cfg.network_gid))
    os.chown(target, int(cfg.network_uid), int(cfg.network_gid))
    return target


def _path_is_writable_dir(path: Optional[str]) -> bool:
    if not path:
        return False
    try:
        p = Path(path).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".hermes-codex-write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _safe_env_for_codex(workspace: Optional[str] = None) -> dict[str, str]:
    allowed = {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "CODEX_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_LOCK",
        "HERMES_KANBAN_WORKSPACE",
        "HERMES_KANBAN_BOARD",
        "HERMES_WORKER_LANE",
        "HERMES_WORKER_KIND",
        "PHASE4G8_WORKER_TOOLCHAIN",
    }
    env = {k: v for k, v in os.environ.items() if k in allowed and v is not None}
    home_writable = _path_is_writable_dir(env.get("HOME"))
    home_rebased = False
    if not home_writable and workspace:
        home = Path(workspace) / ".hermes-codex-home"
        home.mkdir(parents=True, exist_ok=True)
        env["HOME"] = str(home)
        home_writable = True
        home_rebased = True
    codex_home = env.get("CODEX_HOME")
    if codex_home:
        codex_home_writable = _path_is_writable_dir(codex_home)
    else:
        default_codex_home = Path(env["HOME"]).expanduser() / ".codex" if env.get("HOME") else None
        codex_home_writable = bool(default_codex_home and _path_is_writable_dir(str(default_codex_home)))
    if not codex_home_writable and (codex_home or home_rebased or not home_writable) and workspace:
        codex_home = Path(workspace) / ".hermes-codex"
        codex_home.mkdir(parents=True, exist_ok=True)
        env["CODEX_HOME"] = str(codex_home)
    return env


def _codex_approval_config_audit(codex_home: Optional[str]) -> dict[str, Any]:
    """Read only non-secret approval metadata from the active Codex config."""

    if not codex_home:
        return {"config_readable": False}
    config_path = Path(codex_home).expanduser() / "config.toml"
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {"config_readable": False}
    auto_review = config.get("auto_review") if isinstance(config.get("auto_review"), dict) else {}
    features = config.get("features") if isinstance(config.get("features"), dict) else {}
    policy = str(auto_review.get("policy") or "").strip()
    rules_path = config_path.parent / "rules" / "default.rules"
    try:
        exec_policy = rules_path.read_text(encoding="utf-8").strip()
    except OSError:
        exec_policy = ""
    return {
        "config_readable": True,
        "configured_policy": str(config.get("approval_policy") or ""),
        "reviewer": str(config.get("approvals_reviewer") or "user"),
        "auto_review_policy_configured": bool(policy),
        "auto_review_policy_sha256": (
            hashlib.sha256(policy.encode("utf-8")).hexdigest() if policy else None
        ),
        "guardian_approval_enabled": features.get("guardian_approval") is True,
        "exec_policy_configured": bool(exec_policy),
        "exec_policy_sha256": (
            hashlib.sha256(exec_policy.encode("utf-8")).hexdigest()
            if exec_policy
            else None
        ),
    }


def spawn_codex_worker(
    task,
    workspace: str,
    cfg: CodexLaneConfig,
    *,
    board: Optional[str] = None,
) -> Optional[int]:
    """Spawn the Python wrapper that runs Codex CLI."""
    from hermes_cli import kanban_db as kb

    cmd = [
        sys.executable,
        "-m",
        "hermes_cli.codex_worker",
        "run",
        "--task-id",
        task.id,
        "--lane",
        cfg.name,
        "--workspace",
        workspace,
        "--sandbox",
        cfg.sandbox,
        "--approval",
        cfg.approval,
        "--success-policy",
        cfg.success_policy,
        "--heartbeat-interval",
        str(cfg.heartbeat_interval_seconds),
    ]
    if cfg.json_events:
        cmd.append("--json-events")
    if task.current_run_id is not None:
        cmd.extend(["--run-id", str(task.current_run_id)])
    if task.claim_lock:
        cmd.extend(["--claim-lock", task.claim_lock])
    if cfg.model:
        cmd.extend(["--model", cfg.model])
    if cfg.timeout_seconds is not None:
        cmd.extend(["--timeout-seconds", str(cfg.timeout_seconds)])
    if cfg.network_namespace:
        cmd.extend([
            "--network-namespace", cfg.network_namespace,
            "--network-uid", str(cfg.network_uid),
            "--network-gid", str(cfg.network_gid),
        ])
    resolved_board = kb._normalize_board_slug(board) or kb.get_current_board()
    cmd.extend(["--board", resolved_board])

    log_dir = kb.worker_logs_dir(board=board)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task.id}.log"
    kb._rotate_worker_log(log_path, kb.DEFAULT_LOG_ROTATE_BYTES)
    env = _safe_env_for_worker(task, workspace, cfg, board=board)
    if cfg.phase4g8_run_id:
        env["HERMES_PHASE4G8_RUN_ID"] = cfg.phase4g8_run_id

    log_f = open(log_path, "ab")
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell.
            cmd,
            cwd=workspace if os.path.isdir(workspace) else None,
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log_f.close()
    return proc.pid


def build_codex_argv(
    *,
    binary: str,
    workspace: str,
    sandbox: str,
    approval: str,
    model: Optional[str] = None,
    json_events: bool = False,
    resume_session_id: Optional[str] = None,
) -> list[str]:
    argv = [
        binary,
        "--cd",
        workspace,
        "--sandbox",
        sandbox,
        "--ask-for-approval",
        approval,
    ]
    if model:
        argv.extend(["--model", model])
    argv.append("exec")
    if resume_session_id:
        argv.append("resume")
    if json_events:
        argv.append("--json")
    if resume_session_id:
        argv.append(str(resume_session_id))
    argv.append("-")
    return argv


def wrap_codex_network_argv(
    argv: list[str],
    network_namespace: Optional[str],
    *,
    uid: int = 65534,
    gid: int = 65534,
    workspace: Optional[str] = None,
    worker_env: Optional[dict[str, str]] = None,
    filesystem_isolation: bool = False,
) -> list[str]:
    prefix: list[str] = []
    if network_namespace:
        if not re.fullmatch(r"h4g8-[0-9a-f]{8}", network_namespace):
            raise ValueError("invalid Phase 4G8 network namespace")
        prefix = ["ip", "netns", "exec", network_namespace]
    if not filesystem_isolation:
        if not network_namespace:
            return list(argv)
        return [
            *prefix,
            "setpriv",
            "--bounding-set=-net_admin,-net_raw",
            "--inh-caps=-all",
            "--ambient-caps=-all",
            "--no-new-privs",
            f"--reuid={int(uid)}",
            f"--regid={int(gid)}",
            "--clear-groups",
            *argv,
        ]

    bubblewrap = shutil.which("bwrap")
    if not bubblewrap:
        raise RuntimeError("Phase 4G8 filesystem isolation requires bubblewrap")
    if not workspace or not worker_env:
        raise ValueError("Phase 4G8 filesystem isolation requires workspace and worker_env")

    writable_paths = {
        str(Path(workspace).resolve()),
        str(Path(worker_env["HOME"]).resolve()),
        str(Path(worker_env["CODEX_HOME"]).resolve()),
    }
    workspace_path = Path(workspace).resolve()
    common_path: Optional[Path] = None
    dot_git = workspace_path / ".git"
    if dot_git.is_file():
        marker = dot_git.read_text(encoding="utf-8", errors="strict").strip()
        if marker.startswith("gitdir:"):
            git_dir = Path(marker.split(":", 1)[1].strip())
            if not git_dir.is_absolute():
                git_dir = workspace_path / git_dir
            git_dir = git_dir.resolve()
            common_marker = git_dir / "commondir"
            if common_marker.is_file():
                common_path = Path(
                    common_marker.read_text(encoding="utf-8", errors="strict").strip()
                )
                if not common_path.is_absolute():
                    common_path = git_dir / common_path
                common_path = common_path.resolve()
    if common_path is None:
        git_common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=workspace_path,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if git_common.returncode == 0 and git_common.stdout.strip():
            common_path = Path(git_common.stdout.strip())
            if not common_path.is_absolute():
                common_path = workspace_path / common_path
            common_path = common_path.resolve()
    if common_path is not None:
        try:
            common_path.relative_to(workspace_path)
        except ValueError:
            # Detached Runtime worktrees keep their common Git metadata in the
            # primary workspace. Bind only that metadata directory; sibling
            # source worktrees remain invisible inside the worker sandbox.
            writable_paths.add(str(common_path))
    toolchain = str(worker_env.get("PHASE4G8_WORKER_TOOLCHAIN") or "").strip()
    readonly_paths = {str(Path(toolchain).resolve())} if toolchain else set()
    for path in writable_paths | readonly_paths:
        if not Path(path).is_dir():
            raise RuntimeError(f"Phase 4G8 isolation path is not a directory: {path}")

    isolated = [
        bubblewrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--chmod", "1777", "/tmp",
    ]
    for system_path in ("/usr", "/bin", "/lib", "/lib64"):
        if Path(system_path).exists():
            isolated.extend(["--ro-bind", system_path, system_path])
    isolated.extend(["--dir", "/opt"])
    codex_binary = shutil.which("codex")
    if codex_binary:
        resolved_codex = Path(codex_binary).resolve()
        if len(resolved_codex.parts) >= 3 and resolved_codex.parts[1] == "opt":
            codex_runtime = Path("/") / resolved_codex.parts[1] / resolved_codex.parts[2]
            isolated.extend(["--ro-bind", str(codex_runtime), str(codex_runtime)])
    if toolchain:
        isolated.extend([
            "--dir", "/opt/miniconda3",
            "--dir", "/opt/miniconda3/envs",
            "--ro-bind", str(Path(toolchain).resolve()), "/opt/miniconda3/envs/testbed",
        ])
    isolated.extend(["--dir", "/etc"])
    for system_path in (
        "/etc/alternatives",
        "/etc/ca-certificates",
        "/etc/ssl",
        "/etc/pki",
        "/etc/ld.so.cache",
        "/etc/hosts",
        "/etc/resolv.conf",
        "/etc/nsswitch.conf",
        "/etc/passwd",
        "/etc/group",
        "/etc/localtime",
    ):
        if Path(system_path).exists():
            isolated.extend(["--ro-bind", system_path, system_path])

    bind_paths = sorted(writable_paths | readonly_paths, key=lambda value: (len(Path(value).parts), value))
    created_parents: set[str] = {"/tmp", "/etc"}
    for bind_path in bind_paths:
        for parent in reversed(Path(bind_path).parents):
            parent_text = str(parent)
            if parent_text == "/" or parent_text in created_parents:
                continue
            isolated.extend(["--dir", parent_text])
            created_parents.add(parent_text)
        isolated.extend([
            "--ro-bind" if bind_path in readonly_paths else "--bind",
            bind_path,
            bind_path,
        ])
    codex_home = Path(worker_env["CODEX_HOME"]).resolve()
    for protected_path in (
        codex_home / "config.toml",
        codex_home / "auth.json",
        codex_home / "rules",
    ):
        if protected_path.exists():
            isolated.extend(["--ro-bind", str(protected_path), str(protected_path)])
    isolated.extend([
        "--chdir", str(Path(workspace).resolve()),
        "setpriv",
        "--bounding-set=-net_admin,-net_raw",
        "--inh-caps=-all",
        "--ambient-caps=-all",
        "--no-new-privs",
        f"--reuid={int(uid)}",
        f"--regid={int(gid)}",
        "--clear-groups",
        *argv,
    ])
    return [*prefix, *isolated]


def parse_progress_items(text: str) -> list[dict[str, Any]]:
    """Parse supported Codex progress/checklist formats."""
    items: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    implicit_index = 1

    def add_item(item: dict[str, Any]) -> None:
        key = str(item["text"]).strip().casefold()
        if key in positions:
            existing = items[positions[key]]
            existing["status"] = item["status"]
            existing["text"] = item["text"]
            return
        positions[key] = len(items)
        items.append(item)

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _ORDINAL_RE.match(line)
        if m:
            mark, idx, item_text = m.groups()
            item_text = item_text.strip()
            if _is_placeholder_progress_text(item_text):
                continue
            # Codex plan output historically used `o` for completed and `x`
            # for the item currently being worked in examples.
            status = "done" if mark.lower() == "o" else "running"
            add_item({
                "index": int(idx),
                "status": status,
                "text": item_text[:500],
            })
            continue
        m = _CHECKBOX_RE.match(line)
        if m:
            mark, item_text = m.groups()
            item_text = item_text.strip()
            if _is_placeholder_progress_text(item_text):
                continue
            status = "done" if mark.lower() == "x" else "pending"
            add_item({
                "index": implicit_index,
                "status": status,
                "text": item_text[:500],
            })
            implicit_index += 1
    return items[:CODEX_PROGRESS_MAX_ITEMS]


def _is_placeholder_progress_text(text: str) -> bool:
    """Return True for template-only checklist entries echoed by Codex CLI."""
    return text.strip() in {"...", "…"}


def _write_log(log_f, text: str) -> None:
    try:
        log_f.write(text)
        log_f.flush()
    except Exception:
        pass


def _record_event(task_id: str, kind: str, payload: dict[str, Any], *, run_id: Optional[int]) -> None:
    from hermes_cli import kanban_db as kb

    try:
        with kb.connect() as conn:
            kb.record_task_event(conn, task_id, kind, payload, run_id=run_id)
    except Exception:
        pass


def _json_event_item_payload(item: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in ("id", "type", "status", "exit_code"):
        if key in item:
            payload[key] = item.get(key)
    if item.get("command"):
        payload["command"] = _cap(str(item.get("command")), CODEX_EVENT_FIELD_MAX_BYTES)
    if item.get("text"):
        payload["text_tail"] = _cap(str(item.get("text")), CODEX_EVENT_FIELD_MAX_BYTES)
    if item.get("aggregated_output"):
        payload["output_tail"] = _cap(
            str(item.get("aggregated_output")),
            CODEX_EVENT_FIELD_MAX_BYTES,
        )
    changes = item.get("changes")
    if isinstance(changes, list):
        compact_changes: list[dict[str, str]] = []
        for change in changes[:20]:
            if not isinstance(change, dict):
                continue
            compact: dict[str, str] = {}
            if change.get("path"):
                compact["path"] = _cap(str(change.get("path")), CODEX_EVENT_FIELD_MAX_BYTES)
            if change.get("kind"):
                compact["kind"] = _cap(str(change.get("kind")), 80)
            if compact:
                compact_changes.append(compact)
        if compact_changes:
            payload["changes"] = compact_changes
    return payload


def _codex_json_event_payload(
    event: dict[str, Any],
    *,
    lane: str,
    run_id: Optional[int],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "worker_lane": lane,
        "worker_kind": "codex_cli",
        "run_id": run_id,
        "event_type": str(event.get("type") or "unknown"),
    }
    if event.get("thread_id"):
        payload["thread_id"] = str(event["thread_id"])
    if isinstance(event.get("usage"), dict):
        payload["usage"] = {
            key: event["usage"].get(key)
            for key in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
            )
            if key in event["usage"]
        }
    item = event.get("item")
    if isinstance(item, dict):
        payload["item"] = _json_event_item_payload(item)
    return payload


def _codex_json_event_text(event: dict[str, Any]) -> str:
    item = event.get("item")
    if not isinstance(item, dict):
        return ""
    item_type = str(item.get("type") or "")
    if item_type == "agent_message" and item.get("text"):
        return str(item["text"]).rstrip() + "\n"
    if item_type == "command_execution":
        parts: list[str] = []
        command = item.get("command")
        if command:
            parts.append(f"$ {command}")
        status = item.get("status")
        exit_code = item.get("exit_code")
        if status or exit_code is not None:
            parts.append(f"status={status or '-'} exit_code={exit_code}")
        if item.get("aggregated_output"):
            parts.append(str(item["aggregated_output"]).rstrip())
        return "\n".join(part for part in parts if part).rstrip() + ("\n" if parts else "")
    return ""


def _codex_json_event_progress_text(event: dict[str, Any]) -> str:
    item = event.get("item")
    if not isinstance(item, dict):
        return ""
    item_type = str(item.get("type") or "")
    if item_type not in {"file_change", "command_execution"}:
        return ""

    item_id = str(item.get("id") or "")
    match = _CODEX_ITEM_ID_INDEX_RE.search(item_id)
    index = int(match.group(1)) + 1 if match else 1
    status = str(item.get("status") or "")
    event_type = str(event.get("type") or "")
    done = event_type == "item.completed" or status in {"completed", "failed", "cancelled"}
    mark = "o" if done else "x"

    if item_type == "file_change":
        paths: list[str] = []
        changes = item.get("changes")
        if isinstance(changes, list):
            for change in changes[:5]:
                if isinstance(change, dict) and change.get("path"):
                    paths.append(Path(str(change["path"])).name or str(change["path"]))
        suffix = ", ".join(paths) if paths else "workspace files"
        text = f"apply file changes: {suffix}"
    else:
        command = str(item.get("command") or "command").replace("\n", " ")
        text = "run command: " + _cap(command, 180)
    return f"{mark} ({index}) {text}\n"


def _handle_codex_json_line(
    line: str,
    *,
    task_id: str,
    lane: str,
    run_id: Optional[int],
) -> tuple[bool, str, Optional[str], Optional[str], Optional[str]]:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return False, line, None, None, None
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        return False, line, None, None, None
    payload = _codex_json_event_payload(event, lane=lane, run_id=run_id)
    _record_event(task_id, "worker_codex_event", payload, run_id=run_id)
    text = _codex_json_event_text(event)
    text += _codex_json_event_progress_text(event)
    thread_id = str(event["thread_id"]) if event.get("thread_id") else None
    item = event.get("item")
    runtime_receipt_source = None
    if (
        isinstance(item, dict)
        and item.get("type") == "agent_message"
        and _extract_runtime_receipt(str(item.get("text") or "")) is not None
    ):
        runtime_receipt_source = str(item["text"])
    return True, text, thread_id, str(event["type"]), runtime_receipt_source


def _heartbeat(
    task_id: str,
    *,
    run_id: Optional[int],
    claim_lock: Optional[str],
    lane: str,
    execution_mode: str = "fresh",
    backend_session_id: Optional[str] = None,
) -> None:
    from hermes_cli import kanban_db as kb

    try:
        with kb.connect() as conn:
            if claim_lock:
                kb.heartbeat_claim(conn, task_id, claimer=claim_lock)
            accepted = kb.heartbeat_worker(
                conn,
                task_id,
                note=f"worker_lane={lane}",
                expected_run_id=run_id,
            )
            if not accepted:
                return
            kb.record_task_event(
                conn,
                task_id,
                "worker_heartbeat",
                {
                    "worker_lane": lane,
                    "lane": lane,
                    "worker_kind": "codex_cli",
                    "run_id": run_id,
                    "claim_lock": claim_lock,
                    "execution_mode": execution_mode,
                    "backend_session_id": backend_session_id,
                },
                run_id=run_id,
            )
    except Exception:
        pass


def _cap(text: Optional[str], limit: int = CODEX_FIELD_MAX_BYTES) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[truncated {len(text) - limit} chars]"


def _run_git(args: list[str], workspace: str, *, timeout: float = 5.0) -> str:
    try:
        safe_workspace = str(Path(workspace).resolve())
        proc = subprocess.run(
            ["git", "-c", f"safe.directory={safe_workspace}", "-C", workspace, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        out = (proc.stdout or "").rstrip("\r\n")
        err = (proc.stderr or "").strip()
        return out if out else err
    except Exception as exc:
        return str(exc)


def _git_path_fingerprint(workspace: str, relative_path: str) -> str:
    path = Path(workspace) / relative_path.rstrip("/")
    fingerprint = hashlib.sha256()
    fingerprint.update(relative_path.encode("utf-8", errors="replace"))
    if path.is_symlink():
        try:
            fingerprint.update(b"symlink\0" + os.readlink(path).encode("utf-8", errors="replace"))
        except OSError:
            fingerprint.update(b"unreadable-symlink")
    elif path.is_file():
        try:
            fingerprint.update(b"file\0" + path.read_bytes())
        except OSError:
            fingerprint.update(b"unreadable-file")
    elif path.is_dir():
        fingerprint.update(b"directory\0")
        for child in sorted(candidate for candidate in path.rglob("*") if candidate.is_file() or candidate.is_symlink()):
            child_relative = child.relative_to(path).as_posix()
            fingerprint.update(child_relative.encode("utf-8", errors="replace") + b"\0")
            try:
                fingerprint.update(os.readlink(child).encode("utf-8", errors="replace") if child.is_symlink() else child.read_bytes())
            except OSError:
                fingerprint.update(b"<unreadable>")
    else:
        fingerprint.update(b"missing")
    return fingerprint.hexdigest()


def capture_git_change_baseline(workspace: str) -> dict[str, Any]:
    evidence = collect_git_evidence(workspace)
    changed_files = list(evidence.get("changed_files") or [])
    return {
        "head_revision": evidence.get("head_revision"),
        "changed_files": changed_files,
        "path_fingerprints": {
            path: _git_path_fingerprint(workspace, path)
            for path in changed_files
        },
    }


def collect_git_evidence(
    workspace: str,
    *,
    baseline: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if not shutil.which("git"):
        return {"status": "", "changed_files": [], "diff_summary": "git not found"}
    status = _run_git(["status", "--short"], workspace)
    head_revision = _run_git(["rev-parse", "HEAD"], workspace).strip()
    changed_files: list[str] = []
    for line in status.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip() if len(line) >= 3 else line.strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        changed_files.append(path)
    diff_stat = _run_git(["diff", "--stat", "--summary", "HEAD"], workspace)
    if "not a git repository" in status.lower() or "not a git repository" in diff_stat.lower():
        return {
            "status": _cap(status),
            "changed_files": [],
            "diff_summary": "not a git repository",
        }
    untracked = [
        path
        for line, path in zip(status.splitlines(), changed_files)
        if line.startswith("?? ")
    ]
    if untracked:
        untracked_summary = "\n".join(f"?? {path}" for path in untracked[:200])
        diff_stat = (
            diff_stat + "\n" + untracked_summary
            if diff_stat else untracked_summary
        )
    fingerprint = hashlib.sha256()
    fingerprint.update(head_revision.encode("utf-8", errors="replace"))
    fingerprint.update(status.encode("utf-8", errors="replace"))
    fingerprint.update(_run_git(["diff", "--binary", "HEAD"], workspace).encode("utf-8", errors="replace"))
    for relative_path in sorted(untracked):
        path = Path(workspace) / relative_path
        fingerprint.update(relative_path.encode("utf-8", errors="replace"))
        if path.is_file():
            try:
                fingerprint.update(path.read_bytes())
            except OSError:
                fingerprint.update(b"<unreadable>")
    clean = not status.strip()
    workspace_revision = (
        f"git:{head_revision}"
        if clean and head_revision
        else f"git:{head_revision or 'unknown'}:worktree:{fingerprint.hexdigest()}"
    )
    attempt_changed_files = list(changed_files)
    if baseline is not None:
        baseline_files = {
            str(path)
            for path in baseline.get("changed_files") or []
            if str(path).strip()
        }
        baseline_fingerprints = baseline.get("path_fingerprints") or {}
        current_files = set(changed_files)
        attempt_changed_files = sorted(
            path
            for path in baseline_files | current_files
            if (
                path not in baseline_files
                or path not in current_files
                or baseline_fingerprints.get(path) != _git_path_fingerprint(workspace, path)
            )
        )
        baseline_head = str(baseline.get("head_revision") or "").strip()
        if baseline_head and head_revision and baseline_head != head_revision:
            committed_delta = _run_git(
                ["diff", "--name-only", "--diff-filter=ACDMRTUXB", baseline_head, head_revision],
                workspace,
            )
            attempt_changed_files = sorted(
                set(attempt_changed_files)
                | {path.strip() for path in committed_delta.splitlines() if path.strip()}
            )
    return {
        "status": _cap(status),
        "changed_files": changed_files[:200],
        "attempt_changed_files": attempt_changed_files[:200],
        "diff_summary": _cap(diff_stat),
        "head_revision": head_revision,
        "workspace_revision": workspace_revision,
    }


def _extract_verification_summary(output: str) -> dict[str, Any]:
    blocks: list[list[str]] = []
    current: list[str] = []
    capture = False
    for line in output.splitlines():
        lower = line.strip().lower()
        if lower.startswith("verification:"):
            if capture and current:
                blocks.append(current)
            current = []
            capture = True
            continue
        if capture and lower.endswith(":") and not lower.startswith(("command:", "result:")):
            blocks.append(current)
            current = []
            capture = False
            continue
        if capture:
            current.append(line)
    if capture and current:
        blocks.append(current)

    candidates: list[dict[str, Any]] = []
    for block in blocks:
        lines: list[str] = []
        commands: list[str] = []
        for line in block:
            stripped = line.strip()
            if not stripped:
                continue
            lowered = stripped.lower()
            is_command = lowered.startswith("- command:") or lowered.startswith("command:")
            is_result = lowered.startswith("- result:") or lowered.startswith("result:")
            value = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
            value = value.strip("`").strip()
            if (is_command or is_result) and _is_placeholder_progress_text(value):
                continue
            lines.append(line)
            if is_command and value:
                commands.append(value)
        summary = "\n".join(lines).strip()
        if summary or commands:
            candidates.append({
                "commands": commands[:20],
                "summary": _cap(summary),
            })

    if not candidates:
        return {"commands": [], "summary": ""}
    return candidates[-1]


def _receipt_section_key(raw_line: str) -> Optional[str]:
    match = _RECEIPT_SECTION_RE.match(_normalize_markdown_block_line(raw_line))
    if not match:
        return None
    label = match.group(1).strip().lower().replace(" ", "_")
    return label if label in _RECEIPT_SECTION_KEYS else None


def _materialize_receipt_sections(
    sections: dict[str, list[str]],
) -> dict[str, str]:
    return {
        key: _cap("\n".join(lines).strip())
        for key, lines in sections.items()
        if "\n".join(lines).strip()
    }


def _extract_receipt_sections(output: str) -> dict[str, str]:
    """Extract the last real Codex receipt block from stdout/stderr tail.

    Codex CLI output commonly includes the prompt itself before the final
    assistant answer.  The prompt contains the receipt template, so a simple
    "first header wins" parser can accidentally ingest much of the session log
    as ``recommended_reviewer_action``.  Treat each ``Progress:`` header as the
    start of a candidate receipt and keep the last candidate with non-placeholder
    content.
    """

    candidates: list[dict[str, str]] = []
    sections: Optional[dict[str, list[str]]] = None
    current_key: Optional[str] = None
    for raw_line in (output or "").splitlines():
        normalized_line = _normalize_markdown_block_line(raw_line)
        if _VERDICT_LINE_RE.match(normalized_line) or _VERDICT_HEADER_RE.match(normalized_line):
            continue
        label = _receipt_section_key(raw_line)
        if label:
            if label == "progress":
                if sections:
                    materialized = _materialize_receipt_sections(sections)
                    if materialized:
                        candidates.append(materialized)
                sections = {"progress": []}
            elif sections is None:
                sections = {}
            current_key = label
            sections.setdefault(label, [])
            continue
        if sections is not None and current_key is not None:
            sections[current_key].append(raw_line)
    if sections:
        materialized = _materialize_receipt_sections(sections)
        if materialized:
            candidates.append(materialized)

    useful: list[dict[str, str]] = []
    for candidate in candidates:
        progress_items = parse_progress_items(candidate.get("progress", ""))
        has_non_placeholder_content = any(
            value.strip() and not _is_placeholder_progress_text(value.strip())
            for key, value in candidate.items()
            if key != "progress"
        )
        if progress_items or has_non_placeholder_content:
            useful.append(candidate)
    return useful[-1] if useful else {}


def _extract_structured_verdict(output: str) -> Optional[str]:
    def normalize(raw: str) -> Optional[str]:
        value = raw.strip().lower().replace("-", "_")
        return value if value in _ALLOWED_STRUCTURED_VERDICTS else None

    lines = (output or "").splitlines()
    verdicts: list[str] = []
    for index, line in enumerate(lines):
        normalized_line = _normalize_markdown_block_line(line)
        match = _VERDICT_LINE_RE.match(normalized_line)
        if match:
            verdict = normalize(match.group(1))
            if verdict:
                verdicts.append(verdict)
            continue
        if _VERDICT_HEADER_RE.match(normalized_line):
            for next_line in lines[index + 1 : index + 4]:
                normalized_next_line = _normalize_markdown_block_line(next_line)
                if not normalized_next_line.strip():
                    continue
                bullet = _VERDICT_BULLET_RE.match(normalized_next_line)
                if bullet:
                    verdict = normalize(bullet.group(1))
                    if verdict:
                        verdicts.append(verdict)
                break
    return verdicts[-1] if verdicts else None


def _structured_verdict_from_line(line: str) -> Optional[str]:
    match = _VERDICT_LINE_RE.match(_normalize_markdown_block_line(line))
    if not match:
        return None
    value = match.group(1).strip().lower().replace("-", "_")
    return value if value in _ALLOWED_STRUCTURED_VERDICTS else None


def _review_output_header_key(raw_line: str) -> Optional[str]:
    stripped = _normalize_markdown_block_line(raw_line)
    if not stripped.endswith(":"):
        return None
    label = stripped[:-1].strip().lower().replace(" ", "_").replace("-", "_")
    return label if label in _REVIEW_OUTPUT_START_HEADERS else None


def _format_receipt_tail(receipt: dict[str, Any]) -> str:
    sections = receipt.get("sections") if isinstance(receipt, dict) else None
    if not isinstance(sections, dict):
        return ""
    receipt_lines: list[str] = []
    for label, key in (
        ("Progress", "progress"),
        ("Changed files", "changed_files"),
        ("Verification", "verification"),
        ("Remaining risks", "remaining_risks"),
        ("Recommended reviewer action", "recommended_reviewer_action"),
    ):
        value = sections.get(key)
        if isinstance(value, str) and value.strip():
            receipt_lines.append(f"{label}:")
            receipt_lines.extend(value.strip().splitlines())
            receipt_lines.append("")
    verdict = receipt.get("verdict")
    if isinstance(verdict, str) and verdict.strip():
        receipt_lines.append(f"Verdict: {verdict.strip()}")
    tail = "\n".join(receipt_lines).strip()
    return _cap(tail, CODEX_OUTPUT_TAIL_BYTES) if tail else ""


def _review_output_tail(output: str, receipt: dict[str, Any]) -> str:
    """Return bounded reviewable worker output without the echoed prompt prefix."""
    lines = (output or "").splitlines()
    if not lines:
        return ""

    start_index: Optional[int] = None
    verdict_index: Optional[int] = None
    message_boundary = -1
    for index, line in enumerate(lines):
        if line.strip().lower() == "codex":
            message_boundary = index
    for index in range(len(lines) - 1, -1, -1):
        if index <= message_boundary:
            break
        label = _receipt_section_key(lines[index])
        if label == "progress":
            start_index = index
            break
        if _review_output_header_key(lines[index]):
            start_index = index
        if verdict_index is None and _structured_verdict_from_line(lines[index]):
            verdict_index = index

    tail_lines = (
        lines[start_index:]
        if start_index is not None
        else lines[verdict_index:]
        if verdict_index is not None
        else lines[-CODEX_REVIEW_OUTPUT_TAIL_LINES:]
    )
    tail = "\n".join(tail_lines).strip()
    formatted_receipt_tail = _format_receipt_tail(receipt)
    if start_index is not None and formatted_receipt_tail:
        return formatted_receipt_tail
    if verdict_index is not None and formatted_receipt_tail:
        return formatted_receipt_tail
    if tail:
        return _cap(tail, CODEX_OUTPUT_TAIL_BYTES)

    if formatted_receipt_tail:
        return formatted_receipt_tail
    return _cap("\n".join(lines[-CODEX_REVIEW_OUTPUT_TAIL_LINES:]).strip(), CODEX_OUTPUT_TAIL_BYTES)


def _extract_worker_receipt(output: str) -> dict[str, Any]:
    sections = _extract_receipt_sections(output)
    receipt: dict[str, Any] = {
        "schema": "codex_cli_receipt_v1",
        "sections": sections,
    }
    verdict = _extract_structured_verdict(output)
    if verdict:
        receipt["verdict"] = verdict
    if "changed_files" in sections:
        receipt["changed_files_text"] = sections["changed_files"]
    if "remaining_risks" in sections:
        receipt["remaining_risks"] = sections["remaining_risks"]
    if "recommended_reviewer_action" in sections:
        receipt["recommended_reviewer_action"] = sections["recommended_reviewer_action"]
    return receipt


def _extract_runtime_receipt(output: str) -> Optional[dict[str, Any]]:
    """Extract the final explicit runtime receipt envelope from worker output."""
    for match in reversed(list(_JSON_FENCE_RE.finditer(output or ""))):
        try:
            candidate = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("schema") in {
            "runtime_worker_receipt_v1",
            "runtime_worker_structure_checkpoint_v1",
        }:
            return candidate
    return None


def _adapt_missing_phase4g8_runtime_receipt(
    task_id: str,
    *,
    receipt: dict[str, Any],
    git_evidence: dict[str, Any],
    verification: dict[str, Any],
    output_tail: str,
) -> Optional[dict[str, Any]]:
    """Build a non-verifying receipt for a completed Phase 4G8 Codex turn."""

    if not os.environ.get("HERMES_PHASE4G8_RUN_ID"):
        return None
    try:
        from hermes_cli import kanban_db as kb

        with kb.connect() as conn:
            node = conn.execute(
                "SELECT job_id, metadata_json FROM execution_nodes WHERE latest_task_id = ?",
                (task_id,),
            ).fetchone()
            if node is None:
                return None
            metadata = json.loads(node["metadata_json"] or "{}")
            goal_keys = [str(value) for value in metadata.get("goal_item_keys") or [] if str(value).strip()]
            gap_keys = [str(value) for value in metadata.get("gap_keys") or [] if str(value).strip()]
            if not goal_keys and gap_keys:
                placeholders = ",".join("?" for _ in gap_keys)
                goal_keys = [
                    str(row["item_key"])
                    for row in conn.execute(
                        f"""
                        SELECT DISTINCT gi.item_key
                          FROM goal_gaps gg
                          JOIN goal_items gi ON gi.id = gg.goal_item_id
                         WHERE gg.job_id = ? AND gg.gap_key IN ({placeholders})
                         ORDER BY gi.item_key
                        """,
                        (node["job_id"], *gap_keys),
                    ).fetchall()
                ]
        if not goal_keys or not git_evidence.get("workspace_revision"):
            return None
        sections = receipt.get("sections") if isinstance(receipt.get("sections"), dict) else {}
        summary = str(sections.get("progress") or output_tail or "Codex execution completed").strip()
        verification_summary = str(verification.get("summary") or "No explicit worker verification result").strip()
        return {
            "schema": "runtime_worker_receipt_v1",
            "verdict": "pass",
            "summary": _cap(summary, 2000),
            "claimed_goal_items": goal_keys,
            "partial_goal_items": [],
            "unmet_goal_items": [],
            "contradicted_goal_items": [],
            "changed_files": list(git_evidence.get("changed_files") or []),
            "verification": {
                "passed": False,
                "summary": _cap(verification_summary, 2000),
                "adapter_requires_independent_verification": True,
            },
            "artifacts": [],
            "workspace_revision": git_evidence["workspace_revision"],
            "receipt_adapter": "phase4g8_missing_envelope_v1",
        }
    except Exception:
        return None


def _metadata(
    *,
    lane: str,
    task_id: str,
    run_id: Optional[int],
    worker_pid: int,
    claim_lock: Optional[str],
    workspace: str,
    model: Optional[str],
    exit_code: Optional[int],
    timed_out: bool,
    output_tail: str,
    binary_missing: bool = False,
    json_events: bool = False,
    execution_mode: str = "fresh",
    backend_session_id: Optional[str] = None,
    resume_status: Optional[str] = None,
    git_baseline: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    succeeded = (exit_code == 0 and not timed_out and not binary_missing)
    receipt = _extract_worker_receipt(output_tail)
    runtime_receipt = _extract_runtime_receipt(output_tail)
    verification = _extract_verification_summary(output_tail)
    review_output_tail = _review_output_tail(output_tail, receipt)
    git_evidence = collect_git_evidence(workspace, baseline=git_baseline)
    if runtime_receipt is None and succeeded:
        runtime_receipt = _adapt_missing_phase4g8_runtime_receipt(
            task_id,
            receipt=receipt,
            git_evidence=git_evidence,
            verification=verification,
            output_tail=review_output_tail,
        )
    if isinstance(runtime_receipt, dict) and git_evidence.get("workspace_revision"):
        runtime_receipt = dict(runtime_receipt)
        runtime_receipt["worker_declared_changed_files"] = list(runtime_receipt.get("changed_files") or [])
        runtime_receipt["changed_files"] = list(git_evidence.get("attempt_changed_files") or [])
        runtime_receipt["changed_files_source"] = (
            "wrapper_git_attempt_delta" if git_baseline is not None else "wrapper_git_workspace_delta"
        )
        runtime_receipt["workspace_revision"] = git_evidence["workspace_revision"]
    if isinstance(runtime_receipt, dict):
        try:
            from hermes_cli import kanban_db as kb
            from hermes_cli import kanban_runtime_kernel as rk

            with kb.connect() as conn:
                runtime_receipt = rk.bind_runtime_receipt_provenance(
                    conn,
                    task_id,
                    runtime_receipt,
                    backend_session_id=backend_session_id,
                )
        except Exception:
            pass
    if receipt.get("verdict"):
        verification["verdict"] = receipt["verdict"]
    return {
        "worker_instance": {
            "worker_lane": lane,
            "worker_kind": "codex_cli",
            "task_id": task_id,
            "run_id": run_id,
            "worker_pid": worker_pid,
            "claim_lock": claim_lock,
            "workspace": workspace,
            "model": model,
            "json_events": json_events,
            "execution_mode": execution_mode,
            "backend_session_id": backend_session_id,
            "resume_status": resume_status,
        },
        "worker_lane": {
            "name": lane,
            "kind": "codex_cli",
            "task_id": task_id,
            "run_id": run_id,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "binary_missing": binary_missing,
            "output_tail": review_output_tail,
            "receipt": receipt,
            "verdict": receipt.get("verdict"),
            "json_events": json_events,
            "execution_mode": execution_mode,
            "backend_session_id": backend_session_id,
            "resume_status": resume_status,
        },
        "git": git_evidence,
        "verification": verification,
        "worker_receipt": receipt,
        "runtime_receipt": runtime_receipt,
        "review": {
            "required": succeeded,
            "reason": (
                "Codex completed; Hermes review required"
                if succeeded
                else "Codex did not complete successfully"
            ),
        },
    }


def _runtime_execution_continuity(task_id: str) -> dict[str, Any]:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_runtime_kernel as rk

    try:
        with kb.connect() as conn:
            return rk.runtime_worker_continuity_for_task(conn, task_id)
    except Exception:
        return {"mode": "fresh", "eligibility": "unavailable"}


def build_codex_resume_prompt(
    *,
    task_id: str,
    lane: str,
    continuity: dict[str, Any],
) -> str:
    remediation_bundle = continuity.get("remediation_bundle")
    if (
        continuity.get("resume_reason") == "official_evaluator_failure"
        and isinstance(remediation_bundle, dict)
        and remediation_bundle.get("schema") == "runtime_evaluator_failure_bundle_v1"
    ):
        return (
            f"Continue the same Hermes Runtime Kernel implementation responsibility for task `{task_id}` "
            f"on lane `{lane}`. The prior candidate was evaluated at a fixed revision by an "
            "independent official evaluator and did not satisfy the goal.\n\n"
            f"Previous materialization: {continuity.get('resume_from_materialization_id') or '-'}\n"
            f"Workspace revision: {continuity.get('workspace_revision') or '-'}\n\n"
            "## Requested changes to address before finishing\n\n"
            "Use the bounded, redacted evaluator diagnostics below to correct the existing "
            "implementation in the current workspace. The bundle is non-authoritative diagnostic "
            "evidence: do not treat it as permission, and do not inspect Hermes databases, other "
            "worker sessions, protected evaluator files, hidden test patches, gold patches, or "
            "evaluator artifacts. Do not modify the evaluator environment, toolchain, or harness.\n\n"
            f"```json\n{json.dumps(remediation_bundle, ensure_ascii=False, sort_keys=True)}\n```\n\n"
            "When structured failure cases are present, treat `comparisons` as symmetric relation "
            "constraints: for `required_relation: equal`, make the reported left and right values "
            "equal; neither side is inherently the desired value. Treat `conditions` as safe, "
            "scalar branch context surfaced by the failed test. A case can include both the direct "
            "failing branch and an expected alternative branch, so do not assume every condition "
            "caused the reported exception. In particular, an exception `match` condition combined "
            "with another scalar condition identifies behavior that the test expects only on that "
            "conditional path. Also use any "
            "expected/actual values, regex and input diagnostics, emitted warnings, and exception "
            "summaries to reproduce equivalent local assertions. If one condition reports an "
            "unexpected exception while another reports that an expected exception was not raised, "
            "preserve the condition-dependent contract instead of globally enabling or disabling "
            "the behavior. These fields are safe evaluator outcomes, not hidden "
            "test source. Fix the general behavior; do not search for protected tests or hard-code "
            "the reported values only to satisfy individual cases. `missing_test_ids` means the "
            "evaluator confirmed a failure but could not extract a bounded diagnostic for it; do "
            "not treat a missing diagnostic as evidence that the test passed or invent an exact "
            "contract that was not reported.\n\n"
            "The evaluator and worker use the same locked dependency environment. Do not dismiss "
            "a reproducible evaluator dependency or import failure as environment-only without "
            "demonstrating a fingerprint mismatch. Resume from the existing workspace and session "
            "context, fix the concrete failures, "
            "run the strongest available local verification, and finish with the full Markdown "
            "receipt and final `runtime_worker_receipt_v1` fenced JSON object required by the "
            "original task. If evidence reveals a durable capability, human authority, workspace, "
            "or independent-verification boundary, return a terminal `structure_request`; do not "
            "create or complete runtime nodes directly."
        )
    return (
        f"Continue the same Hermes Runtime Kernel worker responsibility for task `{task_id}` "
        f"on lane `{lane}`. The prior materialization ended because of infrastructure failure.\n\n"
        f"Previous materialization: {continuity.get('resume_from_materialization_id') or '-'}\n"
        f"Workspace revision: {continuity.get('workspace_revision') or '-'}\n\n"
        "Resume from the existing workspace and session context. Re-check the original node "
        "acceptance criteria, complete any unfinished implementation and verification, and do "
        "not treat partial prior progress as terminal success. Finish with the full Markdown "
        "receipt and final `runtime_worker_receipt_v1` fenced JSON object required by the original "
        "task. Do not create or complete runtime nodes directly."
    )


def _finish_blocked(
    *,
    task_id: str,
    run_id: Optional[int],
    reason: str,
    metadata: dict[str, Any],
) -> bool:
    from hermes_cli import kanban_db as kb

    with kb.connect() as conn:
        return kb.block_task(
            conn,
            task_id,
            reason=reason,
            expected_run_id=run_id,
            metadata=metadata,
        )


def build_codex_prompt(task_context: str, *, lane: str, model: Optional[str]) -> str:
    is_review_followup = "## Required review output" in task_context
    is_test_followup = "## Required test output" in task_context
    is_structure_assessment = "Early structure assessment mode:" in task_context
    is_runtime_contribution = "Runtime contribution boundary:" in task_context
    is_runtime_integration = "Frozen dependency contributions:" in task_context
    if is_structure_assessment:
        role_lines = (
            f"You are Codex CLI running as Hermes Kanban assessment lane `{lane}`.\n"
            "Inspect the assigned repository and goal, but do not modify source or tests. "
            "Your only responsibility in this attempt is to decide whether durable Runtime "
            "decomposition has evidence-backed value. Return the required structure checkpoint; "
            "do not claim implementation completion."
        )
    elif is_review_followup:
        role_lines = (
            f"You are Codex CLI running as Hermes Kanban review lane `{lane}`.\n"
            "Review the bounded implementation evidence and workspace diff. "
            "Do not implement feature work or modify files unless the review "
            "task explicitly asks for a verification-only scratch artifact.\n"
            "Run only the minimal inspection commands needed, then emit the "
            "required structured review output and final `Verdict: ...` line."
        )
    elif is_test_followup:
        role_lines = (
            f"You are Codex CLI running as Hermes Kanban test lane `{lane}`.\n"
            "Verify the bounded implementation evidence with deterministic "
            "commands. Do not implement feature work or modify files unless "
            "the test task explicitly asks for a verification-only scratch "
            "artifact.\n"
            "Run the smallest sufficient verification, then emit the required "
            "structured test output and final `Verdict: ...` line."
        )
    else:
        role_lines = (
            f"You are Codex CLI running as Hermes Kanban worker lane `{lane}`.\n"
            "Implement the assigned task in the workspace. Do not mark the "
            "Kanban task done yourself; this wrapper will return your "
            "structured receipt to Hermes and block the task for review."
        )
    runtime_receipt_instructions = ""
    if is_structure_assessment:
        runtime_receipt_instructions = """

This is an early Runtime structure assessment. Emit one final fenced JSON
object and no prose after it:

```json
{
  "schema": "runtime_worker_structure_checkpoint_v1",
  "kind": "early_structure_assessment",
  "recommendation": "continue_single_node",
  "summary": "short evidence-backed assessment",
  "inspected_scope": ["workspace-relative/path"],
  "repository_facts": [
    {"fact": "observed structural fact", "evidence_refs": ["workspace:path:..."]}
  ],
  "proposed_nodes": [],
  "integration_owner_node_key": "node key from Runtime footer",
  "shared_integration_scope": [],
  "risks": [],
  "worker_session_should_resume": true,
  "changed_files": []
}
```

Use recommendation `expand` only for 2-3 low-coupling responsibilities with
non-overlapping declared write scopes. Each proposed node must include
`node_key`, `outcome`, `acceptance_criteria`, `declared_write_scope`, and
`requested_capabilities`. Use only Runtime capability names from the task
context; typical local implementation capabilities are `filesystem_read`,
`workspace_write`, `git_read`, and `process_spawn`. Otherwise use
`continue_single_node` and leave `proposed_nodes` empty.
"""
    elif "Runtime footer:" in task_context:
        runtime_receipt_instructions = """

This is a Runtime Kernel node. After the normal Markdown receipt, emit one
final fenced JSON object and no prose after it:

```json
{
  "schema": "runtime_worker_receipt_v1",
  "verdict": "pass",
  "summary": "short factual result",
  "claimed_goal_items": ["only keys listed under Goal items"],
  "partial_goal_items": [],
  "unmet_goal_items": [],
  "changed_files": ["workspace-relative/path"],
  "verification": {"passed": true, "summary": "command and result"},
  "artifacts": []
}
```

Use only goal keys listed in the task context. Do not claim success merely
because the process exits successfully. If verification did not pass, use an
appropriate non-pass verdict and list unmet goal items. `changed_files` must be
a JSON array of workspace-relative paths. If terminal evidence reveals a
durable structural boundary, keep the normal verdict and add a separate
`structure_request`; do not use it as a new verdict or create runtime nodes.
For a Runtime node with a required independent evaluator, use verdict
`candidate_ready` after local verification. This means the fixed revision is
ready for external evaluation; it is not final goal completion.
"""
        if is_runtime_integration:
            runtime_receipt_instructions = runtime_receipt_instructions.replace(
                '  "artifacts": []\n}',
                '  "artifacts": [],\n'
                '  "accepted_contributions": ["artifact IDs applied without modification"],\n'
                '  "modified_contributions": ["artifact IDs adapted during integration"],\n'
                '  "rejected_contributions": ["artifact IDs not integrated"]\n}',
            )
            runtime_receipt_instructions += """

Integration receipt requirement: classify every artifact ID from the frozen
dependency contribution bundle exactly once across accepted_contributions,
modified_contributions, and rejected_contributions. Use accepted only when the
integrated files still match the frozen contribution hashes; use modified when
you adapted that contribution. Unknown, duplicated, or omitted IDs invalidate
the receipt's goal claim.
"""
        if is_runtime_contribution:
            runtime_receipt_instructions += """

Contribution exception: this isolated child is not the integrated candidate.
Use verdict `succeeded`, not `candidate_ready`, after local verification. Any
goal-item claim is non-authoritative partial evidence until the primary
integration owner consumes the frozen contribution.
"""
    if is_structure_assessment:
        return f"""{task_context.rstrip()}

## External worker instructions

{role_lines}
{runtime_receipt_instructions}
"""
    return f"""{task_context.rstrip()}

## External worker instructions

{role_lines}

If the task context contains an actual markdown section headed exactly
"## Requested changes to address before finishing", treat only that section as
mandatory retry feedback. Do not infer retry mode from the phrase appearing in
ordinary task instructions or examples. Fix those items first and include the
verification you ran for the requested changes in your receipt.

When finished, print a concise structured receipt:

Progress:
- [x] ...
- [ ] ...

Changed files:
- ...

Verification:
- command: ...
  result: ...

Remaining risks:
- ...

Recommended reviewer action:
- ...

If this is an independent review or test follow-up task, the task body will
contain a "Required review output" or "Required test output" section. Follow
that section exactly and include one final `Verdict: ...` line. Review verdicts
must be one of `approve`, `request_changes`, or `blocked`; test verdicts must
be one of `pass`, `fail`, or `blocked`.
{runtime_receipt_instructions}
"""


def run_codex_worker(
    *,
    task_id: str,
    lane: str,
    workspace: str,
    sandbox: str,
    approval: str,
    model: Optional[str] = None,
    run_id: Optional[int] = None,
    claim_lock: Optional[str] = None,
    board: Optional[str] = None,
    success_policy: str = "block_for_review",
    timeout_seconds: Optional[float] = None,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    json_events: bool = False,
    network_namespace: Optional[str] = None,
    network_uid: int = 65534,
    network_gid: int = 65534,
) -> int:
    from hermes_cli import kanban_db as kb

    log_path = kb.worker_log_path(task_id, board=board)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    worker_pid = os.getpid()
    tail = _TailBuffer()
    last_progress_json = ""
    continuity = _runtime_execution_continuity(task_id)
    execution_mode = str(continuity.get("mode") or "fresh")
    resume_session_id = (
        str(continuity.get("resume_session_id"))
        if execution_mode == "resume" and continuity.get("resume_session_id")
        else None
    )
    is_runtime_materialization = continuity.get("eligibility") not in {
        "not_runtime_materialization",
        "unavailable",
    }
    effective_json_events = bool(json_events or is_runtime_materialization or resume_session_id)
    backend_session_id = resume_session_id
    resume_status = "pending" if resume_session_id else None
    git_baseline = capture_git_change_baseline(workspace)
    started = time.monotonic()
    approval_config_audit = _codex_approval_config_audit(os.environ.get("CODEX_HOME"))

    with open(log_path, "a", encoding="utf-8", errors="replace") as log_f:
        header = {
            "worker_lane": lane,
            "worker_kind": "codex_cli",
            "task_id": task_id,
            "run_id": run_id,
            "worker_pid": worker_pid,
            "claim_lock": claim_lock,
            "workspace": workspace,
            "model": model,
            "json_events": effective_json_events,
            "execution_mode": execution_mode,
            "backend_session_id": backend_session_id,
            "approval_policy": approval,
            "approval_config": approval_config_audit,
        }
        _write_log(log_f, "[codex-worker] " + json.dumps(header, ensure_ascii=False) + "\n")
        _record_event(task_id, "worker_started", header, run_id=run_id)
        _heartbeat(
            task_id,
            run_id=run_id,
            claim_lock=claim_lock,
            lane=lane,
            execution_mode=execution_mode,
            backend_session_id=backend_session_id,
        )

        codex_bin = shutil.which("codex")
        if not codex_bin:
            msg = "codex binary not found on PATH"
            _write_log(log_f, f"[codex-worker] {msg}\n")
            meta = _metadata(
                lane=lane,
                task_id=task_id,
                run_id=run_id,
                worker_pid=worker_pid,
                claim_lock=claim_lock,
                workspace=workspace,
                model=model,
                exit_code=None,
                timed_out=False,
                output_tail=msg,
                binary_missing=True,
                json_events=effective_json_events,
                execution_mode=execution_mode,
                backend_session_id=backend_session_id,
                resume_status=resume_status,
                git_baseline=git_baseline,
            )
            _record_event(task_id, "worker_failed", meta["worker_lane"], run_id=run_id)
            _finish_blocked(
                task_id=task_id,
                run_id=run_id,
                reason="codex-failed: codex binary not found on PATH",
                metadata=meta,
            )
            return 0

        with kb.connect() as conn:
            task_context = kb.build_worker_context(conn, task_id)
        prompt = (
            build_codex_resume_prompt(task_id=task_id, lane=lane, continuity=continuity)
            if resume_session_id
            else build_codex_prompt(task_context, lane=lane, model=model)
        )
        argv = build_codex_argv(
            binary=codex_bin,
            workspace=workspace,
            sandbox=sandbox,
            approval=approval,
            model=model,
            json_events=effective_json_events,
            resume_session_id=resume_session_id,
        )
        codex_env = _safe_env_for_codex(workspace)
        argv = wrap_codex_network_argv(
            argv,
            network_namespace,
            uid=network_uid,
            gid=network_gid,
            workspace=workspace,
            worker_env=codex_env,
            filesystem_isolation=bool(network_namespace),
        )
        _write_log(log_f, "[codex-worker] exec " + json.dumps(argv, ensure_ascii=False) + "\n")

        try:
            proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell.
                argv,
                cwd=workspace if os.path.isdir(workspace) else None,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=codex_env,
                close_fds=True,
            )
        except OSError as exc:
            msg = f"failed to start codex: {exc}"
            _write_log(log_f, f"[codex-worker] {msg}\n")
            meta = _metadata(
                lane=lane,
                task_id=task_id,
                run_id=run_id,
                worker_pid=worker_pid,
                claim_lock=claim_lock,
                workspace=workspace,
                model=model,
                exit_code=None,
                timed_out=False,
                output_tail=msg,
                json_events=effective_json_events,
                execution_mode=execution_mode,
                backend_session_id=backend_session_id,
                resume_status="failed" if resume_session_id else resume_status,
                git_baseline=git_baseline,
            )
            if resume_session_id:
                _record_event(
                    task_id,
                    "worker_backend_session_resume_failed",
                    {
                        "worker_lane": lane,
                        "backend_session_id": resume_session_id,
                        "reason": str(exc),
                    },
                    run_id=run_id,
                )
            _record_event(task_id, "worker_failed", meta["worker_lane"], run_id=run_id)
            _finish_blocked(
                task_id=task_id,
                run_id=run_id,
                reason=f"codex-failed: {exc}",
                metadata=meta,
            )
            return 0
        _record_event(
            task_id,
            "worker_spawned_external",
            {
                "worker_lane": lane,
                "worker_kind": "codex_cli",
                "run_id": run_id,
                "pid": proc.pid,
                "model": model,
                "execution_mode": execution_mode,
                "backend_session_id": backend_session_id,
            },
            run_id=run_id,
        )
        try:
            assert proc.stdin is not None
            proc.stdin.write(prompt)
            proc.stdin.close()
        except BrokenPipeError:
            pass

        q: "queue.Queue[Optional[str]]" = queue.Queue()

        def _reader() -> None:
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    q.put(line)
            finally:
                q.put(None)

        reader = threading.Thread(target=_reader, name="codex-worker-reader", daemon=True)
        reader.start()
        next_heartbeat = time.monotonic() + max(1.0, float(heartbeat_interval))
        timed_out = False
        reader_done = False
        resume_identity_mismatch = False
        session_event_recorded = False
        terminal_event_at: Optional[float] = None
        runtime_receipt_source = ""
        while True:
            try:
                item = q.get(timeout=0.1)
            except queue.Empty:
                item = ""
            if item is None:
                reader_done = True
            elif item:
                _write_log(log_f, item)
                parsed_json_event = False
                progress_source = item
                observed_session_id: Optional[str] = None
                observed_event_type: Optional[str] = None
                if effective_json_events:
                    (
                        parsed_json_event,
                        progress_source,
                        observed_session_id,
                        observed_event_type,
                        observed_runtime_receipt_source,
                    ) = _handle_codex_json_line(
                        item,
                        task_id=task_id,
                        lane=lane,
                        run_id=run_id,
                    )
                    if observed_runtime_receipt_source:
                        runtime_receipt_source = observed_runtime_receipt_source
                if observed_event_type in {"turn.completed", "turn.failed"}:
                    terminal_event_at = time.monotonic()
                if observed_session_id and not session_event_recorded:
                    if resume_session_id and observed_session_id != resume_session_id:
                        resume_identity_mismatch = True
                        _record_event(
                            task_id,
                            "worker_backend_session_resume_failed",
                            {
                                "worker_lane": lane,
                                "backend_session_id": resume_session_id,
                                "observed_session_id": observed_session_id,
                                "reason": "resumed session identity mismatch",
                            },
                            run_id=run_id,
                        )
                        try:
                            proc.terminate()
                        except OSError:
                            pass
                    else:
                        backend_session_id = observed_session_id
                        resume_status = "resumed" if resume_session_id else "started"
                        _record_event(
                            task_id,
                            "worker_backend_session_resumed" if resume_session_id else "worker_backend_session_started",
                            {
                                "worker_lane": lane,
                                "worker_kind": "codex_cli",
                                "backend_session_id": observed_session_id,
                                "execution_mode": execution_mode,
                                "resume_from_materialization_id": continuity.get("resume_from_materialization_id"),
                            },
                            run_id=run_id,
                        )
                        session_event_recorded = True
                if progress_source:
                    tail.append(progress_source)
                elif not parsed_json_event:
                    tail.append(item)
                items = parse_progress_items(tail.text())
                if items:
                    progress_payload = {
                        "worker_lane": lane,
                        "lane": lane,
                        "worker_kind": "codex_cli",
                        "run_id": run_id,
                        "items": items,
                    }
                    progress_json = json.dumps(progress_payload, ensure_ascii=False, sort_keys=True)
                    if progress_json != last_progress_json:
                        _record_event(
                            task_id,
                            "worker_progress",
                            progress_payload,
                            run_id=run_id,
                        )
                        last_progress_json = progress_json

            now = time.monotonic()
            if now >= next_heartbeat:
                _heartbeat(
                    task_id,
                    run_id=run_id,
                    claim_lock=claim_lock,
                    lane=lane,
                    execution_mode=execution_mode,
                    backend_session_id=backend_session_id,
                )
                next_heartbeat = now + max(1.0, float(heartbeat_interval))

            terminal_grace_active = bool(
                terminal_event_at is not None
                and now - terminal_event_at < CODEX_TERMINAL_EVENT_EXIT_GRACE_SECONDS
            )
            if (
                timeout_seconds is not None
                and now - started > float(timeout_seconds)
                and not terminal_grace_active
            ):
                timed_out = True
                _write_log(log_f, "[codex-worker] timeout exceeded; terminating codex\n")
                try:
                    proc.terminate()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                break

            if proc.poll() is not None and reader_done:
                break

        if not timed_out:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                timed_out = True
                proc.kill()
        reader.join(timeout=1)
        exit_code = proc.returncode
        output_tail = tail.text()
        metadata_output = output_tail
        if runtime_receipt_source and runtime_receipt_source not in output_tail:
            metadata_output = runtime_receipt_source + "\n" + output_tail
        meta = _metadata(
            lane=lane,
            task_id=task_id,
            run_id=run_id,
            worker_pid=worker_pid,
            claim_lock=claim_lock,
            workspace=workspace,
            model=model,
            exit_code=exit_code,
            timed_out=timed_out,
            output_tail=metadata_output,
            json_events=effective_json_events,
            execution_mode=execution_mode,
            backend_session_id=backend_session_id,
            resume_status=resume_status,
            git_baseline=git_baseline,
        )
        if timed_out:
            _record_event(task_id, "worker_timed_out", meta["worker_lane"], run_id=run_id)
            _finish_blocked(
                task_id=task_id,
                run_id=run_id,
                reason=f"codex-timeout: exceeded {timeout_seconds}s",
                metadata=meta,
            )
            return 0
        if resume_session_id and (
            resume_identity_mismatch
            or exit_code != 0
            or not session_event_recorded
            or backend_session_id != resume_session_id
        ):
            reason = (
                "resumed session identity mismatch"
                if resume_identity_mismatch
                else f"codex resume exited with code {exit_code}"
                if exit_code != 0
                else "codex resume did not emit the expected thread id"
            )
            _record_event(
                task_id,
                "worker_backend_session_resume_failed",
                {
                    "worker_lane": lane,
                    "backend_session_id": resume_session_id,
                    "observed_session_id": backend_session_id,
                    "reason": reason,
                },
                run_id=run_id,
            )
            meta["worker_instance"]["resume_status"] = "failed"
            meta["worker_lane"]["resume_status"] = "failed"
            _finish_blocked(
                task_id=task_id,
                run_id=run_id,
                reason=f"codex-resume-failed: {reason}",
                metadata=meta,
            )
            return 0
        if exit_code == 0:
            _record_event(task_id, "worker_review_required", meta["worker_lane"], run_id=run_id)
            reason = "review-required: Codex completed; Hermes review required"
            _finish_blocked(
                task_id=task_id,
                run_id=run_id,
                reason=reason,
                metadata=meta,
            )
            return 0

        _record_event(task_id, "worker_failed", meta["worker_lane"], run_id=run_id)
        _finish_blocked(
            task_id=task_id,
            run_id=run_id,
            reason=f"codex-failed: exit code {exit_code}",
            metadata=meta,
        )
    return 0


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m hermes_cli.codex_worker")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--task-id", required=True)
    run.add_argument("--lane", required=True)
    run.add_argument("--workspace", required=True)
    run.add_argument("--sandbox", required=True)
    run.add_argument("--approval", required=True)
    run.add_argument("--success-policy", default="block_for_review")
    run.add_argument("--model")
    run.add_argument("--run-id", type=int)
    run.add_argument("--claim-lock")
    run.add_argument("--board")
    run.add_argument("--timeout-seconds", type=float)
    run.add_argument("--heartbeat-interval", type=float, default=DEFAULT_HEARTBEAT_INTERVAL_SECONDS)
    run.add_argument("--json-events", action="store_true")
    run.add_argument("--network-namespace")
    run.add_argument("--network-uid", type=int, default=65534)
    run.add_argument("--network-gid", type=int, default=65534)
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    if args.cmd == "run":
        return run_codex_worker(
            task_id=args.task_id,
            lane=args.lane,
            workspace=args.workspace,
            sandbox=args.sandbox,
            approval=args.approval,
            model=args.model,
            run_id=args.run_id,
            claim_lock=args.claim_lock,
            board=args.board,
            success_policy=args.success_policy,
            timeout_seconds=args.timeout_seconds,
            heartbeat_interval=args.heartbeat_interval,
            json_events=bool(args.json_events),
            network_namespace=args.network_namespace,
            network_uid=args.network_uid,
            network_gid=args.network_gid,
        )
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
