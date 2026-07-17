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
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
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
