"""Phase 4G10 durable Runtime orchestra Arm 2 harness."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import time
from typing import Any

from hermes_cli import kanban_runtime_phase4g8 as p4g8
from hermes_cli import kanban_runtime_phase4g8_run as p4g8_run
from hermes_cli import validation_artifacts


ARM2_REPORT_SCHEMA = "hermes_phase4g10_arm2_v1"
OPERATOR_STOP_REQUEST = "operator-stop-request.json"


def _loads(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _codex_session_metrics(
    run_root: Path,
    *,
    node_keys: dict[str, str],
) -> dict[str, Any]:
    sessions: list[dict[str, Any]] = []
    aggregate = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "total_tokens": 0,
    }
    for home in sorted((run_root / "codex-homes").glob("node-*")):
        try:
            node_id = (home / ".execution-node").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        for session_path in sorted((home / "sessions").rglob("*.jsonl")):
            latest_usage: dict[str, int] = {}
            compact_count = 0
            try:
                with session_path.open(encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        payload = event.get("payload") or {}
                        if event.get("type") != "event_msg":
                            continue
                        if payload.get("type") == "context_compacted":
                            compact_count += 1
                        elif payload.get("type") == "token_count":
                            usage = ((payload.get("info") or {}).get("total_token_usage"))
                            if isinstance(usage, dict):
                                latest_usage = {
                                    key: int(usage.get(key) or 0)
                                    for key in aggregate
                                }
            except OSError:
                continue
            if not latest_usage:
                continue
            for key, value in latest_usage.items():
                aggregate[key] += value
            sessions.append(
                {
                    "node_id": node_id,
                    "node_key": node_keys.get(node_id),
                    "session_file": session_path.name,
                    "context_compactions": compact_count,
                    "usage": latest_usage,
                }
            )
    aggregate["cache_ratio"] = round(
        aggregate["cached_input_tokens"] / aggregate["input_tokens"],
        6,
    ) if aggregate["input_tokens"] else 0.0
    return {
        "aggregate": aggregate,
        "context_compactions": sum(
            int(session["context_compactions"]) for session in sessions
        ),
        "sessions": sessions,
    }


def request_operator_stop(run_root: Path, *, reason: str) -> dict[str, Any]:
    run_root = run_root.resolve()
    if not run_root.is_dir():
        raise ValueError("operator stop run root does not exist")
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("operator stop reason is required")
    request = {
        "schema": "hermes_evaluated_operator_stop_v1",
        "reason": reason,
        "requested_at": int(time.time()),
    }
    _write_json(run_root / OPERATOR_STOP_REQUEST, request)
    return request


def _load_operator_stop_request(run_root: Path) -> dict[str, Any] | None:
    path = run_root.resolve() / OPERATOR_STOP_REQUEST
    if not path.is_file():
        return None
    try:
        request = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("operator stop request is unreadable") from exc
    if (
        not isinstance(request, dict)
        or request.get("schema") != "hermes_evaluated_operator_stop_v1"
        or not str(request.get("reason") or "").strip()
        or not isinstance(request.get("requested_at"), int)
    ):
        raise ValueError("operator stop request is invalid")
    return request


def build_arm2_orchestration_report(
    run_root: Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    run_root = run_root.resolve()
    job_id = str(payload["job_id"])
    db_path = run_root / "hermes-home" / "kanban.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        nodes = _rows(
            conn,
            """
            SELECT n.*, t.workspace_path, t.workspace_kind
              FROM execution_nodes n
              LEFT JOIN tasks t ON t.id = n.latest_task_id
             WHERE n.job_id = ? ORDER BY n.created_at, n.node_key
            """,
            (job_id,),
        )
        checkpoints = _rows(
            conn,
            """
            SELECT id, node_id, payload_json, created_at
              FROM execution_events
             WHERE job_id = ? AND event_type = 'worker_structure_checkpointed'
             ORDER BY id
            """,
            (job_id,),
        )
        checkpoint_payload = (
            _loads(checkpoints[0]["payload_json"]) if checkpoints else {}
        )
        primary_id = str(checkpoints[0]["node_id"]) if checkpoints else ""
        primary = next((node for node in nodes if node["id"] == primary_id), None)
        child_nodes = [
            node
            for node in nodes
            if _loads(node.get("metadata_json")).get(
                "non_authoritative_contribution"
            )
        ]
        contributions = _rows(
            conn,
            """
            SELECT a.*, n.node_key
              FROM node_artifacts a
              JOIN execution_nodes n ON n.id = a.node_id
             WHERE a.job_id = ? AND a.artifact_type = 'runtime_node_contribution'
             ORDER BY a.created_at, a.id
            """,
            (job_id,),
        )
        contribution_payloads = [
            {**_loads(row["metadata_json"]), "artifact_id": row["id"]}
            for row in contributions
        ]
        sessions = _rows(
            conn,
            """
            SELECT * FROM backend_worker_sessions
             WHERE job_id = ? ORDER BY created_at, id
            """,
            (job_id,),
        )
        primary_sessions = [
            session for session in sessions if session["node_id"] == primary_id
        ]
        child_ids = {str(node["id"]) for node in child_nodes}
        child_sessions = [
            session for session in sessions if str(session["node_id"]) in child_ids
        ]
        attribution_events = _rows(
            conn,
            """
            SELECT id, payload_json, created_at FROM execution_events
             WHERE job_id = ? AND event_type = 'contribution_attribution_verified'
             ORDER BY id
            """,
            (job_id,),
        )
        attribution = (
            _loads(attribution_events[-1]["payload_json"])
            if attribution_events
            else {}
        )
        first_patch = conn.execute(
            """
            SELECT patch_json FROM graph_patches
             WHERE job_id = ? AND status = 'applied'
             ORDER BY created_at, id LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        first_patch_payload = _loads(first_patch["patch_json"]) if first_patch else {}
        first_execution_nodes = [
            op
            for op in first_patch_payload.get("ops") or []
            if op.get("op") in {"create_node", "strategy_update", "insert_verifier"}
        ]
        workspaces = {
            str(node.get("workspace_path") or "")
            for node in child_nodes
            if node.get("workspace_path")
        }
        child_session_keys = {
            str(session["backend_session_key"]) for session in child_sessions
        }
        nonempty_contributions = [
            item for item in contribution_payloads if int(item.get("patch_bytes") or 0) > 0
        ]
        integrated = set(attribution.get("accepted_contributions") or []) | set(
            attribution.get("modified_contributions") or []
        )
        process_boundaries = (
            ((payload.get("run_report") or {}).get("runtime_validation") or {}).get(
                "process_boundaries"
            )
            or {}
        )
        assertions = {
            "initial_graph_single_primary": len(first_execution_nodes) == 1,
            "accepted_early_structure_checkpoint": len(checkpoints) == 1
            and not (
                ((checkpoint_payload.get("checkpoint") or {}).get("changed_files"))
            ),
            "two_or_three_durable_children": 2 <= len(child_nodes) <= 3,
            "isolated_child_workspaces_and_sessions": len(workspaces)
            == len(child_nodes)
            and len(child_session_keys) == len(child_nodes),
            "child_scopes_declared": all(
                bool(
                    ((_loads(node.get("constraints_json")).get("contract") or {}).get(
                        "declared_write_scope"
                    ))
                )
                for node in child_nodes
            ),
            "two_nonempty_frozen_contributions": len(nonempty_contributions) >= 2,
            "two_contributions_integrated": len(integrated) >= 2,
            "primary_same_session_resumed": len(primary_sessions) == 1
            and int(primary_sessions[0].get("resume_count") or 0) >= 1,
            "official_evaluator_fixed_revision": bool(
                process_boundaries.get("fixed_revision_evaluated")
            ),
            "candidate_has_contribution_lineage": bool(attribution_events)
            and integrated.issubset(
                {str(item.get("artifact_id")) for item in contribution_payloads}
            ),
        }
        runtime_report = payload.get("run_report") or {}
        capability = runtime_report.get("capability_validation") or {}
        runtime_validation = runtime_report.get("runtime_validation") or {}
        task_runs = _rows(
            conn,
            """
            SELECT tr.id, tr.task_id, tr.status, tr.started_at, tr.ended_at,
                   tr.metadata, n.id AS node_id, n.node_key, n.node_type
              FROM task_runs tr
              JOIN node_materializations m ON m.task_id = tr.task_id
              JOIN execution_nodes n ON n.id = m.node_id
             WHERE n.job_id = ? ORDER BY tr.id
            """,
            (job_id,),
        )
        evaluator_progression: list[dict[str, Any]] = []
        run_timeline: list[dict[str, Any]] = []
        for run in task_runs:
            metadata = _loads(run.pop("metadata", {}))
            evaluator_result = metadata.get("official_evaluator_result") or {}
            run_timeline.append(
                {
                    **run,
                    "wall_time_seconds": (
                        int(run["ended_at"]) - int(run["started_at"])
                        if run.get("ended_at") and run.get("started_at")
                        else None
                    ),
                }
            )
            if evaluator_result:
                evaluator_progression.append(
                    {
                        "round": len(evaluator_progression) + 1,
                        "run_id": run["id"],
                        "node_key": run["node_key"],
                        "fail_to_pass": evaluator_result.get("fail_to_pass") or {},
                        "pass_to_pass": evaluator_result.get("pass_to_pass") or {},
                        "resolved": evaluator_result.get("resolved") is True,
                        "wall_time_seconds": (
                            int(run["ended_at"]) - int(run["started_at"])
                            if run.get("ended_at") and run.get("started_at")
                            else None
                        ),
                    }
                )
        job = conn.execute(
            "SELECT created_at, updated_at FROM runtime_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        stop = ((runtime_report.get("metrics") or {}).get("operator_stop") or {})
        stop_event = conn.execute(
            "SELECT MIN(created_at) FROM execution_events WHERE job_id = ? "
            "AND event_type = 'operator_stopped_after_evaluated_plateau'",
            (job_id,),
        ).fetchone()[0]
        finished_at = int(stop_event or stop.get("requested_at") or job["updated_at"])
        child_ids = {str(node["id"]) for node in child_nodes}
        child_walls = [
            int(run["wall_time_seconds"])
            for run in run_timeline
            if str(run["node_id"]) in child_ids and run["wall_time_seconds"] is not None
        ]
        execution_metrics = {
            "started_at": int(job["created_at"]),
            "finished_at": finished_at,
            "wall_time_seconds": max(0, finished_at - int(job["created_at"])),
            "execution_node_count": len(nodes),
            "implementation_node_count": sum(
                node["node_type"] == "implementation" for node in nodes
            ),
            "durable_child_count": len(child_nodes),
            "verification_node_count": sum(
                node["node_type"] == "verification" for node in nodes
            ),
            "strategy_node_count": sum(
                node["node_type"] == "strategy_update" for node in nodes
            ),
            "materialization_count": len(run_timeline),
            "evaluator_round_count": len(evaluator_progression),
            "decision_round_count": int(
                conn.execute(
                    "SELECT COUNT(*) FROM kernel_decisions WHERE job_id = ?",
                    (job_id,),
                ).fetchone()[0]
            ),
            "graph_patch_count": int(
                conn.execute(
                    "SELECT COUNT(*) FROM graph_patches WHERE job_id = ?",
                    (job_id,),
                ).fetchone()[0]
            ),
            "accepted_checkpoint_count": int(
                conn.execute(
                    "SELECT COUNT(*) FROM decision_checkpoints WHERE job_id = ? "
                    "AND validator_status = 'accepted'",
                    (job_id,),
                ).fetchone()[0]
            ),
            "child_serial_wall_seconds": sum(child_walls),
            "child_parallel_wall_seconds": max(child_walls) if child_walls else 0,
            "child_parallel_savings_upper_bound_seconds": (
                sum(child_walls) - max(child_walls) if child_walls else 0
            ),
        }
        token_usage = _codex_session_metrics(
            run_root,
            node_keys={str(node["id"]): str(node["node_key"]) for node in nodes},
        )
        report = {
            "schema": ARM2_REPORT_SCHEMA,
            "run_id": payload.get("run_id"),
            "job_id": job_id,
            "instance_id": runtime_report.get("instance_id"),
            "classification": {
                "runtime_correctness": (
                    "passed" if runtime_validation.get("passed") else "failed"
                ),
                "effective_orchestration": (
                    "passed" if all(assertions.values()) else "failed"
                ),
                "task_capability": (
                    "resolved" if capability.get("official_resolved") else "task-failed"
                ),
            },
            "orchestration_assertions": assertions,
            "primary": {
                "node_key": primary.get("node_key") if primary else None,
                "node_id": primary_id or None,
                "checkpoint_event_id": checkpoints[0]["id"] if checkpoints else None,
                "session_ids": [
                    session["backend_session_key"] for session in primary_sessions
                ],
                "resume_count": sum(
                    int(session.get("resume_count") or 0)
                    for session in primary_sessions
                ),
            },
            "children": [
                {
                    "node_key": node["node_key"],
                    "node_id": node["id"],
                    "state": node["state"],
                    "workspace_path": node.get("workspace_path"),
                    "contract": _loads(node.get("constraints_json")).get("contract")
                    or {},
                    "session_ids": [
                        session["backend_session_key"]
                        for session in child_sessions
                        if session["node_id"] == node["id"]
                    ],
                }
                for node in child_nodes
            ],
            "contributions": contribution_payloads,
            "integration_attribution": attribution,
            "quality": {
                "official_resolved": bool(capability.get("official_resolved")),
                "fail_to_pass": capability.get("fail_to_pass"),
                "pass_to_pass": capability.get("pass_to_pass"),
                "reference_only": {
                    "phase4g9_native_ultra_best_fail_to_pass": "63/68",
                    "hard_quality_gate": None,
                },
            },
            "execution_metrics": execution_metrics,
            "evaluator_progression": evaluator_progression,
            "run_timeline": run_timeline,
            "token_usage": token_usage,
            "runtime_validation": runtime_validation,
            "generated_at": int(time.time()),
        }
        return report
    finally:
        conn.close()


def render_arm2_summary(report: dict[str, Any]) -> str:
    quality = report["quality"]
    assertions = report["orchestration_assertions"]
    lines = [
        "# Phase 4G10 Runtime Arm 2 执行总结",
        "",
        "## 三轴结论",
        "",
        f"- Runtime correctness：`{report['classification']['runtime_correctness']}`；",
        f"- Effective orchestration：`{report['classification']['effective_orchestration']}`；",
        f"- Task capability：`{report['classification']['task_capability']}`；",
        f"- FAIL_TO_PASS：`{(quality.get('fail_to_pass') or {}).get('passed', 0)}/"
        f"{(quality.get('fail_to_pass') or {}).get('total', 0)}`；",
        f"- PASS_TO_PASS：`{(quality.get('pass_to_pass') or {}).get('passed', 0)}/"
        f"{(quality.get('pass_to_pass') or {}).get('total', 0)}`。",
        "",
        "`63/68` 仅为 Native Ultra 参考值，不是本实验硬门槛。",
        "",
        "## 实际执行图",
        "",
        "```text",
        "primary early assessment",
        "├── plots-diffs-output-model",
        "├── stage-runtime-and-run-cache",
        "└── tree-remote-and-streaming",
        "          ↓ frozen contributions",
        "primary original thread resume + integration + evaluator remediation",
        "```",
        "",
        "## Orchestra 断言",
        "",
    ]
    lines.extend(
        f"- {'PASS' if passed else 'FAIL'} `{name}`"
        for name, passed in assertions.items()
    )
    lines.extend(
        [
            "",
            "## 节点",
            "",
            f"Primary：`{report['primary'].get('node_key')}`，resume "
            f"`{report['primary'].get('resume_count')}` 次。",
            "",
        ]
    )
    for child in report.get("children") or []:
        lines.append(
            f"- `{child['node_key']}`：state=`{child['state']}`，"
            f"workspace=`{child.get('workspace_path')}`"
        )
    lines.extend(
        [
            "",
            f"Frozen contributions：`{len(report.get('contributions') or [])}`。",
            "",
            "## Official evaluator 进展",
            "",
            "| Round | F2P | P2P | Resolved |",
            "| ---: | ---: | ---: | --- |",
        ]
    )
    for item in report.get("evaluator_progression") or []:
        fail = item.get("fail_to_pass") or {}
        passed = item.get("pass_to_pass") or {}
        lines.append(
            f"| {item['round']} | {fail.get('passed', 0)}/{fail.get('total', 0)} | "
            f"{passed.get('passed', 0)}/{passed.get('total', 0)} | "
            f"{'yes' if item.get('resolved') else 'no'} |"
        )
    metrics = report.get("execution_metrics") or {}
    usage = (report.get("token_usage") or {}).get("aggregate") or {}
    lines.extend(
        [
            "",
            "## 成本摘要",
            "",
            f"- 运行 wall time：`{metrics.get('wall_time_seconds', 0)}s`；",
            f"- durable child 串行总时长：`{metrics.get('child_serial_wall_seconds', 0)}s`；",
            f"- durable child 实际并行窗口：`{metrics.get('child_parallel_wall_seconds', 0)}s`；",
            f"- 并行节省上界：`{metrics.get('child_parallel_savings_upper_bound_seconds', 0)}s`；",
            f"- implementation input tokens：`{usage.get('input_tokens', 0)}`；",
            f"- cached input tokens：`{usage.get('cached_input_tokens', 0)}`；",
            f"- cache ratio：`{usage.get('cache_ratio', 0)}`；",
            f"- output tokens：`{usage.get('output_tokens', 0)}`；",
            f"- context compactions：`{(report.get('token_usage') or {}).get('context_compactions', 0)}`。",
            "",
            "本报告只证明 durable nodes 被真实创建、并行执行并进入最终 candidate；最终任务仍未 resolved。",
            "",
        ]
    )
    return "\n".join(lines)


def run_arm2(
    *,
    qualification_spec_path: Path,
    run_root: Path,
    source_codex_home: Path,
    artifact_root: Path,
    execute_real: bool,
    max_wall_seconds: float,
    worker_timeout_seconds: int,
    resume_run: Path | None = None,
) -> dict[str, Any]:
    operator_stop = (
        _load_operator_stop_request(resume_run)
        if resume_run is not None
        else None
    )
    payload = p4g8_run.run_phase4g8_real_case(
        qualification_spec_path=qualification_spec_path,
        run_root=None if resume_run is not None else run_root,
        resume_run=resume_run,
        source_codex_home=source_codex_home,
        case_size="large",
        execute_real=execute_real,
        max_wall_seconds=max_wall_seconds,
        worker_timeout_seconds=worker_timeout_seconds,
        max_unresolved_evaluator_attempts=100,
        max_evaluator_no_progress_streak=100,
        orchestration_policy={
            "mode": "early_structure_assessment",
            "required": True,
            "require_contribution_attribution": True,
            "minimum_integrated_contributions": 2,
            "max_child_nodes": 3,
        },
        fault_profile="small",
        run_id_prefix="phase4g10-arm2",
        reasoning_effort_override="max",
        operator_stop=operator_stop,
    )
    actual_root = Path(str(payload["paths"]["root"])).resolve()
    report = build_arm2_orchestration_report(actual_root, payload)
    reports = actual_root / "reports"
    _write_json(reports / "arm2-orchestration.json", report)
    (reports / "execution-summary.md").write_text(
        render_arm2_summary(report),
        encoding="utf-8",
    )
    expected_entries = {
        "reports",
        "hermes-home",
        "codex-homes",
        "service",
    }
    if (actual_root / "runtime-contributions").is_dir():
        expected_entries.add("runtime-contributions")
    archive = validation_artifacts.archive_validation_run(
        actual_root,
        artifact_root=artifact_root,
        phase="phase4g10",
        instance_id=str(report.get("instance_id") or "unknown"),
        redactions=validation_artifacts.model_source_redactions(source_codex_home),
        expected_entries=expected_entries,
    )
    report["artifact_archive"] = archive
    _write_json(reports / "arm2-orchestration.json", report)
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--run-root")
    target.add_argument("--resume-run")
    parser.add_argument("--source-codex-home", default=str(Path.home() / ".codex"))
    parser.add_argument(
        "--artifact-root",
        default="/root/hermes-validation-artifacts",
    )
    parser.add_argument("--max-wall-seconds", type=float, default=43_200)
    parser.add_argument("--worker-timeout-seconds", type=int, default=10_800)
    parser.add_argument("--execute-real", action="store_true", required=True)
    parser.add_argument("--request-operator-stop")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.request_operator_stop:
        if not args.resume_run:
            raise ValueError("--request-operator-stop requires --resume-run")
        report = request_operator_stop(
            Path(args.resume_run),
            reason=str(args.request_operator_stop),
        )
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    report = run_arm2(
        qualification_spec_path=Path(args.spec),
        run_root=Path(args.run_root or args.resume_run),
        source_codex_home=Path(args.source_codex_home),
        artifact_root=Path(args.artifact_root),
        execute_real=bool(args.execute_real),
        max_wall_seconds=float(args.max_wall_seconds),
        worker_timeout_seconds=int(args.worker_timeout_seconds),
        resume_run=Path(args.resume_run) if args.resume_run else None,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
