"""Phase 4G12 evidence-driven dynamic graph mutation validation."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

from hermes_cli import codex_worker
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_kernel as rk
from hermes_cli import phase4g11_small as phase4g11
from hermes_cli import validation_artifacts
from hermes_cli.worker_lanes import clear_worker_lanes, register_worker_lane


@dataclass(frozen=True)
class SmallRunConfig:
    root: Path
    lane_name: str = "phase4g12-codex"
    model: str = "gpt-5.6-sol"
    worker_timeout_seconds: int = 900
    decision_timeout_seconds: float = 180.0
    decision_limit: int = 6
    execution_cycle_limit: int = 8
    artifact_root: Path | None = None


def _git(workspace: Path, *args: str) -> str:
    return phase4g11._git(workspace, *args)


def _write_small_repository(workspace: Path) -> str:
    (workspace / "src").mkdir(parents=True)
    (workspace / "tests").mkdir()
    (workspace / "fixtures").mkdir()
    (workspace / "src" / "token_parser.py").write_text(
        """def parse_tokens(text: str) -> list[str]:
    return [part.lower() for part in text.split() if part]
""",
        encoding="utf-8",
    )
    (workspace / "src" / "token_renderer.py").write_text(
        """def render_tokens(tokens: list[str]) -> str:
    return "|".join(tokens)
""",
        encoding="utf-8",
    )
    (workspace / "fixtures" / "legacy_tokens.json").write_text(
        '["Legacy", "VALUE"]\n',
        encoding="utf-8",
    )
    (workspace / "tests" / "test_pipeline.py").write_text(
        """import json
import unittest

from src.token_compat import adapt_legacy_tokens
from src.token_parser import parse_tokens
from src.token_renderer import render_tokens


class PipelineTest(unittest.TestCase):
    def test_versioned_token_pipeline(self):
        tokens = parse_tokens("Hello WORLD")
        self.assertTrue(all(token["kind"] == "word" for token in tokens))
        self.assertEqual(render_tokens(tokens), "word:hello|word:world")

    def test_legacy_tokens_use_the_same_contract(self):
        with open("fixtures/legacy_tokens.json", encoding="utf-8") as handle:
            legacy = json.load(handle)
        tokens = adapt_legacy_tokens(legacy)
        self.assertEqual(tokens, [
            {"kind": "word", "text": "legacy"},
            {"kind": "word", "text": "value"},
        ])
        self.assertEqual(render_tokens(tokens), "word:legacy|word:value")


if __name__ == "__main__":
    unittest.main()
""",
        encoding="utf-8",
    )
    (workspace / "README.md").write_text(
        "# Phase 4G12 Small\n\nA versioned token pipeline with legacy input.\n",
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
    _git(workspace, "commit", "-m", "phase4g12 small base")
    return _git(workspace, "rev-parse", "HEAD")


def _contract(outcome: str, scope: str, *criteria: str) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "acceptance_criteria": list(criteria),
        "success_evidence": ["changed_files", "verification", "worker_summary"],
        "declared_write_scope": [scope],
        "prohibited_actions": ["production_deployment", "modify_sibling_scope"],
        "workspace_mode": "isolated_worktree",
    }


def _register_lane(config: SmallRunConfig) -> None:
    clear_worker_lanes()
    lane = codex_worker.make_codex_worker_lane(
        {
            "name": config.lane_name,
            "model": config.model,
            "sandbox": "workspace-write",
            "approval": "never",
            "max_concurrency": 3,
            "success_policy": "block_for_review",
            "timeout_seconds": config.worker_timeout_seconds,
            "json_events": True,
        },
        source="phase4g12-small",
    )
    register_worker_lane(lane)


def _create_job(
    conn,
    config: SmallRunConfig,
    workspace: Path,
) -> tuple[str, int, list[str]]:
    root_task_id = kb.create_task(
        conn,
        title="Phase 4G12 dynamic token pipeline",
        initial_status="running",
        workspace_kind="dir",
        workspace_path=str(workspace),
    )
    job_id = rk.create_runtime_job(
        conn,
        root_task_id,
        (
            "Upgrade the token pipeline to versioned token objects and support the legacy "
            "fixture through one compatibility adapter. Start only with parser and renderer "
            "responsibilities. A real worker checkpoint must expose any omitted durable "
            "compatibility responsibility before Runtime may add it. Integrate all frozen "
            "contributions and pass python3 -m unittest discover -s tests."
        ),
        workspace_path=str(workspace),
        goal_items=[{
            "item_key": "versioned-token-pipeline",
            "description": "current and legacy tokens pass the integrated pipeline tests",
            "required": True,
            "verifier_required": False,
            "acceptance_criteria": [
                "parser emits versioned token objects",
                "renderer consumes the shared token contract",
                "legacy records use the same contract",
                "pipeline tests pass",
            ],
        }],
        initial_assignee=config.lane_name,
        initialization_mode="fixture",
        orchestration_policy={
            "schema": rk.RUNTIME_ORCHESTRATION_POLICY_SCHEMA,
            "mode": "closed_loop_coordination",
            "worker_lane": config.lane_name,
            "max_child_nodes": 3,
            "artifact_root": str(config.root / "runtime-artifacts"),
            "retention": "retain",
        },
    )
    primary = conn.execute(
        "SELECT * FROM execution_nodes WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    primary_constraints = json.loads(primary["constraints_json"])
    primary_constraints["contract"] = {
        "outcome": "Integrate every evidence-backed child contribution and verify the pipeline.",
        "acceptance_criteria": [
            "all required contributions integrated",
            "current and legacy pipeline tests pass",
        ],
        "success_evidence": ["changed_files", "verification", "worker_summary"],
        "declared_write_scope": ["**"],
        "prohibited_actions": ["production_deployment"],
        "workspace_mode": "shared_job_workspace",
    }
    conn.execute(
        """
        UPDATE execution_nodes
           SET node_key = 'pipeline-integration', title = 'Integrate token pipeline',
               description = 'Apply every frozen child contribution and run the full suite.',
               state = 'waiting_structure', constraints_json = ?
         WHERE id = ?
        """,
        (json.dumps(primary_constraints), primary["id"]),
    )
    structure_event_id = rk._event(
        conn,
        job_id,
        "worker_structure_checkpointed",
        {
            "node_key": "pipeline-integration",
            "checkpoint": {
                "schema": rk.STRUCTURE_CHECKPOINT_SCHEMA,
                "kind": "early_structure_assessment",
                "recommendation": "expand",
                "summary": (
                    "The initial fixture exposes parser and renderer only; later durable "
                    "responsibilities must come from runtime evidence."
                ),
            },
        },
        node_id=primary["id"],
        source="phase4g12_small_fixture",
    )
    child_ops = [
        {
            "op": "create_node",
            "node_key": "parser-contract",
            "node_type": "implementation",
            "title": "Implement parser token contract",
            "description": (
                "Implement the versioned parser in src/token_parser.py. During the first "
                "bounded slice inspect tests/test_pipeline.py and repository evidence. The "
                "test imports an omitted compatibility module outside your write scope. "
                "Report a gap_discovered finding and one advisory responsibility candidate "
                "with candidate_key=legacy-token-adapter, reason_type="
                "execution_discovered_gap, scope src/token_compat.py, goal item "
                "versioned-token-pipeline, integration owner pipeline-integration, and "
                "evidence workspace:path:tests/test_pipeline.py. Do not create that module."
            ),
            "assignee": config.lane_name,
            "goal_item_keys": ["versioned-token-pipeline"],
            "requested_capabilities": list(rk.RUNTIME_ORCHESTRATION_CHILD_CAPABILITIES),
            "contract": _contract(
                "Parser emits lowercase {'kind': 'word', 'text': value} objects.",
                "src/token_parser.py",
                "parser contract is explicit",
                "parser-focused checks pass",
            ),
        },
        {
            "op": "create_node",
            "node_key": "renderer-contract",
            "node_type": "implementation",
            "title": "Implement renderer token consumer",
            "description": (
                "Inspect src/token_renderer.py and tests/test_pipeline.py in the first bounded "
                "slice. Do not modify parser or compatibility files. Report a "
                "blocking_dependency safe point affecting renderer-contract while Runtime "
                "decides how the parser contract and omitted adapter responsibility proceed. "
                "The parser-contract node is the sole candidate source in this controlled "
                "validation, so set responsibility_candidates=[] even if you observe the same "
                "gap. After a Runtime directive, implement the renderer token consumer."
            ),
            "assignee": config.lane_name,
            "goal_item_keys": ["versioned-token-pipeline"],
            "requested_capabilities": list(rk.RUNTIME_ORCHESTRATION_CHILD_CAPABILITIES),
            "contract": _contract(
                "Renderer consumes versioned token objects.",
                "src/token_renderer.py",
                "first slice reaches a coordination safe point",
                "renderer-focused checks pass after directive",
            ),
        },
    ]
    patch = {
        "schema": rk.PATCH_SCHEMA,
        "expected_revision": int(
            conn.execute(
                "SELECT graph_revision FROM runtime_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()[0]
        ),
        "rationale_summary": "Seed only the parser and renderer responsibilities.",
        "ops": [
            *child_ops,
            *[
                {
                    "op": "add_dependency",
                    "from_node_key": child["node_key"],
                    "to_node_key": "pipeline-integration",
                }
                for child in child_ops
            ],
        ],
        "decomposition": {
            "policy_version": "1",
            "mode": "multiple_runtime_nodes",
            "justifications": [{
                "type": "durable_parallelism",
                "nodes": ["parser-contract", "renderer-contract"],
                "explanation": "Parser and renderer have disjoint initial write scopes.",
                "evidence_refs": [f"event:{structure_event_id}"],
                "declared_write_scopes": {
                    "parser-contract": ["src/token_parser.py"],
                    "renderer-contract": ["src/token_renderer.py"],
                },
                "integration_owner_node_key": "pipeline-integration",
            }],
        },
    }
    result = rk.apply_graph_patch(conn, job_id, patch)
    if result["status"] != "applied":
        raise RuntimeError(f"initial topology rejected: {result}")
    initial_node_keys = [
        str(row["node_key"])
        for row in conn.execute(
            "SELECT node_key FROM execution_nodes WHERE job_id = ? ORDER BY node_key",
            (job_id,),
        ).fetchall()
    ]
    if "legacy-token-adapter" in initial_node_keys:
        raise RuntimeError("dynamic responsibility was incorrectly seeded")
    return job_id, structure_event_id, initial_node_keys


def _route_coordination(
    conn,
    config: SmallRunConfig,
    job_id: str,
) -> list[dict[str, Any]]:
    provider = phase4g11._real_decision_provider(config)
    results: list[dict[str, Any]] = []
    for _ in range(config.decision_limit):
        waiting = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM execution_nodes
                 WHERE job_id = ? AND state = 'waiting_coordination'
                """,
                (job_id,),
            ).fetchone()[0]
        )
        if waiting == 0:
            break
        result = rk.advance_runtime_job(
            conn,
            job_id,
            create_tasks=False,
            decision_provider=provider,
            max_patches=1,
            auto_compact=False,
        )
        results.append(asdict(result))
    waiting_keys = [
        str(row["node_key"])
        for row in conn.execute(
            """
            SELECT node_key FROM execution_nodes
             WHERE job_id = ? AND state = 'waiting_coordination'
             ORDER BY node_key
            """,
            (job_id,),
        ).fetchall()
    ]
    if waiting_keys:
        raise RuntimeError(
            "real Decision Provider did not resolve coordination epoch: "
            + ", ".join(waiting_keys)
        )
    return results


def _run_execution_cycles(
    conn,
    config: SmallRunConfig,
    job_id: str,
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    task_ids: list[str] = []
    advances: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    for _ in range(config.execution_cycle_limit):
        child_states = {
            str(row["node_key"]): str(row["state"])
            for row in conn.execute(
                """
                SELECT node_key, state FROM execution_nodes
                 WHERE job_id = ? AND node_key != 'pipeline-integration'
                """,
                (job_id,),
            ).fetchall()
        }
        if child_states and set(child_states.values()) == {"succeeded"}:
            break
        dispatched = phase4g11._dispatch_ready(
            conn,
            job_id,
            config.worker_timeout_seconds,
        )
        task_ids.extend(dispatched)
        advanced = rk.advance_runtime_job(
            conn,
            job_id,
            create_tasks=False,
            auto_compact=False,
        )
        advances.append(asdict(advanced))
        waiting = conn.execute(
            """
            SELECT 1 FROM execution_nodes
             WHERE job_id = ? AND state = 'waiting_coordination' LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        if waiting is not None:
            decisions.extend(_route_coordination(conn, config, job_id))
        elif not dispatched:
            break
    child_states = {
        str(row["node_key"]): str(row["state"])
        for row in conn.execute(
            """
            SELECT node_key, state FROM execution_nodes
             WHERE job_id = ? AND node_key != 'pipeline-integration'
            """,
            (job_id,),
        ).fetchall()
    }
    if set(child_states) != {
        "parser-contract",
        "renderer-contract",
        "legacy-token-adapter",
    } or set(child_states.values()) != {"succeeded"}:
        raise RuntimeError(f"dynamic children did not all succeed: {child_states}")
    return task_ids, advances, decisions


def _finish_integration(
    conn,
    config: SmallRunConfig,
    job_id: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    task_ids: list[str] = []
    advances: list[dict[str, Any]] = []
    for _ in range(3):
        state = conn.execute(
            """
            SELECT state FROM execution_nodes
             WHERE job_id = ? AND node_key = 'pipeline-integration'
            """,
            (job_id,),
        ).fetchone()
        if state is not None and state["state"] == "succeeded":
            break
        dispatched = phase4g11._dispatch_ready(
            conn,
            job_id,
            config.worker_timeout_seconds,
        )
        if not dispatched:
            break
        task_ids.extend(dispatched)
        advances.append(
            asdict(
                rk.advance_runtime_job(
                    conn,
                    job_id,
                    create_tasks=False,
                    auto_compact=False,
                )
            )
        )
    state = conn.execute(
        """
        SELECT state FROM execution_nodes
         WHERE job_id = ? AND node_key = 'pipeline-integration'
        """,
        (job_id,),
    ).fetchone()
    if state is None or state["state"] != "succeeded":
        raise RuntimeError(
            "integration responsibility did not succeed: "
            + (str(state["state"]) if state is not None else "missing")
        )
    return task_ids, advances


def _build_report(
    conn,
    config: SmallRunConfig,
    *,
    workspace: Path,
    job_id: str,
    base_revision: str,
    structure_event_id: int,
    initial_node_keys: list[str],
    first_task_ids: list[str],
    child_task_ids: list[str],
    integration_task_ids: list[str],
    advances: list[dict[str, Any]],
    decision_results: list[dict[str, Any]],
) -> dict[str, Any]:
    rk.sync_runtime_backend_sessions(conn, job_id)
    status = rk.status_runtime_job(conn, job_id)
    orchestration = status["orchestration"]
    consistency = rk.check_runtime_consistency(conn, job_id, write_events=False)
    tests = subprocess.run(
        [os.sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=workspace,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    patches = [
        {
            "id": str(row["id"]),
            "status": str(row["status"]),
            "base_revision": int(row["base_revision"]),
            "applied_revision": row["applied_revision"],
            "patch": json.loads(row["patch_json"]),
        }
        for row in conn.execute(
            """
            SELECT * FROM graph_patches WHERE job_id = ?
             ORDER BY created_at, id
            """,
            (job_id,),
        ).fetchall()
    ]
    dynamic_patches = [
        patch
        for patch in patches
        if any(
            op.get("op") == "create_node"
            and op.get("source_responsibility_ref")
            for op in patch["patch"].get("ops") or []
        )
    ]
    checkpoint_events = [
        {
            "event_id": int(row["id"]),
            "node_key": str(row["node_key"]),
            "checkpoint": json.loads(row["payload_json"]).get("checkpoint"),
        }
        for row in conn.execute(
            """
            SELECT event.id, event.payload_json, node.node_key
              FROM execution_events event
              JOIN execution_nodes node ON node.id = event.node_id
             WHERE event.job_id = ?
               AND event.event_type = 'worker_coordination_checkpointed'
             ORDER BY event.id
            """,
            (job_id,),
        ).fetchall()
    ]
    report = {
        "schema": "phase4g12_small_report_v1",
        "job_id": job_id,
        "workspace": str(workspace),
        "base_revision": base_revision,
        "structure_event_id": structure_event_id,
        "initial_node_keys": initial_node_keys,
        "final_node_keys": [str(node["node_key"]) for node in status["nodes"]],
        "first_task_ids": first_task_ids,
        "child_task_ids": child_task_ids,
        "integration_task_ids": integration_task_ids,
        "advance_results": advances,
        "decision_results": decision_results,
        "decision_history": [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, status, model, error, created_at, completed_at
                  FROM kernel_decisions WHERE job_id = ?
                 ORDER BY created_at, id
                """,
                (job_id,),
            ).fetchall()
        ],
        "graph_patches": patches,
        "dynamic_graph_patches": dynamic_patches,
        "coordination_checkpoints": checkpoint_events,
        "final_state": status["job"]["state"],
        "goal_items": [
            {"item_key": item["item_key"], "state": item["state"]}
            for item in status["goal_items"]
        ],
        "nodes": [
            {
                "node_key": node["node_key"],
                "state": node["state"],
                "contract_revision": node.get("contract_revision"),
            }
            for node in status["nodes"]
        ],
        "coordination": orchestration["coordination"],
        "children": orchestration["children"],
        "contributions": orchestration["contributions"],
        "worker_sessions": orchestration["worker_sessions"],
        "consistency": {
            "status": consistency["status"],
            "violation_count": consistency["violation_count"],
            "warning_count": consistency["warning_count"],
            "violations": consistency["violations"],
            "warnings": consistency["warnings"],
        },
        "workspace_tests": {
            "returncode": tests.returncode,
            "output": tests.stdout[-4000:],
        },
    }
    (config.root.resolve() / "run-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def _archive_run_evidence(
    config: SmallRunConfig,
    report: dict[str, Any],
) -> dict[str, Any]:
    source_root = config.root.resolve().parent
    source_codex_home = Path(os.environ.get("CODEX_HOME") or "").expanduser()
    expected_codex_home = source_root / "codex-home"
    if (
        not source_codex_home.is_dir()
        or source_codex_home.resolve() != expected_codex_home.resolve()
    ):
        raise RuntimeError(
            "Phase 4G12 real validation requires CODEX_HOME at "
            f"{expected_codex_home} so raw sessions can be archived"
        )
    reports = source_root / "reports"
    reports.mkdir(exist_ok=True)
    shutil.copy2(config.root / "run-report.json", reports / "run-report.json")

    contribution_source = (
        config.root
        / "runtime-artifacts"
        / str(report["job_id"])
        / "contributions"
    )
    contribution_target = source_root / "runtime-contributions"
    if contribution_target.exists():
        raise RuntimeError(
            "refusing to replace existing runtime-contributions evidence"
        )
    shutil.copytree(contribution_source, contribution_target)

    manifest = validation_artifacts.archive_validation_run(
        source_root,
        artifact_root=config.artifact_root,
        phase="phase4g12",
        instance_id="dynamic-small",
        redactions=validation_artifacts.model_source_redactions(
            source_codex_home
        ),
        expected_entries=(
            "codex-home",
            "hermes-home",
            "reports",
            "runtime-contributions",
        ),
    )
    return {
        "artifact_path": manifest["artifact_path"],
        "manifest_path": str(Path(manifest["artifact_path"]) / "manifest.json"),
        "status": manifest["status"],
        "file_count": manifest["file_count"],
        "total_bytes": manifest["total_bytes"],
    }


def run_phase4g12_small(config: SmallRunConfig) -> dict[str, Any]:
    root = config.root.resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError(f"small run root must be empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    workspace = root / "workspace"
    workspace.mkdir()
    base_revision = _write_small_repository(workspace)
    _register_lane(config)
    kb.init_db()
    with kb.connect() as conn:
        job_id, structure_event_id, initial_node_keys = _create_job(
            conn,
            config,
            workspace,
        )
        first_task_ids = phase4g11._dispatch_ready(
            conn,
            job_id,
            config.worker_timeout_seconds,
        )
        first_advance = rk.advance_runtime_job(
            conn,
            job_id,
            create_tasks=False,
            auto_compact=False,
        )
        if set(first_advance.ingested_nodes) != {
            "parser-contract",
            "renderer-contract",
        }:
            raise RuntimeError(
                "initial coordination checkpoints were not ingested: "
                + json.dumps(asdict(first_advance), ensure_ascii=False)
            )
        revision_before_dynamic = int(
            conn.execute(
                "SELECT graph_revision FROM runtime_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()[0]
        )
        first_decisions = _route_coordination(conn, config, job_id)
        dynamic = conn.execute(
            """
            SELECT * FROM execution_nodes
             WHERE job_id = ? AND node_key = 'legacy-token-adapter'
            """,
            (job_id,),
        ).fetchone()
        if dynamic is None:
            raise RuntimeError(
                "real Decision Provider did not create the evidence-backed adapter node"
            )
        revision_after_dynamic = int(
            conn.execute(
                "SELECT graph_revision FROM runtime_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()[0]
        )
        if revision_after_dynamic <= revision_before_dynamic:
            raise RuntimeError("dynamic graph expansion did not change graph revision")
        child_task_ids, child_advances, later_decisions = _run_execution_cycles(
            conn,
            config,
            job_id,
        )
        integration_task_ids, integration_advances = _finish_integration(
            conn,
            config,
            job_id,
        )
        report = _build_report(
            conn,
            config,
            workspace=workspace,
            job_id=job_id,
            base_revision=base_revision,
            structure_event_id=structure_event_id,
            initial_node_keys=initial_node_keys,
            first_task_ids=first_task_ids,
            child_task_ids=child_task_ids,
            integration_task_ids=integration_task_ids,
            advances=[
                asdict(first_advance),
                *child_advances,
                *integration_advances,
            ],
            decision_results=[*first_decisions, *later_decisions],
        )
        report["graph_revision_before_dynamic"] = revision_before_dynamic
        report["graph_revision_after_dynamic"] = revision_after_dynamic
        (root / "run-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    report["artifact_archive"] = _archive_run_evidence(config, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="运行 Phase 4G12 真实动态拓扑 Small 验证",
    )
    parser.add_argument("--root", required=True)
    parser.add_argument("--lane", default="phase4g12-codex")
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--worker-timeout", type=int, default=900)
    parser.add_argument("--decision-timeout", type=float, default=180.0)
    parser.add_argument(
        "--artifact-root",
        default=str(validation_artifacts.default_artifact_root()),
    )
    args = parser.parse_args(argv)
    report = run_phase4g12_small(
        SmallRunConfig(
            root=Path(args.root),
            lane_name=args.lane,
            model=args.model,
            worker_timeout_seconds=args.worker_timeout,
            decision_timeout_seconds=args.decision_timeout,
            artifact_root=Path(args.artifact_root),
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if (
        report["final_state"] == "done"
        and report["consistency"]["status"] == "passed"
        and report["workspace_tests"]["returncode"] == 0
        and report["coordination"]["dynamic_node_count"] >= 1
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
