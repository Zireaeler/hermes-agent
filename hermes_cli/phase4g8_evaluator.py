"""Dedicated process entrypoint for the Phase 4G8 official evaluator lane."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import uuid
from typing import Any, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_kernel as rk
from hermes_cli import kanban_runtime_phase4g8 as p4g8
from hermes_cli.codex_worker import collect_git_evidence


def run_official_evaluator(
    *,
    task_id: str,
    workspace: Path,
    spec_path: Path,
    run_id: str,
    task_run_id: Optional[int] = None,
    board: Optional[str] = None,
) -> dict[str, Any]:
    spec = p4g8.load_qualification_spec(spec_path)
    session_id = f"official-evaluator:{run_id}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    with kb.connect(board=board) as conn:
        node = conn.execute("SELECT * FROM execution_nodes WHERE latest_task_id = ?", (task_id,)).fetchone()
        if node is None or node["node_type"] != "verification":
            raise ValueError("official evaluator task is not linked to a verification node")
        target = rk._independent_verification_target(conn, dict(node))
        provenance = rk.build_independent_verification_provenance(
            conn,
            node["id"],
            producer_kind="official_evaluator",
            producer_session_id=session_id,
        )
        goal_keys = rk._loads(node["metadata_json"]).get("goal_item_keys") or []
        kb.record_task_event(
            conn,
            task_id,
            "worker_backend_session_started",
            {
                "worker_lane": node["assignee"],
                "worker_kind": "phase4g8_evaluator",
                "backend_session_id": session_id,
                "target_revision": target["target_revision"],
            },
            run_id=task_run_id,
        )

    before = collect_git_evidence(str(workspace))
    if before.get("workspace_revision") != target["target_revision"]:
        result = _stale_target_result(before.get("workspace_revision"), target["target_revision"])
    else:
        result = p4g8._run_evaluator(spec, workspace)
    after = collect_git_evidence(str(workspace))
    workspace_unchanged = before.get("workspace_revision") == after.get("workspace_revision")
    passed = result.get("resolved") is True and workspace_unchanged
    failed_tests = [
        str(test_id)
        for section in (result.get("fail_to_pass") or {}, result.get("pass_to_pass") or {})
        for test_id in section.get("failed_tests") or []
    ][:20]
    failure_detail = ", ".join(failed_tests)
    failure_summary = (
        f"official evaluator did not resolve fixed target; failed tests: {failure_detail}"
        if failure_detail
        else "official evaluator did not resolve fixed target"
    )
    diagnostics = result.get("failure_diagnostics")
    if isinstance(diagnostics, dict) and diagnostics.get("text"):
        failure_summary += "\nFailure diagnostics:\n" + str(diagnostics["text"])
    receipt = {
        "schema": "runtime_worker_receipt_v1",
        "verdict": "pass" if passed else "failed",
        "summary": "official evaluator resolved fixed target" if passed else failure_summary,
        "claimed_goal_items": list(goal_keys) if passed else [],
        "partial_goal_items": [],
        "unmet_goal_items": [],
        "contradicted_goal_items": [] if passed else list(goal_keys),
        "changed_files": [],
        "verification": {
            "passed": passed,
            "summary": "official evaluator result" if passed else failure_summary,
            "workspace_unchanged": workspace_unchanged,
        },
        "verification_provenance": provenance,
        "artifacts": [{
            "artifact_type": "official_evaluator_result",
            "path_or_ref": f"evaluator:{spec['instance_id']}:{result.get('stdout_sha256') or 'stale-target'}",
            "summary": "bounded official evaluator result",
        }],
        "official_evaluator_result": result,
    }
    with kb.connect(board=board) as conn:
        kb.complete_task(
            conn,
            task_id,
            result=receipt["summary"],
            summary=receipt["summary"],
            metadata=receipt,
        )
        kb.record_task_event(
            conn,
            task_id,
            "official_evaluator_completed",
            {
                "resolved": bool(result.get("resolved")),
                "workspace_unchanged": workspace_unchanged,
                "target_revision": target["target_revision"],
                "result_ref": receipt["artifacts"][0]["path_or_ref"],
            },
            run_id=task_run_id,
        )
    return receipt


def _stale_target_result(actual: Any, expected: Any) -> dict[str, Any]:
    return {
        "schema": p4g8.EVALUATOR_RESULT_SCHEMA,
        "resolved": False,
        "fail_to_pass": {"passed": 0, "failed": 1, "total": 1},
        "pass_to_pass": {"passed": 0, "failed": 1, "total": 1},
        "error": "stale_target_revision",
        "actual_revision": actual,
        "expected_revision": expected,
        "wall_time_seconds": 0.0,
    }


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m hermes_cli.phase4g8_evaluator")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--task-run-id", type=int)
    parser.add_argument("--board")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        receipt = run_official_evaluator(
            task_id=args.task_id,
            workspace=Path(args.workspace).resolve(),
            spec_path=Path(args.spec).resolve(),
            run_id=args.run_id,
            task_run_id=args.task_run_id,
            board=args.board,
        )
    except Exception as exc:
        try:
            with kb.connect(board=args.board) as conn:
                kb.record_task_event(
                    conn,
                    args.task_id,
                    "official_evaluator_failed",
                    {"error_type": type(exc).__name__},
                    run_id=args.task_run_id,
                )
                kb.block_task(
                    conn,
                    args.task_id,
                    reason=f"official-evaluator-failed:{type(exc).__name__}",
                    expected_run_id=args.task_run_id,
                    metadata={"error_type": type(exc).__name__},
                )
        except Exception:
            pass
        print(json.dumps({"status": "failed", "error": type(exc).__name__}), flush=True)
        return 1
    print(json.dumps({"status": "completed", "verdict": receipt["verdict"]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
