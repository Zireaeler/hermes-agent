"""Phase 4G11 closed-loop coordination small validation.

The runner uses real Codex workers and a real no-tools Decision Provider on a
small repository. It seeds only the initial durable topology; all checkpoint,
directive, resume, acknowledgment, contribution, and completion facts travel
through the production Runtime and Kanban paths.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace
from typing import Any

from hermes_cli import codex_worker
from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_decision as rd
from hermes_cli import kanban_runtime_kernel as rk
from hermes_cli.worker_lanes import (
    clear_worker_lanes,
    register_worker_lane,
)


@dataclass(frozen=True)
class SmallRunConfig:
    root: Path
    lane_name: str = "phase4g11-codex"
    model: str = "gpt-5.6-sol"
    worker_timeout_seconds: int = 900
    decision_timeout_seconds: float = 180.0
    decision_limit: int = 5
    child_dispatch_limit: int = 3
    integration_dispatch_limit: int = 3


def _git(workspace: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=workspace,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return completed.stdout.strip()


def _write_small_repository(workspace: Path) -> str:
    (workspace / "src").mkdir(parents=True)
    (workspace / "tests").mkdir()
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
    (workspace / "tests" / "test_pipeline.py").write_text(
        """import unittest

from src.token_parser import parse_tokens
from src.token_renderer import render_tokens


class PipelineTest(unittest.TestCase):
    def test_versioned_token_pipeline(self):
        tokens = parse_tokens("Hello WORLD")
        self.assertTrue(all(token["kind"] == "word" for token in tokens))
        self.assertEqual(render_tokens(tokens), "word:hello|word:world")


if __name__ == "__main__":
    unittest.main()
""",
        encoding="utf-8",
    )
    (workspace / "README.md").write_text(
        "# Phase 4G11 Small\n\nA two-module token pipeline.\n",
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
    _git(workspace, "commit", "-m", "small base")
    return _git(workspace, "rev-parse", "HEAD")


def _contract(
    outcome: str,
    scope: str,
    *criteria: str,
) -> dict[str, Any]:
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
            "max_concurrency": 2,
            "success_policy": "block_for_review",
            "timeout_seconds": config.worker_timeout_seconds,
            "json_events": True,
        },
        source="phase4g11-small",
    )
    register_worker_lane(lane)


def _create_job(
    conn,
    config: SmallRunConfig,
    workspace: Path,
) -> tuple[str, int]:
    root_task_id = kb.create_task(
        conn,
        title="Phase 4G11 small token pipeline",
        initial_status="running",
        workspace_kind="dir",
        workspace_path=str(workspace),
    )
    job_id = rk.create_runtime_job(
        conn,
        root_task_id,
        (
            "Upgrade the small token pipeline to a versioned token object. The parser "
            "must emit lowercase {'kind': 'word', 'text': value} objects. The renderer "
            "must consume the parser contract and render word:<text> entries joined by |. "
            "Use Runtime coordination before the renderer commits to the shared contract, "
            "then integrate both contributions and pass python3 -m unittest discover -s tests."
        ),
        workspace_path=str(workspace),
        goal_items=[{
            "item_key": "versioned-token-pipeline",
            "description": "the integrated versioned token pipeline passes its tests",
            "required": True,
            "verifier_required": False,
            "acceptance_criteria": [
                "parser emits versioned token objects",
                "renderer consumes the shared token contract",
                "pipeline tests pass",
            ],
        }],
        initial_assignee=config.lane_name,
        initialization_mode="fixture",
        orchestration_policy={
            "schema": rk.RUNTIME_ORCHESTRATION_POLICY_SCHEMA,
            "mode": "closed_loop_coordination",
            "worker_lane": config.lane_name,
            "max_child_nodes": 2,
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
        "outcome": "Integrate parser and renderer contributions and verify the pipeline.",
        "acceptance_criteria": ["all contributions integrated", "unittest passes"],
        "success_evidence": ["changed_files", "verification", "worker_summary"],
        "declared_write_scope": ["**"],
        "prohibited_actions": ["production_deployment"],
        "workspace_mode": "shared_job_workspace",
    }
    conn.execute(
        """
        UPDATE execution_nodes
           SET node_key = 'pipeline-integration', title = 'Integrate token pipeline',
               description = 'Apply child contributions, resolve shared seams, and run tests.',
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
                "summary": "The validation topology is intentionally fixed at two isolated domains.",
            },
        },
        node_id=primary["id"],
        source="phase4g11_small_fixture",
    )
    child_ops = [
        {
            "op": "create_node",
            "node_key": "parser-contract",
            "node_type": "implementation",
            "title": "Implement parser token contract",
            "description": (
                "Implement the parser-owned versioned token representation in "
                "src/token_parser.py. In the first bounded slice, establish the exact "
                "shared representation and report it as shared_contract_changed affecting "
                "renderer-contract and pipeline-integration. Do not modify renderer files."
            ),
            "assignee": config.lane_name,
            "goal_item_keys": ["versioned-token-pipeline"],
            "requested_capabilities": [
                "filesystem_read",
                "workspace_write",
                "git_read",
                "process_spawn",
            ],
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
                "Inspect src/token_renderer.py but do not guess or implement the final token "
                "shape in the first slice. Report a blocking_dependency safe point affecting "
                "renderer-contract. After Runtime supplies the parser checkpoint directive, "
                "implement the renderer without modifying parser files."
            ),
            "assignee": config.lane_name,
            "goal_item_keys": ["versioned-token-pipeline"],
            "requested_capabilities": [
                "filesystem_read",
                "workspace_write",
                "git_read",
                "process_spawn",
            ],
            "contract": _contract(
                "Renderer waits for and then consumes the parser-selected token contract.",
                "src/token_renderer.py",
                "first slice does not guess the token shape",
                "renderer-focused checks pass after directive",
            ),
        },
    ]
    dependency_ops = [
        {
            "op": "add_dependency",
            "from_node_key": child["node_key"],
            "to_node_key": "pipeline-integration",
        }
        for child in child_ops
    ]
    patch = {
        "schema": rk.PATCH_SCHEMA,
        "expected_revision": int(
            conn.execute(
                "SELECT graph_revision FROM runtime_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()[0]
        ),
        "rationale_summary": "Seed the two-domain small validation topology.",
        "ops": [*child_ops, *dependency_ops],
        "decomposition": {
            "policy_version": "1",
            "mode": "multiple_runtime_nodes",
            "justifications": [{
                "type": "durable_parallelism",
                "nodes": ["parser-contract", "renderer-contract"],
                "explanation": "Parser and renderer have disjoint files and one explicit shared contract.",
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
        raise RuntimeError(f"small topology rejected: {result}")
    return job_id, structure_event_id


def _wait_tasks(conn, task_ids: list[str], timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        tasks = [kb.get_task(conn, task_id) for task_id in task_ids]
        if tasks and all(task and task.status in {"done", "blocked"} for task in tasks):
            return
        time.sleep(0.5)
    tasks = [kb.get_task(conn, task_id) for task_id in task_ids]
    if tasks and all(task and task.status in {"done", "blocked"} for task in tasks):
        return
    raise TimeoutError(f"worker tasks did not finish: {task_ids}")


def _dispatch_ready(conn, job_id: str, timeout_seconds: float) -> list[str]:
    nodes = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM execution_nodes WHERE job_id = ? AND state = 'ready' ORDER BY node_key",
            (job_id,),
        ).fetchall()
    ]
    task_ids = [
        task_id
        for node in nodes
        if (task_id := rk.materialize_runtime_node(conn, node)) is not None
    ]
    if not task_ids:
        return []
    dispatch = kb.dispatch_once(
        conn,
        max_spawn=len(task_ids),
        only_task_ids=task_ids,
    )
    spawned = [item[0] for item in dispatch.spawned]
    if set(spawned) != set(task_ids):
        raise RuntimeError(
            f"not all Runtime tasks dispatched: expected={task_ids} spawned={spawned}"
        )
    _wait_tasks(conn, task_ids, timeout_seconds)
    return task_ids


def _real_decision_provider(config: SmallRunConfig) -> rd.RuntimeDecisionProvider:
    source = kanban_cli._runtime_model_source_from_codex_config(
        SimpleNamespace(model=config.model)
    )
    return rd.RuntimeDecisionProvider(
        provider_name=source["provider_name"],
        model=source["model"],
        profile_name="graph_patch_decision",
        max_retries=1,
        timeout_seconds=config.decision_timeout_seconds,
        explicit_base_url=source["explicit_base_url"],
        explicit_api_key=source["explicit_api_key"],
    )


def _route_coordination(
    conn,
    config: SmallRunConfig,
    job_id: str,
) -> list[dict[str, Any]]:
    provider = _real_decision_provider(config)
    decision_results: list[dict[str, Any]] = []
    for _ in range(config.decision_limit):
        waiting = conn.execute(
            """
            SELECT COUNT(*) FROM execution_nodes
             WHERE job_id = ? AND state = 'waiting_coordination'
            """,
            (job_id,),
        ).fetchone()[0]
        if int(waiting) == 0:
            break
        result = rk.advance_runtime_job(
            conn,
            job_id,
            create_tasks=False,
            decision_provider=provider,
            max_patches=1,
            auto_compact=False,
        )
        decision_results.append(asdict(result))

    waiting = conn.execute(
        """
        SELECT node_key FROM execution_nodes
         WHERE job_id = ? AND state = 'waiting_coordination'
        """,
        (job_id,),
    ).fetchall()
    if waiting:
        raise RuntimeError(
            "real Decision Provider did not release coordination safe points: "
            + ", ".join(row["node_key"] for row in waiting)
        )
    return decision_results


def _finish_phase4g11_small(
    conn,
    config: SmallRunConfig,
    *,
    workspace: Path,
    job_id: str,
    base_revision: str,
    structure_event_id: int,
    first_task_ids: list[str],
    first_advance: dict[str, Any],
    decision_results: list[dict[str, Any]],
    resumed_existing: bool,
) -> dict[str, Any]:
    second_task_ids: list[str] = []
    child_advances: list[dict[str, Any]] = []
    child_states: dict[str, str] = {}
    for _ in range(config.child_dispatch_limit):
        child_states = {
            row["node_key"]: row["state"]
            for row in conn.execute(
                """
                SELECT node_key, state FROM execution_nodes
                 WHERE job_id = ?
                   AND node_key IN ('parser-contract', 'renderer-contract')
                """,
                (job_id,),
            ).fetchall()
        }
        if set(child_states.values()) == {"succeeded"}:
            break
        task_ids = _dispatch_ready(
            conn,
            job_id,
            config.worker_timeout_seconds,
        )
        if not task_ids:
            break
        second_task_ids.extend(task_ids)
        child_advance = rk.advance_runtime_job(
            conn,
            job_id,
            create_tasks=False,
            auto_compact=False,
        )
        child_advances.append(asdict(child_advance))

    child_states = {
        row["node_key"]: row["state"]
        for row in conn.execute(
            """
            SELECT node_key, state FROM execution_nodes
             WHERE job_id = ? AND node_key IN ('parser-contract', 'renderer-contract')
            """,
            (job_id,),
        ).fetchall()
    }
    if set(child_states.values()) != {"succeeded"}:
        raise RuntimeError(f"child responsibilities did not succeed: {child_states}")

    integration_task_ids: list[str] = []
    final_advances: list[dict[str, Any]] = []
    for _ in range(config.integration_dispatch_limit):
        integration = conn.execute(
            """
            SELECT state FROM execution_nodes
             WHERE job_id = ? AND node_key = 'pipeline-integration'
            """,
            (job_id,),
        ).fetchone()
        if integration is not None and integration["state"] == "succeeded":
            break
        task_ids = _dispatch_ready(
            conn,
            job_id,
            config.worker_timeout_seconds,
        )
        if not task_ids:
            break
        integration_task_ids.extend(task_ids)
        final_advance = rk.advance_runtime_job(
            conn,
            job_id,
            create_tasks=False,
            auto_compact=False,
        )
        final_advances.append(asdict(final_advance))

    integration = conn.execute(
        """
        SELECT state FROM execution_nodes
         WHERE job_id = ? AND node_key = 'pipeline-integration'
        """,
        (job_id,),
    ).fetchone()
    if integration is None or integration["state"] != "succeeded":
        raise RuntimeError(
            "integration responsibility did not succeed: "
            + (str(integration["state"]) if integration is not None else "missing")
        )
    if not final_advances:
        final_advances.append(
            asdict(
                rk.advance_runtime_job(
                    conn,
                    job_id,
                    create_tasks=False,
                    auto_compact=False,
                )
            )
        )

    rk.sync_runtime_backend_sessions(conn, job_id)
    status = rk.status_runtime_job(conn, job_id)
    orchestration = status["orchestration"]
    consistency = rk.check_runtime_consistency(
        conn,
        job_id,
        write_events=False,
    )
    tests = subprocess.run(
        [os.sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=workspace,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    decision_history = [
        dict(row)
        for row in conn.execute(
            """
            SELECT id, status, model, error, created_at, completed_at
              FROM kernel_decisions WHERE job_id = ? ORDER BY created_at, id
            """,
            (job_id,),
        ).fetchall()
    ]
    report = {
        "schema": "phase4g11_small_report_v1",
        "job_id": job_id,
        "workspace": str(workspace),
        "base_revision": base_revision,
        "structure_event_id": structure_event_id,
        "resumed_existing": resumed_existing,
        "first_task_ids": first_task_ids,
        "second_task_ids": second_task_ids,
        "integration_task_ids": integration_task_ids,
        "decision_results": decision_results,
        "decision_history": decision_history,
        "first_advance": first_advance,
        "second_advance": (
            child_advances[-1]
            if child_advances
            else {"resumed_from_db": resumed_existing}
        ),
        "child_advances": child_advances,
        "final_advance": final_advances[-1],
        "final_advances": final_advances,
        "final_state": status["job"]["state"],
        "goal_items": [
            {"item_key": item["item_key"], "state": item["state"]}
            for item in status["goal_items"]
        ],
        "coordination": orchestration["coordination"],
        "children": orchestration["children"],
        "contributions": orchestration["contributions"],
        "worker_sessions": orchestration["worker_sessions"],
        "consistency": {
            "status": consistency["status"],
            "violation_count": consistency["violation_count"],
            "warning_count": consistency["warning_count"],
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


def run_phase4g11_small(config: SmallRunConfig) -> dict[str, Any]:
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
        job_id, structure_event_id = _create_job(conn, config, workspace)
        first_task_ids = _dispatch_ready(
            conn,
            job_id,
            config.worker_timeout_seconds,
        )
        first_advance_result = rk.advance_runtime_job(
            conn,
            job_id,
            create_tasks=False,
            auto_compact=False,
        )
        first_advance = asdict(first_advance_result)
        if set(first_advance_result.ingested_nodes) != {
            "parser-contract",
            "renderer-contract",
        }:
            raise RuntimeError(
                f"coordination checkpoints were not ingested: {first_advance}"
            )
        decision_results = _route_coordination(conn, config, job_id)
        return _finish_phase4g11_small(
            conn,
            config,
            workspace=workspace,
            job_id=job_id,
            base_revision=base_revision,
            structure_event_id=structure_event_id,
            first_task_ids=first_task_ids,
            first_advance=first_advance,
            decision_results=decision_results,
            resumed_existing=False,
        )


def resume_phase4g11_small(config: SmallRunConfig) -> dict[str, Any]:
    root = config.root.resolve()
    workspace = root / "workspace"
    if not workspace.is_dir():
        raise ValueError(f"small run workspace does not exist: {workspace}")
    _register_lane(config)
    kb.init_db()
    with kb.connect() as conn:
        jobs = conn.execute("SELECT * FROM runtime_jobs ORDER BY created_at").fetchall()
        if len(jobs) != 1:
            raise ValueError(
                f"small run resume requires exactly one Runtime job, found {len(jobs)}"
            )
        job_id = str(jobs[0]["id"])
        structure = conn.execute(
            """
            SELECT id FROM execution_events
             WHERE job_id = ? AND event_type = 'worker_structure_checkpointed'
             ORDER BY id LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        if structure is None:
            raise ValueError("small run resume is missing its structure checkpoint")
        first_task_ids = [
            str(row["task_id"])
            for row in conn.execute(
                """
                SELECT m.task_id FROM node_materializations m
                  JOIN execution_nodes n ON n.id = m.node_id
                 WHERE m.job_id = ? AND m.attempt = 1
                   AND n.node_key IN ('parser-contract', 'renderer-contract')
                 ORDER BY m.created_at
                """,
                (job_id,),
            ).fetchall()
        ]
        decision_results = _route_coordination(conn, config, job_id)
        base_revision = _git(
            workspace,
            "rev-list",
            "--max-parents=0",
            "HEAD",
        ).splitlines()[0]
        return _finish_phase4g11_small(
            conn,
            config,
            workspace=workspace,
            job_id=job_id,
            base_revision=base_revision,
            structure_event_id=int(structure["id"]),
            first_task_ids=first_task_ids,
            first_advance={
                "resumed_from_db": True,
                "job_state": str(jobs[0]["state"]),
            },
            decision_results=decision_results,
            resumed_existing=True,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="运行 Phase 4G11 真实 closed-loop small 验证",
    )
    parser.add_argument("--root", required=True)
    parser.add_argument("--lane", default="phase4g11-codex")
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--worker-timeout", type=int, default=900)
    parser.add_argument("--decision-timeout", type=float, default=180.0)
    parser.add_argument("--resume-existing", action="store_true")
    args = parser.parse_args(argv)
    config = SmallRunConfig(
        root=Path(args.root),
        lane_name=args.lane,
        model=args.model,
        worker_timeout_seconds=args.worker_timeout,
        decision_timeout_seconds=args.decision_timeout,
    )
    report = (
        resume_phase4g11_small(config)
        if args.resume_existing
        else run_phase4g11_small(config)
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if (
        report["final_state"] == "done"
        and report["consistency"]["status"] == "passed"
        and report["workspace_tests"]["returncode"] == 0
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
