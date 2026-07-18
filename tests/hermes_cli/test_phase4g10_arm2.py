from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_kernel as rk
from hermes_cli import phase4g10_arm2 as arm2


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True)


def _arm2_run_root(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    run_root = tmp_path / "run"
    hermes_home = run_root / "hermes-home"
    hermes_home.mkdir(parents=True)
    db_path = hermes_home / "kanban.db"
    kb.init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rk.ensure_runtime_schema(conn)
    now = 100
    job_id = "rjob_arm2"

    task_ids: dict[str, str] = {}
    for task_key, workspace, workspace_kind in (
        ("task_primary", run_root / "worker", "dir"),
        ("task_child_a", run_root / "runtime-worktrees" / "child-a", "worktree"),
        ("task_child_b", run_root / "runtime-worktrees" / "child-b", "worktree"),
    ):
        task_ids[task_key] = kb.create_task(
            conn,
            title=task_key,
            initial_status="running",
            workspace_path=str(workspace),
            workspace_kind=workspace_kind,
        )

    conn.execute(
        """
        INSERT INTO runtime_jobs (
            id, state, objective, decision_profile, graph_revision,
            metadata_json, created_at, updated_at
        ) VALUES (?, 'done', 'Arm 2 fixture', 'graph_patch_decision', 2, '{}', ?, ?)
        """,
        (job_id, now, now),
    )
    primary_contract = {
        "contract": {
            "outcome": "Integrate the repository evolution.",
            "declared_write_scope": ["**"],
        }
    }
    child_contracts = {
        "child-a": {
            "contract": {
                "outcome": "Implement subsystem A.",
                "declared_write_scope": ["src/a/**"],
                "workspace_mode": "isolated_worktree",
            }
        },
        "child-b": {
            "contract": {
                "outcome": "Implement subsystem B.",
                "declared_write_scope": ["src/b/**"],
                "workspace_mode": "isolated_worktree",
            }
        },
    }
    node_rows = [
        (
            "node_primary",
            "primary-integration",
            task_ids["task_primary"],
            primary_contract,
            {},
        ),
        (
            "node_child_a",
            "child-a",
            task_ids["task_child_a"],
            child_contracts["child-a"],
            {"non_authoritative_contribution": True},
        ),
        (
            "node_child_b",
            "child-b",
            task_ids["task_child_b"],
            child_contracts["child-b"],
            {"non_authoritative_contribution": True},
        ),
    ]
    for index, (node_id, node_key, task_id, constraints, metadata) in enumerate(node_rows):
        conn.execute(
            """
            INSERT INTO execution_nodes (
                id, job_id, node_key, node_type, state, title, description,
                latest_task_id, assumptions_json, constraints_json, metadata_json,
                created_at, updated_at, completed_at
            ) VALUES (?, ?, ?, 'implementation', 'completed', ?, ?, ?, '{}', ?, ?, ?, ?, ?)
            """,
            (
                node_id,
                job_id,
                node_key,
                node_key,
                node_key,
                task_id,
                _json(constraints),
                _json(metadata),
                now + index,
                now + index,
                now + 20,
            ),
        )

    checkpoint = {
        "checkpoint": {
            "schema": rk.STRUCTURE_CHECKPOINT_SCHEMA,
            "kind": "early_structure_assessment",
            "recommendation": "expand",
            "changed_files": [],
        }
    }
    conn.execute(
        """
        INSERT INTO execution_events (
            job_id, node_id, event_type, payload_json, source, graph_revision, created_at
        ) VALUES (?, 'node_primary', 'worker_structure_checkpointed', ?, 'worker', 1, ?)
        """,
        (job_id, _json(checkpoint), now + 3),
    )
    conn.execute(
        """
        INSERT INTO graph_patches (
            id, job_id, base_revision, applied_revision, patch_json,
            status, created_at, applied_at
        ) VALUES ('patch_initial', ?, 0, 1, ?, 'applied', ?, ?)
        """,
        (
            job_id,
            _json({"ops": [{"op": "create_node", "node_key": "primary-integration"}]}),
            now,
            now,
        ),
    )
    session_rows = [
        ("session_primary", "node_primary", "thread-primary", 1),
        ("session_child_a", "node_child_a", "thread-child-a", 0),
        ("session_child_b", "node_child_b", "thread-child-b", 0),
    ]
    for session_id, node_id, session_key, resume_count in session_rows:
        conn.execute(
            """
            INSERT INTO backend_worker_sessions (
                id, job_id, node_id, backend_kind, backend_session_key, status,
                initial_materialization_id, latest_materialization_id,
                capability_fingerprint, node_contract_fingerprint, checkpoint_json,
                resume_count, created_at, updated_at, metadata_json
            ) VALUES (?, ?, ?, 'codex_cli', ?, 'completed', 'mat-1', 'mat-2',
                      'cap', 'contract', '{}', ?, ?, ?, '{}')
            """,
            (session_id, job_id, node_id, session_key, resume_count, now, now + 20),
        )

    contribution_ids = ["artifact_a", "artifact_b"]
    for index, (artifact_id, node_id, changed_file) in enumerate(
        zip(
            contribution_ids,
            ("node_child_a", "node_child_b"),
            ("src/a/x.py", "src/b/y.py"),
        )
    ):
        metadata = {
            "schema": "runtime_node_contribution_v1",
            "patch_bytes": 128 + index,
            "changed_files": [changed_file],
            "patch_sha256": str(index + 1) * 64,
        }
        conn.execute(
            """
            INSERT INTO node_artifacts (
                id, job_id, node_id, artifact_type, path_or_ref,
                summary, metadata_json, created_at
            ) VALUES (?, ?, ?, 'runtime_node_contribution', ?, 'contribution', ?, ?)
            """,
            (
                artifact_id,
                job_id,
                node_id,
                str(run_root / "runtime-contributions" / f"{artifact_id}.patch"),
                _json(metadata),
                now + 10 + index,
            ),
        )
    attribution = {
        "accepted_contributions": contribution_ids,
        "modified_contributions": [],
        "rejected_contributions": [],
    }
    conn.execute(
        """
        INSERT INTO execution_events (
            job_id, node_id, event_type, payload_json, source, graph_revision, created_at
        ) VALUES (?, 'node_primary', 'contribution_attribution_verified', ?, 'kernel', 2, ?)
        """,
        (job_id, _json(attribution), now + 30),
    )
    conn.commit()
    conn.close()

    for name in (
        "worker",
        "runtime-worktrees",
        "runtime-contributions",
        "codex-homes",
        "service",
        "reports",
    ):
        (run_root / name).mkdir(parents=True, exist_ok=True)

    payload: dict[str, object] = {
        "run_id": "phase4g10-arm2-fixture",
        "job_id": job_id,
        "paths": {"root": str(run_root)},
        "run_report": {
            "instance_id": "iterative-telemetry",
            "runtime_validation": {
                "passed": True,
                "process_boundaries": {"fixed_revision_evaluated": True},
            },
            "capability_validation": {
                "official_resolved": False,
                "fail_to_pass": {"passed": 60, "failed": 8, "total": 68},
                "pass_to_pass": {"passed": 200, "failed": 0, "total": 200},
            },
        },
    }
    return run_root, payload


def test_arm2_report_requires_real_split_contributions_and_keeps_quality_separate(tmp_path):
    run_root, payload = _arm2_run_root(tmp_path)

    report = arm2.build_arm2_orchestration_report(run_root, payload)

    assert report["classification"] == {
        "runtime_correctness": "passed",
        "effective_orchestration": "passed",
        "task_capability": "task-failed",
    }
    assert all(report["orchestration_assertions"].values())
    assert len(report["children"]) == 2
    assert len(report["contributions"]) == 2
    assert report["primary"]["resume_count"] == 1
    assert report["quality"]["reference_only"]["hard_quality_gate"] is None
    summary = arm2.render_arm2_summary(report)
    assert "Runtime correctness：`passed`" in summary
    assert "Effective orchestration：`passed`" in summary
    assert "Task capability：`task-failed`" in summary
    assert "`63/68` 仅为 Native Ultra 参考值" in summary


def test_run_arm2_freezes_real_policy_and_archives_orchestra_evidence(tmp_path, monkeypatch):
    run_root, payload = _arm2_run_root(tmp_path)
    calls: dict[str, object] = {}

    def fake_run(**kwargs):
        calls["run"] = kwargs
        return payload

    def fake_archive(path, **kwargs):
        calls["archive_path"] = path
        calls["archive"] = kwargs
        return {"status": "verified", "artifact_path": "/archive/arm2"}

    monkeypatch.setattr(arm2.p4g8_run, "run_phase4g8_real_case", fake_run)
    monkeypatch.setattr(
        arm2.validation_artifacts,
        "archive_validation_run",
        fake_archive,
    )

    report = arm2.run_arm2(
        qualification_spec_path=tmp_path / "qualified.json",
        run_root=run_root,
        source_codex_home=tmp_path / "source-codex-home",
        artifact_root=tmp_path / "artifacts",
        execute_real=True,
        max_wall_seconds=1234,
        worker_timeout_seconds=567,
    )

    run = calls["run"]
    assert run["case_size"] == "large"
    assert run["execute_real"] is True
    assert run["reasoning_effort_override"] == "max"
    assert run["operator_stop"] is None
    assert run["orchestration_policy"] == {
        "mode": "early_structure_assessment",
        "required": True,
        "require_contribution_attribution": True,
        "minimum_integrated_contributions": 2,
        "max_child_nodes": 3,
    }
    assert calls["archive_path"] == run_root.resolve()
    assert calls["archive"]["phase"] == "phase4g10"
    assert calls["archive"]["expected_entries"] == {
        "reports",
        "hermes-home",
        "codex-homes",
        "service",
        "runtime-contributions",
    }
    assert report["artifact_archive"]["status"] == "verified"
    assert (run_root / "reports" / "arm2-orchestration.json").is_file()
    assert (run_root / "reports" / "execution-summary.md").is_file()


def test_operator_stop_request_round_trip(tmp_path):
    run_root = tmp_path / "run"
    run_root.mkdir()

    request = arm2.request_operator_stop(run_root, reason="evaluated plateau")

    assert request["schema"] == "hermes_evaluated_operator_stop_v1"
    assert request["reason"] == "evaluated plateau"
    assert arm2._load_operator_stop_request(run_root) == request


def test_run_arm2_forwards_existing_operator_stop(tmp_path, monkeypatch):
    run_root, payload = _arm2_run_root(tmp_path)
    request = arm2.request_operator_stop(run_root, reason="same complete failure set")
    calls: dict[str, object] = {}

    def fake_run(**kwargs):
        calls["run"] = kwargs
        return payload

    monkeypatch.setattr(arm2.p4g8_run, "run_phase4g8_real_case", fake_run)
    monkeypatch.setattr(
        arm2.validation_artifacts,
        "archive_validation_run",
        lambda *args, **kwargs: {
            "status": "verified",
            "artifact_path": "/archive/arm2",
        },
    )

    arm2.run_arm2(
        qualification_spec_path=tmp_path / "qualified.json",
        run_root=run_root,
        resume_run=run_root,
        source_codex_home=tmp_path / "source-codex-home",
        artifact_root=tmp_path / "artifacts",
        execute_real=True,
        max_wall_seconds=1234,
        worker_timeout_seconds=567,
    )

    assert calls["run"]["operator_stop"] == request
