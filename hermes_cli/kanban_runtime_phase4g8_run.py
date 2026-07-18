"""Phase 4G8 real-provider, real-worker, daemon and evaluator orchestration."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import re
import signal
import shlex
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
from hermes_cli import phase4g8_swe_evo as swe_evo
from hermes_cli import kanban_runtime_supervisor as supervisor
from hermes_cli import validation_artifacts
from hermes_cli.codex_worker import (
    _safe_env_for_codex,
    collect_git_evidence,
    make_codex_worker_lane,
    wrap_codex_network_argv,
)
from hermes_cli.worker_lanes import clear_worker_lanes, register_worker_lane


REAL_CASE_REPORT_SCHEMA = "hermes_phase4g8_real_case_v1"
WORKER_TOOLCHAIN_MANIFEST_SCHEMA = "hermes_phase4g8_worker_toolchain_v1"
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
EVALUATED_STOP_POLICY_SCHEMA = "hermes_evaluated_validation_stop_v1"
OWNERSHIP_CANARY_SCHEMA = "hermes_runtime_ownership_canary_v1"


def run_phase4g8_real_case(
    *,
    qualification_spec_path: Path,
    run_root: Optional[Path],
    source_codex_home: Path,
    case_size: str,
    execute_real: bool,
    resume_run: Optional[Path] = None,
    max_wall_seconds: float = 14_400,
    worker_timeout_seconds: int = 7_200,
    decision_timeout_seconds: float = 300.0,
    compaction_timeout_seconds: float = 300.0,
    compaction_token_threshold: Optional[int] = None,
    poll_interval_seconds: float = 0.5,
    worker_event_startup_timeout_seconds: float = 300.0,
    worker_event_stall_timeout_seconds: float = 3600.0,
    max_unresolved_evaluator_attempts: int = 3,
    max_evaluator_no_progress_streak: int = 2,
    orchestration_policy: Optional[dict[str, Any]] = None,
    fault_profile: Optional[str] = None,
    run_id_prefix: str = "phase4g8",
    reasoning_effort_override: Optional[str] = None,
    compaction_reasoning_effort_override: Optional[str] = None,
    operator_stop: Optional[dict[str, Any]] = None,
    evaluated_stop_policy: Optional[dict[str, Any]] = None,
    workspace_ownership_canary: bool = False,
) -> dict[str, Any]:
    """Run one qualified SWE-EVO case through production runtime boundaries."""

    if not execute_real:
        raise ValueError("Phase 4G8 real case requires execute_real=True")
    if case_size not in {"small", "medium", "large"}:
        raise ValueError("case_size must be small, medium, or large")
    selected_fault_profile = str(fault_profile or case_size)
    if selected_fault_profile not in {"small", "medium", "large"}:
        raise ValueError("fault_profile must be small, medium, or large")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", run_id_prefix):
        raise ValueError("run_id_prefix must be a lowercase slug")
    if int(max_unresolved_evaluator_attempts) < 1:
        raise ValueError("max_unresolved_evaluator_attempts must be positive")
    if int(max_evaluator_no_progress_streak) < 1:
        raise ValueError("max_evaluator_no_progress_streak must be positive")
    if (run_root is None) == (resume_run is None):
        raise ValueError("exactly one of run_root or resume_run is required")
    effective_evaluated_stop_policy = _normalize_evaluated_stop_policy(
        evaluated_stop_policy
    )
    if os.name == "nt" or "fork" not in multiprocessing.get_all_start_methods():
        raise RuntimeError("Phase 4G8 real cases require POSIX fork semantics")
    spec = p4g8.load_qualification_spec(qualification_spec_path.resolve())
    _require_qualified(spec, qualification_spec_path)
    if not str((spec.get("benchmark") or {}).get("official_image") or "").strip():
        raise ValueError("Phase 4G8 real case requires benchmark.official_image")
    source = p4g8.load_codex_model_source(source_codex_home)
    resumed_run = resume_run is not None
    previous_run_retention: list[dict[str, Any]] = []
    if resumed_run:
        root = resume_run.resolve()
        run_id = root.name
        if not run_id.startswith(f"{run_id_prefix}-{case_size}-"):
            raise ValueError("resume run directory does not match the requested case size")
    else:
        instance_run_root = run_root.resolve() / str(spec["instance_id"])
        previous_run_retention = (
            _compact_completed_phase4g8_runs(instance_run_root)
            if run_id_prefix == "phase4g8"
            else []
        )
        run_id = f"{run_id_prefix}-{case_size}-{uuid.uuid4().hex[:10]}"
        root = instance_run_root / run_id
    worker_uid, worker_gid = _derive_run_identity(run_id)
    paths = (
        _load_existing_real_layout(root, spec)
        if resumed_run
        else _prepare_real_layout(root, spec, worker_uid=worker_uid, worker_gid=worker_gid)
    )
    ownership_canary = (
        _prepare_workspace_ownership_canary(paths, worker_uid=worker_uid, worker_gid=worker_gid)
        if workspace_ownership_canary and not resumed_run
        else _load_workspace_ownership_canary(paths)
        if workspace_ownership_canary
        else None
    )
    if orchestration_policy:
        contribution_root = paths["root"] / "runtime-contributions"
        contribution_root.mkdir(parents=True, exist_ok=True)
        os.chmod(contribution_root, 0o755)
        paths["runtime_contributions"] = contribution_root
    worker_environment_audit = _load_worker_toolchain_manifest(paths["worker_toolchain"])
    boundaries = {
        "daemon_process_started": False,
        "daemon_restarted": False,
        "worker_process_started": False,
        "independent_evaluator_process": False,
        "fixed_revision_evaluated": False,
        "filesystem_isolation_preflight": False,
        "worker_evaluator_environment_parity_preflight": False,
        "worker_auto_review_preflight": False,
    }
    if workspace_ownership_canary:
        boundaries["workspace_ownership_canary"] = False
    if selected_fault_profile in {"medium", "large"}:
        boundaries.update({
            "worker_process_interrupted": False,
            "worker_backend_session_resumed": False,
        })
    if selected_fault_profile == "large":
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
    receipt_before_ingest_materialization_id: Optional[str] = None
    receipt_before_ingest_before: Optional[dict[str, int]] = None
    job_id: Optional[str] = None
    evaluator_budget_exhausted = False
    resource_exhausted = False
    operator_stop_applied: Optional[dict[str, Any]] = None
    evaluated_stop_applied: Optional[dict[str, Any]] = None
    ownership_canary_audit: dict[str, Any] = {}
    evaluator_progress_status: dict[str, Any] = {}
    evaluator_attempts: list[dict[str, Any]] = []
    evaluator_budget_session_sync: dict[str, Any] = {}
    evaluator_container_cleanup: list[dict[str, Any]] = []
    model_transport_audit: dict[str, Any] = {}
    candidate_evidence: dict[str, Any] = {}
    next_evaluator_cleanup_at = 0.0
    codex_home_audit: dict[str, Any] = {}
    resume_audit: dict[str, Any] = {"resumed": resumed_run}
    try:
        namespace = p4g8.Phase4G8NetworkNamespace(run_id, source["explicit_base_url"]).start()
        codex_home_audit = p4g8.prepare_isolated_codex_home(
            source_codex_home,
            paths["codex_home"],
            proxy_base_url=str(namespace.proxy_base_url),
            model=source["model"],
            worker_uid=worker_uid,
            worker_gid=worker_gid,
            reasoning_effort_override=reasoning_effort_override,
        )
        boundaries["worker_auto_review_preflight"] = bool(
            codex_home_audit.get("approval", {}).get("configured")
        )
        if resumed_run:
            resume_audit["node_codex_homes"] = _refresh_existing_node_codex_homes(
                paths,
                worker_uid=worker_uid,
                worker_gid=worker_gid,
            )
        old_environment = _install_isolated_environment(paths)
        worker_execution_fingerprint = _assert_worker_filesystem_isolation(
            paths,
            namespace=namespace.namespace,
            worker_uid=worker_uid,
            worker_gid=worker_gid,
            qualification_spec_path=qualification_spec_path.resolve(),
            source_mirror=Path(spec["source"]["local_mirror"]).resolve(),
        )
        boundaries["filesystem_isolation_preflight"] = True
        boundaries["worker_evaluator_environment_parity_preflight"] = bool(
            worker_execution_fingerprint["sha256"]
            == worker_environment_audit["environment_fingerprint"]["sha256"]
        )
        _register_real_case_lanes(
            run_id=run_id,
            model=source["model"],
            namespace=namespace.namespace,
            worker_timeout_seconds=worker_timeout_seconds,
            evaluator_spec=qualification_spec_path.resolve(),
            expected_environment_sha256=str(
                worker_environment_audit["environment_fingerprint"]["sha256"]
            ),
            worker_uid=worker_uid,
            worker_gid=worker_gid,
            codex_home_seed=paths["codex_home"],
            codex_home_root=paths["node_codex_homes"],
        )
        if resumed_run:
            job_id = _load_resumable_job(
                run_id=run_id,
                spec=spec,
                workspace=paths["workspace"],
            )
            pre_recovery = _reconstruct_resume_state(
                job_id,
                case_size=selected_fault_profile,
            )
            resume_audit["runtime_recovery"] = _prepare_resumed_runtime_job(job_id)
            recovered = _reconstruct_resume_state(
                job_id,
                case_size=selected_fault_profile,
            )
            boundaries.update(recovered["boundaries"])
            worker_killed = bool(
                pre_recovery["worker_interrupted"] or recovered["worker_interrupted"]
            )
            excluded_crashed_task_id = recovered["dead_running_task_id"]
            daemon_restarted = True
            boundaries["daemon_restarted"] = True
            resume_audit["before_recovery"] = pre_recovery
            resume_audit["after_recovery"] = recovered
        else:
            resolved_orchestration_policy = dict(orchestration_policy or {})
            if resolved_orchestration_policy:
                resolved_orchestration_policy.setdefault(
                    "base_revision",
                    str(spec["base_commit"]),
                )
                resolved_orchestration_policy.setdefault(
                    "worktree_root",
                    str(paths["root"] / "runtime-worktrees"),
                )
                resolved_orchestration_policy.setdefault(
                    "contribution_root",
                    str(paths["root"] / "runtime-contributions"),
                )
                resolved_orchestration_policy.setdefault(
                    "workspace_owner",
                    {"uid": worker_uid, "gid": worker_gid},
                )
            job_id = _create_real_job(
                spec,
                paths["workspace"],
                run_id,
                max_evaluator_no_progress_streak=int(max_evaluator_no_progress_streak),
                orchestration_policy=resolved_orchestration_policy or None,
            )
        _write_runner_state(
            paths["reports"] / "runner-state.json",
            run_id=run_id,
            job_id=job_id,
            case_size=case_size,
            qualification_spec_path=qualification_spec_path,
            worker_toolchain=paths["worker_toolchain"],
            worker_uid=worker_uid,
            worker_gid=worker_gid,
            resumed=resumed_run,
        )
        if operator_stop is not None:
            if not resumed_run:
                raise ValueError("operator stop is only valid while resuming an existing run")
            with kb.connect() as conn:
                evaluator_attempts = _official_evaluator_attempts(conn, job_id)
            operator_stop_applied = _validate_evaluated_operator_stop(
                operator_stop,
                evaluator_attempts=evaluator_attempts,
                workspace=paths["workspace"],
                base_commit=str(spec["base_commit"]),
            )
            evaluator_progress_status = _evaluator_progress_status(
                evaluator_attempts
            )
        decision_provider = rd.RuntimeDecisionProvider(
            provider_name=source["provider_name"],
            model=source["model"],
            profile_name="graph_patch_decision",
            max_retries=1,
            timeout_seconds=decision_timeout_seconds,
            reasoning_effort=(
                reasoning_effort_override or source.get("reasoning_effort")
            ),
            explicit_base_url=source["explicit_base_url"],
            explicit_api_key=source["explicit_api_key"],
        )
        compaction_provider = rd.RuntimeCompactionProvider(
            provider_name=source["provider_name"],
            model=source["model"],
            profile_name="token_budget_compaction",
            max_retries=1,
            timeout_seconds=compaction_timeout_seconds,
            reasoning_effort=(
                compaction_reasoning_effort_override
                or reasoning_effort_override
                or source.get("reasoning_effort")
            ),
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
        if operator_stop_applied is None:
            daemon_process = _start_daemon(
                daemon_config,
                decision_provider,
                compaction_provider,
            )
            boundaries["daemon_process_started"] = True

        deadline = (
            time.monotonic() + max(60.0, float(max_wall_seconds))
            if operator_stop_applied is None
            else time.monotonic() - 1.0
        )
        while time.monotonic() < deadline:
            if time.monotonic() >= next_evaluator_cleanup_at:
                evaluator_container_cleanup.append(
                    swe_evo.cleanup_phase4g8_evaluator_containers(run_id)
                )
                next_evaluator_cleanup_at = time.monotonic() + 10.0
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
                evaluator_progress_status = _evaluator_progress_status(evaluator_attempts)
                primary_worker = _implementation_worker(conn, job_id)
            if evaluator_progress_status["latest_feedback_extraction_incomplete"]:
                raise RuntimeError(
                    "official evaluator feedback extraction incomplete; protected raw "
                    "artifacts retained for infrastructure diagnosis"
                )
            evaluated_stop_candidate = _evaluated_coverage_stop_candidate(
                effective_evaluated_stop_policy,
                evaluator_attempts=evaluator_attempts,
                workspace=paths["workspace"],
                base_commit=str(spec["base_commit"]),
                required_feedback_consumer_node_id=str(
                    (primary_worker or {}).get("node_id") or ""
                ),
            )
            if evaluated_stop_candidate is not None:
                evaluated_stop_applied = evaluated_stop_candidate
                if daemon_process is not None:
                    _stop_daemon(daemon_process, hard=False)
                    daemon_process = None
                _terminate_owned_job_workers(job_id, run_id=run_id)
                with kb.connect() as conn:
                    evaluator_budget_session_sync = rk.sync_runtime_backend_sessions(
                        conn,
                        job_id,
                    )
                    rk._event_once(
                        conn,
                        job_id,
                        "validation_stopped_after_evaluated_coverage",
                        "evaluated-coverage-stop:"
                        + str(evaluated_stop_applied["requested_at"]),
                        evaluated_stop_applied,
                        source="phase4g8_runner",
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
                    if selected_fault_profile == "large" and evaluator_daemon_stopped
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

            if (
                worker
                and worker.get("worker_pid")
                and not daemon_restarted
                and selected_fault_profile != "large"
            ):
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
                selected_fault_profile in {"medium", "large"}
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
                selected_fault_profile == "large"
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
                selected_fault_profile == "large"
                and evaluator and evaluator.get("worker_pid")
                and not evaluator_fault_injected
                and daemon_process is not None
                and daemon_process.is_alive()
            ):
                _stop_daemon(daemon_process, hard=True)
                evaluator_daemon_stopped = True
                evaluator_fault_injected = True

            if (
                selected_fault_profile == "large"
                and evaluator_daemon_stopped
                and receipt_trigger["ready"]
            ):
                receipt_before_ingest_node_id = str(receipt_trigger["facts"]["node_id"])
                receipt_before_ingest_materialization_id = str(
                    receipt_trigger["facts"]["materialization_id"]
                )
                with kb.connect() as conn:
                    receipt_before_ingest_before = p4g8.runtime_fact_counts(
                        conn,
                        job_id,
                        receipt_before_ingest_node_id,
                        materialization_id=receipt_before_ingest_materialization_id,
                    )
                daemon_process = _start_daemon(daemon_config, decision_provider, compaction_provider)
                evaluator_daemon_stopped = False
                boundaries["receipt_before_ingest_restart"] = True

            if status["job"]["state"] in {"done", "failed", "cancelled"}:
                break
            time.sleep(max(0.05, poll_interval_seconds))
        else:
            resource_exhausted = operator_stop_applied is None
            if daemon_process is not None:
                _stop_daemon(daemon_process, hard=False)
            _terminate_owned_job_workers(job_id, run_id=run_id)
            with kb.connect() as conn:
                evaluator_budget_session_sync = rk.sync_runtime_backend_sessions(conn, job_id)
                if operator_stop_applied is not None:
                    rk._event_once(
                        conn,
                        job_id,
                        "operator_stopped_after_evaluated_plateau",
                        "operator-stop:" + str(operator_stop_applied["requested_at"]),
                        operator_stop_applied,
                        source="phase4g8_runner",
                    )

        candidate_evidence = _archive_candidate_evidence(
            paths["reports"],
            paths["workspace"],
            base_commit=str(spec["base_commit"]),
        )
        if ownership_canary is not None:
            ownership_canary_audit = _audit_workspace_ownership_canary(
                paths,
                ownership_canary,
                worker_uid=worker_uid,
                worker_gid=worker_gid,
            )
            boundaries["workspace_ownership_canary"] = bool(
                ownership_canary_audit.get("passed")
            )
        with kb.connect() as conn:
            if receipt_before_ingest_node_id and receipt_before_ingest_materialization_id:
                after = p4g8.runtime_fact_counts(
                    conn,
                    job_id,
                    receipt_before_ingest_node_id,
                    materialization_id=receipt_before_ingest_materialization_id,
                )
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
            model_transport_audit = namespace.transport_audit()
            report = p4g8.build_phase4g8_run_report(
                conn,
                job_id,
                instance_id=str(spec["instance_id"]),
                evaluator_result=evaluator_result,
                process_boundaries=boundaries,
                metrics={
                    "wall_time_seconds": round(time.monotonic() - started, 3),
                    "case_size": case_size,
                    "fault_profile": selected_fault_profile,
                    "orchestration_policy": orchestration_policy or {},
                    "worker_interrupted": worker_killed,
                    "evaluator_attempt_count": len(evaluator_attempts),
                    "evaluator_failure_count": sum(
                        attempt["result"].get("resolved") is not True
                        for attempt in evaluator_attempts
                    ),
                    "deprecated_max_unresolved_evaluator_attempts": int(
                        max_unresolved_evaluator_attempts
                    ),
                    "max_evaluator_no_progress_streak": int(
                        max_evaluator_no_progress_streak
                    ),
                    "evaluator_progress": evaluator_progress_status,
                    "evaluator_budget_exhausted": evaluator_budget_exhausted,
                    "resource_exhausted": resource_exhausted,
                    "operator_stop": operator_stop_applied,
                    "evaluated_validation_stop": evaluated_stop_applied,
                    "workspace_ownership_canary": ownership_canary_audit,
                    "evaluator_budget_session_sync": evaluator_budget_session_sync,
                    "evaluator_container_cleanup": evaluator_container_cleanup,
                    "resumed_run": resumed_run,
                    "resume_audit": resume_audit,
                    "model_transport_audit": model_transport_audit,
                    "previous_run_retention": previous_run_retention,
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
            "worker_environment": worker_environment_audit,
            "worker_approval": codex_home_audit.get("approval", {}),
            "resume": resume_audit,
            "model_source": source["summary"],
            "model_transport_audit": model_transport_audit,
            "candidate_evidence": candidate_evidence,
            "previous_run_retention": previous_run_retention,
            "run_report": report,
            "termination": {
                "reason": (
                    "evaluated_validation_coverage_satisfied"
                    if evaluated_stop_applied is not None
                    else "operator_requested_stop_after_evaluated_plateau"
                    if operator_stop_applied is not None
                    else
                    "total_resource_budget_exhausted"
                    if resource_exhausted
                    else "runtime_terminal"
                ),
                "evaluator_budget_exhausted": evaluator_budget_exhausted,
                "resource_exhausted": resource_exhausted,
                "operator_stop": operator_stop_applied,
                "evaluated_validation_stop": evaluated_stop_applied,
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
        if not candidate_evidence:
            try:
                candidate_evidence = _archive_candidate_evidence(
                    paths["reports"],
                    paths["workspace"],
                    base_commit=str(spec["base_commit"]),
                )
            except Exception as archive_exc:
                candidate_evidence = {
                    "status": "archive_failed",
                    "error": type(archive_exc).__name__,
                }
        failure = {
            "schema": REAL_CASE_REPORT_SCHEMA,
            "run_id": run_id,
            "job_id": job_id,
            "status": "infrastructure_invalid",
            "classification": "infrastructure_invalid",
            "model_source": source["summary"],
            "candidate_evidence": candidate_evidence,
            "model_transport_audit": (
                namespace.transport_audit() if namespace is not None else {}
            ),
            "worker_environment": worker_environment_audit,
            "worker_approval": codex_home_audit.get("approval", {}),
            "resume": resume_audit,
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
                "fault_profile": selected_fault_profile,
                "previous_run_retention": previous_run_retention,
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
        try:
            evaluator_container_cleanup.append(
                swe_evo.cleanup_phase4g8_evaluator_containers(run_id, include_active=True)
            )
        except Exception:
            pass
        clear_worker_lanes()
        if old_environment:
            _restore_environment(old_environment)
        if namespace is not None:
            namespace.close()


def _compact_completed_phase4g8_runs(
    instance_root: Path,
    *,
    artifact_root: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """Remove only rebuildable state while retaining raw execution evidence."""

    if not instance_root.is_dir():
        return []
    results: list[dict[str, Any]] = []
    for run_root in sorted(instance_root.glob("phase4g8-*-*")):
        if not run_root.is_dir():
            continue
        report_path = run_root / "reports" / "run-report.json"
        retention_path = run_root / "reports" / "retention.json"
        if retention_path.is_file() or not report_path.is_file():
            continue
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if report.get("schema") != REAL_CASE_REPORT_SCHEMA:
            continue
        if not (
            isinstance(report.get("termination"), dict)
            or report.get("status") == "infrastructure_invalid"
            or isinstance(report.get("run_report"), dict)
        ):
            continue

        rebuildable_entries = {"workspace", "home", "codex-home-seed"}
        expected = {"reports"}
        if report.get("status") != "infrastructure_invalid":
            expected.update({"codex-homes", "service", "hermes-home"})
        archive = validation_artifacts.archive_validation_run(
            run_root,
            artifact_root=artifact_root,
            phase="phase4g8",
            instance_id=instance_root.name,
            expected_entries=expected,
        )
        cleanup = validation_artifacts.cleanup_rebuildable_entries(
            run_root,
            manifest_path=Path(str(archive["artifact_path"])) / "manifest.json",
            entries=rebuildable_entries,
        )
        preserved = sorted(child.name for child in run_root.iterdir())
        retention = {
            "schema": "hermes_phase4g8_run_retention_v1",
            "run_id": run_root.name,
            "status": "compacted_after_verified_raw_archive",
            "bytes_removed": cleanup["bytes_removed"],
            "removed_entries": cleanup["removed_entries"],
            "preserved_entries": preserved,
            "raw_evidence_retained": True,
            "artifact_path": archive["artifact_path"],
            "artifact_file_count": archive["file_count"],
            "artifact_total_bytes": archive["total_bytes"],
            "deletion_policy": "verified_archive_then_workspace_home_seed_allowlist",
            "compacted_at": int(time.time()),
        }
        _write_json(retention_path, retention)
        results.append(retention)
    return results


def _archive_candidate_evidence(
    reports_root: Path,
    workspace: Path,
    *,
    base_commit: str,
) -> dict[str, Any]:
    """Persist the auditable candidate patch before bulky run state is compacted."""

    patch = swe_evo.collect_candidate_patch(workspace, base_commit)
    encoded = patch.encode("utf-8")
    evidence = collect_git_evidence(str(workspace))
    patch_path = reports_root / "candidate.patch"
    evidence_path = reports_root / "candidate-evidence.json"
    reports_root.mkdir(parents=True, exist_ok=True)
    patch_path.write_bytes(encoded)
    os.chmod(patch_path, 0o600)
    payload = {
        "schema": "hermes_phase4g8_candidate_evidence_v1",
        "status": "archived",
        "base_commit": base_commit,
        "workspace_revision": evidence.get("workspace_revision"),
        "patch_sha256": hashlib.sha256(encoded).hexdigest(),
        "patch_bytes": len(encoded),
        "changed_files": list(evidence.get("changed_files") or []),
        "protected_oracle_included": False,
        "patch_ref": "candidate.patch",
    }
    _write_json(evidence_path, payload)
    return payload


def _path_tree_size(path: Path) -> int:
    if path.is_file() and not path.is_symlink():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    if path.is_dir() and not path.is_symlink():
        for child in path.rglob("*"):
            if child.is_file() and not child.is_symlink():
                try:
                    total += child.stat().st_size
                except OSError:
                    pass
    return total


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
) -> dict[str, Any]:
    canary = Path("/tmp") / f"phase4g8-host-leak-{uuid.uuid4().hex}.txt"
    canary.write_text("phase4g8-protected-canary\n", encoding="utf-8")
    os.chmod(canary, 0o644)
    worker_env = _safe_env_for_codex(str(paths["workspace"]))

    def quote(value: Any) -> str:
        return shlex.quote(str(value))

    probe = " && ".join([
        f"test -r {quote(paths['workspace'])}",
        f"test -r {quote(paths['worker_toolchain'] / 'bin' / 'python')}",
        f"test ! -e {quote(canary)}",
        f"test ! -e {quote(qualification_spec_path)}",
        f"test ! -e {quote(source_mirror)}",
        "test ! -e /root",
        "printf isolated > /tmp/phase4g8-worker-canary",
        (
            "/opt/miniconda3/envs/testbed/bin/python -c "
            + quote(swe_evo.environment_fingerprint_code())
        ),
    ])
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
    fingerprint = swe_evo.parse_environment_fingerprint(result.stdout)
    expected = _load_worker_toolchain_manifest(
        paths["worker_toolchain"]
    )["environment_fingerprint"]
    if fingerprint["sha256"] != expected["sha256"]:
        raise RuntimeError("Phase 4G8 isolated worker environment fingerprint mismatch")
    return fingerprint


def _prepare_workspace_ownership_canary(
    paths: dict[str, Path],
    *,
    worker_uid: int,
    worker_gid: int,
) -> dict[str, Any]:
    worktree_root = paths["root"] / "runtime-worktrees"
    sibling = worktree_root / "ownership-canary"
    target = paths["reports"] / "ownership-canary-target.txt"
    sentinel = sibling / "sentinel.txt"
    link = sibling / "target-link"
    worktree_root.mkdir(parents=True, exist_ok=True)
    sibling.mkdir()
    target.write_text("outside worktree root\n", encoding="utf-8")
    sentinel.write_text("sibling worktree ownership\n", encoding="utf-8")
    link.symlink_to(target)
    state = {
        "schema": OWNERSHIP_CANARY_SCHEMA,
        "worktree_root": str(worktree_root),
        "sibling": str(sibling),
        "sentinel": str(sentinel),
        "target": str(target),
        "link": str(link),
        "expected_worker_owner": {"uid": int(worker_uid), "gid": int(worker_gid)},
        "initial_sentinel_owner": {
            "uid": sentinel.stat().st_uid,
            "gid": sentinel.stat().st_gid,
        },
        "initial_target_owner": {
            "uid": target.stat().st_uid,
            "gid": target.stat().st_gid,
        },
        "sentinel_sha256": hashlib.sha256(sentinel.read_bytes()).hexdigest(),
        "target_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    }
    _write_json(paths["reports"] / "ownership-canary.json", state)
    return state


def _load_workspace_ownership_canary(paths: dict[str, Path]) -> dict[str, Any]:
    path = paths["reports"] / "ownership-canary.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("resume run is missing a valid ownership canary") from exc
    if not isinstance(state, dict) or state.get("schema") != OWNERSHIP_CANARY_SCHEMA:
        raise ValueError("resume run ownership canary schema is invalid")
    return state


def _audit_workspace_ownership_canary(
    paths: dict[str, Path],
    state: dict[str, Any],
    *,
    worker_uid: int,
    worker_gid: int,
) -> dict[str, Any]:
    worktree_root = Path(str(state["worktree_root"]))
    sibling = Path(str(state["sibling"]))
    sentinel = Path(str(state["sentinel"]))
    target = Path(str(state["target"]))
    link = Path(str(state["link"]))
    child_worktrees = sorted(
        path.name
        for path in worktree_root.iterdir()
        if path.is_dir() and path != sibling and (path / ".git").is_file()
    )
    checks = {
        "worktree_root_owner_applied": (
            worktree_root.stat().st_uid,
            worktree_root.stat().st_gid,
        ) == (int(worker_uid), int(worker_gid)),
        "sibling_owner_preserved": {
            "uid": sentinel.stat().st_uid,
            "gid": sentinel.stat().st_gid,
        } == state["initial_sentinel_owner"],
        "sibling_content_preserved": hashlib.sha256(
            sentinel.read_bytes()
        ).hexdigest() == state["sentinel_sha256"],
        "symlink_target_owner_preserved": {
            "uid": target.stat().st_uid,
            "gid": target.stat().st_gid,
        } == state["initial_target_owner"],
        "symlink_target_content_preserved": hashlib.sha256(
            target.read_bytes()
        ).hexdigest() == state["target_sha256"],
        "symlink_still_external": link.is_symlink()
        and link.resolve() == target.resolve(),
        "two_child_worktrees_created": len(child_worktrees) >= 2,
    }
    audit = {
        **state,
        "checks": checks,
        "child_worktrees": child_worktrees,
        "passed": all(checks.values()),
        "audited_at": int(time.time()),
    }
    _write_json(paths["reports"] / "ownership-canary.json", audit)
    return audit


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
    worker_environment_setup = _render_worker_environment_setup(spec) if official_image else None
    worker_toolchain = (
        _prepare_worker_toolchain(
            official_image,
            environment_setup=worker_environment_setup,
            setup_env=(spec.get("worker_environment") or spec.get("evaluator") or {}).get("env") or {},
        )
        if official_image
        else Path(sys.prefix).resolve()
    )
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
        "worker_toolchain_manifest": worker_toolchain / ".hermes-phase4g8-toolchain.json",
        "db": root / "hermes-home" / "kanban.db",
    }


def _load_existing_real_layout(root: Path, spec: dict[str, Any]) -> dict[str, Path]:
    """Validate and reopen a durable Phase 4G8 run without recreating it."""

    root = root.resolve()
    workspace = root / "workspace"
    required_directories = {
        "root": root,
        "home": root / "home",
        "hermes_home": root / "hermes-home",
        "codex_home": root / "codex-home-seed",
        "node_codex_homes": root / "codex-homes",
        "service": root / "service",
        "reports": root / "reports",
        "workspace": workspace,
    }
    missing = [name for name, path in required_directories.items() if not path.is_dir()]
    if missing:
        raise ValueError(f"resume run is missing required directories: {', '.join(sorted(missing))}")
    db = required_directories["hermes_home"] / "kanban.db"
    if not db.is_file():
        raise ValueError("resume run does not contain a Kanban runtime database")
    head = subprocess.run(
        ["git", "-c", f"safe.directory={workspace}", "rev-parse", "HEAD"],
        cwd=workspace,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    ).stdout.strip()
    if head != str(spec["base_commit"]):
        raise ValueError("resume workspace base commit does not match the qualification spec")
    _protect_source_mirror(Path(spec["source"]["local_mirror"]).resolve())
    worker_toolchain = _resolve_resume_worker_toolchain(root, spec)
    return {
        **required_directories,
        "worker_toolchain": worker_toolchain,
        "worker_toolchain_manifest": worker_toolchain / ".hermes-phase4g8-toolchain.json",
        "db": db,
    }


def _resolve_resume_worker_toolchain(root: Path, spec: dict[str, Any]) -> Path:
    state_path = root / "reports" / "runner-state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        state = {}
    recorded = Path(str(state.get("worker_toolchain") or ""))
    if recorded.is_absolute() and recorded.is_dir():
        try:
            _load_worker_toolchain_manifest(recorded)
        except RuntimeError:
            pass
        else:
            return recorded.resolve()

    official_image = str((spec.get("benchmark") or {}).get("official_image") or "").strip()
    environment_setup = _render_worker_environment_setup(spec)
    setup_script = str(environment_setup.get("setup_script") or "")
    setup_sha256 = hashlib.sha256(setup_script.encode("utf-8")).hexdigest()
    setup_env = (spec.get("worker_environment") or spec.get("evaluator") or {}).get("env") or {}
    image_identity = _docker_image_identity(official_image)
    _unused, setup_env_sha256 = _worker_toolchain_cache_identity(
        official_image,
        image_identity,
        setup_sha256,
        setup_env,
    )
    candidates: list[Path] = []
    cache_root = Path("/tmp/phase4g8-worker-toolchains")
    for manifest_path in sorted(cache_root.glob("*/.hermes-phase4g8-toolchain.json")):
        try:
            manifest = _load_worker_toolchain_manifest(manifest_path.parent)
        except RuntimeError:
            continue
        if (
            manifest.get("official_image") == official_image
            and manifest.get("image_content_identity") == image_identity["content_identity"]
            and manifest.get("setup_sha256") == setup_sha256
            and manifest.get("setup_env_sha256") == setup_env_sha256
        ):
            candidates.append(manifest_path.parent.resolve())
    if not candidates:
        return _prepare_worker_toolchain(
            official_image,
            environment_setup=environment_setup,
            setup_env=setup_env,
        )
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0]


def _refresh_existing_node_codex_homes(
    paths: dict[str, Path],
    *,
    worker_uid: int,
    worker_gid: int,
) -> dict[str, Any]:
    """Rotate proxy credentials/config while preserving durable Codex session state."""

    seed = paths["codex_home"]
    root = paths["node_codex_homes"]
    refreshed: list[str] = []
    preserved_state_files = 0
    for target in sorted(root.glob("node-*")):
        if not target.is_dir() or not (target / ".execution-node").is_file():
            continue
        preserved_state_files += len(list(target.glob("*.sqlite*")))
        for name in ("config.toml", "auth.json"):
            destination = target / name
            destination.write_bytes((seed / name).read_bytes())
            os.chmod(destination, 0o600)
            os.chown(destination, int(worker_uid), int(worker_gid))
        seed_policy = seed / "rules" / "default.rules"
        target_rules = target / "rules"
        target_rules.mkdir(mode=0o700, exist_ok=True)
        target_policy = target_rules / "default.rules"
        target_policy.write_bytes(seed_policy.read_bytes())
        os.chmod(target_rules, 0o700)
        os.chmod(target_policy, 0o600)
        os.chown(target_rules, int(worker_uid), int(worker_gid))
        os.chown(target_policy, int(worker_uid), int(worker_gid))
        os.chown(target, int(worker_uid), int(worker_gid))
        refreshed.append(target.name)
    return {
        "refreshed": refreshed,
        "refreshed_count": len(refreshed),
        "preserved_state_file_count": preserved_state_files,
    }


def _protect_source_mirror(mirror: Path) -> None:
    """Keep the full-history qualification mirror outside the worker trust boundary."""

    if not mirror.is_dir():
        raise ValueError("Phase 4G8 source mirror must be a directory")
    if mirror.stat().st_uid != os.geteuid():
        raise PermissionError("Phase 4G8 source mirror must be owned by the runner")
    os.chmod(mirror, 0o700)


def _render_worker_environment_setup(spec: dict[str, Any]) -> dict[str, Any]:
    worker_environment = spec.get("worker_environment")
    if isinstance(worker_environment, dict):
        argv = list(worker_environment.get("renderer_argv") or [])
        renderer_env = worker_environment.get("env") or {}
    else:
        evaluator = spec.get("evaluator") or {}
        evaluator_argv = list(evaluator.get("argv") or [])
        if len(evaluator_argv) < 6 or evaluator_argv[1:3] != ["-m", "hermes_cli.phase4g8_swe_evo"]:
            raise ValueError("SWE-EVO worker environment renderer is not configured")
        try:
            instance_index = evaluator_argv.index("--instance") + 1
            instance_path = evaluator_argv[instance_index]
        except (ValueError, IndexError) as exc:
            raise ValueError("SWE-EVO evaluator argv does not identify its protected instance") from exc
        argv = [
            evaluator_argv[0],
            "-m",
            "hermes_cli.phase4g8_swe_evo",
            "render-worker-environment",
            "--instance",
            instance_path,
        ]
        renderer_env = evaluator.get("env") or {}
    if not argv:
        raise ValueError("worker environment renderer argv is empty")
    env = os.environ.copy()
    env.update({str(key): str(value) for key, value in renderer_env.items()})
    project_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = project_root + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    completed = subprocess.run(
        argv,
        cwd=project_root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=120,
    )
    if completed.returncode != 0:
        raise RuntimeError("SWE-EVO worker environment renderer failed")
    try:
        setup = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("SWE-EVO worker environment renderer returned invalid JSON") from exc
    if setup.get("schema") != swe_evo.WORKER_ENVIRONMENT_SETUP_SCHEMA:
        raise RuntimeError("SWE-EVO worker environment setup schema is invalid")
    setup_script = str(setup.get("setup_script") or "")
    setup_sha256 = hashlib.sha256(setup_script.encode("utf-8")).hexdigest()
    if not setup_script or setup.get("setup_sha256") != setup_sha256:
        raise RuntimeError("SWE-EVO worker environment setup hash is invalid")
    official_image = str((spec.get("benchmark") or {}).get("official_image") or "")
    if setup.get("official_image") != official_image:
        raise RuntimeError("SWE-EVO worker environment image does not match qualification spec")
    return setup


def _docker_image_identity(image: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["docker", "image", "inspect", image],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=120,
    )
    if completed.returncode != 0:
        raise RuntimeError("official image is not available for worker toolchain")
    try:
        rows = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("docker image inspect returned invalid JSON") from exc
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise RuntimeError("docker image inspect returned an unexpected result")
    image_id = str(rows[0].get("Id") or "").strip()
    repo_digests = sorted(str(value) for value in rows[0].get("RepoDigests") or [] if str(value))
    if not image_id:
        raise RuntimeError("official image does not have a content identity")
    return {
        "image_id": image_id,
        "repo_digests": repo_digests,
        "content_identity": repo_digests[0] if repo_digests else image_id,
    }


def _worker_toolchain_cache_identity(
    image: str,
    image_identity: dict[str, Any],
    setup_sha256: str,
    setup_env: dict[str, Any],
    environment_sha256: str = "",
) -> tuple[str, str]:
    env_payload = {str(key): str(value) for key, value in sorted(setup_env.items())}
    env_sha256 = hashlib.sha256(
        json.dumps(env_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    payload = {
        "schema": WORKER_TOOLCHAIN_MANIFEST_SCHEMA,
        "official_image": image,
        "image_content_identity": image_identity["content_identity"],
        "setup_sha256": setup_sha256,
        "setup_env_sha256": env_sha256,
        "environment_sha256": str(environment_sha256),
    }
    cache_key = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    return cache_key, env_sha256


def _prepare_worker_toolchain(
    official_image: str,
    *,
    environment_setup: Optional[dict[str, Any]] = None,
    setup_env: Optional[dict[str, Any]] = None,
) -> Path:
    image = str(official_image or "").strip()
    if not image:
        raise ValueError("official image is required for worker toolchain")
    setup_script = str((environment_setup or {}).get("setup_script") or "#!/bin/bash\nset -euo pipefail\n")
    setup_sha256 = hashlib.sha256(setup_script.encode("utf-8")).hexdigest()
    if environment_setup and environment_setup.get("setup_sha256") != setup_sha256:
        raise ValueError("worker environment setup hash does not match its script")
    image_identity = _docker_image_identity(image)
    setup_cache_key, setup_env_sha256 = _worker_toolchain_cache_identity(
        image,
        image_identity,
        setup_sha256,
        setup_env or {},
    )
    cache_root = Path("/tmp/phase4g8-worker-toolchains")
    cache_root.mkdir(parents=True, exist_ok=True)
    os.chmod(cache_root, 0o755)
    temp = cache_root / f".{setup_cache_key}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    setup_path = cache_root / f".{setup_cache_key}.{os.getpid()}.{uuid.uuid4().hex[:8]}.setup.sh"
    temp.mkdir(mode=0o755)
    setup_path.write_text(setup_script, encoding="utf-8")
    os.chmod(setup_path, 0o600)
    container_id = ""
    try:
        create_argv = ["docker", "create"]
        create_env = os.environ.copy()
        for key, value in sorted((setup_env or {}).items()):
            key_text = str(key)
            value_text = str(value)
            if not key_text or "=" in key_text or "\x00" in key_text + value_text:
                raise ValueError("worker environment setup contains an invalid environment variable")
            create_env[key_text] = value_text
            create_argv.extend(["--env", key_text])
        create_argv.extend([image, "tail", "-f", "/dev/null"])
        created = subprocess.run(
            create_argv,
            env=create_env,
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
            ["docker", "start", container_id],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=120,
        )
        subprocess.run(
            ["docker", "cp", str(setup_path), f"{container_id}:/tmp/hermes-worker-environment.sh"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=120,
        )
        subprocess.run(
            ["docker", "exec", container_id, "/bin/bash", "/tmp/hermes-worker-environment.sh"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=900,
        )
        source_fingerprint_run = subprocess.run(
            [
                "docker",
                "exec",
                "--workdir",
                "/",
                container_id,
                "/opt/miniconda3/envs/testbed/bin/python",
                "-c",
                swe_evo.environment_fingerprint_code(),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=60,
        )
        source_fingerprint = swe_evo.parse_environment_fingerprint(source_fingerprint_run.stdout)
        cache_key, _ = _worker_toolchain_cache_identity(
            image,
            image_identity,
            setup_sha256,
            setup_env or {},
            source_fingerprint["sha256"],
        )
        target = cache_root / cache_key
        python = target / "bin" / "python"
        manifest_path = target / ".hermes-phase4g8-toolchain.json"
        if python.is_file() and manifest_path.is_file():
            manifest = _load_worker_toolchain_manifest(target)
            if (
                manifest.get("cache_key") == cache_key
                and manifest["environment_fingerprint"]["sha256"]
                == source_fingerprint["sha256"]
            ):
                return target
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
        worker_fingerprint = swe_evo.fingerprint_python_environment(temp / "bin" / "python")
        if worker_fingerprint["sha256"] != source_fingerprint["sha256"]:
            raise RuntimeError(
                "extracted worker toolchain does not match prepared official environment: "
                f"source={source_fingerprint['sha256']} worker={worker_fingerprint['sha256']} "
                f"source_selected={source_fingerprint['selected_packages']} "
                f"worker_selected={worker_fingerprint['selected_packages']}"
            )
        manifest = {
            "schema": WORKER_TOOLCHAIN_MANIFEST_SCHEMA,
            "cache_key": cache_key,
            "official_image": image,
            "image_id": image_identity["image_id"],
            "image_content_identity": image_identity["content_identity"],
            "repo_digests": image_identity["repo_digests"],
            "setup_sha256": setup_sha256,
            "setup_env_sha256": setup_env_sha256,
            "resolved_environment_sha256": source_fingerprint["sha256"],
            "environment_fingerprint": worker_fingerprint,
            "parity_status": "passed",
        }
        (temp / ".hermes-phase4g8-toolchain.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
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
        setup_path.unlink(missing_ok=True)
        if temp.exists():
            shutil.rmtree(temp)
    if not python.is_file() or not manifest_path.is_file():
        raise RuntimeError("Phase 4G8 worker toolchain cache was not created")
    _load_worker_toolchain_manifest(target)
    return target


def _load_worker_toolchain_manifest(toolchain: Path) -> dict[str, Any]:
    manifest_path = toolchain / ".hermes-phase4g8-toolchain.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Phase 4G8 worker toolchain manifest is invalid") from exc
    if manifest.get("schema") != WORKER_TOOLCHAIN_MANIFEST_SCHEMA:
        raise RuntimeError("Phase 4G8 worker toolchain manifest schema is invalid")
    fingerprint = manifest.get("environment_fingerprint")
    if (
        manifest.get("parity_status") != "passed"
        or not isinstance(fingerprint, dict)
        or len(str(fingerprint.get("sha256") or "")) != 64
        or manifest.get("resolved_environment_sha256") != fingerprint.get("sha256")
    ):
        raise RuntimeError("Phase 4G8 worker toolchain parity evidence is invalid")
    return manifest


def _install_isolated_environment(paths: dict[str, Path]) -> dict[str, Optional[str]]:
    values = {
        "HOME": str(paths["home"]),
        "HERMES_HOME": str(paths["hermes_home"]),
        "HERMES_KANBAN_HOME": str(paths["hermes_home"] / "kanban"),
        "HERMES_KANBAN_DB": str(paths["db"]),
        "CODEX_HOME": str(paths["codex_home"]),
        "PATH": str(paths["worker_toolchain"] / "bin") + os.pathsep + os.environ.get("PATH", ""),
        "PHASE4G8_WORKER_TOOLCHAIN": str(paths["worker_toolchain"]),
        "HERMES_RUNTIME_CONTRIBUTION_ROOT": str(
            paths.get("runtime_contributions") or ""
        ),
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
    expected_environment_sha256: str,
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
        "approval": p4g8.PHASE4G8_CODEX_APPROVAL_POLICY,
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
        "expected_environment_sha256": expected_environment_sha256,
    }), replace=True)


def _create_real_job(
    spec: dict[str, Any],
    workspace: Path,
    run_id: str,
    *,
    max_evaluator_no_progress_streak: int = 2,
    orchestration_policy: Optional[dict[str, Any]] = None,
) -> str:
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
                **(
                    {"orchestration_policy": orchestration_policy}
                    if orchestration_policy is not None
                    else {}
                ),
                "verification_policy": {
                    "mode": "required_evaluator",
                    "assignee": "phase4g8-evaluator",
                    "require_workspace_revision": True,
                    "remediation": {
                        "mode": "resume_target_session",
                        "max_no_progress_streak": int(max_evaluator_no_progress_streak),
                        "diagnostic_batch_size": 20,
                        "max_diagnostics_chars_per_case": 4000,
                    },
                },
            },
        )


def _load_resumable_job(*, run_id: str, spec: dict[str, Any], workspace: Path) -> str:
    with kb.connect() as conn:
        rows = conn.execute("SELECT * FROM runtime_jobs ORDER BY created_at").fetchall()
        matches = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if metadata.get("phase4g8_run_id") == run_id:
                matches.append(row)
        if len(matches) != 1:
            raise ValueError("resume run must contain exactly one matching Phase 4G8 runtime job")
        job = matches[0]
        if str(job["state"]) in {"done", "failed", "cancelled"}:
            raise ValueError(f"resume runtime job is already terminal: {job['state']}")
        if Path(str(job["workspace_path"] or "")).resolve() != workspace.resolve():
            raise ValueError("resume runtime job workspace does not match the run directory")
        root = conn.execute("SELECT tenant, title FROM tasks WHERE id = ?", (job["root_task_id"],)).fetchone()
        if root is None or root["tenant"] != f"phase4g8:{run_id}":
            raise ValueError("resume runtime root task does not belong to the requested run")
        if str(spec["instance_id"]) not in str(root["title"] or ""):
            raise ValueError("resume runtime job does not match the qualification instance")
        live_workers = []
        for row in conn.execute(
            """
            SELECT DISTINCT t.worker_pid
              FROM execution_nodes n JOIN tasks t ON t.id = n.latest_task_id
             WHERE n.job_id = ? AND t.status = 'running' AND t.worker_pid IS NOT NULL
            """,
            (job["id"],),
        ):
            pid = int(row["worker_pid"])
            if _pid_is_alive(pid):
                live_workers.append(pid)
        if live_workers:
            raise RuntimeError(
                "resume run still has live worker processes: "
                + ", ".join(str(pid) for pid in sorted(live_workers))
            )
        return str(job["id"])


def _prepare_resumed_runtime_job(job_id: str) -> dict[str, Any]:
    """Convert runner-owned dead processes into resumable runtime facts."""

    with kb.connect() as conn:
        dead_rows = conn.execute(
            """
            SELECT n.id AS node_id, n.node_key, n.node_type, t.id AS task_id,
                   t.worker_pid, m.id AS materialization_id
              FROM execution_nodes n
              JOIN tasks t ON t.id = n.latest_task_id
              JOIN node_materializations m ON m.task_id = t.id AND m.node_id = n.id
             WHERE n.job_id = ? AND t.status = 'running' AND t.worker_pid IS NOT NULL
             ORDER BY n.created_at
            """,
            (job_id,),
        ).fetchall()
        live = [int(row["worker_pid"]) for row in dead_rows if _pid_is_alive(int(row["worker_pid"]))]
        if live:
            raise RuntimeError(
                "resume run still has live worker processes: "
                + ", ".join(str(pid) for pid in sorted(live))
            )
        dead = [dict(row) for row in dead_rows]
        detected = kb.detect_crashed_workers(conn) if dead else []
        missed = sorted({item["task_id"] for item in dead} - set(detected))
        if missed:
            raise RuntimeError(
                "resume could not reclaim dead worker tasks: " + ", ".join(missed)
            )
        session_sync = rk.sync_runtime_backend_sessions(conn, job_id)
        reclaimed_dead_advance_lock = _reclaim_dead_phase4g8_advance_lock(conn, job_id)
        repaired_contribution_attribution_branch = (
            _repair_resume_contribution_attribution_branch(conn, job_id)
        )
        repaired_timeout_branch = _repair_resume_timeout_branch(conn, job_id)
        requeued_incomplete_evaluators = _requeue_incomplete_evaluator_nodes(conn, job_id)
        adapted_candidate_receipts = _ingest_adaptable_candidate_receipts(
            conn,
            job_id,
        )
        adapted_structure_request_receipts = (
            _ingest_adaptable_structure_request_receipts(conn, job_id)
        )
        requeued_receipt_recoveries = _requeue_mixed_budget_receipt_failures(
            conn,
            job_id,
        )
        repaired_structure_request_branch = (
            _repair_resume_structure_request_branch(conn, job_id)
        )
        repaired_receipt_branch = (
            {
                "repaired": False,
                "reason": "structure_request_consumed_latest_receipt",
                "superseded_nodes": [],
            }
            if repaired_structure_request_branch.get("consumed")
            else _repair_resume_receipt_recovery_branch(conn, job_id)
        )
        resumed_nodes: list[str] = []
        superseded_nodes = set(repaired_timeout_branch.get("superseded_nodes") or [])
        superseded_nodes.update(
            repaired_contribution_attribution_branch.get("superseded_nodes") or []
        )
        superseded_nodes.update(
            repaired_receipt_branch.get("superseded_nodes") or []
        )
        superseded_nodes.update(
            repaired_structure_request_branch.get("superseded_nodes") or []
        )
        now = int(time.time())
        for item in dead:
            if item["node_key"] in superseded_nodes:
                continue
            node = conn.execute("SELECT * FROM execution_nodes WHERE id = ?", (item["node_id"],)).fetchone()
            if node is None or node["state"] in rk.TERMINAL_NODE_STATES:
                continue
            materialization = conn.execute(
                "SELECT * FROM node_materializations WHERE id = ?",
                (item["materialization_id"],),
            ).fetchone()
            rk._mark_backend_worker_session_interrupted(
                conn,
                dict(node),
                dict(materialization),
                "phase4g8_runner_interrupted",
                now=now,
            )
            metadata = json.loads(materialization["metadata_json"] or "{}")
            metadata["runner_resume"] = {
                "reason": "phase4g8_runner_interrupted",
                "recovered_at": now,
                "prior_worker_pid": item["worker_pid"],
            }
            conn.execute(
                """
                UPDATE node_materializations
                   SET status = 'crashed', completed_at = COALESCE(completed_at, ?), metadata_json = ?
                 WHERE id = ?
                """,
                (now, json.dumps(metadata, ensure_ascii=False, sort_keys=True), item["materialization_id"]),
            )
            conn.execute(
                """
                UPDATE tasks
                   SET status = 'blocked', claim_lock = NULL, claim_expires = NULL,
                       worker_pid = NULL, current_run_id = NULL,
                       result = COALESCE(result, 'Superseded by Phase 4G8 runner resume.')
                 WHERE id = ? AND status = 'ready'
                """,
                (item["task_id"],),
            )
            conn.execute(
                """
                UPDATE execution_nodes
                   SET state = 'ready', latest_task_id = NULL, latest_run_id = NULL,
                       output_summary = NULL, completed_at = NULL, updated_at = ?
                 WHERE id = ?
                """,
                (now, item["node_id"]),
            )
            rk._event(
                conn,
                job_id,
                "phase4g8_runner_resume_scheduled",
                {
                    "node_key": item["node_key"],
                    "materialization_id": item["materialization_id"],
                    "task_id": item["task_id"],
                    "prior_worker_pid": item["worker_pid"],
                    "recovery_reason": "runner_process_interrupted",
                },
                node_id=item["node_id"],
                task_id=item["task_id"],
            )
            resumed_nodes.append(str(item["node_key"]))
        if (
            resumed_nodes
            or repaired_timeout_branch.get("repaired")
            or repaired_receipt_branch.get("repaired")
            or repaired_structure_request_branch.get("repaired")
            or requeued_incomplete_evaluators
            or adapted_candidate_receipts
            or adapted_structure_request_receipts
            or requeued_receipt_recoveries
        ):
            conn.execute(
                "UPDATE runtime_jobs SET state = 'active', updated_at = ? WHERE id = ?",
                (now, job_id),
            )
        return {
            "dead_worker_tasks": [item["task_id"] for item in dead],
            "detected_crashed_tasks": list(detected),
            "session_sync": session_sync,
            "reclaimed_dead_advance_lock": reclaimed_dead_advance_lock,
            "resumed_nodes": resumed_nodes,
            "timeout_branch_repair": repaired_timeout_branch,
            "contribution_attribution_branch_repair": (
                repaired_contribution_attribution_branch
            ),
            "receipt_branch_repair": repaired_receipt_branch,
            "structure_request_branch_repair": (
                repaired_structure_request_branch
            ),
            "requeued_incomplete_evaluators": requeued_incomplete_evaluators,
            "adapted_candidate_receipts": adapted_candidate_receipts,
            "adapted_structure_request_receipts": (
                adapted_structure_request_receipts
            ),
            "requeued_receipt_recoveries": requeued_receipt_recoveries,
        }


def _repair_resume_contribution_attribution_branch(
    conn: sqlite3.Connection,
    job_id: str,
) -> dict[str, Any]:
    """Replay a receipt rejected by the pre-lineage contribution validator."""

    failure = conn.execute(
        """
        SELECT event.*, node.node_key, node.state AS node_state
          FROM execution_events event
          JOIN execution_nodes node ON node.id = event.node_id
         WHERE event.job_id = ?
           AND event.event_type = 'contribution_attribution_failed'
         ORDER BY event.id DESC LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    if failure is None:
        return {"repaired": False, "reason": "no_contribution_attribution_failure"}
    if failure["node_state"] != "failed":
        return {"repaired": False, "reason": "attribution_node_not_failed"}
    failure_payload = json.loads(failure["payload_json"] or "{}")
    violations = [str(value) for value in failure_payload.get("violations") or []]
    prefix = "modified_contribution_not_observed:"
    if not violations or any(not value.startswith(prefix) for value in violations):
        return {"repaired": False, "reason": "attribution_failure_not_lineage_only"}
    missing_artifacts = {value.removeprefix(prefix) for value in violations}
    prior_events = conn.execute(
        """
        SELECT id, payload_json FROM execution_events
         WHERE job_id = ? AND node_id = ?
           AND event_type = 'contribution_attribution_verified' AND id < ?
         ORDER BY id DESC
        """,
        (job_id, failure["node_id"], failure["id"]),
    ).fetchall()
    prior_modified: set[str] = set()
    for event in prior_events:
        payload = json.loads(event["payload_json"] or "{}")
        prior_modified.update(
            str(value) for value in payload.get("modified_contributions") or []
        )
    if not missing_artifacts <= prior_modified:
        return {"repaired": False, "reason": "verified_lineage_missing"}
    materialization = conn.execute(
        """
        SELECT materialization.*, run.metadata AS run_metadata
          FROM node_materializations materialization
          JOIN task_runs run ON run.task_id = materialization.task_id
             AND run.id = (
                 SELECT MAX(latest.id) FROM task_runs latest
                  WHERE latest.task_id = materialization.task_id
             )
         WHERE materialization.node_id = ?
           AND materialization.task_id = ?
         ORDER BY materialization.attempt DESC LIMIT 1
        """,
        (failure["node_id"], failure["task_id"]),
    ).fetchone()
    if materialization is None:
        return {"repaired": False, "reason": "failed_materialization_missing"}
    run_metadata = json.loads(materialization["run_metadata"] or "{}")
    receipt = run_metadata.get("runtime_receipt")
    if not isinstance(receipt, dict):
        return {"repaired": False, "reason": "failed_receipt_missing"}
    receipt_modified = {
        str(value) for value in receipt.get("modified_contributions") or []
    }
    if not missing_artifacts <= receipt_modified:
        return {"repaired": False, "reason": "failed_receipt_lineage_mismatch"}
    job = conn.execute(
        "SELECT workspace_path FROM runtime_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    current_revision = collect_git_evidence(job["workspace_path"]).get(
        "workspace_revision"
    )
    if not receipt.get("workspace_revision") or current_revision != receipt.get(
        "workspace_revision"
    ):
        raise RuntimeError(
            "cannot repair contribution attribution after candidate revision changed"
        )
    speculative_nodes = conn.execute(
        """
        SELECT * FROM execution_nodes
         WHERE job_id = ? AND id != ? AND node_type = 'strategy_update'
           AND created_at >= ? AND state NOT IN ('superseded', 'cancelled')
         ORDER BY created_at
        """,
        (job_id, failure["node_id"], failure["created_at"]),
    ).fetchall()
    for node in speculative_nodes:
        produced_receipt = conn.execute(
            """
            SELECT 1
              FROM node_materializations materialization
              JOIN task_runs run ON run.task_id = materialization.task_id
             WHERE materialization.node_id = ?
               AND json_type(run.metadata, '$.runtime_receipt') = 'object'
             LIMIT 1
            """,
            (node["id"],),
        ).fetchone()
        if produced_receipt is not None:
            raise RuntimeError(
                "cannot repair contribution attribution after strategy receipt"
            )
    now = int(time.time())
    for node in speculative_nodes:
        conn.execute(
            """
            UPDATE tasks
               SET status = 'archived', claim_lock = NULL, claim_expires = NULL,
                   worker_pid = NULL, current_run_id = NULL,
                   result = COALESCE(
                       result,
                       'Superseded after contribution attribution lineage repair.'
                   )
             WHERE id IN (
                 SELECT task_id FROM node_materializations WHERE node_id = ?
             )
            """,
            (node["id"],),
        )
        conn.execute(
            """
            UPDATE node_materializations
               SET status = CASE
                       WHEN status IN ('candidate_ready', 'succeeded', 'failed', 'blocked')
                       THEN status ELSE 'crashed' END,
                   completed_at = COALESCE(completed_at, ?)
             WHERE node_id = ?
            """,
            (now, node["id"]),
        )
        conn.execute(
            """
            UPDATE execution_nodes
               SET state = 'superseded', latest_task_id = NULL,
                   latest_run_id = NULL,
                   output_summary =
                       'Superseded after contribution attribution lineage repair.',
                   completed_at = ?, updated_at = ?
             WHERE id = ?
            """,
            (now, now, node["id"]),
        )
        conn.execute(
            """
            UPDATE backend_worker_sessions
               SET status = 'completed', completed_at = COALESCE(completed_at, ?),
                   updated_at = ?
             WHERE node_id = ?
            """,
            (now, now, node["id"]),
        )
    conn.execute(
        """
        UPDATE execution_nodes
           SET state = 'running', completed_at = NULL, updated_at = ?
         WHERE id = ? AND state = 'failed'
        """,
        (now, failure["node_id"]),
    )
    conn.execute(
        """
        UPDATE node_materializations
           SET status = 'running', completed_at = NULL
         WHERE id = ?
        """,
        (materialization["id"],),
    )
    if not rk.ingest_runtime_node_evidence(conn, str(failure["node_id"])):
        raise RuntimeError("contribution attribution receipt replay was not ingested")
    rk._event(
        conn,
        job_id,
        "phase4g8_contribution_attribution_branch_repaired",
        {
            "primary_node_key": failure["node_key"],
            "failed_event_id": failure["id"],
            "replayed_materialization_id": materialization["id"],
            "workspace_revision": current_revision,
            "restored_lineage": sorted(missing_artifacts),
            "superseded_nodes": [str(node["node_key"]) for node in speculative_nodes],
        },
        node_id=failure["node_id"],
        task_id=failure["task_id"],
    )
    return {
        "repaired": True,
        "primary_node_key": str(failure["node_key"]),
        "restored_lineage": sorted(missing_artifacts),
        "superseded_nodes": [str(node["node_key"]) for node in speculative_nodes],
        "workspace_revision": current_revision,
    }


def _reclaim_dead_phase4g8_advance_lock(
    conn: sqlite3.Connection,
    job_id: str,
) -> dict[str, Any]:
    """Release only a Phase 4G8 runtime-daemon lock whose owner PID is dead."""

    row = conn.execute(
        "SELECT advance_lock, claim_expires_at, metadata_json FROM runtime_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    if row is None or not row["advance_lock"]:
        return {"reclaimed": False, "reason": "no_advance_lock"}
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        metadata = {}
    owner = str(row["advance_lock"])
    parts = owner.split(":")
    if not metadata.get("phase4g8_run_id") or len(parts) < 4 or parts[0] != "runtime-daemon":
        return {"reclaimed": False, "reason": "lock_not_phase4g8_runtime_daemon"}
    try:
        owner_pid = int(parts[-2])
    except ValueError:
        return {"reclaimed": False, "reason": "lock_owner_pid_invalid"}
    if _pid_is_alive(owner_pid):
        return {
            "reclaimed": False,
            "reason": "lock_owner_alive",
            "owner_pid": owner_pid,
        }
    result = rk.release_runtime_advance_lock(
        conn,
        job_id,
        owner=owner,
        force=True,
    )
    if not result.get("released"):
        return {
            "reclaimed": False,
            "reason": "lock_release_rejected",
            "owner_pid": owner_pid,
        }
    rk._event(
        conn,
        job_id,
        "phase4g8_dead_advance_lock_reclaimed",
        {
            "owner": owner,
            "owner_pid": owner_pid,
            "prior_claim_expires_at": row["claim_expires_at"],
        },
    )
    return {
        "reclaimed": True,
        "owner": owner,
        "owner_pid": owner_pid,
        "prior_claim_expires_at": row["claim_expires_at"],
    }


def _requeue_incomplete_evaluator_nodes(
    conn: sqlite3.Connection,
    job_id: str,
) -> list[str]:
    """Retry fixed-target evaluators whose prior feedback could not be extracted."""

    rows = conn.execute(
        """
        SELECT n.*, tr.metadata AS run_metadata
          FROM execution_nodes n
          JOIN task_runs tr ON tr.task_id = n.latest_task_id
         WHERE n.job_id = ? AND n.node_type = 'verification' AND n.state = 'blocked'
           AND tr.id = (
               SELECT MAX(latest.id) FROM task_runs latest
                WHERE latest.task_id = n.latest_task_id
           )
         ORDER BY n.created_at
        """,
        (job_id,),
    ).fetchall()
    now = int(time.time())
    requeued: list[str] = []
    for row in rows:
        try:
            receipt = json.loads(row["run_metadata"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        result = receipt.get("official_evaluator_result")
        coverage = result.get("feedback_coverage") if isinstance(result, dict) else None
        if not (
            receipt.get("infrastructure_invalid") is True
            and isinstance(result, dict)
            and (
                result.get("error") == "evaluator_feedback_extraction_incomplete"
                or (
                    isinstance(coverage, dict)
                    and coverage.get("status") == "extraction_incomplete"
                )
            )
        ):
            continue
        conn.execute(
            """
            UPDATE execution_nodes
               SET state = 'ready', latest_task_id = NULL, latest_run_id = NULL,
                   output_summary = NULL, completed_at = NULL, updated_at = ?
             WHERE id = ? AND state = 'blocked'
            """,
            (now, row["id"]),
        )
        rk._event(
            conn,
            job_id,
            "phase4g8_incomplete_evaluator_requeued",
            {
                "node_key": row["node_key"],
                "prior_task_id": row["latest_task_id"],
                "reason": "evaluator_feedback_extraction_incomplete",
            },
            node_id=row["id"],
            task_id=row["latest_task_id"],
        )
        requeued.append(str(row["node_key"]))
    return requeued


def _ingest_adaptable_candidate_receipts(
    conn: sqlite3.Connection,
    job_id: str,
) -> list[str]:
    """Recover terminal Phase 4G8 candidate receipts accepted by a new adapter."""

    rows = conn.execute(
        """
        SELECT n.*, m.id AS materialization_id, m.task_id AS materialization_task_id
          FROM execution_nodes n
          JOIN node_materializations m ON m.node_id = n.id
         WHERE n.job_id = ? AND n.node_type != 'verification' AND n.state = 'failed'
           AND m.attempt = (
               SELECT MAX(latest.attempt) FROM node_materializations latest
                WHERE latest.node_id = n.id
           )
           AND m.status = 'receipt_invalid'
         ORDER BY n.created_at
        """,
        (job_id,),
    ).fetchall()
    recovered: list[str] = []
    for row in rows:
        snapshot = kb.task_progress_snapshot(
            conn,
            row["materialization_task_id"],
        )
        if snapshot is None or not isinstance(snapshot.evidence, dict):
            continue
        evidence = dict(snapshot.evidence)
        if not rk._is_codex_lane_evidence(evidence):
            continue
        adapted = rk._runtime_receipt_from_evidence(
            evidence,
            dict(row),
            conn=conn,
        )
        if not (
            isinstance(adapted, dict)
            and adapted.get("receipt_adapter") == "phase4g8_candidate_shape_v1"
        ):
            continue
        job = conn.execute(
            "SELECT workspace_path FROM runtime_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        current_revision = collect_git_evidence(job["workspace_path"]).get(
            "workspace_revision"
        )
        if adapted.get("workspace_revision") != current_revision:
            raise RuntimeError(
                "cannot adapt terminal candidate receipt after workspace revision changed"
            )
        now = int(time.time())
        conn.execute(
            """
            UPDATE execution_nodes
               SET state = 'running', output_summary = NULL,
                   completed_at = NULL, updated_at = ?
             WHERE id = ? AND state = 'failed'
            """,
            (now, row["id"]),
        )
        if not rk.ingest_runtime_node_evidence(conn, row["id"]):
            raise RuntimeError("adaptable Phase 4G8 candidate receipt was not ingested")
        recovered.append(str(row["node_key"]))
    return recovered


def _ingest_adaptable_structure_request_receipts(
    conn: sqlite3.Connection,
    job_id: str,
) -> list[str]:
    """Recover a terminal Phase 4G8 structure request accepted by its adapter."""

    rows = conn.execute(
        """
        SELECT n.*, m.id AS materialization_id, m.task_id AS materialization_task_id
          FROM execution_nodes n
          JOIN node_materializations m ON m.node_id = n.id
         WHERE n.job_id = ? AND n.node_type != 'verification' AND n.state = 'failed'
           AND m.attempt = (
               SELECT MAX(latest.attempt) FROM node_materializations latest
                WHERE latest.node_id = n.id
           )
           AND m.status = 'receipt_invalid'
         ORDER BY n.created_at
        """,
        (job_id,),
    ).fetchall()
    recovered: list[str] = []
    for row in rows:
        snapshot = kb.task_progress_snapshot(
            conn,
            row["materialization_task_id"],
        )
        if snapshot is None or not isinstance(snapshot.evidence, dict):
            continue
        evidence = dict(snapshot.evidence)
        if not rk._is_codex_lane_evidence(evidence):
            continue
        adapted = rk._runtime_receipt_from_evidence(
            evidence,
            dict(row),
            conn=conn,
        )
        if not (
            isinstance(adapted, dict)
            and adapted.get("receipt_adapter")
            == "phase4g8_structure_request_shape_v1"
        ):
            continue
        job = conn.execute(
            "SELECT workspace_path FROM runtime_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        current_revision = collect_git_evidence(job["workspace_path"]).get(
            "workspace_revision"
        )
        if adapted.get("workspace_revision") != current_revision:
            raise RuntimeError(
                "cannot adapt terminal structure request after workspace revision changed"
            )
        now = int(time.time())
        conn.execute(
            """
            UPDATE execution_nodes
               SET state = 'running', output_summary = NULL,
                   completed_at = NULL, updated_at = ?
             WHERE id = ? AND state = 'failed'
            """,
            (now, row["id"]),
        )
        if not rk.ingest_runtime_node_evidence(conn, row["id"]):
            raise RuntimeError("adaptable Phase 4G8 structure request was not ingested")
        recovered.append(str(row["node_key"]))
    return recovered


def _requeue_mixed_budget_receipt_failures(
    conn: sqlite3.Connection,
    job_id: str,
) -> list[str]:
    """Repair receipt failures that an earlier infra failure wrongly exhausted."""

    rows = conn.execute(
        """
        SELECT n.*, m.id AS materialization_id, m.status AS materialization_status,
               m.task_id AS materialization_task_id, m.attempt
          FROM execution_nodes n
          JOIN node_materializations m ON m.node_id = n.id
         WHERE n.job_id = ? AND n.node_type != 'verification' AND n.state = 'failed'
           AND m.attempt = (
               SELECT MAX(latest.attempt) FROM node_materializations latest
                WHERE latest.node_id = n.id
           )
           AND m.status IN ('receipt_missing', 'receipt_invalid')
         ORDER BY n.created_at
        """,
        (job_id,),
    ).fetchall()
    now = int(time.time())
    receipt_limit = int(
        rk.DEFAULT_RUNTIME_RECOVERY_POLICY["receipt_recovery_limit"]
    )
    requeued: list[str] = []
    for row in rows:
        event = conn.execute(
            """
            SELECT payload_json FROM execution_events
             WHERE job_id = ? AND node_id = ?
               AND event_type = 'node_recovery_not_retryable'
             ORDER BY id DESC LIMIT 1
            """,
            (job_id, row["id"]),
        ).fetchone()
        try:
            event_payload = json.loads(event["payload_json"] or "{}") if event else {}
        except (TypeError, json.JSONDecodeError):
            continue
        if event_payload.get("recovery_reason") not in {
            "receipt_missing",
            "receipt_invalid",
        }:
            continue
        receipt_count = conn.execute(
            """
            SELECT COUNT(*) AS count FROM node_materializations
             WHERE node_id = ? AND status IN ('receipt_missing', 'receipt_invalid')
            """,
            (row["id"],),
        ).fetchone()["count"]
        infra_count = conn.execute(
            """
            SELECT COUNT(*) AS count FROM node_materializations
             WHERE node_id = ? AND status IN ('lost', 'stale', 'timed_out', 'crashed')
            """,
            (row["id"],),
        ).fetchone()["count"]
        if int(receipt_count or 0) > receipt_limit or int(infra_count or 0) < 1:
            continue
        conn.execute(
            """
            UPDATE execution_nodes
               SET state = 'ready', latest_task_id = NULL, latest_run_id = NULL,
                   output_summary = NULL, completed_at = NULL, updated_at = ?
             WHERE id = ? AND state = 'failed'
            """,
            (now, row["id"]),
        )
        rk._event(
            conn,
            job_id,
            "phase4g8_receipt_recovery_requeued",
            {
                "node_key": row["node_key"],
                "prior_task_id": row["materialization_task_id"],
                "prior_materialization_id": row["materialization_id"],
                "prior_materialization_status": row["materialization_status"],
                "receipt_failure_count": int(receipt_count or 0),
                "infra_failure_count": int(infra_count or 0),
                "reason": "recovery_budget_category_repair",
            },
            node_id=row["id"],
            task_id=row["materialization_task_id"],
        )
        requeued.append(str(row["node_key"]))
    return requeued


def _repair_resume_receipt_recovery_branch(
    conn: sqlite3.Connection,
    job_id: str,
) -> dict[str, Any]:
    """Supersede a speculative strategy node created from a bad receipt."""

    repair_event = conn.execute(
        """
        SELECT * FROM execution_events
         WHERE job_id = ? AND event_type = 'phase4g8_receipt_recovery_requeued'
         ORDER BY id DESC LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    if repair_event is None or repair_event["node_id"] is None:
        return {"repaired": False, "reason": "no_receipt_recovery_requeue"}
    primary = conn.execute(
        "SELECT * FROM execution_nodes WHERE id = ? AND job_id = ?",
        (repair_event["node_id"], job_id),
    ).fetchone()
    if primary is None:
        return {"repaired": False, "reason": "primary_node_missing"}
    receipt_materialization = conn.execute(
        """
        SELECT m.*, tr.metadata AS run_metadata
          FROM node_materializations m
          LEFT JOIN task_runs tr ON tr.task_id = m.task_id
             AND tr.id = (
                 SELECT MAX(latest.id) FROM task_runs latest
                  WHERE latest.task_id = m.task_id
             )
         WHERE m.node_id = ? AND m.status IN ('receipt_missing', 'receipt_invalid')
         ORDER BY m.attempt DESC LIMIT 1
        """,
        (primary["id"],),
    ).fetchone()
    if receipt_materialization is None:
        return {"repaired": False, "reason": "receipt_failure_missing"}
    strategy_nodes = conn.execute(
        """
        SELECT * FROM execution_nodes
         WHERE job_id = ? AND node_type = 'strategy_update'
           AND created_at >= ?
         ORDER BY created_at
        """,
        (job_id, receipt_materialization["completed_at"] or 0),
    ).fetchall()
    if not strategy_nodes:
        return {"repaired": False, "reason": "no_speculative_strategy_branch"}
    if len(strategy_nodes) != 1:
        raise RuntimeError(
            "cannot repair receipt recovery branch with multiple strategy nodes"
        )
    strategy = strategy_nodes[0]
    terminal_fact = conn.execute(
        """
        SELECT 1 FROM execution_events
         WHERE job_id = ? AND node_id = ?
           AND event_type IN ('node_candidate_ready', 'node_completed', 'node_failed')
         LIMIT 1
        """,
        (job_id, strategy["id"]),
    ).fetchone()
    if terminal_fact is not None:
        raise RuntimeError(
            "cannot repair receipt recovery branch after strategy terminal evidence"
        )
    try:
        receipt_run_metadata = json.loads(
            receipt_materialization["run_metadata"] or "{}"
        )
    except (TypeError, json.JSONDecodeError):
        return {"repaired": False, "reason": "receipt_run_metadata_invalid"}
    receipt = receipt_run_metadata.get("runtime_receipt")
    expected_revision = (
        receipt.get("workspace_revision") if isinstance(receipt, dict) else None
    )
    job = conn.execute(
        "SELECT workspace_path FROM runtime_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    current_revision = collect_git_evidence(job["workspace_path"]).get(
        "workspace_revision"
    )
    if not expected_revision or current_revision != expected_revision:
        raise RuntimeError(
            "cannot repair receipt recovery branch after candidate revision changed"
        )
    valid_strategy_receipt = False
    strategy_tasks = conn.execute(
        """
        SELECT t.id, t.result, tr.metadata
          FROM node_materializations m
          JOIN tasks t ON t.id = m.task_id
          LEFT JOIN task_runs tr ON tr.task_id = t.id
             AND tr.id = (
                 SELECT MAX(latest.id) FROM task_runs latest
                  WHERE latest.task_id = t.id
             )
         WHERE m.node_id = ?
         ORDER BY m.attempt
        """,
        (strategy["id"],),
    ).fetchall()
    for task in strategy_tasks:
        try:
            metadata = json.loads(task["metadata"] or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        if isinstance(metadata.get("runtime_receipt"), dict):
            valid_strategy_receipt = True
            break
    if valid_strategy_receipt:
        raise RuntimeError(
            "cannot repair receipt recovery branch after strategy receipt"
        )
    now = int(time.time())
    task_ids = [str(task["id"]) for task in strategy_tasks]
    if task_ids:
        placeholders = ",".join("?" for _ in task_ids)
        conn.execute(
            f"""
            UPDATE tasks
               SET status = 'archived', claim_lock = NULL, claim_expires = NULL,
                   worker_pid = NULL, current_run_id = NULL,
                   result = COALESCE(result, 'Superseded after Phase 4G8 receipt recovery repair.')
             WHERE id IN ({placeholders})
            """,
            task_ids,
        )
    conn.execute(
        """
        UPDATE node_materializations
           SET status = CASE
                   WHEN status IN ('candidate_ready', 'succeeded', 'failed', 'blocked')
                   THEN status ELSE 'crashed' END,
               completed_at = COALESCE(completed_at, ?)
         WHERE node_id = ?
        """,
        (now, strategy["id"]),
    )
    conn.execute(
        """
        UPDATE execution_nodes
           SET state = 'superseded', latest_task_id = NULL, latest_run_id = NULL,
               output_summary = 'Superseded after receipt recovery branch repair.',
               completed_at = ?, updated_at = ?
         WHERE id = ?
        """,
        (now, now, strategy["id"]),
    )
    conn.execute(
        """
        UPDATE backend_worker_sessions
           SET status = 'completed', completed_at = COALESCE(completed_at, ?),
               updated_at = ?
         WHERE node_id = ?
        """,
        (now, now, strategy["id"]),
    )
    rk._event(
        conn,
        job_id,
        "phase4g8_receipt_recovery_branch_repaired",
        {
            "primary_node_key": primary["node_key"],
            "superseded_node_key": strategy["node_key"],
            "receipt_materialization_id": receipt_materialization["id"],
            "workspace_revision": current_revision,
            "reason": "speculative_strategy_created_from_invalid_receipt",
        },
        node_id=primary["id"],
    )
    return {
        "repaired": True,
        "primary_node_key": str(primary["node_key"]),
        "superseded_nodes": [str(strategy["node_key"])],
        "workspace_revision": current_revision,
    }


def _repair_resume_structure_request_branch(
    conn: sqlite3.Connection,
    job_id: str,
) -> dict[str, Any]:
    """Supersede speculative strategy work after a structure request is accepted."""

    adapter_event = conn.execute(
        """
        SELECT * FROM execution_events
         WHERE job_id = ? AND event_type = 'runtime_receipt_adapted'
           AND json_extract(payload_json, '$.adapter') =
               'phase4g8_structure_request_shape_v1'
         ORDER BY id DESC LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    if adapter_event is None or adapter_event["node_id"] is None:
        return {
            "repaired": False,
            "consumed": False,
            "reason": "no_adapted_structure_request",
            "superseded_nodes": [],
        }
    materialization = conn.execute(
        """
        SELECT m.*, tr.ended_at AS receipt_ended_at
          FROM node_materializations m
          LEFT JOIN task_runs tr ON tr.task_id = m.task_id
             AND tr.id = (
                 SELECT MAX(latest.id) FROM task_runs latest
                  WHERE latest.task_id = m.task_id
             )
         WHERE m.node_id = ? AND m.task_id = ?
         ORDER BY m.attempt DESC LIMIT 1
        """,
        (adapter_event["node_id"], adapter_event["task_id"]),
    ).fetchone()
    if materialization is None:
        raise RuntimeError("adapted structure request materialization is missing")
    all_strategy_nodes = conn.execute(
        """
        SELECT * FROM execution_nodes
         WHERE job_id = ? AND node_type = 'strategy_update'
           AND created_at >= ?
         ORDER BY created_at
        """,
        (
            job_id,
            materialization["receipt_ended_at"]
            or materialization["completed_at"]
            or materialization["created_at"],
        ),
    ).fetchall()
    if not all_strategy_nodes:
        return {
            "repaired": False,
            "consumed": True,
            "reason": "no_speculative_strategy_branch",
            "superseded_nodes": [],
        }
    now = int(time.time())
    superseded: list[str] = []
    for strategy in all_strategy_nodes:
        strategy_task_ids = {
            str(row["task_id"])
            for row in conn.execute(
                "SELECT task_id FROM node_materializations WHERE node_id = ?",
                (strategy["id"],),
            ).fetchall()
            if row["task_id"]
        }
        if strategy["latest_task_id"]:
            strategy_task_ids.add(str(strategy["latest_task_id"]))
        if strategy_task_ids:
            placeholders = ",".join("?" for _ in strategy_task_ids)
            conn.execute(
                f"""
                UPDATE tasks
                   SET status = 'archived', claim_lock = NULL, claim_expires = NULL,
                       worker_pid = NULL, current_run_id = NULL,
                       result = COALESCE(
                           result,
                           'Superseded by accepted worker structure request.'
                       )
                 WHERE id IN ({placeholders})
                """,
                tuple(sorted(strategy_task_ids)),
            )
        conn.execute(
            """
            UPDATE node_materializations
               SET status = CASE
                       WHEN status IN (
                           'candidate_ready', 'succeeded', 'failed', 'blocked'
                       ) THEN status ELSE 'crashed' END,
                   completed_at = COALESCE(completed_at, ?)
             WHERE node_id = ?
            """,
            (now, strategy["id"]),
        )
        conn.execute(
            """
            UPDATE backend_worker_sessions
               SET status = 'completed', completed_at = COALESCE(completed_at, ?),
                   updated_at = ?
             WHERE node_id = ?
            """,
            (now, now, strategy["id"]),
        )
        if strategy["state"] == "superseded":
            continue
        terminal_fact = conn.execute(
            """
            SELECT 1 FROM execution_events
             WHERE job_id = ? AND node_id = ?
               AND event_type IN (
                   'node_candidate_ready', 'node_completed', 'node_failed'
               )
             LIMIT 1
            """,
            (job_id, strategy["id"]),
        ).fetchone()
        if terminal_fact is not None:
            raise RuntimeError(
                "cannot supersede strategy work with terminal evidence after "
                "a structure request"
            )
        conn.execute(
            """
            UPDATE execution_nodes
               SET state = 'superseded', latest_task_id = NULL,
                   latest_run_id = NULL,
                   output_summary =
                       'Superseded after worker structure request acceptance.',
                   completed_at = ?, updated_at = ?
             WHERE id = ?
            """,
            (now, now, strategy["id"]),
        )
        superseded.append(str(strategy["node_key"]))
    if not superseded:
        return {
            "repaired": False,
            "consumed": True,
            "reason": "speculative_strategy_already_superseded",
            "superseded_nodes": [],
        }
    rk._event(
        conn,
        job_id,
        "phase4g8_structure_request_branch_repaired",
        {
            "source_node_id": adapter_event["node_id"],
            "source_task_id": adapter_event["task_id"],
            "superseded_nodes": superseded,
            "reason": "accepted_structure_request_precedes_speculative_strategy",
        },
        node_id=adapter_event["node_id"],
        task_id=adapter_event["task_id"],
    )
    return {
        "repaired": True,
        "consumed": True,
        "reason": "accepted_structure_request",
        "superseded_nodes": superseded,
    }


def _repair_resume_timeout_branch(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    """Repair the narrow pre-fix state where resume was stale-timed-out first."""

    primary = conn.execute(
        """
        SELECT n.*, m.id AS materialization_id, m.task_id AS materialization_task_id,
               m.status AS materialization_status
          FROM execution_nodes n
          JOIN node_materializations m ON m.node_id = n.id
         WHERE n.job_id = ? AND n.node_type = 'implementation'
         ORDER BY n.created_at, m.attempt DESC LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    if primary is None or primary["state"] != "failed" or primary["materialization_status"] != "timed_out":
        return {"repaired": False, "reason": "no_matching_timeout_branch"}
    timeout_event = conn.execute(
        """
        SELECT 1 FROM execution_events
         WHERE job_id = ? AND node_id = ? AND event_type = 'worker_run_timeout'
         LIMIT 1
        """,
        (job_id, primary["id"]),
    ).fetchone()
    session = conn.execute(
        "SELECT * FROM backend_worker_sessions WHERE node_id = ? ORDER BY created_at LIMIT 1",
        (primary["id"],),
    ).fetchone()
    if timeout_event is None or session is None or session["status"] != "interrupted":
        return {"repaired": False, "reason": "timeout_provenance_or_session_missing"}
    job = conn.execute("SELECT workspace_path FROM runtime_jobs WHERE id = ?", (job_id,)).fetchone()
    current_revision = rk._workspace_revision(job["workspace_path"])
    if session["workspace_revision"] != current_revision:
        raise RuntimeError("cannot repair resume timeout branch after workspace revision changed")
    recovery_nodes = conn.execute(
        """
        SELECT n.*, m.id AS materialization_id, m.task_id AS materialization_task_id,
               m.status AS materialization_status
          FROM execution_nodes n
          LEFT JOIN node_materializations m ON m.node_id = n.id
         WHERE n.job_id = ? AND n.created_at > ? AND n.node_type = 'strategy_update'
         ORDER BY n.created_at
        """,
        (job_id, primary["created_at"]),
    ).fetchall()
    if len(recovery_nodes) != 1:
        return {"repaired": False, "reason": "recovery_branch_not_unique"}
    recovery = recovery_nodes[0]
    task = conn.execute(
        "SELECT result FROM tasks WHERE id = ?",
        (recovery["materialization_task_id"],),
    ).fetchone()
    if task is None or task["result"]:
        raise RuntimeError("cannot repair resume timeout branch after recovery produced a receipt")
    now = int(time.time())
    conn.execute(
        """
        UPDATE tasks
           SET status = 'blocked', claim_lock = NULL, claim_expires = NULL,
               worker_pid = NULL, current_run_id = NULL,
               result = 'Superseded after Phase 4G8 resume timeout repair.'
         WHERE id IN (?, ?)
        """,
        (primary["materialization_task_id"], recovery["materialization_task_id"]),
    )
    conn.execute(
        """
        UPDATE node_materializations
           SET status = 'crashed', completed_at = COALESCE(completed_at, ?)
         WHERE id = ?
        """,
        (now, recovery["materialization_id"]),
    )
    conn.execute(
        """
        UPDATE execution_nodes
           SET state = 'superseded', latest_task_id = NULL, latest_run_id = NULL,
               output_summary = 'Superseded after runner resume timeout misclassification.',
               completed_at = ?, updated_at = ?
         WHERE id = ?
        """,
        (now, now, recovery["id"]),
    )
    conn.execute(
        """
        UPDATE execution_nodes
           SET state = 'ready', latest_task_id = NULL, latest_run_id = NULL,
               output_summary = NULL, completed_at = NULL, updated_at = ?
         WHERE id = ?
        """,
        (now, primary["id"]),
    )
    recovery_session = conn.execute(
        "SELECT id FROM backend_worker_sessions WHERE node_id = ?",
        (recovery["id"],),
    ).fetchone()
    if recovery_session is not None:
        conn.execute(
            """
            UPDATE backend_worker_sessions
               SET status = 'completed', completed_at = COALESCE(completed_at, ?), updated_at = ?
             WHERE id = ?
            """,
            (now, now, recovery_session["id"]),
        )
    rk._event(
        conn,
        job_id,
        "phase4g8_resume_timeout_repaired",
        {
            "primary_node_key": primary["node_key"],
            "superseded_node_key": recovery["node_key"],
            "workspace_revision": current_revision,
            "preserved_timeout_events": True,
        },
        node_id=primary["id"],
    )
    return {
        "repaired": True,
        "primary_node_key": str(primary["node_key"]),
        "superseded_nodes": [str(recovery["node_key"])],
        "workspace_revision": current_revision,
    }


def _reconstruct_resume_state(job_id: str, *, case_size: str) -> dict[str, Any]:
    with kb.connect() as conn:
        materializations = conn.execute(
            "SELECT task_id, status FROM node_materializations WHERE job_id = ? ORDER BY created_at",
            (job_id,),
        ).fetchall()
        sessions = conn.execute(
            "SELECT resume_count FROM backend_worker_sessions WHERE job_id = ?",
            (job_id,),
        ).fetchall()
        supervisor_starts = conn.execute(
            """
            SELECT id FROM execution_events
             WHERE job_id = ? AND event_type = 'runtime_supervisor_started'
             ORDER BY id
            """,
            (job_id,),
        ).fetchall()
        dead_running = []
        for row in conn.execute(
            """
            SELECT t.id, t.worker_pid
              FROM execution_nodes n JOIN tasks t ON t.id = n.latest_task_id
             WHERE n.job_id = ? AND t.status = 'running' AND t.worker_pid IS NOT NULL
            """,
            (job_id,),
        ):
            if not _pid_is_alive(int(row["worker_pid"])):
                dead_running.append(str(row["id"]))
        if len(dead_running) > 1:
            raise RuntimeError("resume run has multiple dead running tasks and requires operator repair")
        evaluator_attempts = _official_evaluator_attempts(conn, job_id)
        accepted_checkpoints = _accepted_checkpoint_count(conn, job_id)
    worker_interrupted = bool(
        dead_running or any(str(row["status"]) == "crashed" for row in materializations)
    )
    resumed_session = any(int(row["resume_count"] or 0) > 0 for row in sessions)
    evaluator_provenance = evaluator_attempts[-1]["provenance"] if evaluator_attempts else {}
    boundaries: dict[str, bool] = {
        "daemon_process_started": bool(supervisor_starts or materializations),
        "daemon_restarted": True,
        "worker_process_started": bool(materializations),
        "independent_evaluator_process": bool(evaluator_provenance.get("producer_session_id")),
        "fixed_revision_evaluated": bool(evaluator_provenance.get("target_revision")),
    }
    if case_size in {"medium", "large"}:
        boundaries.update({
            "worker_process_interrupted": worker_interrupted,
            "worker_backend_session_resumed": resumed_session,
        })
    return {
        "boundaries": boundaries,
        "worker_interrupted": worker_interrupted,
        "dead_running_task_id": dead_running[0] if dead_running else None,
        "prior_materialization_count": len(materializations),
        "prior_supervisor_start_count": len(supervisor_starts),
        "prior_session_resume_count": sum(int(row["resume_count"] or 0) for row in sessions),
        "accepted_checkpoint_count": accepted_checkpoints,
        "prior_evaluator_attempt_count": len(evaluator_attempts),
    }


def _write_runner_state(
    path: Path,
    *,
    run_id: str,
    job_id: str,
    case_size: str,
    qualification_spec_path: Path,
    worker_toolchain: Path,
    worker_uid: int,
    worker_gid: int,
    resumed: bool,
) -> None:
    _write_json(path, {
        "schema": "hermes_phase4g8_runner_state_v1",
        "run_id": run_id,
        "job_id": job_id,
        "case_size": case_size,
        "qualification_spec_path": str(qualification_spec_path.resolve()),
        "qualification_spec_sha256": hashlib.sha256(
            qualification_spec_path.resolve().read_bytes()
        ).hexdigest(),
        "worker_toolchain": str(worker_toolchain.resolve()),
        "worker_identity": {"uid": int(worker_uid), "gid": int(worker_gid)},
        "resumed": bool(resumed),
        "updated_at": int(time.time()),
    })


def _pid_is_alive(pid: int) -> bool:
    if int(pid) <= 0:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


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
        SELECT n.id AS node_id, n.node_key, n.state, n.latest_task_id, t.worker_pid
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
    consumers_by_verifier_id: dict[str, set[str]] = {}
    for event in conn.execute(
        "SELECT payload_json FROM execution_events WHERE job_id = ? "
        "AND event_type = 'evaluator_failure_feedback_consumed'",
        (job_id,),
    ).fetchall():
        payload = rk._loads(event["payload_json"])
        verifier_id = str(payload.get("source_verifier_node_id") or "")
        consumer_id = str(payload.get("consumer_node_id") or "")
        if verifier_id:
            consumers_by_verifier_id.setdefault(verifier_id, set()).add(consumer_id)
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
        consumer_node_ids = sorted(
            consumers_by_verifier_id.get(str(row["node_id"]), set()) - {""}
        )
        attempts.append({
            "run_id": int(row["run_id"]),
            "task_id": str(row["task_id"]),
            "node_id": str(row["node_id"]),
            "result": result,
            "provenance": provenance if isinstance(provenance, dict) else {},
            "feedback_consumed": bool(consumer_node_ids),
            "feedback_consumer_node_ids": consumer_node_ids,
        })
    return attempts


def _validate_evaluated_operator_stop(
    request: dict[str, Any],
    *,
    evaluator_attempts: list[dict[str, Any]],
    workspace: Path,
    base_commit: str,
) -> dict[str, Any]:
    if not isinstance(request, dict) or request.get("schema") != "hermes_evaluated_operator_stop_v1":
        raise ValueError("operator stop request has an invalid schema")
    reason = p4g8.redact_sensitive_text(str(request.get("reason") or "")).strip()
    if not reason:
        raise ValueError("operator stop reason is required")
    try:
        requested_at = int(request["requested_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("operator stop requested_at is invalid") from exc
    if not evaluator_attempts:
        raise ValueError("operator stop requires a completed official evaluator attempt")
    latest = evaluator_attempts[-1]
    result = latest.get("result") or {}
    coverage = result.get("feedback_coverage") or {}
    if result.get("resolved") is True:
        raise ValueError("resolved runs must complete normally instead of using operator stop")
    if result.get("error") or coverage.get("status") != "current_failure_complete":
        raise ValueError("operator stop requires complete non-infrastructure evaluator feedback")
    expected_patch_sha256 = str(result.get("candidate_patch_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_patch_sha256):
        raise ValueError("latest evaluator result is missing candidate patch identity")
    candidate_patch = swe_evo.collect_candidate_patch(workspace, base_commit)
    current_patch_sha256 = hashlib.sha256(candidate_patch.encode("utf-8")).hexdigest()
    if current_patch_sha256 != expected_patch_sha256:
        raise ValueError(
            "workspace changed after the latest evaluator; refusing to archive an "
            "unevaluated candidate"
        )
    return {
        "schema": "hermes_evaluated_operator_stop_v1",
        "reason": reason[:2000],
        "requested_at": requested_at,
        "latest_evaluator_run_id": int(latest["run_id"]),
        "latest_evaluator_node_id": str(latest["node_id"]),
        "candidate_patch_sha256": current_patch_sha256,
        "target_revision": str((latest.get("provenance") or {}).get("target_revision") or ""),
        "fail_to_pass": dict(result.get("fail_to_pass") or {}),
        "pass_to_pass": dict(result.get("pass_to_pass") or {}),
    }


def _normalize_evaluated_stop_policy(
    policy: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    if policy is None:
        return None
    if (
        not isinstance(policy, dict)
        or policy.get("schema") != EVALUATED_STOP_POLICY_SCHEMA
    ):
        raise ValueError("evaluated stop policy has an invalid schema")
    try:
        min_attempts = int(policy["min_completed_evaluator_attempts"])
        min_consumed = int(policy["min_consumed_evaluator_feedback"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("evaluated stop policy thresholds are invalid") from exc
    if min_attempts < 1 or min_consumed < 0 or min_attempts <= min_consumed:
        raise ValueError(
            "evaluated stop policy requires more attempts than consumed feedback"
        )
    reason = p4g8.redact_sensitive_text(str(policy.get("reason") or "")).strip()
    if not reason:
        raise ValueError("evaluated stop policy reason is required")
    return {
        "schema": EVALUATED_STOP_POLICY_SCHEMA,
        "min_completed_evaluator_attempts": min_attempts,
        "min_consumed_evaluator_feedback": min_consumed,
        "reason": reason[:2000],
    }


def _evaluated_coverage_stop_candidate(
    policy: Optional[dict[str, Any]],
    *,
    evaluator_attempts: list[dict[str, Any]],
    workspace: Path,
    base_commit: str,
    required_feedback_consumer_node_id: str,
) -> Optional[dict[str, Any]]:
    if policy is None or any(
        attempt.get("result", {}).get("resolved") is True
        for attempt in evaluator_attempts
    ):
        return None
    completed = len(evaluator_attempts)
    consumed = [
        attempt
        for attempt in evaluator_attempts
        if attempt.get("feedback_consumed")
        and attempt.get("feedback_consumer_node_ids")
        == [required_feedback_consumer_node_id]
    ]
    if (
        completed < int(policy["min_completed_evaluator_attempts"])
        or len(consumed) < int(policy["min_consumed_evaluator_feedback"])
    ):
        return None
    request = {
        "schema": "hermes_evaluated_operator_stop_v1",
        "reason": str(policy["reason"]),
        "requested_at": int(time.time()),
    }
    try:
        validated = _validate_evaluated_operator_stop(
            request,
            evaluator_attempts=evaluator_attempts,
            workspace=workspace,
            base_commit=base_commit,
        )
    except ValueError as exc:
        if "workspace changed after the latest evaluator" in str(exc):
            return None
        raise
    return {
        "schema": EVALUATED_STOP_POLICY_SCHEMA,
        "reason": validated["reason"],
        "requested_at": validated["requested_at"],
        "completed_evaluator_attempts": completed,
        "consumed_evaluator_feedback": len(consumed),
        "consumed_evaluator_run_ids": [int(item["run_id"]) for item in consumed],
        "feedback_consumer_node_id": required_feedback_consumer_node_id,
        "latest_evaluator_run_id": validated["latest_evaluator_run_id"],
        "latest_evaluator_node_id": validated["latest_evaluator_node_id"],
        "candidate_patch_sha256": validated["candidate_patch_sha256"],
        "target_revision": validated["target_revision"],
        "fail_to_pass": validated["fail_to_pass"],
        "pass_to_pass": validated["pass_to_pass"],
    }


def _evaluator_progress_status(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    def extraction_incomplete(attempt: dict[str, Any]) -> bool:
        result = attempt.get("result", {})
        coverage = result.get("feedback_coverage")
        return bool(
            result.get("error") == "evaluator_feedback_extraction_incomplete"
            or (
                isinstance(coverage, dict)
                and coverage.get("status") == "extraction_incomplete"
            )
        )

    effective = [
        attempt
        for attempt in attempts
        if attempt.get("result", {}).get("error") not in {
            "stale_target_revision",
            "evaluator_feedback_extraction_incomplete",
        }
        and not extraction_incomplete(attempt)
    ]
    history: list[dict[str, Any]] = []
    no_progress_streak = 0
    previous: Optional[dict[str, Any]] = None
    for attempt in effective:
        result = attempt.get("result", {})
        current = {
            "node_id": attempt.get("node_id"),
            "feedback_consumed": bool(attempt.get("feedback_consumed")),
            "fail_to_pass_passed": int((result.get("fail_to_pass") or {}).get("passed") or 0),
            "pass_to_pass_passed": int((result.get("pass_to_pass") or {}).get("passed") or 0),
            "failure_signature": rk._evaluator_failure_signature(result),
        }
        if previous is None:
            progress = True
            count_progress = False
            signature_changed = False
        else:
            count_progress = bool(
                current["fail_to_pass_passed"] >= previous["fail_to_pass_passed"]
                and current["pass_to_pass_passed"] >= previous["pass_to_pass_passed"]
                and (
                    current["fail_to_pass_passed"] > previous["fail_to_pass_passed"]
                    or current["pass_to_pass_passed"] > previous["pass_to_pass_passed"]
                )
            )
            signature_changed = bool(
                current["failure_signature"] != previous["failure_signature"]
            )
            progress = count_progress or signature_changed
        no_progress_streak = 0 if progress else no_progress_streak + 1
        current.update({
            "count_progress": count_progress,
            "signature_changed": signature_changed,
            "progress": progress,
            "no_progress_streak": no_progress_streak,
        })
        history.append(current)
        previous = current
    latest_resolved = bool(
        attempts and attempts[-1].get("result", {}).get("resolved") is True
    )
    return {
        "attempt_count": len(attempts),
        "effective_failure_count": sum(
            attempt.get("result", {}).get("resolved") is not True
            for attempt in effective
        ),
        "latest_resolved": latest_resolved,
        "latest_feedback_extraction_incomplete": bool(
            attempts and extraction_incomplete(attempts[-1])
        ),
        "latest_feedback_consumed": bool(
            effective and effective[-1].get("feedback_consumed")
        ),
        "no_progress_streak": no_progress_streak,
        "history": history,
        "exhausted": False,
    }


def _evaluator_failure_budget_status(
    attempts: list[dict[str, Any]],
    *,
    max_unresolved_evaluator_attempts: int,
) -> dict[str, Any]:
    if int(max_unresolved_evaluator_attempts) < 1:
        raise ValueError("max_unresolved_evaluator_attempts must be positive")
    status = _evaluator_progress_status(attempts)
    return {
        **status,
        "failure_count": status["effective_failure_count"],
        "max_unresolved_evaluator_attempts": int(max_unresolved_evaluator_attempts),
        "deprecated_fixed_attempt_budget_ignored": True,
        "exhausted": False,
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
