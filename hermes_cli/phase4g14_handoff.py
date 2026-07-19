"""Phase 4G14 durable contribution handoff controlled validation."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_kernel as rk
from hermes_cli import validation_artifacts


INSTANCE_ID = "controlled-two-child-handoff"


@dataclass(frozen=True)
class HandoffRunConfig:
    root: Path
    artifact_root: Path | None = None
    cleanup_source: bool = True


def _git(workspace: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(workspace), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.strip() or completed.stdout.strip() or "git failed"
        )
    return completed.stdout.strip()


def _run_test(workspace: Path, test_name: str) -> dict[str, Any]:
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "-v", test_name],
        cwd=workspace,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return {
        "command": f"{sys.executable} -m unittest -v {test_name}",
        "returncode": completed.returncode,
        "wall_time_seconds": round(time.monotonic() - started, 6),
        "output": completed.stdout[-8000:],
    }


def _write_repository(workspace: Path) -> str:
    (workspace / "src").mkdir(parents=True)
    (workspace / "tests").mkdir()
    (workspace / "src" / "__init__.py").write_text("", encoding="utf-8")
    (workspace / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (workspace / "src" / "a.py").write_text(
        "def value() -> int:\n    return 1\n",
        encoding="utf-8",
    )
    (workspace / "src" / "b.py").write_text(
        "def value() -> int:\n    return 1\n",
        encoding="utf-8",
    )
    (workspace / "tests" / "test_components.py").write_text(
        """import unittest

from src import a, b


class ComponentTests(unittest.TestCase):
    def test_a(self):
        self.assertEqual(a.value(), 2)

    def test_b(self):
        self.assertEqual(b.value(), 2)


if __name__ == "__main__":
    unittest.main()
""",
        encoding="utf-8",
    )
    (workspace / ".gitignore").write_text(
        "__pycache__/\n*.py[cod]\n",
        encoding="utf-8",
    )
    _git(workspace, "init")
    _git(workspace, "config", "user.email", "runtime@example.invalid")
    _git(workspace, "config", "user.name", "Runtime Validation")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", "phase4g14 handoff base")
    return _git(workspace, "rev-parse", "HEAD")


def _contract(scope: str, outcome: str) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "acceptance_criteria": ["bounded component test passes"],
        "success_evidence": ["changed_files", "verification", "worker_summary"],
        "declared_write_scope": [scope],
        "prohibited_actions": ["modify_sibling_scope"],
        "workspace_mode": "isolated_worktree",
    }


def _node(conn, job_id: str, node_key: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM execution_nodes WHERE job_id = ? AND node_key = ?",
        (job_id, node_key),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"missing runtime node {node_key}")
    return dict(row)


def _complete_codex_shaped_task(
    conn,
    node: dict[str, Any],
    receipt: dict[str, Any],
) -> None:
    if not kb.complete_task(
        conn,
        str(node["latest_task_id"]),
        result=str(receipt["summary"]),
        summary=str(receipt["summary"]),
        metadata={
            "summary": receipt["summary"],
            "worker_lane": {
                "name": "phase4g14-controlled-worker",
                "kind": "codex_cli",
            },
            "runtime_receipt": receipt,
        },
    ):
        raise RuntimeError(f"could not complete task for {node['node_key']}")


def _child_receipt(
    *,
    summary: str,
    goal_value: str,
    test_result: dict[str, Any],
    malformed: bool,
) -> dict[str, Any]:
    return {
        "schema": rk.RUNTIME_CONTRIBUTION_RECEIPT_SCHEMA,
        "verdict": "succeeded",
        "summary": summary,
        "claimed_goal_items": [],
        "partial_goal_items": [goal_value],
        "unmet_goal_items": [],
        "contradicted_goal_items": [],
        # Runtime must derive the actual paths from git, not trust this array.
        "changed_files": [],
        "verification": {
            "passed": test_result["returncode"] == 0,
            "summary": (
                f"{test_result['command']} exited {test_result['returncode']}"
            ),
        },
        "artifacts": [test_result["command"]],
        "accepted_contributions": (
            ["Natural-language implementation summary"] if malformed else []
        ),
        "modified_contributions": [],
        "rejected_contributions": [],
        "active_assumptions": [],
        "rejected_approaches": [],
        "known_failure_boundaries": [],
        "consumed_directive_ids": [],
        "structure_request": None,
        "responsibility_candidates": [],
    }


def _create_job(conn, root: Path, workspace: Path, base_revision: str) -> str:
    root_task_id = kb.create_task(
        conn,
        title="Phase 4G14 durable handoff validation",
        initial_status="running",
        workspace_kind="dir",
        workspace_path=str(workspace),
    )
    job_id = rk.create_runtime_job(
        conn,
        root_task_id,
        "Integrate two isolated component changes and pass the complete unittest suite.",
        workspace_path=str(workspace),
        goal_items=[{
            "item_key": "integrated-components",
            "description": "both component changes are integrated and tested",
            "required": True,
            "verifier_required": False,
        }],
        initialization_mode="fixture",
        runtime_metadata={
            "orchestration_policy": {
                "schema": rk.RUNTIME_ORCHESTRATION_POLICY_SCHEMA,
                "enabled": True,
                "mode": "closed_loop_coordination",
                "base_revision": base_revision,
                "worktree_root": str(root / "runtime-worktrees"),
                "contribution_root": str(root / "runtime-contributions"),
                "require_contribution_attribution": True,
                "minimum_integrated_contributions": 2,
                "retention": {
                    "worktrees": "retain",
                    "contributions": "retain",
                },
            },
        },
    )
    primary = _node(conn, job_id, "understand-scope")
    constraints = json.loads(primary["constraints_json"])
    constraints["contract"] = {
        "outcome": "Integrate both promoted child patches and run the complete suite.",
        "acceptance_criteria": ["two artifacts are attributed", "all tests pass"],
        "success_evidence": ["changed_files", "verification", "worker_summary"],
        "declared_write_scope": ["**"],
        "prohibited_actions": ["discard_promoted_contribution"],
        "workspace_mode": "shared_job_workspace",
    }
    conn.execute(
        """
        UPDATE execution_nodes
           SET node_key = 'integration-owner', title = 'Integrate components',
               description = 'Apply both frozen patches and run all tests.',
               state = 'waiting_structure', constraints_json = ?
         WHERE id = ?
        """,
        (json.dumps(constraints), primary["id"]),
    )
    structure_event_id = rk._event(
        conn,
        job_id,
        "worker_structure_checkpointed",
        {
            "node_key": "integration-owner",
            "checkpoint": {
                "schema": rk.STRUCTURE_CHECKPOINT_SCHEMA,
                "kind": "early_structure_assessment",
                "recommendation": "expand",
                "summary": "Components A and B have disjoint files and tests.",
            },
        },
        node_id=primary["id"],
        source="phase4g14_controlled_fixture",
    )
    child_specs = [
        ("component-a", "src/a.py", "Implement component A value."),
        ("component-b", "src/b.py", "Implement component B value."),
    ]
    ops: list[dict[str, Any]] = []
    for node_key, scope, outcome in child_specs:
        ops.append({
            "op": "create_node",
            "node_key": node_key,
            "node_type": "implementation",
            "title": f"Implement {node_key}",
            "description": outcome,
            "goal_item_keys": ["integrated-components"],
            "contract": _contract(scope, outcome),
        })
    for node_key, _scope, _outcome in child_specs:
        ops.append({
            "op": "add_dependency",
            "from_node_key": node_key,
            "to_node_key": "integration-owner",
            "dependency_type": "depends_on",
        })
    patch = {
        "schema": rk.PATCH_SCHEMA,
        "expected_revision": int(
            conn.execute(
                "SELECT graph_revision FROM runtime_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()[0]
        ),
        "rationale_summary": "Use two isolated low-coupling child contributions.",
        "ops": ops,
        "decomposition": {
            "policy_version": "1",
            "mode": "multiple_runtime_nodes",
            "justifications": [{
                "type": "durable_parallelism",
                "nodes": ["component-a", "component-b"],
                "explanation": "The source and focused tests are independent.",
                "evidence_refs": [f"event:{structure_event_id}"],
                "declared_write_scopes": {
                    "component-a": ["src/a.py"],
                    "component-b": ["src/b.py"],
                },
                "integration_owner_node_key": "integration-owner",
            }],
        },
    }
    result = rk.apply_graph_patch(conn, job_id, patch)
    if result["status"] != "applied":
        raise RuntimeError(f"could not create controlled graph: {result}")
    return job_id


def _run_children_and_capture(
    conn,
    job_id: str,
) -> dict[str, Any]:
    tests: dict[str, dict[str, Any]] = {}
    for node_key, source, test_name in (
        ("component-a", "src/a.py", "tests.test_components.ComponentTests.test_a"),
        ("component-b", "src/b.py", "tests.test_components.ComponentTests.test_b"),
    ):
        node = _node(conn, job_id, node_key)
        task_id = rk.materialize_runtime_node(conn, node)
        task = kb.get_task(conn, str(task_id))
        child_workspace = Path(str(task.workspace_path))
        (child_workspace / source).write_text(
            "def value() -> int:\n    return 2\n",
            encoding="utf-8",
        )
        tests[node_key] = _run_test(child_workspace, test_name)
        if tests[node_key]["returncode"] != 0:
            raise RuntimeError(f"{node_key} focused test failed")
        receipt = _child_receipt(
            summary=f"{node_key} implementation and focused test completed",
            goal_value=(
                "Natural language is not a goal key"
                if node_key == "component-b"
                else "integrated-components"
            ),
            test_result=tests[node_key],
            malformed=node_key == "component-b",
        )
        _complete_codex_shaped_task(
            conn,
            _node(conn, job_id, node_key),
            receipt,
        )
    reconcile = rk.reconcile_runtime_materializations(conn, job_id)
    if "receipt_invalid" not in reconcile["events"]:
        raise RuntimeError("controlled malformed receipt was not rejected")
    component_a = _node(conn, job_id, "component-a")
    if not rk.ingest_runtime_node_evidence(conn, component_a["id"]):
        raise RuntimeError("component A receipt was not ingested")
    return {"tests": tests, "reconcile": reconcile}


def _repair_component_b(conn, job_id: str) -> dict[str, Any]:
    node = _node(conn, job_id, "component-b")
    repair_task_id = rk.materialize_runtime_node(conn, node)
    repair_task = kb.get_task(conn, str(repair_task_id))
    repair_materialization = conn.execute(
        "SELECT * FROM node_materializations WHERE task_id = ?",
        (repair_task_id,),
    ).fetchone()
    repair_metadata = json.loads(repair_materialization["metadata_json"])
    repair = repair_metadata["receipt_protocol_repair"]
    if "Do not inspect files, run shell commands" not in str(repair_task.body):
        raise RuntimeError("repair task did not prohibit implementation reexecution")
    original = dict(repair["original_receipt"])
    original["partial_goal_items"] = ["integrated-components"]
    original["accepted_contributions"] = []
    _complete_codex_shaped_task(
        conn,
        _node(conn, job_id, "component-b"),
        original,
    )
    repaired_node = _node(conn, job_id, "component-b")
    if not rk.ingest_runtime_node_evidence(conn, repaired_node["id"]):
        raise RuntimeError("component B repaired receipt was not ingested")
    return {
        "task_id": repair_task_id,
        "source_attempt_artifact_id": repair["source_attempt_artifact_id"],
        "validation_errors": repair["validation_errors"],
        "shell_commands_run": 0,
        "workspace_modified": False,
    }


def _integrate(conn, job_id: str, workspace: Path) -> dict[str, Any]:
    primary = _node(conn, job_id, "integration-owner")
    if primary["state"] != "ready":
        raise RuntimeError(f"integration owner is not ready: {primary['state']}")
    contributions = conn.execute(
        """
        SELECT artifact.id, artifact.path_or_ref
          FROM execution_dependencies dep
          JOIN node_artifacts artifact ON artifact.node_id = dep.from_node_id
         WHERE dep.to_node_id = ?
           AND artifact.artifact_type = 'runtime_node_contribution'
         ORDER BY artifact.id
        """,
        (primary["id"],),
    ).fetchall()
    if len(contributions) != 2:
        raise RuntimeError("integration owner did not receive two contributions")
    for artifact in contributions:
        _git(workspace, "apply", str(artifact["path_or_ref"]))
    complete_test = _run_test(workspace, "tests.test_components")
    if complete_test["returncode"] != 0:
        raise RuntimeError("integrated unittest suite failed")
    task_id = rk.materialize_runtime_node(conn, primary)
    task = kb.get_task(conn, str(task_id))
    artifact_ids = [str(row["id"]) for row in contributions]
    if any(artifact_id not in str(task.body) for artifact_id in artifact_ids):
        raise RuntimeError("integration task context omitted a contribution")
    receipt = {
        "schema": rk.RUNTIME_INTEGRATION_RECEIPT_SCHEMA,
        "verdict": "succeeded",
        "summary": "Integrated both promoted child contributions and passed all tests.",
        "claimed_goal_items": ["integrated-components"],
        "partial_goal_items": [],
        "unmet_goal_items": [],
        "contradicted_goal_items": [],
        "changed_files": ["src/a.py", "src/b.py"],
        "verification": {
            "passed": True,
            "summary": f"{complete_test['command']} exited 0",
        },
        "artifacts": [complete_test["command"]],
        "accepted_contributions": artifact_ids,
        "modified_contributions": [],
        "rejected_contributions": [],
        "active_assumptions": [],
        "rejected_approaches": [],
        "known_failure_boundaries": [],
        "consumed_directive_ids": [],
        "structure_request": None,
        "responsibility_candidates": [],
    }
    _complete_codex_shaped_task(
        conn,
        _node(conn, job_id, "integration-owner"),
        receipt,
    )
    if not rk.ingest_runtime_node_evidence(conn, primary["id"]):
        raise RuntimeError("integration receipt was not ingested")
    return {
        "task_id": task_id,
        "artifact_ids": artifact_ids,
        "test": complete_test,
    }


def _write_reports(root: Path, report: dict[str, Any]) -> None:
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "run-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    handoff = report["orchestration"]["contribution_handoff"]
    (reports / "capability-trace.md").write_text(
        f"""# Phase 4G14 Durable Contribution Handoff 验证

## 结论

两个 isolated child 都执行了真实文件修改和 Python unittest。`component-b` 第一份 receipt
被确定性注入非法 goal key 和自然语言 contribution 值；Runtime 在拒绝 receipt 前已保存其
immutable attempt patch。重开数据库连接后，protocol repair 没有运行 shell、没有修改
workspace，随后从同一个 attempt artifact 晋升正式 contribution。

Primary 收到并应用两个正式 artifact，完整 unittest 通过，最终 job 状态为
`{report['final_state']}`。

## 指标

- Attempt patch captured: `{handoff['attempt_patch_captured_count']}`
- Promoted contribution: `{handoff['promoted_contribution_count']}`
- Receipt repair: `{handoff['receipt_repair_count']}`
- Implementation reexecution due to receipt: `{handoff['implementation_reexecution_due_to_receipt_count']}`
- Integrated contribution: `{handoff['integrated_contribution_count']}`
- Preservation ratio: `{handoff['contribution_preservation_ratio']}`
- Consistency: `{report['consistency']['status']}`

本验证不调用 Decision Provider、模型 worker 或 official evaluator；它验证真实 git/worktree、
subprocess test、DB restart、Kanban task/receipt、artifact promotion、Primary integration 和 cleanup
路径，不评价模型编码能力。
""",
        encoding="utf-8",
    )


def run_phase4g14_handoff(config: HandoffRunConfig) -> dict[str, Any]:
    root = config.root.expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError(f"handoff run root must be empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    workspace = root / "workspace"
    workspace.mkdir()
    hermes_home = root / "hermes-home"
    prior_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(hermes_home)
    try:
        base_revision = _write_repository(workspace)
        kb.init_db()
        with kb.connect() as conn:
            job_id = _create_job(conn, root, workspace, base_revision)
            child_result = _run_children_and_capture(conn, job_id)
            pre_restart = rk.summarize_runtime_orchestration(conn, job_id)
        # A fresh DB connection is the controlled process boundary.
        with kb.connect() as conn:
            repair_result = _repair_component_b(conn, job_id)
            integration_result = _integrate(conn, job_id, workspace)
            status = rk.status_runtime_job(conn, job_id)
            consistency = rk.check_runtime_consistency(conn, job_id)
            cleanup = rk.cleanup_runtime_orchestration_worktrees(
                conn,
                job_id,
                reason="phase4g14_validation_complete",
            )
            orchestration = rk.summarize_runtime_orchestration(conn, job_id)
            report = {
                "schema": "hermes_phase4g14_handoff_report_v1",
                "instance_id": INSTANCE_ID,
                "job_id": job_id,
                "base_revision": base_revision,
                "process_boundaries": 1,
                "official_evaluator_used": False,
                "model_worker_used": False,
                "child_execution": child_result,
                "pre_restart_handoff": pre_restart["contribution_handoff"],
                "receipt_repair": repair_result,
                "integration": integration_result,
                "orchestration": orchestration,
                "final_state": status["job"]["state"],
                "goal_items": status["goal_items"],
                "consistency": consistency,
                "cleanup": cleanup,
                "generated_at": int(time.time()),
            }
            _write_reports(root, report)
        manifest = validation_artifacts.archive_validation_run(
            root,
            artifact_root=config.artifact_root,
            phase="phase4g14",
            instance_id=INSTANCE_ID,
            expected_entries=("hermes-home", "reports", "runtime-contributions"),
        )
        report["artifact_archive"] = {
            "artifact_path": manifest["artifact_path"],
            "manifest_path": str(Path(manifest["artifact_path"]) / "manifest.json"),
            "status": manifest["status"],
            "file_count": manifest["file_count"],
            "total_bytes": manifest["total_bytes"],
        }
        if config.cleanup_source:
            shutil.rmtree(root)
        return report
    finally:
        if prior_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = prior_home


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="运行 Phase 4G14 durable contribution handoff 验证",
    )
    parser.add_argument("--root", required=True)
    parser.add_argument(
        "--artifact-root",
        default=str(validation_artifacts.default_artifact_root()),
    )
    parser.add_argument("--keep-source", action="store_true")
    args = parser.parse_args(argv)
    report = run_phase4g14_handoff(
        HandoffRunConfig(
            root=Path(args.root),
            artifact_root=Path(args.artifact_root),
            cleanup_source=not args.keep_source,
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    handoff = report["orchestration"]["contribution_handoff"]
    passed = bool(
        report["final_state"] == "done"
        and report["consistency"]["status"] == "passed"
        and report["cleanup"]["status"] in {"cleaned", "already_clean"}
        and handoff["attempt_patch_captured_count"] == 2
        and handoff["promoted_contribution_count"] == 2
        and handoff["receipt_repair_count"] == 1
        and handoff["implementation_reexecution_due_to_receipt_count"] == 0
        and handoff["integrated_contribution_count"] == 2
        and handoff["contribution_preservation_ratio"] == 1.0
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
