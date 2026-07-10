"""Packaged daemon wrapper for the Kanban runtime supervisor."""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import json
import logging
import os
import signal
import socket
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

from agent.redact import redact_sensitive_text
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_kernel as rk


STATE_SCHEMA_VERSION = 1
logger = logging.getLogger(__name__)


class SupervisorAlreadyRunningError(RuntimeError):
    """Raised when the configured PID file belongs to a live process."""


@dataclass(frozen=True)
class RuntimeSupervisorDaemonConfig:
    board: Optional[str] = None
    interval_seconds: float = 5.0
    limit: int = 10
    lock_ttl_seconds: int = 60
    create_tasks: bool = True
    max_consecutive_errors: int = 5
    error_backoff_max_seconds: float = 60.0
    max_polls: int = 0
    pidfile: Optional[Path] = None
    state_file: Optional[Path] = None
    health_host: str = "127.0.0.1"
    health_port: Optional[int] = None
    readiness_timeout_seconds: Optional[float] = None

    def validate(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be greater than zero")
        if self.limit < 1:
            raise ValueError("limit must be at least 1")
        if self.lock_ttl_seconds < 1:
            raise ValueError("lock_ttl_seconds must be at least 1")
        if self.max_consecutive_errors < 1:
            raise ValueError("max_consecutive_errors must be at least 1")
        if self.error_backoff_max_seconds < self.interval_seconds:
            raise ValueError("error_backoff_max_seconds must be at least interval_seconds")
        if self.max_polls < 0:
            raise ValueError("max_polls cannot be negative")
        if self.health_port is not None and not 0 <= self.health_port <= 65535:
            raise ValueError("health_port must be between 0 and 65535")
        if self.health_port is not None and not _is_loopback_host(self.health_host):
            raise ValueError("runtime supervisor health endpoint must bind to loopback")
        if self.readiness_timeout_seconds is not None and self.readiness_timeout_seconds <= 0:
            raise ValueError("readiness_timeout_seconds must be greater than zero")
        if self.pidfile is not None and self.state_file is not None:
            if self.pidfile.expanduser().resolve() == self.state_file.expanduser().resolve():
                raise ValueError("pidfile and state_file must be different paths")


def default_runtime_supervisor_paths(board: Optional[str] = None) -> tuple[Path, Path]:
    """Return PID/state paths derived from the selected Kanban database."""

    runtime_dir = kb.kanban_db_path(board=board).parent / "runtime-supervisor"
    return runtime_dir / "supervisor.pid", runtime_dir / "state.json"


def new_runtime_supervisor_owner() -> str:
    """Return a process-unique DB lease owner; never restore this from disk."""

    host = socket.gethostname().replace(":", "-") or "unknown-host"
    return f"runtime-daemon:{host}:{os.getpid()}:{uuid.uuid4().hex[:10]}"


def _is_loopback_host(host: str) -> bool:
    value = (host or "").strip().lower()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def claim_runtime_supervisor_pidfile(path: Path, *, pid: Optional[int] = None) -> None:
    """Atomically claim a PID file, replacing it only when the old PID is stale."""

    current_pid = int(pid or os.getpid())
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                existing_pid = int(path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                existing_pid = -1
            if _pid_is_alive(existing_pid):
                raise SupervisorAlreadyRunningError(
                    f"runtime supervisor PID file {path} belongs to live PID {existing_pid}"
                )
            if attempt:
                raise SupervisorAlreadyRunningError(f"could not replace stale PID file {path}")
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        try:
            os.write(fd, f"{current_pid}\n".encode("ascii"))
        finally:
            os.close(fd)
        return
    raise SupervisorAlreadyRunningError(f"could not claim PID file {path}")


def release_runtime_supervisor_pidfile(path: Path, *, pid: Optional[int] = None) -> None:
    """Remove a PID file only when it still names this process."""

    current_pid = int(pid or os.getpid())
    try:
        existing_pid = int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError, ValueError):
        return
    if existing_pid != current_pid:
        return
    try:
        path.unlink()
    except OSError:
        pass


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    try:
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, encoded.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _safe_error(exc: BaseException) -> str:
    message = redact_sensitive_text(str(exc), force=True).replace("\n", " ").strip()
    fingerprint = hashlib.sha256(message.encode("utf-8")).hexdigest()[:12]
    return f"{type(exc).__name__}: poll failed (detail_sha256={fingerprint})"


def _summarize_poll(result: dict[str, Any]) -> dict[str, Any]:
    ticks = result.get("ticks") if isinstance(result.get("ticks"), list) else []
    reasons = Counter(
        str(tick.get("reason") or "unknown")
        for tick in ticks
        if isinstance(tick, dict) and tick.get("status") != "advanced"
    )
    return {
        "job_count": int(result.get("job_count") or len(ticks)),
        "advanced_count": int(result.get("advanced_count") or 0),
        "skipped_count": len([tick for tick in ticks if tick.get("status") != "advanced"]),
        "skip_reasons": dict(sorted(reasons.items())),
    }


class RuntimeSupervisorOperationalState:
    """Thread-safe operational state; never used for runtime correctness."""

    def __init__(self, owner: str, readiness_timeout_seconds: float):
        now = time.time()
        self._lock = threading.Lock()
        self._readiness_timeout_seconds = readiness_timeout_seconds
        self._data: dict[str, Any] = {
            "schema_version": STATE_SCHEMA_VERSION,
            "status": "starting",
            "owner": owner,
            "pid": os.getpid(),
            "started_at": now,
            "stopped_at": None,
            "last_poll_started_at": None,
            "last_poll_completed_at": None,
            "last_success_at": None,
            "poll_count": 0,
            "consecutive_errors": 0,
            "last_error": None,
            "last_result": None,
        }

    def update(self, **values: Any) -> None:
        with self._lock:
            self._data.update(values)

    def begin_poll(self) -> int:
        with self._lock:
            self._data["status"] = "running"
            self._data["poll_count"] += 1
            self._data["last_poll_started_at"] = time.time()
            return int(self._data["poll_count"])

    def poll_succeeded(self, result: dict[str, Any]) -> None:
        now = time.time()
        with self._lock:
            self._data.update(
                status="running",
                last_poll_completed_at=now,
                last_success_at=now,
                consecutive_errors=0,
                last_error=None,
                last_result=_summarize_poll(result),
            )

    def poll_failed(self, exc: BaseException) -> int:
        now = time.time()
        with self._lock:
            errors = int(self._data["consecutive_errors"]) + 1
            self._data.update(
                status="degraded",
                last_poll_completed_at=now,
                consecutive_errors=errors,
                last_error=_safe_error(exc),
                last_result=None,
            )
            return errors

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            payload = dict(self._data)
        now = time.time()
        status = str(payload["status"])
        last_success = payload.get("last_success_at")
        payload["live"] = status not in {"stopped", "failed"}
        payload["ready"] = bool(
            status == "running"
            and int(payload.get("consecutive_errors") or 0) == 0
            and last_success is not None
            and now - float(last_success) <= self._readiness_timeout_seconds
        )
        payload["readiness_timeout_seconds"] = self._readiness_timeout_seconds
        return payload


class _RuntimeSupervisorHealthServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], state: RuntimeSupervisorOperationalState):
        self.operational_state = state
        super().__init__(server_address, _RuntimeSupervisorHealthHandler)


class _RuntimeSupervisorHealthHandler(BaseHTTPRequestHandler):
    server: _RuntimeSupervisorHealthServer

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        state = self.server.operational_state.snapshot()
        if self.path == "/health/live":
            payload = {"status": "ok" if state["live"] else "stopped", "live": state["live"]}
            code = 200 if state["live"] else 503
        elif self.path == "/health/ready":
            payload = {
                "status": "ready" if state["ready"] else "not_ready",
                "ready": state["ready"],
                "last_success_at": state["last_success_at"],
                "consecutive_errors": state["consecutive_errors"],
            }
            code = 200 if state["ready"] else 503
        elif self.path == "/health":
            payload = {
                key: state[key]
                for key in (
                    "schema_version",
                    "status",
                    "owner",
                    "pid",
                    "started_at",
                    "last_poll_completed_at",
                    "last_success_at",
                    "poll_count",
                    "consecutive_errors",
                    "last_error",
                    "last_result",
                    "live",
                    "ready",
                )
            }
            code = 200 if state["live"] else 503
        else:
            payload = {"error": "not_found"}
            code = 404
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def start_runtime_supervisor_health_server(
    host: str,
    port: int,
    state: RuntimeSupervisorOperationalState,
) -> tuple[_RuntimeSupervisorHealthServer, threading.Thread]:
    if not _is_loopback_host(host):
        raise ValueError("runtime supervisor health endpoint must bind to loopback")
    server = _RuntimeSupervisorHealthServer((host, port), state)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.1},
        name="runtime-supervisor-health",
        daemon=True,
    )
    thread.start()
    return server, thread


def _install_signal_handlers(stop_event: threading.Event) -> dict[int, Any]:
    if threading.current_thread() is not threading.main_thread():
        return {}
    previous: dict[int, Any] = {}

    def stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, stop)
        except (OSError, ValueError):
            continue
    return previous


def _restore_signal_handlers(previous: dict[int, Any]) -> None:
    if threading.current_thread() is not threading.main_thread():
        return
    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)
        except (OSError, ValueError):
            pass


def run_runtime_supervisor_daemon(
    config: RuntimeSupervisorDaemonConfig,
    *,
    decision_provider: Optional[Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]] = None,
    stop_event: Optional[threading.Event] = None,
    owner: Optional[str] = None,
    poll_once: Optional[Callable[[str], dict[str, Any]]] = None,
    on_poll: Optional[Callable[[dict[str, Any]], None]] = None,
) -> dict[str, Any]:
    """Run the packaged supervisor loop until signalled or bounded completion."""

    config.validate()
    pidfile_default, state_file_default = default_runtime_supervisor_paths(config.board)
    pidfile = config.pidfile or pidfile_default
    state_file = config.state_file or state_file_default
    owner_id = owner or new_runtime_supervisor_owner()
    readiness_timeout = config.readiness_timeout_seconds or max(
        config.interval_seconds * 3,
        config.lock_ttl_seconds * 2,
    )
    operational_state = RuntimeSupervisorOperationalState(owner_id, readiness_timeout)
    shutdown = stop_event or threading.Event()
    signal_handlers: dict[int, Any] = {}
    health_server: Optional[_RuntimeSupervisorHealthServer] = None
    health_thread: Optional[threading.Thread] = None
    pid_claimed = False
    fatal = False
    exit_reason = "stopped"

    if poll_once is None:

        def poll_once(current_owner: str) -> dict[str, Any]:
            with contextlib.closing(kb.connect(board=config.board)) as conn:
                return rk.supervise_runtime_jobs_once(
                    conn,
                    owner=current_owner,
                    limit=config.limit,
                    board=config.board,
                    create_tasks=config.create_tasks,
                    decision_provider=decision_provider,
                    lock_ttl_seconds=config.lock_ttl_seconds,
                )

    try:
        claim_runtime_supervisor_pidfile(pidfile)
        pid_claimed = True
        _atomic_write_json(state_file, operational_state.snapshot())
        if config.health_port is not None:
            health_server, health_thread = start_runtime_supervisor_health_server(
                config.health_host,
                config.health_port,
                operational_state,
            )
        signal_handlers = _install_signal_handlers(shutdown)

        while not shutdown.is_set():
            poll_number = operational_state.begin_poll()
            _atomic_write_json(state_file, operational_state.snapshot())
            wait_seconds = config.interval_seconds
            try:
                result = poll_once(owner_id)
                operational_state.poll_succeeded(result)
                if on_poll is not None:
                    on_poll(result)
            except Exception as exc:
                errors = operational_state.poll_failed(exc)
                logger.error(
                    "Runtime supervisor poll failed (%s/%s): %s",
                    errors,
                    config.max_consecutive_errors,
                    _safe_error(exc),
                )
                wait_seconds = min(
                    config.error_backoff_max_seconds,
                    config.interval_seconds * (2 ** max(0, errors - 1)),
                )
                if errors >= config.max_consecutive_errors:
                    fatal = True
                    exit_reason = "max_consecutive_errors"
            _atomic_write_json(state_file, operational_state.snapshot())
            if fatal:
                break
            if config.max_polls and poll_number >= config.max_polls:
                exit_reason = "max_polls"
                break
            shutdown.wait(wait_seconds)
        else:
            exit_reason = "signal"
    except Exception:
        fatal = True
        exit_reason = "startup_error"
        operational_state.update(status="failed", stopped_at=time.time())
        if pid_claimed:
            with contextlib.suppress(OSError):
                _atomic_write_json(state_file, operational_state.snapshot())
        raise
    finally:
        if pid_claimed and not fatal:
            operational_state.update(status="stopping")
            with contextlib.suppress(OSError):
                _atomic_write_json(state_file, operational_state.snapshot())
        if health_server is not None:
            health_server.shutdown()
            health_server.server_close()
        if health_thread is not None:
            health_thread.join(timeout=2)
        _restore_signal_handlers(signal_handlers)
        operational_state.update(
            status="failed" if fatal else "stopped",
            stopped_at=time.time(),
        )
        if pid_claimed:
            with contextlib.suppress(OSError):
                _atomic_write_json(state_file, operational_state.snapshot())
        if pid_claimed:
            release_runtime_supervisor_pidfile(pidfile)

    return {
        "status": "failed" if fatal else "stopped",
        "exit_reason": exit_reason,
        "owner": owner_id,
        "pidfile": str(pidfile),
        "state_file": str(state_file),
        "health_port": health_server.server_address[1] if health_server is not None else None,
        "state": operational_state.snapshot(),
    }
