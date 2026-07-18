"""Phase 4G10.1 clean durable Runtime replay harness."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import time
from typing import Any

from hermes_cli import kanban_runtime_phase4g8_run as p4g8_run
from hermes_cli import phase4g10_arm2 as arm2
from hermes_cli import validation_artifacts


CLEAN_REPLAY_REPORT_SCHEMA = "hermes_phase4g10_clean_replay_v1"
HISTORICAL_REPAIR_EVENTS = {
    "phase4g8_contribution_attribution_branch_repaired",
    "phase4g8_receipt_recovery_branch_repaired",
    "phase4g8_structure_request_branch_repaired",
    "phase4g8_resume_timeout_repaired",
    "phase4g8_receipt_recovery_requeued",
    "runtime_receipt_adapted",
}
RECEIPT_RECOVERY_EVENTS = {
    "receipt_missing",
    "receipt_invalid",
    "receipt_recovery_requested",
}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _runtime_source_state() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout.splitlines()
    return {
        "root": str(root),
        "revision": revision,
        "clean": not status,
        "status": status,
    }


def _initial_source_state_from_run(run_root: Path) -> dict[str, Any]:
    db_path = run_root.resolve() / "hermes-home" / "kanban.db"
    if not db_path.is_file():
        raise RuntimeError("Clean Replay resume is missing its Runtime database")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT metadata_json FROM runtime_jobs").fetchall()
    finally:
        conn.close()
    if len(rows) != 1:
        raise RuntimeError("Clean Replay resume requires exactly one Runtime job")
    metadata = json.loads(rows[0]["metadata_json"] or "{}")
    source = (metadata.get("orchestration_policy") or {}).get(
        "clean_replay_source_state"
    )
    if not isinstance(source, dict) or not source.get("revision"):
        raise RuntimeError("Clean Replay resume is missing its initial source state")
    return source


def _event_counts(
    conn: sqlite3.Connection,
    job_id: str,
    event_types: set[str],
) -> dict[str, int]:
    if not event_types:
        return {}
    placeholders = ",".join("?" for _ in event_types)
    rows = conn.execute(
        f"""
        SELECT event_type, COUNT(*) AS count FROM execution_events
         WHERE job_id = ? AND event_type IN ({placeholders})
         GROUP BY event_type
        """,
        (job_id, *sorted(event_types)),
    ).fetchall()
    observed = {str(row["event_type"]): int(row["count"]) for row in rows}
    return {name: observed.get(name, 0) for name in sorted(event_types)}


def build_clean_replay_report(
    run_root: Path,
    payload: dict[str, Any],
    *,
    source_before: dict[str, Any],
    source_after: dict[str, Any],
) -> dict[str, Any]:
    run_root = run_root.resolve()
    orchestration = arm2.build_arm2_orchestration_report(run_root, payload)
    job_id = str(payload["job_id"])
    conn = sqlite3.connect(run_root / "hermes-home" / "kanban.db")
    conn.row_factory = sqlite3.Row
    try:
        repair_counts = _event_counts(conn, job_id, HISTORICAL_REPAIR_EVENTS)
        receipt_counts = _event_counts(conn, job_id, RECEIPT_RECOVERY_EVENTS)
        supervisor_rows = conn.execute(
            """
            SELECT payload_json FROM execution_events
             WHERE job_id = ? AND event_type = 'runtime_supervisor_started'
             ORDER BY id
            """,
            (job_id,),
        ).fetchall()
        supervisor_owners = {
            str(json.loads(row["payload_json"] or "{}").get("owner") or "")
            for row in supervisor_rows
        }
        feedback_rows = conn.execute(
            """
            SELECT payload_json FROM execution_events
             WHERE job_id = ?
               AND event_type = 'evaluator_failure_feedback_consumed'
             ORDER BY id
            """,
            (job_id,),
        ).fetchall()
        feedback_consumed = len(feedback_rows)
        feedback_consumer_node_ids = sorted({
            str(json.loads(row["payload_json"] or "{}").get("consumer_node_id") or "")
            for row in feedback_rows
        } - {""})
        strategy_nodes = int(
            conn.execute(
                "SELECT COUNT(*) FROM execution_nodes "
                "WHERE job_id = ? AND node_type = 'strategy_update'",
                (job_id,),
            ).fetchone()[0]
        )
        lineage_events = 0
        for row in conn.execute(
            """
            SELECT payload_json FROM execution_events
             WHERE job_id = ?
               AND event_type = 'contribution_attribution_verified'
             ORDER BY id
            """,
            (job_id,),
        ):
            payload_json = json.loads(row["payload_json"] or "{}")
            if payload_json.get("contribution_lineage_refs"):
                lineage_events += 1
    finally:
        conn.close()

    runtime_report = payload.get("run_report") or {}
    runtime_validation = runtime_report.get("runtime_validation") or {}
    capability = runtime_report.get("capability_validation") or {}
    metrics = runtime_report.get("metrics") or {}
    evaluated_stop = metrics.get("evaluated_validation_stop") or {}
    ownership_canary = metrics.get("workspace_ownership_canary") or {}
    resolved = capability.get("official_resolved") is True
    evaluator_rounds = len(orchestration.get("evaluator_progression") or [])
    primary_node_id = str(orchestration.get("primary", {}).get("node_id") or "")
    duplicate_terminal = int(runtime_validation.get("duplicate_terminal_fact_count") or 0)
    duplicate_ledger = int(runtime_validation.get("duplicate_ledger_fact_count") or 0)
    assertions = {
        "fresh_runtime_source_revision": bool(source_before.get("clean"))
        and source_before.get("revision") == source_after.get("revision")
        and bool(source_after.get("clean")),
        "fresh_run_origin": bool(source_before.get("clean")),
        "runtime_correctness_passed": runtime_validation.get("passed") is True,
        "effective_orchestration_passed": orchestration.get("classification", {}).get(
            "effective_orchestration"
        ) == "passed",
        "evaluated_coverage_stop_or_resolved": resolved
        or evaluated_stop.get("schema") == p4g8_run.EVALUATED_STOP_POLICY_SCHEMA,
        "three_evaluated_candidates_or_resolved": resolved or evaluator_rounds >= 3,
        "two_feedback_cycles_consumed_or_resolved": resolved or feedback_consumed >= 2,
        "feedback_consumed_by_same_primary_or_resolved": resolved
        or (
            feedback_consumed >= 2
            and feedback_consumer_node_ids == [primary_node_id]
        ),
        "latest_candidate_is_evaluated": resolved
        or bool(evaluated_stop.get("candidate_patch_sha256")),
        "no_historical_repair_events": not any(repair_counts.values()),
        "no_receipt_recovery_events": not any(receipt_counts.values()),
        "no_speculative_strategy_nodes": strategy_nodes == 0,
        "no_duplicate_terminal_facts": duplicate_terminal == 0,
        "no_duplicate_ledger_facts": duplicate_ledger == 0,
        "ownership_canary_passed": ownership_canary.get("passed") is True,
        "supervisor_restart_has_db_lineage": len(supervisor_owners - {""}) >= 2,
        "primary_attribution_lineage_resumed": resolved or lineage_events >= 1,
    }
    return {
        "schema": CLEAN_REPLAY_REPORT_SCHEMA,
        "run_id": payload.get("run_id"),
        "job_id": job_id,
        "instance_id": orchestration.get("instance_id"),
        "classification": {
            "runtime_correctness": orchestration.get("classification", {}).get(
                "runtime_correctness"
            ),
            "clean_replay": "passed" if all(assertions.values()) else "failed",
            "effective_orchestration": orchestration.get("classification", {}).get(
                "effective_orchestration"
            ),
            "task_capability": orchestration.get("classification", {}).get(
                "task_capability"
            ),
        },
        "assertions": assertions,
        "event_counts": {
            "historical_repairs": repair_counts,
            "receipt_recovery": receipt_counts,
            "evaluator_failure_feedback_consumed": feedback_consumed,
            "feedback_consumer_node_ids": feedback_consumer_node_ids,
            "supervisor_started": len(supervisor_rows),
            "supervisor_owner_count": len(supervisor_owners - {""}),
            "attribution_lineage": lineage_events,
            "strategy_nodes": strategy_nodes,
        },
        "source_before": source_before,
        "source_after": source_after,
        "evaluated_validation_stop": evaluated_stop,
        "ownership_canary": ownership_canary,
        "orchestration": orchestration,
        "generated_at": int(time.time()),
    }


def render_clean_replay_summary(report: dict[str, Any]) -> str:
    classification = report["classification"]
    orchestration = report["orchestration"]
    lines = [
        "# Phase 4G10.1 Clean Runtime Replay 总结",
        "",
        "## 四轴结论",
        "",
        f"- Runtime correctness：`{classification['runtime_correctness']}`；",
        f"- Clean replay invariants：`{classification['clean_replay']}`；",
        f"- Effective orchestration：`{classification['effective_orchestration']}`；",
        f"- Task capability：`{classification['task_capability']}`。",
        "",
        "本次运行验证当前已提交 Runtime HEAD 能否从全新 DB/workspace 干净完成 durable orchestra，"
        "不以 benchmark resolved 或 `63/68` 为门槛。",
        "",
        "## Clean 断言",
        "",
    ]
    lines.extend(
        f"- {'PASS' if passed else 'FAIL'} `{name}`"
        for name, passed in report["assertions"].items()
    )
    lines.extend(
        [
            "",
            "## 实际节点与反馈循环",
            "",
            f"- Primary：`{orchestration.get('primary', {}).get('node_key')}`，resume "
            f"`{orchestration.get('primary', {}).get('resume_count', 0)}` 次；",
            f"- Durable children：`{len(orchestration.get('children') or [])}`；",
            f"- Frozen contributions：`{len(orchestration.get('contributions') or [])}`；",
            f"- Evaluator rounds：`{len(orchestration.get('evaluator_progression') or [])}`；",
            f"- 已消费 evaluator feedback："
            f"`{report['event_counts']['evaluator_failure_feedback_consumed']}`。",
            "",
            "## Evaluator 进展",
            "",
            "| Round | F2P | P2P | Resolved |",
            "| ---: | ---: | ---: | --- |",
        ]
    )
    for item in orchestration.get("evaluator_progression") or []:
        fail = item.get("fail_to_pass") or {}
        passed = item.get("pass_to_pass") or {}
        lines.append(
            f"| {item['round']} | {fail.get('passed', 0)}/{fail.get('total', 0)} | "
            f"{passed.get('passed', 0)}/{passed.get('total', 0)} | "
            f"{'yes' if item.get('resolved') else 'no'} |"
        )
    lines.extend(
        [
            "",
            "## Recovery 与边界",
            "",
            f"- 历史 repair events：`{sum(report['event_counts']['historical_repairs'].values())}`；",
            f"- Receipt recovery events：`{sum(report['event_counts']['receipt_recovery'].values())}`；",
            f"- Strategy nodes：`{report['event_counts']['strategy_nodes']}`；",
            f"- Supervisor owners：`{report['event_counts']['supervisor_owner_count']}`；",
            f"- Ownership canary：`{'passed' if report['ownership_canary'].get('passed') else 'failed'}`。",
            "",
        ]
    )
    return "\n".join(lines)


def run_clean_replay(
    *,
    qualification_spec_path: Path,
    run_root: Path | None,
    resume_run: Path | None = None,
    source_codex_home: Path,
    artifact_root: Path,
    execute_real: bool,
    max_wall_seconds: float,
    worker_timeout_seconds: int,
) -> dict[str, Any]:
    if (run_root is None) == (resume_run is None):
        raise ValueError("exactly one of run_root or resume_run is required")
    current_source = _runtime_source_state()
    if not current_source["clean"]:
        raise RuntimeError("Clean Replay requires a committed clean Runtime source tree")
    if resume_run is not None:
        source_before = _initial_source_state_from_run(resume_run)
        if source_before.get("revision") != current_source.get("revision"):
            raise RuntimeError(
                "Clean Replay cannot resume after the Runtime source revision changed"
            )
    else:
        source_before = current_source
    payload = p4g8_run.run_phase4g8_real_case(
        qualification_spec_path=qualification_spec_path,
        run_root=run_root,
        resume_run=resume_run,
        source_codex_home=source_codex_home,
        case_size="large",
        execute_real=execute_real,
        max_wall_seconds=max_wall_seconds,
        worker_timeout_seconds=worker_timeout_seconds,
        decision_timeout_seconds=600.0,
        max_unresolved_evaluator_attempts=100,
        max_evaluator_no_progress_streak=100,
        orchestration_policy={
            "mode": "early_structure_assessment",
            "required": True,
            "require_contribution_attribution": True,
            "minimum_integrated_contributions": 2,
            "max_child_nodes": 3,
            "clean_replay_source_state": source_before,
            "assessment_replay": {
                "schema": "runtime_early_structure_replay_v1",
                "required_recommendation": "expand",
                "validated_responsibility_families": [
                    "plots, diff, metrics/params reporting, templates, and completions",
                    "stage lifecycle, pipeline serialization, and run-cache behavior",
                    "tree/remote streaming, import/update, transfer, and compatibility",
                ],
                "primary_owned_shared_scope": [
                    "central CLI and Repo bindings",
                    "release version metadata",
                    "cross-family compatibility cleanup",
                    "final integration and broad validation",
                ],
            },
        },
        fault_profile="small",
        run_id_prefix="phase4g10-clean",
        reasoning_effort_override="max",
        compaction_reasoning_effort_override="low",
        evaluated_stop_policy={
            "schema": p4g8_run.EVALUATED_STOP_POLICY_SCHEMA,
            "min_completed_evaluator_attempts": 3,
            "min_consumed_evaluator_feedback": 2,
            "reason": "Clean Replay 已完成两次 evaluator feedback remediation，并保留第三个固定已评估 candidate。",
        },
        workspace_ownership_canary=True,
    )
    actual_root = Path(str(payload["paths"]["root"])).resolve()
    source_after = _runtime_source_state()
    _write_json(
        actual_root / "reports" / "clean-replay-source-state.json",
        {"source_before": source_before, "source_after": source_after},
    )
    report = build_clean_replay_report(
        actual_root,
        payload,
        source_before=source_before,
        source_after=source_after,
    )
    reports = actual_root / "reports"
    _write_json(reports / "arm2-orchestration.json", report["orchestration"])
    _write_json(reports / "clean-replay.json", report)
    (reports / "clean-replay-summary.md").write_text(
        render_clean_replay_summary(report),
        encoding="utf-8",
    )
    archive = validation_artifacts.archive_validation_run(
        actual_root,
        artifact_root=artifact_root,
        phase="phase4g10-clean-replay",
        instance_id=str(report.get("instance_id") or "unknown"),
        redactions=validation_artifacts.model_source_redactions(source_codex_home),
        expected_entries={
            "reports",
            "hermes-home",
            "codex-homes",
            "service",
            "runtime-contributions",
        },
    )
    cleanup = validation_artifacts.cleanup_rebuildable_entries(
        actual_root,
        manifest_path=Path(str(archive["artifact_path"])) / "manifest.json",
        entries={"workspace", "home", "codex-home-seed", "runtime-worktrees"},
    )
    report["artifact_archive"] = archive
    report["local_cleanup"] = cleanup
    _write_json(reports / "clean-replay.json", report)
    _write_json(
        reports / "retention.json",
        {
            "schema": "hermes_phase4g10_clean_replay_retention_v1",
            "archive": archive,
            "cleanup": cleanup,
            "retained_at": int(time.time()),
        },
    )
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
    parser.add_argument("--max-wall-seconds", type=float, default=28_800)
    parser.add_argument("--worker-timeout-seconds", type=int, default=10_800)
    parser.add_argument("--execute-real", action="store_true", required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report = run_clean_replay(
        qualification_spec_path=Path(args.spec),
        run_root=Path(args.run_root) if args.run_root else None,
        resume_run=Path(args.resume_run) if args.resume_run else None,
        source_codex_home=Path(args.source_codex_home),
        artifact_root=Path(args.artifact_root),
        execute_real=bool(args.execute_real),
        max_wall_seconds=float(args.max_wall_seconds),
        worker_timeout_seconds=int(args.worker_timeout_seconds),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["classification"]["clean_replay"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
