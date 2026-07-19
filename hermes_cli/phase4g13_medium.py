"""Phase 4G13 natural Medium single-worker and Runtime comparison harness."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
from typing import Any
import uuid

from hermes_cli import kanban_runtime_kernel as rk
from hermes_cli import kanban_runtime_phase4g8 as p4g8
from hermes_cli import kanban_runtime_phase4g8_run as p4g8_run
from hermes_cli import phase4g9_native_arm as native_arm
from hermes_cli import phase4g10_arm2 as arm2
from hermes_cli import phase4g8_swe_evo as swe_evo
from hermes_cli import validation_artifacts


REPORT_SCHEMA = "hermes_phase4g13_medium_comparison_v1"
ARM_REPORT_SCHEMA = "hermes_phase4g13_medium_arm_v1"
FROZEN_INSTANCE_ID = "dask__dask_2023.6.1_2023.7.0"
FROZEN_DATASET_REVISION = "9b83d5af943ba7a17567336f5b18239f73960219"
FROZEN_BASE_COMMIT = "85c99bc20abc382774cfb6e5bf5f2db76ac09378"
FROZEN_SRS_SHA256 = "89faba26da7f2c25b9892200e24c4d35adc056934fa19a4f57424fb012d98695"
FROZEN_TEST_PATCH_SHA256 = "67df087cf3a6057e637e78bb349dec6461b697112faa1a40f8f49542c3d59628"
FROZEN_IMAGE_DIGEST = "sha256:e0ee1e98546c7599146b341c40503c109b69bddc740802ef8d287b388f8cd29f"
WORKER_UID = 65534
WORKER_GID = 65534


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_frozen_inputs(spec_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    spec_path = spec_path.expanduser().resolve()
    spec = p4g8.load_qualification_spec(spec_path)
    p4g8_run._require_qualified(spec, spec_path)
    expected = {
        "instance_id": FROZEN_INSTANCE_ID,
        "dataset_revision": FROZEN_DATASET_REVISION,
        "base_commit": FROZEN_BASE_COMMIT,
    }
    actual = {key: spec.get(key) for key in expected}
    if actual != expected:
        raise ValueError("Phase 4G13 requires the frozen natural Medium instance")
    if _sha256_text(str(spec.get("srs") or "")) != FROZEN_SRS_SHA256:
        raise ValueError("Phase 4G13 frozen SRS hash mismatch")
    benchmark = spec.get("benchmark") or {}
    if benchmark.get("test_patch_sha256") != FROZEN_TEST_PATCH_SHA256:
        raise ValueError("Phase 4G13 frozen test patch hash mismatch")
    locked_path = spec_path.parent.parent / "worker" / "locked-task.json"
    if not locked_path.is_file():
        raise ValueError("Phase 4G13 requires the worker-visible locked task")
    locked = json.loads(locked_path.read_text(encoding="utf-8"))
    if (
        locked.get("instance_id") != FROZEN_INSTANCE_ID
        or locked.get("base_commit") != FROZEN_BASE_COMMIT
        or _sha256_text(str(locked.get("srs") or "")) != FROZEN_SRS_SHA256
    ):
        raise ValueError("Phase 4G13 locked task does not match the frozen instance")
    return spec, locked


def build_single_worker_prompt(locked_task: dict[str, Any], *, toolchain: Path) -> str:
    srs = str(locked_task.get("srs") or "").strip()
    if not srs:
        raise ValueError("locked task SRS is missing")
    return f"""You own the complete implementation outcome for this software evolution task.

Work continuously from repository inspection through implementation, integration, testing,
debugging, and final verification. This arm intentionally uses one coherent worker session.
Do not delegate to subagents and do not stop after analysis or after only changing code.

The workspace is a clean detached checkout of the exact benchmark base revision. A dependency
toolchain matching the official image is available at {toolchain}. Use project-visible tests and
write additional tests when useful. Continue until you have the strongest terminal result you can
produce or a real blocker prevents further work.

Integrity constraints:
- Do not seek, fetch, infer from, or inspect the target release implementation, gold patch,
  protected tests, evaluator sources, historical benchmark candidates, or prior run transcripts.
- Do not contact external networks from shell tools.
- No evaluator result will be returned to this session.
- Do not modify files outside this workspace.
- Your final response must summarize the approach, changed areas, tests run, known limitations,
  and any blocker. Do not claim official benchmark success.

Software Requirements Specification:

{srs}
"""


def _prepare_single_layout(root: Path, spec: dict[str, Any]) -> dict[str, Path]:
    mirror = Path(spec["source"]["local_mirror"]).resolve()
    p4g8_run._protect_source_mirror(mirror)
    root.mkdir(parents=True, exist_ok=False)
    os.chmod(root, 0o711)
    paths = {
        "root": root,
        "home": root / "home",
        "codex_home": root / "codex-home",
        "workspace": root / "workspace",
        "worker_events": root / "worker-events",
        "evaluator_runs": root / "evaluator-runs",
        "reports": root / "reports",
        "worker_tmp": root / "worker-tmp",
    }
    for name in ("home", "worker_events", "evaluator_runs", "reports", "worker_tmp"):
        paths[name].mkdir()
    for name in ("worker_events", "evaluator_runs", "reports"):
        os.chmod(paths[name], 0o700)
    subprocess.run(["git", "init", "--quiet", str(paths["workspace"])], check=True)
    subprocess.run(
        ["git", "remote", "add", "source", mirror.as_uri()],
        cwd=paths["workspace"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "fetch",
            "--quiet",
            "--depth=1",
            "--no-tags",
            "source",
            str(spec["base_commit"]),
        ],
        cwd=paths["workspace"],
        check=True,
    )
    subprocess.run(
        ["git", "checkout", "--quiet", "--detach", "FETCH_HEAD"],
        cwd=paths["workspace"],
        check=True,
    )
    subprocess.run(
        ["git", "remote", "remove", "source"],
        cwd=paths["workspace"],
        check=True,
    )
    setup = p4g8_run._render_worker_environment_setup(spec)
    toolchain = p4g8_run._prepare_worker_toolchain(
        str(spec["benchmark"]["official_image"]),
        environment_setup=setup,
        setup_env=(spec.get("worker_environment") or {}).get("env") or {},
    )
    paths["worker_toolchain"] = toolchain
    p4g8.prepare_worker_workspace(
        paths["workspace"], worker_uid=WORKER_UID, worker_gid=WORKER_GID
    )
    for name in ("home", "worker_tmp"):
        os.chmod(paths[name], 0o700)
        os.chown(paths[name], WORKER_UID, WORKER_GID)
    return paths


def _freeze_candidate(workspace: Path, reports: Path) -> dict[str, Any]:
    native_arm.cleanup_worker_test_artifacts(workspace)
    patch = swe_evo.collect_candidate_patch(workspace, FROZEN_BASE_COMMIT)
    changed_files = native_arm._changed_files(workspace)
    candidate = {
        "base_commit": FROZEN_BASE_COMMIT,
        "revision": f"patch-sha256:{_sha256_text(patch)}",
        "patch_sha256": _sha256_text(patch),
        "patch_bytes": len(patch.encode("utf-8")),
        "changed_files": changed_files,
        "frozen_at": int(time.time()),
    }
    _write_text(reports / "candidate.patch", patch)
    _write_json(reports / "candidate.json", candidate)
    return candidate


def _quality_summary(evaluator: dict[str, Any]) -> dict[str, Any]:
    return {
        "resolved": evaluator.get("resolved") is True,
        "fail_to_pass": evaluator.get("fail_to_pass") or {},
        "pass_to_pass": evaluator.get("pass_to_pass") or {},
        "feedback_coverage": evaluator.get("feedback_coverage") or {},
    }


def run_single_worker_arm(
    *,
    qualification_spec_path: Path,
    run_root: Path,
    source_codex_home: Path,
    artifact_root: Path,
    execute_real: bool,
    max_wall_seconds: float,
) -> dict[str, Any]:
    if not execute_real:
        raise ValueError("Phase 4G13 single-worker arm requires execute_real=True")
    spec, locked = _load_frozen_inputs(qualification_spec_path)
    source = p4g8.load_codex_model_source(source_codex_home)
    run_root = run_root.expanduser().resolve()
    paths = _prepare_single_layout(run_root, spec)
    run_id = f"phase4g13-single-{uuid.uuid4().hex[:12]}"
    started_at = int(time.time())
    started = time.monotonic()
    raw_lines: list[str] = []
    with p4g8.Phase4G8NetworkNamespace(
        run_id, source["explicit_base_url"]
    ) as network:
        config_audit = p4g8.prepare_isolated_codex_home(
            source_codex_home,
            paths["codex_home"],
            proxy_base_url=str(network.proxy_base_url),
            model=source["model"],
            worker_uid=WORKER_UID,
            worker_gid=WORKER_GID,
            reasoning_effort_override="max",
            multi_agent_enabled=False,
        )
        native_arm._prepare_worker_turn_paths(paths)
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(paths["home"]),
                "CODEX_HOME": str(paths["codex_home"]),
                "PATH": str(paths["worker_toolchain"] / "bin")
                + os.pathsep
                + env.get("PATH", ""),
                "PYTHONPATH": str(paths["workspace"]),
                "TMPDIR": str(paths["worker_tmp"]),
                "TMP": str(paths["worker_tmp"]),
                "TEMP": str(paths["worker_tmp"]),
                p4g8.PROCESS_OWNER_ENV: run_id,
            }
        )
        turn = native_arm._run_native_codex_turn(
            network=network,
            paths=paths,
            env=env,
            prompt=build_single_worker_prompt(
                locked, toolchain=paths["worker_toolchain"]
            ),
            run_id=run_id,
            candidate_round=1,
            timeout_seconds=float(max_wall_seconds),
            resume_session_id=None,
        )
        raw_lines = list(turn.pop("raw_lines"))
        transport = network.transport_audit()
    native_arm._reclaim_workspace(paths["workspace"])
    candidate = _freeze_candidate(paths["workspace"], paths["reports"])
    evaluator = p4g8._run_evaluator(spec, paths["workspace"])
    _write_json(paths["evaluator_runs"] / "invocation-001.json", evaluator)
    events = native_arm.summarize_exec_events(raw_lines)
    parent_thread_id = native_arm.validated_parent_thread_id(
        None, turn.get("observed_session_id")
    )
    rollouts = native_arm.summarize_rollout_sessions(
        paths["codex_home"], parent_thread_id=parent_thread_id
    )
    report = {
        "schema": ARM_REPORT_SCHEMA,
        "arm": "coherent_single_worker",
        "run_id": run_id,
        "instance_id": FROZEN_INSTANCE_ID,
        "started_at": started_at,
        "finished_at": int(time.time()),
        "wall_time_seconds": round(time.monotonic() - started, 3),
        "model": source["model"],
        "reasoning_effort": "max",
        "runtime_kernel_used": False,
        "worker_count": 1,
        "worker_turn_count": 1,
        "evaluator_invocation_count": 1,
        "evaluator_feedback_consumed": 0,
        "candidate": candidate,
        "quality": _quality_summary(evaluator),
        "worker": {
            "return_code": turn["return_code"],
            "timed_out": turn["timed_out"],
            "thread_id": parent_thread_id,
            "turn": turn,
            "events": events,
            "rollouts": rollouts,
        },
        "config": config_audit,
        "model_transport": transport,
        "integrity": {
            "source_codex_home_unchanged": p4g8.verify_codex_source_unchanged(
                source_codex_home.expanduser().resolve(), source["source_hashes"]
            ),
            "gold_or_protected_tests_exposed_to_worker": False,
            "candidate_key_or_file_partition_in_prompt": False,
            "native_subagents_enabled": False,
        },
    }
    _write_json(paths["reports"] / "arm-report.json", report)
    archive = validation_artifacts.archive_validation_run(
        paths["root"],
        artifact_root=artifact_root,
        phase="phase4g13",
        instance_id=FROZEN_INSTANCE_ID,
        redactions=validation_artifacts.model_source_redactions(source_codex_home),
        expected_entries={"codex-home", "worker-events", "evaluator-runs", "reports"},
    )
    report["artifact_archive"] = archive
    _write_json(paths["reports"] / "arm-report.json", report)
    report["cleanup"] = validation_artifacts.cleanup_rebuildable_entries(
        paths["root"],
        manifest_path=Path(str(archive["artifact_path"])) / "manifest.json",
        entries={"workspace", "home"},
    )
    _write_json(paths["reports"] / "arm-report.json", report)
    return report


def runtime_arm_call_kwargs(
    *,
    qualification_spec_path: Path,
    run_root: Path,
    source_codex_home: Path,
    max_wall_seconds: float,
    worker_timeout_seconds: int,
) -> dict[str, Any]:
    return {
        "qualification_spec_path": qualification_spec_path,
        "run_root": run_root,
        "source_codex_home": source_codex_home,
        "case_size": "medium",
        "execute_real": True,
        "max_wall_seconds": float(max_wall_seconds),
        "worker_timeout_seconds": int(worker_timeout_seconds),
        "max_unresolved_evaluator_attempts": 1,
        "max_evaluator_no_progress_streak": 1,
        "orchestration_policy": {
            "schema": rk.RUNTIME_ORCHESTRATION_POLICY_SCHEMA,
            "mode": "closed_loop_coordination",
            "worker_lane": "phase4g8-codex",
            "max_child_nodes": 3,
            "artifact_root": str(run_root.resolve() / "runtime-orchestration"),
            "retention": "retain",
        },
        "fault_profile": "none",
        "run_id_prefix": "phase4g13-runtime",
        "reasoning_effort_override": "max",
        "worker_multi_agent_enabled": False,
        "evaluated_stop_policy": {
            "schema": p4g8_run.EVALUATED_STOP_POLICY_SCHEMA,
            "min_completed_evaluator_attempts": 1,
            "min_consumed_evaluator_feedback": 0,
            "reason": (
                "Phase 4G13 runs one terminal acceptance only and does not return evaluator "
                "diagnostics to workers."
            ),
        },
    }


def _runtime_evidence(run_root: Path, job_id: str) -> dict[str, Any]:
    db_path = run_root / "hermes-home" / "kanban.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        nodes = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, node_key, node_type, state, created_at, latest_task_id
                  FROM execution_nodes WHERE job_id = ? ORDER BY created_at, node_key
                """,
                (job_id,),
            ).fetchall()
        ]
        node_keys = {str(row["id"]): str(row["node_key"]) for row in nodes}
        event_types = {
            "worker_structure_checkpointed",
            "worker_coordination_checkpointed",
            "worker_responsibility_candidates_recorded",
            "worker_directive_issued",
            "worker_directive_acknowledged",
            "runtime_node_contribution_frozen",
        }
        placeholders = ",".join("?" for _ in event_types)
        events = [
            {
                **dict(row),
                "payload": json.loads(row["payload_json"] or "{}"),
            }
            for row in conn.execute(
                f"""
                SELECT id, event_type, node_id, payload_json, created_at
                  FROM execution_events
                 WHERE job_id = ? AND event_type IN ({placeholders})
                 ORDER BY id
                """,
                (job_id, *sorted(event_types)),
            ).fetchall()
        ]
        attempts = p4g8_run._official_evaluator_attempts(conn, job_id)
        return {
            "runtime_status": rk.status_runtime_job(conn, job_id),
            "coordination_cost": rk.summarize_runtime_coordination_cost(conn, job_id),
            "nodes": nodes,
            "events": events,
            "worker_sessions": arm2._codex_session_metrics(
                run_root, node_keys=node_keys
            ),
            "evaluator_attempt_count": len(attempts),
            "evaluator_feedback_consumed": sum(
                bool(item.get("feedback_consumed")) for item in attempts
            ),
            "evaluator_result": (
                dict(attempts[-1].get("result") or {}) if attempts else {}
            ),
        }
    finally:
        conn.close()


def run_runtime_arm(
    *,
    qualification_spec_path: Path,
    run_root: Path,
    source_codex_home: Path,
    artifact_root: Path,
    execute_real: bool,
    max_wall_seconds: float,
    worker_timeout_seconds: int,
) -> dict[str, Any]:
    if not execute_real:
        raise ValueError("Phase 4G13 Runtime arm requires execute_real=True")
    _load_frozen_inputs(qualification_spec_path)
    payload = p4g8_run.run_phase4g8_real_case(
        **runtime_arm_call_kwargs(
            qualification_spec_path=qualification_spec_path,
            run_root=run_root,
            source_codex_home=source_codex_home,
            max_wall_seconds=max_wall_seconds,
            worker_timeout_seconds=worker_timeout_seconds,
        )
    )
    actual_root = Path(str(payload["paths"]["root"])).resolve()
    evidence = _runtime_evidence(actual_root, str(payload["job_id"]))
    if evidence["evaluator_attempt_count"] != 1:
        raise RuntimeError("Phase 4G13 Runtime arm requires exactly one evaluator attempt")
    if evidence["evaluator_feedback_consumed"] != 0:
        raise RuntimeError("Phase 4G13 evaluator feedback reached a worker")
    evaluator = evidence["evaluator_result"]
    candidate = payload.get("candidate_evidence") or {}
    non_evaluator_nodes = [
        item for item in evidence["nodes"] if item["node_type"] != "verification"
    ]
    report = {
        "schema": ARM_REPORT_SCHEMA,
        "arm": "runtime_orchestra",
        "run_id": payload["run_id"],
        "job_id": payload["job_id"],
        "instance_id": FROZEN_INSTANCE_ID,
        "runtime_kernel_used": True,
        "worker_count": len(non_evaluator_nodes),
        "evaluator_invocation_count": evidence["evaluator_attempt_count"],
        "evaluator_feedback_consumed": evidence["evaluator_feedback_consumed"],
        "candidate": candidate,
        "quality": _quality_summary(evaluator),
        "runtime": evidence,
        "source_report": payload,
        "integrity": {
            "candidate_key_or_file_partition_in_prompt": False,
            "gold_or_protected_tests_exposed_to_worker": False,
            "native_subagents_enabled": False,
            "single_terminal_acceptance": evidence["evaluator_attempt_count"] == 1,
            "evaluator_feedback_returned_to_worker": evidence[
                "evaluator_feedback_consumed"
            ]
            != 0,
        },
    }
    _write_json(actual_root / "reports" / "phase4g13-arm-report.json", report)
    expected = {"reports", "hermes-home", "codex-homes", "service"}
    if (actual_root / "runtime-contributions").is_dir():
        expected.add("runtime-contributions")
    archive = validation_artifacts.archive_validation_run(
        actual_root,
        artifact_root=artifact_root,
        phase="phase4g13",
        instance_id=FROZEN_INSTANCE_ID,
        redactions=validation_artifacts.model_source_redactions(source_codex_home),
        expected_entries=expected,
    )
    report["artifact_archive"] = archive
    _write_json(actual_root / "reports" / "phase4g13-arm-report.json", report)
    cleanup_entries = {"workspace", "home", "codex-home-seed", "runtime-worktrees"}
    report["cleanup"] = validation_artifacts.cleanup_rebuildable_entries(
        actual_root,
        manifest_path=Path(str(archive["artifact_path"])) / "manifest.json",
        entries=cleanup_entries,
    )
    _write_json(actual_root / "reports" / "phase4g13-arm-report.json", report)
    return report


def build_comparison_report(
    single: dict[str, Any], runtime: dict[str, Any]
) -> dict[str, Any]:
    single_usage = (((single.get("worker") or {}).get("events") or {}).get("usage")) or {}
    runtime_usage = (
        (((runtime.get("runtime") or {}).get("worker_sessions") or {}).get("aggregate"))
        or {}
    )
    coordination = ((runtime.get("runtime") or {}).get("coordination_cost")) or {}
    return {
        "schema": REPORT_SCHEMA,
        "instance_id": FROZEN_INSTANCE_ID,
        "generated_at": int(time.time()),
        "arms": {
            "coherent_single_worker": {
                "run_id": single.get("run_id"),
                "quality": single.get("quality"),
                "worker_count": single.get("worker_count"),
                "wall_time_seconds": single.get("wall_time_seconds"),
                "token_usage": single_usage,
            },
            "runtime_orchestra": {
                "run_id": runtime.get("run_id"),
                "quality": runtime.get("quality"),
                "worker_count": runtime.get("worker_count"),
                "token_usage": runtime_usage,
                "coordination_cost": coordination,
            },
        },
        "integrity": {
            "same_instance": single.get("instance_id") == runtime.get("instance_id"),
            "one_evaluator_per_arm": all(
                int(item.get("evaluator_invocation_count") or 0) == 1
                for item in (single, runtime)
            ),
            "zero_evaluator_feedback_consumed": all(
                int(item.get("evaluator_feedback_consumed") or 0) == 0
                for item in (single, runtime)
            ),
            "topology_answer_not_injected": all(
                not bool(
                    (item.get("integrity") or {}).get(
                        "candidate_key_or_file_partition_in_prompt"
                    )
                )
                for item in (single, runtime)
            ),
        },
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--source-codex-home", default=str(Path.home() / ".codex"))
    parser.add_argument(
        "--artifact-root", default=str(validation_artifacts.default_artifact_root())
    )
    parser.add_argument("--max-wall-seconds", type=float, default=7200)
    parser.add_argument("--worker-timeout-seconds", type=int, default=7200)
    parser.add_argument("--execute-real", action="store_true", required=True)
    sub = parser.add_subparsers(dest="arm", required=True)
    single = sub.add_parser("single")
    single.add_argument("--run-root", required=True)
    runtime = sub.add_parser("runtime")
    runtime.add_argument("--run-root", required=True)
    compare = sub.add_parser("compare")
    compare.add_argument("--single-report", required=True)
    compare.add_argument("--runtime-report", required=True)
    compare.add_argument("--output", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.arm == "single":
        result = run_single_worker_arm(
            qualification_spec_path=Path(args.spec),
            run_root=Path(args.run_root),
            source_codex_home=Path(args.source_codex_home),
            artifact_root=Path(args.artifact_root),
            execute_real=bool(args.execute_real),
            max_wall_seconds=float(args.max_wall_seconds),
        )
    elif args.arm == "runtime":
        result = run_runtime_arm(
            qualification_spec_path=Path(args.spec),
            run_root=Path(args.run_root),
            source_codex_home=Path(args.source_codex_home),
            artifact_root=Path(args.artifact_root),
            execute_real=bool(args.execute_real),
            max_wall_seconds=float(args.max_wall_seconds),
            worker_timeout_seconds=int(args.worker_timeout_seconds),
        )
    else:
        single = json.loads(Path(args.single_report).read_text(encoding="utf-8"))
        runtime = json.loads(Path(args.runtime_report).read_text(encoding="utf-8"))
        result = build_comparison_report(single, runtime)
        _write_json(Path(args.output), result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
