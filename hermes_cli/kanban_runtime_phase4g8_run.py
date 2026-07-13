"""Phase 4G8 real-provider, real-worker, daemon and evaluator orchestration."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import signal
import shutil
import sqlite3
import subprocess
import sys
import time
from typing import Any, Optional
import uuid

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_decision as rd
from hermes_cli import kanban_runtime_kernel as rk
from hermes_cli import kanban_runtime_phase4g8 as p4g8
from hermes_cli import phase4g8_capability_trace as capability_trace
from hermes_cli import kanban_runtime_supervisor as supervisor
from hermes_cli.codex_worker import (
    _safe_env_for_codex,
    make_codex_worker_lane,
    wrap_codex_network_argv,
)
from hermes_cli.worker_lanes import clear_worker_lanes, register_worker_lane


REAL_CASE_REPORT_SCHEMA = "hermes_phase4g8_real_case_v1"
PHASE4G8_CONTEXT_WINDOW_TOKENS = 353_400
PHASE4G8_COMPACTION_POLICY = {
    "context_window_tokens": PHASE4G8_CONTEXT_WINDOW_TOKENS,
    "compaction_trigger_ratio": 0.65,
    "max_context_window_ratio": 0.65,
    "reserved_output_tokens": 8_192,
    "reserved_reasoning_tokens": 32_768,
    "estimation_safety_tokens": 32_768,
    "max_compaction_input_ratio": 0.55,
    "max_compaction_input_chars": 1_000_000,
    "max_single_entry_chars": 16_000,
    "max_segment_entries": 200,
    "max_active_segment_tokens": None,
}


def run_phase4g8_real_case(
    *,
    qualification_spec_path: Path,
    run_root: Path,
    source_codex_home: Path,
    case_size: str,
    execute_real: bool,
    max_wall_seconds: float = 14_400,
    worker_timeout_seconds: int = 7_200,
    decision_timeout_seconds: float = 300.0,
    compaction_timeout_seconds: float = 300.0,
    compaction_token_threshold: Optional[int] = None,
    poll_interval_seconds: float = 0.5,
    worker_event_startup_timeout_seconds: float = 300.0,
    worker_event_stall_timeout_seconds: float = 3600.0,
    max_unresolved_evaluator_attempts: int = 3,
) -> dict[str, Any]:
    """Run one qualified SWE-EVO case through production runtime boundaries."""

    if not execute_real:
        raise ValueError("Phase 4G8 real case requires execute_real=True")
    if case_size not in {"small", "medium", "large"}:
        raise ValueError("case_size must be small, medium, or large")
    if int(max_unresolved_evaluator_attempts) < 1:
        raise ValueError("max_unresolved_evaluator_attempts must be positive")
    if os.name == "nt" or "fork" not in multiprocessing.get_all_start_methods():
        raise RuntimeError("Phase 4G8 real cases require POSIX fork semantics")
    spec = p4g8.load_qualification_spec(qualification_spec_path.resolve())
    _require_qualified(spec, qualification_spec_path)
    if not str((spec.get("benchmark") or {}).get("official_image") or "").strip():
        raise ValueError("Phase 4G8 real case requires benchmark.official_image")
    source = p4g8.load_codex_model_source(source_codex_home)
    run_id = f"phase4g8-{case_size}-{uuid.uuid4().hex[:10]}"
    worker_uid, worker_gid = _derive_run_identity(run_id)
    root = run_root.resolve() / str(spec["instance_id"]) / run_id
    paths = _prepare_real_layout(root, spec, worker_uid=worker_uid, worker_gid=worker_gid)
    boundaries = {
        "daemon_process_started": False,
        "daemon_restarted": False,
        "worker_process_started": False,
        "independent_evaluator_process": False,
        "fixed_revision_evaluated": False,
        "filesystem_isolation_preflight": False,
    }
    if case_size in {"medium", "large"}:
        boundaries.update({
            "worker_process_interrupted": False,
            "worker_backend_session_resumed": False,
        })
    if case_size == "large":
        boundaries.update({
            "daemon_hard_crash": False,
            "expired_lease_takeover": False,
            "receipt_before_ingest_restart": False,
            "two_real_checkpoints": False,
        })
    daemon_process: Optional[multiprocessing.Process] = None
    namespace: Optional[p4g8.Phase4G8NetworkNamespace] = None
    started = time.monotonic()
    old_environment: dict[str, Optional[str]] = {}
    worker_killed = False
    excluded_crashed_task_id: Optional[str] = None
    daemon_restarted = False
    large_lease_exercised = False
    evaluator_daemon_stopped = False
    evaluator_fault_injected = False
    receipt_before_ingest_node_id: Optional[str] = None
    receipt_before_ingest_before: Optional[dict[str, int]] = None
    job_id: Optional[str] = None
    evaluator_budget_exhausted = False
    evaluator_attempts: list[dict[str, Any]] = []
    evaluator_budget_session_sync: dict[str, Any] = {}
    try:
        namespace = p4g8.Phase4G8NetworkNamespace(run_id, source["explicit_base_url"]).start()
        p4g8.prepare_isolated_codex_home(
            source_codex_home,
            paths["codex_home"],
            proxy_base_url=str(namespace.proxy_base_url),
            model=source["model"],
            worker_uid=worker_uid,
            worker_gid=worker_gid,
        )
        old_environment = _install_isolated_environment(paths)
        _assert_worker_filesystem_isolation(
            paths,
            namespace=namespace.namespace,
            worker_uid=worker_uid,
            worker_gid=worker_gid,
            qualification_spec_path=qualification_spec_path.resolve(),
            source_mirror=Path(spec["source"]["local_mirror"]).resolve(),
        )
        boundaries["filesystem_isolation_preflight"] = True
        _register_real_case_lanes(
            run_id=run_id,
            model=source["model"],
            namespace=namespace.namespace,
            worker_timeout_seconds=worker_timeout_seconds,
            evaluator_spec=qualification_spec_path.resolve(),
            worker_uid=worker_uid,
            worker_gid=worker_gid,
            codex_home_seed=paths["codex_home"],
            codex_home_root=paths["node_codex_homes"],
        )
        job_id = _create_real_job(spec, paths["workspace"], run_id)
        decision_provider = rd.RuntimeDecisionProvider(
            provider_name=source["provider_name"],
            model=source["model"],
            profile_name="graph_patch_decision",
            max_retries=1,
            timeout_seconds=decision_timeout_seconds,
            reasoning_effort=source.get("reasoning_effort"),
            explicit_base_url=source["explicit_base_url"],
            explicit_api_key=source["explicit_api_key"],
        )
        compaction_provider = rd.RuntimeCompactionProvider(
            provider_name=source["provider_name"],
            model=source["model"],
            profile_name="token_budget_compaction",
            max_retries=1,
            timeout_seconds=compaction_timeout_seconds,
            reasoning_effort=source.get("reasoning_effort"),
            explicit_base_url=source["explicit_base_url"],
            explicit_api_key=source["explicit_api_key"],
        )
        compaction_policy = dict(PHASE4G8_COMPACTION_POLICY)
        if compaction_token_threshold is not None:
            if int(compaction_token_threshold) < 1:
                raise ValueError("compaction_token_threshold must be positive when provided")
            compaction_policy["max_active_segment_tokens"] = int(compaction_token_threshold)
            compaction_policy["test_only_forced_threshold"] = True
        daemon_config = supervisor.RuntimeSupervisorDaemonConfig(
            interval_seconds=max(0.1, poll_interval_seconds),
            limit=1,
            lock_ttl_seconds=int(decision_timeout_seconds * 2 + compaction_timeout_seconds * 2 + 30),
            create_tasks=True,
            max_consecutive_errors=1,
            error_backoff_max_seconds=5.0,
            pidfile=paths["service"] / "supervisor.pid",
            state_file=paths["service"] / "supervisor-state.json",
            auto_compact=True,
            compaction_policy=compaction_policy,
            compaction_fallback_to_deterministic=False,
        )
        daemon_process = _start_daemon(daemon_config, decision_provider, compaction_provider)
        boundaries["daemon_process_started"] = True

        deadline = time.monotonic() + max(60.0, float(max_wall_seconds))
        while time.monotonic() < deadline:
            daemon_state = _read_daemon_state(paths["service"] / "supervisor-state.json")
            if (
                not evaluator_daemon_stopped
                and daemon_state.get("status") == "failed"
            ):
                raise RuntimeError(
                    "runtime supervisor daemon failed: "
                    f"{daemon_state.get('last_error') or daemon_state.get('exit_reason') or 'unknown'}"
                )
            if (
                daemon_process is not None
                and not daemon_process.is_alive()
                and not evaluator_daemon_stopped
            ):
                raise RuntimeError(f"runtime supervisor daemon exited with {daemon_process.exitcode}")
            with kb.connect() as conn:
                evaluator_attempts = _official_evaluator_attempts(conn, job_id)
                evaluator_budget_exhausted = _evaluator_failure_budget_status(
                    evaluator_attempts,
                    max_unresolved_evaluator_attempts=int(max_unresolved_evaluator_attempts),
                )["exhausted"]
            if evaluator_budget_exhausted:
                if daemon_process is not None:
                    _stop_daemon(daemon_process, hard=False)
                with kb.connect() as conn:
                    evaluator_budget_session_sync = rk.sync_runtime_backend_sessions(
                        conn,
                        job_id,
                    )
                break
            with kb.connect() as conn:
                dispatchable = _dispatchable_task_ids(conn, job_id, exclude_task_id=excluded_crashed_task_id)
                kb.dispatch_once(
                    conn,
                    max_spawn=3,
                    stale_timeout_seconds=30,
                    only_task_ids=dispatchable,
                )
                status = rk.status_runtime_job(conn, job_id)
                worker = _implementation_worker(conn, job_id)
                evaluator = _evaluator_worker(conn, job_id)
                continuity = rk.summarize_worker_execution_continuity(conn, job_id)
                receipt_trigger = (
                    p4g8.evaluate_fault_trigger(conn, job_id, "receipt_before_ingest")
                    if case_size == "large" and evaluator_daemon_stopped
                    else {"ready": False, "facts": {}}
                )
                active_backend_session = conn.execute(
                    "SELECT 1 FROM backend_worker_sessions WHERE job_id = ? AND status = 'active' LIMIT 1",
                    (job_id,),
                ).fetchone() is not None
                worker_event_stall = _worker_event_stall(
                    conn,
                    job_id,
                    startup_timeout_seconds=worker_event_startup_timeout_seconds,
                    stall_timeout_seconds=worker_event_stall_timeout_seconds,
                )
            if worker_event_stall.get("stalled"):
                stalled_pid = worker_event_stall.get("worker_pid")
                if stalled_pid:
                    try:
                        p4g8.terminate_owned_process_group(int(stalled_pid), run_id=run_id, hard=True)
                    except ValueError:
                        pass
                raise RuntimeError(
                    "Phase 4G8 worker backend event stalled: "
                    f"{worker_event_stall.get('reason') or 'unknown'}"
                )
            if worker and worker.get("worker_pid"):
                boundaries["worker_process_started"] = True
            if evaluator and evaluator.get("worker_pid"):
                boundaries["independent_evaluator_process"] = True

            if worker and worker.get("worker_pid") and not daemon_restarted and case_size != "large":
                _stop_daemon(daemon_process, hard=False)
                with kb.connect() as conn:
                    if _accepted_checkpoint_count(conn, job_id) < 1:
                        _append_and_compact_real_checkpoint(
                            conn,
                            job_id,
                            compaction_provider,
                            reason="phase4g8_controlled_process_boundary",
                        )
                daemon_process = _start_daemon(daemon_config, decision_provider, compaction_provider)
                daemon_restarted = True
                boundaries["daemon_restarted"] = True

            if (
                case_size in {"medium", "large"}
                and worker and worker.get("worker_pid")
                and active_backend_session
                and not worker_killed
            ):
                p4g8.terminate_owned_process_group(int(worker["worker_pid"]), run_id=run_id, hard=True)
                worker_killed = True
                excluded_crashed_task_id = str(worker["latest_task_id"])
                boundaries["worker_process_interrupted"] = True

            if any(int(session.get("resume_count") or 0) > 0 for session in continuity.get("sessions") or []):
                boundaries["worker_backend_session_resumed"] = True

            if (
                case_size == "large"
                and boundaries["worker_backend_session_resumed"]
                and worker and worker.get("worker_pid")
                and not large_lease_exercised
            ):
                _stop_daemon(daemon_process, hard=True)
                boundaries["daemon_hard_crash"] = True
                with kb.connect() as conn:
                    if _accepted_checkpoint_count(conn, job_id) < 1:
                        _append_and_compact_real_checkpoint(
                            conn,
                            job_id,
                            compaction_provider,
                            reason="phase4g8_post_resume_boundary",
                        )
                boundaries["expired_lease_takeover"] = _exercise_expired_lease_takeover(
                    job_id,
                    run_id=run_id,
                    marker_path=paths["service"] / "expired-lease-ready",
                )
                with kb.connect() as conn:
                    if _accepted_checkpoint_count(conn, job_id) < 2:
                        _append_and_compact_real_checkpoint(
                            conn,
                            job_id,
                            compaction_provider,
                            reason="phase4g8_expired_lease_boundary",
                        )
                    boundaries["two_real_checkpoints"] = _accepted_checkpoint_count(conn, job_id) >= 2
                daemon_process = _start_daemon(daemon_config, decision_provider, compaction_provider)
                daemon_restarted = True
                large_lease_exercised = True
                boundaries["daemon_restarted"] = True

            if (
                case_size == "large"
                and evaluator and evaluator.get("worker_pid")
                and not evaluator_fault_injected
                and daemon_process is not None
                and daemon_process.is_alive()
            ):
                _stop_daemon(daemon_process, hard=True)
                evaluator_daemon_stopped = True
                evaluator_fault_injected = True

            if case_size == "large" and evaluator_daemon_stopped and receipt_trigger["ready"]:
                receipt_before_ingest_node_id = str(receipt_trigger["facts"]["node_id"])
                with kb.connect() as conn:
                    receipt_before_ingest_before = p4g8.runtime_fact_counts(
                        conn,
                        job_id,
                        receipt_before_ingest_node_id,
                    )
                daemon_process = _start_daemon(daemon_config, decision_provider, compaction_provider)
                evaluator_daemon_stopped = False
                boundaries["receipt_before_ingest_restart"] = True

            if status["job"]["state"] in {"done", "failed", "cancelled"}:
                break
            time.sleep(max(0.05, poll_interval_seconds))
        else:
            raise RuntimeError("Phase 4G8 real case exceeded max wall time")

        with kb.connect() as conn:
            if receipt_before_ingest_node_id:
                after = p4g8.runtime_fact_counts(conn, job_id, receipt_before_ingest_node_id)
                if receipt_before_ingest_before != {
                    "ledger": 0,
                    "terminal_events": 0,
                    "terminal_materializations": 0,
                } or after != {
                    "ledger": 1,
                    "terminal_events": 1,
                    "terminal_materializations": 1,
                }:
                    boundaries["receipt_before_ingest_restart"] = False
            evaluator_result, evaluator_facts = _official_evaluator_result(conn, job_id)
            boundaries["independent_evaluator_process"] = bool(evaluator_facts.get("producer_session_id"))
            boundaries["fixed_revision_evaluated"] = bool(evaluator_facts.get("target_revision"))
            report = p4g8.build_phase4g8_run_report(
                conn,
                job_id,
                instance_id=str(spec["instance_id"]),
                evaluator_result=evaluator_result,
                process_boundaries=boundaries,
                metrics={
                    "wall_time_seconds": round(time.monotonic() - started, 3),
                    "case_size": case_size,
                    "worker_interrupted": worker_killed,
                    "evaluator_attempt_count": len(evaluator_attempts),
                    "evaluator_failure_count": sum(
                        attempt["result"].get("resolved") is not True
                        for attempt in evaluator_attempts
                    ),
                    "max_unresolved_evaluator_attempts": int(max_unresolved_evaluator_attempts),
                    "evaluator_budget_exhausted": evaluator_budget_exhausted,
                    "evaluator_budget_session_sync": evaluator_budget_session_sync,
                },
                source_config_unchanged=p4g8.verify_codex_source_unchanged(
                    source_codex_home, source["source_hashes"]
                ),
            )
            trace = capability_trace.build_capability_trace(
                conn,
                job_id,
                run_id=run_id,
                instance_id=str(spec["instance_id"]),
                case_size=case_size,
                run_report=report,
            )
        trace_paths = capability_trace.write_capability_trace(paths["reports"], trace)
        payload = {
            "schema": REAL_CASE_REPORT_SCHEMA,
            "run_id": run_id,
            "job_id": job_id,
            "worker_identity": {"uid": worker_uid, "gid": worker_gid},
            "model_source": source["summary"],
            "run_report": report,
            "termination": {
                "reason": (
                    "evaluator_failure_budget_exhausted"
                    if evaluator_budget_exhausted
                    else "runtime_terminal"
                ),
                "evaluator_budget_exhausted": evaluator_budget_exhausted,
            },
            "paths": {
                "root": str(root),
                "report": str(paths["reports"] / "run-report.json"),
                "capability_trace": trace_paths,
            },
        }
        _write_json(paths["reports"] / "run-report.json", payload)
        return payload
    except Exception as exc:
        evaluator_facts: dict[str, Any] = {}
        continuity: dict[str, Any] = {}
        runtime_state: Optional[str] = None
        worker_cleanup = (
            _terminate_owned_job_workers(job_id, run_id=run_id)
            if job_id is not None
            else {"checked_pids": [], "terminated_pids": [], "errors": []}
        )
        if job_id is not None:
            try:
                with kb.connect() as conn:
                    _evaluator_result, evaluator_facts = _official_evaluator_result(conn, job_id)
                    continuity = rk.summarize_worker_execution_continuity(conn, job_id)
                    runtime_state = str(rk.status_runtime_job(conn, job_id)["job"]["state"])
                boundaries["independent_evaluator_process"] = bool(evaluator_facts.get("producer_session_id"))
                boundaries["fixed_revision_evaluated"] = bool(evaluator_facts.get("target_revision"))
            except Exception:
                pass
        failure = {
            "schema": REAL_CASE_REPORT_SCHEMA,
            "run_id": run_id,
            "job_id": job_id,
            "status": "infrastructure_invalid",
            "classification": "infrastructure_invalid",
            "model_source": source["summary"],
            "process_boundaries": boundaries,
            "failure": {
                "type": type(exc).__name__,
                "message": p4g8.redact_sensitive_text(str(exc)),
            },
            "runtime_state": runtime_state,
            "evaluator_facts": evaluator_facts,
            "continuity": continuity,
            "worker_cleanup": worker_cleanup,
            "metrics": {
                "wall_time_seconds": round(time.monotonic() - started, 3),
                "case_size": case_size,
            },
            "paths": {
                "root": str(root),
                "report": str(paths["reports"] / "run-report.json"),
            },
        }
        _write_json(paths["reports"] / "run-report.json", failure)
        raise
    finally:
        if daemon_process is not None:
            _stop_daemon(daemon_process, hard=False)
        if job_id is not None:
            _terminate_owned_job_workers(job_id, run_id=run_id)
        clear_worker_lanes()
        if old_environment:
            _restore_environment(old_environment)
        if namespace is not None:
            namespace.close()


def _derive_run_identity(run_id: str) -> tuple[int, int]:
    """Derive an unprivileged identity that is isolated from other real runs."""

    digest = hashlib.sha256(run_id.encode("utf-8")).digest()
    worker_uid = 200_000 + int.from_bytes(digest[:4], "big") % 1_000_000_000
    worker_gid = 200_000 + int.from_bytes(digest[4:8], "big") % 1_000_000_000
    return worker_uid, worker_gid


def _assert_worker_filesystem_isolation(
    paths: dict[str, Path],
    *,
    namespace: str,
    worker_uid: int,
    worker_gid: int,
    qualification_spec_path: Path,
    source_mirror: Path,
) -> None:
    canary = Path("/tmp") / f"phase4g8-host-leak-{uuid.uuid4().hex}.txt"
    canary.write_text("phase4g8-protected-canary\n", encoding="utf-8")
    os.chmod(canary, 0o644)
    worker_env = _safe_env_for_codex(str(paths["workspace"]))
    probe = (
        f"test -r {paths['workspace']} "
        f"&& test -r {paths['worker_toolchain'] / 'bin' / 'python'} "
        f"&& test ! -e {canary} "
        f"&& test ! -e {qualification_spec_path} "
        f"&& test ! -e {source_mirror} "
        "&& test ! -e /root "
        "&& printf isolated > /tmp/phase4g8-worker-canary"
    )
    argv = wrap_codex_network_argv(
        ["/bin/sh", "-c", probe],
        namespace,
        uid=worker_uid,
        gid=worker_gid,
        workspace=str(paths["workspace"]),
        worker_env=worker_env,
        filesystem_isolation=True,
    )
    try:
        result = subprocess.run(
            argv,
            env=worker_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
    finally:
        canary.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(
            "Phase 4G8 worker filesystem isolation preflight failed: "
            + (result.stderr or result.stdout or f"exit {result.returncode}").strip()[:1000]
        )


def _prepare_real_layout(
    root: Path,
    spec: dict[str, Any],
    *,
    worker_uid: int = 65534,
    worker_gid: int = 65534,
) -> dict[str, Path]:
    mirror = Path(spec["source"]["local_mirror"]).resolve()
    _protect_source_mirror(mirror)
    workspace = root / "workspace"
    for path in (
        root,
        root / "home",
        root / "hermes-home",
        root / "codex-home-seed",
        root / "codex-homes",
        root / "service",
        root / "reports",
    ):
        path.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o711)
    for path in (root / "hermes-home", root / "service", root / "reports"):
        os.chmod(path, 0o700)
    workspace.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=workspace, check=True)
    subprocess.run(["git", "remote", "add", "phase4g8-source", mirror.as_uri()], cwd=workspace, check=True)
    subprocess.run(
        [
            "git", "-c", "protocol.file.allow=always", "fetch", "--quiet", "--depth=1", "--no-tags",
            "phase4g8-source", str(spec["base_commit"]),
        ],
        cwd=workspace,
        check=True,
    )
    subprocess.run(["git", "checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=workspace, check=True)
    subprocess.run(["git", "remote", "remove", "phase4g8-source"], cwd=workspace, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    ).stdout.strip()
    if head != str(spec["base_commit"]):
        raise RuntimeError("Phase 4G8 worker workspace base commit mismatch")
    tags = subprocess.run(
        ["git", "tag", "--list"],
        cwd=workspace,
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    ).stdout.strip()
    if tags:
        raise RuntimeError("Phase 4G8 worker workspace unexpectedly contains tags")
    official_image = str((spec.get("benchmark") or {}).get("official_image") or "").strip()
    worker_toolchain = _prepare_worker_toolchain(official_image) if official_image else Path(sys.prefix).resolve()
    p4g8.prepare_worker_workspace(workspace, worker_uid=worker_uid, worker_gid=worker_gid)
    for path in (root / "home", root / "codex-home-seed"):
        os.chown(path, int(worker_uid), int(worker_gid))
        os.chmod(path, 0o700)
    os.chmod(root / "codex-homes", 0o711)
    return {
        "root": root,
        "home": root / "home",
        "hermes_home": root / "hermes-home",
        "codex_home": root / "codex-home-seed",
        "node_codex_homes": root / "codex-homes",
        "service": root / "service",
        "reports": root / "reports",
        "workspace": workspace,
        "worker_toolchain": worker_toolchain,
        "db": root / "hermes-home" / "kanban.db",
    }


def _protect_source_mirror(mirror: Path) -> None:
    """Keep the full-history qualification mirror outside the worker trust boundary."""

    if not mirror.is_dir():
        raise ValueError("Phase 4G8 source mirror must be a directory")
    if mirror.stat().st_uid != os.geteuid():
        raise PermissionError("Phase 4G8 source mirror must be owned by the runner")
    os.chmod(mirror, 0o700)


def _prepare_worker_toolchain(official_image: str) -> Path:
    image = str(official_image or "").strip()
    if not image:
        raise ValueError("official image is required for worker toolchain")
    cache_root = Path("/tmp/phase4g8-worker-toolchains")
    cache_root.mkdir(parents=True, exist_ok=True)
    os.chmod(cache_root, 0o755)
    cache_key = hashlib.sha256(image.encode("utf-8")).hexdigest()[:20]
    target = cache_root / cache_key
    python = target / "bin" / "python"
    if python.is_file():
        return target

    temp = cache_root / f".{cache_key}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    temp.mkdir(mode=0o755)
    container_id = ""
    try:
        created = subprocess.run(
            ["docker", "create", image],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=120,
        )
        container_id = created.stdout.strip()
        if not container_id:
            raise RuntimeError("docker create did not return a container id")
        subprocess.run(
            ["docker", "cp", f"{container_id}:/opt/miniconda3/envs/testbed/.", str(temp)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=600,
        )
        for pth in temp.rglob("*.pth"):
            try:
                lines = pth.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            kept = [line for line in lines if not line.strip().startswith(("/testbed", "/workspace"))]
            if kept != lines:
                pth.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        if not (temp / "bin" / "python").is_file():
            raise RuntimeError("official image does not contain /opt/miniconda3/envs/testbed")
        subprocess.run(["chmod", "-R", "a-w", str(temp)], check=True, timeout=120)
        try:
            temp.rename(target)
        except FileExistsError:
            shutil.rmtree(temp)
    finally:
        if container_id:
            subprocess.run(
                ["docker", "rm", "-f", container_id],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=60,
            )
        if temp.exists():
            shutil.rmtree(temp)
    if not python.is_file():
        raise RuntimeError("Phase 4G8 worker toolchain cache was not created")
    return target


def _install_isolated_environment(paths: dict[str, Path]) -> dict[str, Optional[str]]:
    values = {
        "HOME": str(paths["home"]),
        "HERMES_HOME": str(paths["hermes_home"]),
        "HERMES_KANBAN_HOME": str(paths["hermes_home"] / "kanban"),
        "HERMES_KANBAN_DB": str(paths["db"]),
        "CODEX_HOME": str(paths["codex_home"]),
        "PATH": str(paths["worker_toolchain"] / "bin") + os.pathsep + os.environ.get("PATH", ""),
        "PHASE4G8_WORKER_TOOLCHAIN": str(paths["worker_toolchain"]),
    }
    old = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    kb.init_db()
    return old


def _restore_environment(old: dict[str, Optional[str]]) -> None:
    for key, value in old.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _register_real_case_lanes(
    *,
    run_id: str,
    model: str,
    namespace: str,
    worker_timeout_seconds: int,
    evaluator_spec: Path,
    worker_uid: int = 65534,
    worker_gid: int = 65534,
    codex_home_seed: Path,
    codex_home_root: Path,
) -> None:
    clear_worker_lanes()
    register_worker_lane(make_codex_worker_lane({
        "name": "phase4g8-codex",
        "type": "codex_cli",
        "model": model,
        "sandbox": "danger-full-access",
        "approval": "never",
        "success_policy": "auto_complete",
        "timeout_seconds": int(worker_timeout_seconds),
        "json_events": True,
        "network_namespace": namespace,
        "phase4g8_run_id": run_id,
        "network_uid": int(worker_uid),
        "network_gid": int(worker_gid),
        "isolated_codex_home_seed": str(codex_home_seed),
        "isolated_codex_home_root": str(codex_home_root),
    }, source="phase4g8"), replace=True)
    register_worker_lane(p4g8.make_phase4g8_evaluator_lane({
        "name": "phase4g8-evaluator",
        "spec_path": str(evaluator_spec),
        "run_id": run_id,
    }), replace=True)


def _create_real_job(spec: dict[str, Any], workspace: Path, run_id: str) -> str:
    with kb.connect() as conn:
        root_task = kb.create_task(
            conn,
            title=f"Phase 4G8 {spec['instance_id']}",
            body=str(spec["srs"]),
            initial_status="running",
            workspace_kind="dir",
            workspace_path=str(workspace),
            created_by="phase4g8",
            tenant=f"phase4g8:{run_id}",
        )
        return rk.create_runtime_job(
            conn,
            root_task,
            str(spec["srs"]),
            workspace_path=str(workspace),
            goal_items=[{
                "item_key": "official-evaluator-resolved",
                "description": "Implement the SRS and pass the fixed official SWE-EVO evaluator.",
                "required": True,
                "acceptance_criteria": {"benchmark": "SWE-EVO", "resolved": True},
                "evidence_requirements": {"requires_verification": True, "producer": "official_evaluator"},
                "verifier_required": True,
            }],
            initial_assignee="phase4g8-codex",
            initialization_mode="provider_first",
            runtime_metadata={
                "phase4g8_run_id": run_id,
                "verification_policy": {
                    "mode": "required_evaluator",
                    "assignee": "phase4g8-evaluator",
                    "require_workspace_revision": True,
                },
            },
        )


def _start_daemon(config: supervisor.RuntimeSupervisorDaemonConfig, decision: Any, compaction: Any):
    context = multiprocessing.get_context("fork")
    process = context.Process(target=_daemon_target, args=(config, decision, compaction), daemon=False)
    process.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if config.pidfile and config.pidfile.exists():
            state = _read_daemon_state(config.state_file) if config.state_file else {}
            if int(state.get("pid") or 0) == int(process.pid or 0) and state.get("status") in {"starting", "running"}:
                return process
        if not process.is_alive():
            raise RuntimeError(f"runtime supervisor failed to start: {process.exitcode}")
        time.sleep(0.05)
    raise RuntimeError("runtime supervisor PID file was not created")


def _daemon_target(config: supervisor.RuntimeSupervisorDaemonConfig, decision: Any, compaction: Any) -> None:
    supervisor.run_runtime_supervisor_daemon(
        config,
        decision_provider=decision,
        compaction_provider=compaction,
    )


def _read_daemon_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _terminate_owned_job_workers(job_id: str, *, run_id: str) -> dict[str, Any]:
    pids: set[int] = set()
    try:
        with kb.connect() as conn:
            rows = conn.execute(
                """
                SELECT t.worker_pid AS pid
                  FROM execution_nodes n JOIN tasks t ON t.id = n.latest_task_id
                 WHERE n.job_id = ? AND t.worker_pid IS NOT NULL
                UNION
                SELECT tr.worker_pid AS pid
                  FROM execution_nodes n JOIN task_runs tr ON tr.task_id = n.latest_task_id
                 WHERE n.job_id = ? AND tr.status = 'running' AND tr.worker_pid IS NOT NULL
                """,
                (job_id, job_id),
            ).fetchall()
            pids = {int(row["pid"]) for row in rows if row["pid"] is not None}
    except Exception:
        return {"checked_pids": [], "terminated_pids": [], "errors": ["worker_pid_query_failed"]}

    terminated: list[int] = []
    errors: list[str] = []
    for pid in sorted(pids):
        try:
            p4g8.terminate_owned_process_group(pid, run_id=run_id, hard=True)
            terminated.append(pid)
        except ValueError:
            continue
        except OSError as exc:
            errors.append(f"{pid}:{type(exc).__name__}")
    deadline = time.monotonic() + 5
    while terminated and time.monotonic() < deadline:
        alive = []
        for pid in terminated:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                continue
            alive.append(pid)
        if not alive:
            break
        time.sleep(0.05)
    return {
        "checked_pids": sorted(pids),
        "terminated_pids": terminated,
        "errors": errors,
    }


def _worker_event_stall(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    startup_timeout_seconds: float,
    stall_timeout_seconds: float,
    now: Optional[int] = None,
) -> dict[str, Any]:
    current = int(now if now is not None else time.time())
    row = conn.execute(
        """
        SELECT tr.id AS run_id, tr.task_id, tr.worker_pid, tr.started_at,
               MAX(CASE WHEN te.kind = 'worker_codex_event' THEN te.created_at END) AS last_codex_event_at
          FROM task_runs tr
          JOIN execution_nodes n ON n.latest_task_id = tr.task_id
          LEFT JOIN task_events te ON te.run_id = tr.id
         WHERE n.job_id = ? AND tr.status = 'running' AND tr.profile = 'phase4g8-codex'
         GROUP BY tr.id, tr.task_id, tr.worker_pid, tr.started_at
         ORDER BY tr.id DESC LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    if row is None:
        return {"stalled": False}
    started_at = int(row["started_at"] or current)
    last_event_at = row["last_codex_event_at"]
    if last_event_at is None and current - started_at >= max(1, int(startup_timeout_seconds)):
        return {
            "stalled": True,
            "reason": "no_worker_codex_event_after_startup_timeout",
            "run_id": int(row["run_id"]),
            "task_id": str(row["task_id"]),
            "worker_pid": row["worker_pid"],
            "elapsed_seconds": current - started_at,
        }
    if last_event_at is not None and current - int(last_event_at) >= max(1, int(stall_timeout_seconds)):
        return {
            "stalled": True,
            "reason": "worker_codex_event_stall_timeout",
            "run_id": int(row["run_id"]),
            "task_id": str(row["task_id"]),
            "worker_pid": row["worker_pid"],
            "elapsed_seconds": current - int(last_event_at),
        }
    return {
        "stalled": False,
        "run_id": int(row["run_id"]),
        "task_id": str(row["task_id"]),
        "worker_pid": row["worker_pid"],
        "last_codex_event_at": last_event_at,
    }


def _stop_daemon(process: multiprocessing.Process, *, hard: bool) -> None:
    if not process.is_alive():
        process.join(timeout=1)
        return
    try:
        os.kill(process.pid, signal.SIGKILL if hard else signal.SIGTERM)
    except ProcessLookupError:
        process.join(timeout=1)
        return
    process.join(timeout=10)
    if process.is_alive():
        try:
            os.kill(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.join(timeout=5)


def _lease_holder_target(job_id: str, owner: str, ttl_seconds: int, marker_path: str) -> None:
    with kb.connect() as conn:
        result = rk.acquire_runtime_advance_lock(
            conn,
            job_id,
            owner=owner,
            ttl_seconds=ttl_seconds,
        )
    if not result.get("acquired"):
        raise RuntimeError("Phase 4G8 lease holder could not acquire runtime lease")
    Path(marker_path).write_text("acquired\n", encoding="utf-8")
    time.sleep(max(30, ttl_seconds * 10))


def _exercise_expired_lease_takeover(
    job_id: str,
    *,
    run_id: str,
    marker_path: Path,
    ttl_seconds: int = 3,
) -> bool:
    marker_path.unlink(missing_ok=True)
    owner = f"{run_id}:crashed-lease-holder"
    process = multiprocessing.get_context("fork").Process(
        target=_lease_holder_target,
        args=(job_id, owner, int(ttl_seconds), str(marker_path)),
        daemon=False,
    )
    process.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not marker_path.is_file():
        if not process.is_alive():
            process.join(timeout=1)
            raise RuntimeError("Phase 4G8 lease holder exited before acquiring lease")
        time.sleep(0.05)
    if not marker_path.is_file():
        _stop_daemon(process, hard=True)
        raise RuntimeError("Phase 4G8 lease holder did not publish acquisition marker")
    _stop_daemon(process, hard=True)
    with kb.connect() as conn:
        if p4g8.evaluate_fault_trigger(conn, job_id, "lease_expired")["ready"]:
            return True
    deadline = time.monotonic() + max(10, ttl_seconds * 4)
    while time.monotonic() < deadline:
        with kb.connect() as conn:
            if p4g8.evaluate_fault_trigger(conn, job_id, "lease_expired")["ready"]:
                return True
        time.sleep(0.1)
    return False


def _accepted_checkpoint_count(conn: sqlite3.Connection, job_id: str) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM decision_checkpoints WHERE job_id = ? AND validator_status = 'accepted'",
        (job_id,),
    ).fetchone()[0])


def _append_and_compact_real_checkpoint(
    conn: sqlite3.Connection,
    job_id: str,
    compaction_provider: Any,
    *,
    reason: str,
) -> dict[str, Any]:
    session = conn.execute(
        "SELECT id FROM decision_sessions WHERE job_id = ? AND state = 'active' ORDER BY created_at DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    if session is None:
        raise RuntimeError("Phase 4G8 real compaction requires an active decision session")
    delta = rk.build_decision_delta(conn, job_id)
    rk.append_decision_delta(conn, str(session["id"]), delta)
    result = rd.compact_decision_session(
        conn,
        job_id,
        profile_name="token_budget_compaction",
        reason=reason,
        compaction_provider=compaction_provider,
        fallback_to_deterministic=False,
    )
    if result.get("status") != "compacted" or result.get("fallback_used"):
        raise RuntimeError("Phase 4G8 real compaction checkpoint was not accepted")
    return result


def _implementation_worker(conn: sqlite3.Connection, job_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        """
        SELECT n.node_key, n.state, n.latest_task_id, t.worker_pid
          FROM execution_nodes n LEFT JOIN tasks t ON t.id = n.latest_task_id
         WHERE n.job_id = ? AND n.node_type != 'verification' AND n.latest_task_id IS NOT NULL
         ORDER BY n.created_at LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    return dict(row) if row else None


def _evaluator_worker(conn: sqlite3.Connection, job_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        """
        SELECT n.node_key, n.state, n.latest_task_id, t.worker_pid
          FROM execution_nodes n LEFT JOIN tasks t ON t.id = n.latest_task_id
         WHERE n.job_id = ? AND n.node_type = 'verification' AND n.latest_task_id IS NOT NULL
         ORDER BY n.created_at DESC LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    return dict(row) if row else None


def _dispatchable_task_ids(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    exclude_task_id: Optional[str],
) -> list[str]:
    rows = conn.execute(
        """
        SELECT n.latest_task_id
          FROM execution_nodes n JOIN tasks t ON t.id = n.latest_task_id
         WHERE n.job_id = ? AND t.status = 'ready'
         ORDER BY n.created_at
        """,
        (job_id,),
    ).fetchall()
    return [
        str(row["latest_task_id"])
        for row in rows
        if row["latest_task_id"] and str(row["latest_task_id"]) != str(exclude_task_id or "")
    ]


def _official_evaluator_result(conn: sqlite3.Connection, job_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    attempts = _official_evaluator_attempts(conn, job_id)
    if not attempts:
        raise RuntimeError("official evaluator did not produce a terminal receipt")
    latest = attempts[-1]
    return latest["result"], latest["provenance"]


def _official_evaluator_attempts(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT tr.id AS run_id, tr.task_id, n.id AS node_id, tr.metadata
          FROM execution_nodes n
          JOIN node_materializations m ON m.node_id = n.id
          JOIN task_runs tr ON tr.task_id = m.task_id
         WHERE n.job_id = ?
           AND n.node_type = 'verification'
           AND n.state IN ('succeeded', 'failed', 'blocked', 'cancelled', 'superseded')
           AND m.status IN ('succeeded', 'failed', 'blocked', 'waiting_human')
           AND tr.status IN ('done', 'blocked', 'failed')
           AND tr.metadata IS NOT NULL
         ORDER BY tr.id
        """,
        (job_id,),
    ).fetchall()
    attempts: list[dict[str, Any]] = []
    for row in rows:
        try:
            receipt = json.loads(row["metadata"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(receipt, dict):
            continue
        result = receipt.get("official_evaluator_result")
        if not isinstance(result, dict) or result.get("schema") != p4g8.EVALUATOR_RESULT_SCHEMA:
            continue
        provenance = receipt.get("verification_provenance")
        attempts.append({
            "run_id": int(row["run_id"]),
            "task_id": str(row["task_id"]),
            "node_id": str(row["node_id"]),
            "result": result,
            "provenance": provenance if isinstance(provenance, dict) else {},
        })
    return attempts


def _evaluator_failure_budget_status(
    attempts: list[dict[str, Any]],
    *,
    max_unresolved_evaluator_attempts: int,
) -> dict[str, Any]:
    if int(max_unresolved_evaluator_attempts) < 1:
        raise ValueError("max_unresolved_evaluator_attempts must be positive")
    failure_count = sum(
        attempt.get("result", {}).get("resolved") is not True
        for attempt in attempts
    )
    latest_resolved = bool(
        attempts and attempts[-1].get("result", {}).get("resolved") is True
    )
    return {
        "attempt_count": len(attempts),
        "failure_count": failure_count,
        "max_unresolved_evaluator_attempts": int(max_unresolved_evaluator_attempts),
        "latest_resolved": latest_resolved,
        "exhausted": bool(
            attempts
            and not latest_resolved
            and failure_count >= int(max_unresolved_evaluator_attempts)
        ),
    }


def _require_qualified(spec: dict[str, Any], spec_path: Path) -> None:
    report_path = spec_path.resolve().parent / "qualification-report.json"
    if not report_path.is_file():
        raise ValueError("Phase 4G8 real case requires a qualification report")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("schema") != p4g8.QUALIFICATION_REPORT_SCHEMA or report.get("oracle", {}).get("status") != "qualified":
        raise ValueError("Phase 4G8 qualification report is not qualified")
    if report.get("instance_id") != spec.get("instance_id") or report.get("base_commit") != spec.get("base_commit"):
        raise ValueError("Phase 4G8 qualification report does not match spec")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
