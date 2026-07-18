from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_kernel as rk
from hermes_cli import kanban_runtime_phase4g8_run as p4g8_run
from hermes_cli import phase4g10_clean_replay as clean


def _clean_report_fixture(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    run_root = tmp_path / "run"
    hermes_home = run_root / "hermes-home"
    hermes_home.mkdir(parents=True)
    db_path = hermes_home / "kanban.db"
    kb.init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rk.ensure_runtime_schema(conn)
    conn.execute(
        """
        INSERT INTO runtime_jobs (
            id, state, objective, decision_profile, graph_revision,
            metadata_json, created_at, updated_at
        ) VALUES ('job-clean', 'waiting_worker', 'clean', 'graph_patch_decision',
                  2, '{}', 1, 2)
        """
    )
    for index, owner in enumerate(("daemon-a", "daemon-b"), start=1):
        conn.execute(
            """
            INSERT INTO execution_events (
                job_id, event_type, payload_json, source, graph_revision, created_at
            ) VALUES ('job-clean', 'runtime_supervisor_started', ?, 'runtime', 2, ?)
            """,
            (json.dumps({"owner": owner}), index),
        )
    for index in range(2):
        conn.execute(
            """
            INSERT INTO execution_events (
                job_id, event_type, payload_json, source, graph_revision, created_at
            ) VALUES ('job-clean', 'evaluator_failure_feedback_consumed', ?,
                      'runtime', 2, ?)
            """,
            (json.dumps({"consumer_node_id": "node-primary"}), 10 + index),
        )
    conn.execute(
        """
        INSERT INTO execution_events (
            job_id, event_type, payload_json, source, graph_revision, created_at
        ) VALUES ('job-clean', 'contribution_attribution_verified', ?,
                  'runtime', 2, 20)
        """,
        (json.dumps({"contribution_lineage_refs": {"artifact-a": "event:1"}}),),
    )
    conn.commit()
    conn.close()
    payload: dict[str, object] = {
        "run_id": "phase4g10-clean-large-fixture",
        "job_id": "job-clean",
        "resume": {"resumed": False},
        "run_report": {
            "runtime_validation": {
                "passed": True,
                "duplicate_terminal_fact_count": 0,
                "duplicate_ledger_fact_count": 0,
            },
            "capability_validation": {"official_resolved": False},
            "metrics": {
                "evaluated_validation_stop": {
                    "schema": p4g8_run.EVALUATED_STOP_POLICY_SCHEMA,
                    "candidate_patch_sha256": "a" * 64,
                },
                "workspace_ownership_canary": {"passed": True},
            },
        },
    }
    return run_root, payload


def test_clean_report_rejects_any_historical_repair(tmp_path, monkeypatch):
    run_root, payload = _clean_report_fixture(tmp_path)
    monkeypatch.setattr(
        clean.arm2,
        "build_arm2_orchestration_report",
        lambda *args, **kwargs: {
            "instance_id": "iterative-dvc",
            "classification": {
                "runtime_correctness": "passed",
                "effective_orchestration": "passed",
                "task_capability": "task-failed",
            },
            "primary": {
                "node_key": "primary",
                "node_id": "node-primary",
                "resume_count": 3,
            },
            "children": [{}, {}],
            "contributions": [{}, {}],
            "evaluator_progression": [{"round": 1}, {"round": 2}, {"round": 3}],
        },
    )
    source = {"revision": "abc", "clean": True, "status": []}

    report = clean.build_clean_replay_report(
        run_root,
        payload,
        source_before=source,
        source_after=source,
    )

    assert report["classification"]["clean_replay"] == "passed"
    assert all(report["assertions"].values())

    conn = sqlite3.connect(run_root / "hermes-home" / "kanban.db")
    conn.execute(
        """
        INSERT INTO execution_events (
            job_id, event_type, payload_json, source, graph_revision, created_at
        ) VALUES ('job-clean', 'phase4g8_receipt_recovery_branch_repaired',
                  '{}', 'runtime', 2, 30)
        """
    )
    conn.commit()
    conn.close()

    failed = clean.build_clean_replay_report(
        run_root,
        payload,
        source_before=source,
        source_after=source,
    )
    assert failed["classification"]["clean_replay"] == "failed"
    assert failed["assertions"]["no_historical_repair_events"] is False


def test_run_clean_replay_freezes_policy_and_cleans_only_after_archive(
    tmp_path,
    monkeypatch,
):
    actual_root = tmp_path / "actual-run"
    reports = actual_root / "reports"
    reports.mkdir(parents=True)
    payload = {
        "run_id": "phase4g10-clean-large-fixture",
        "job_id": "job-clean",
        "paths": {"root": str(actual_root)},
    }
    source = {"root": "/repo", "revision": "abc", "clean": True, "status": []}
    calls: dict[str, object] = {}

    def fake_run(**kwargs):
        calls["run"] = kwargs
        return payload

    report = {
        "classification": {
            "runtime_correctness": "passed",
            "clean_replay": "passed",
            "effective_orchestration": "passed",
            "task_capability": "task-failed",
        },
        "assertions": {"all": True},
        "event_counts": {
            "historical_repairs": {},
            "receipt_recovery": {},
            "evaluator_failure_feedback_consumed": 2,
            "strategy_nodes": 0,
            "supervisor_owner_count": 2,
        },
        "ownership_canary": {"passed": True},
        "orchestration": {
            "primary": {"node_key": "primary", "resume_count": 3},
            "children": [{}, {}],
            "contributions": [{}, {}],
            "evaluator_progression": [],
        },
    }
    monkeypatch.setattr(clean, "_runtime_source_state", lambda: source)
    monkeypatch.setattr(clean.p4g8_run, "run_phase4g8_real_case", fake_run)
    monkeypatch.setattr(clean, "build_clean_replay_report", lambda *a, **k: report)
    monkeypatch.setattr(
        clean.validation_artifacts,
        "archive_validation_run",
        lambda *a, **k: {
            "status": "verified",
            "artifact_path": str(tmp_path / "archive"),
        },
    )
    monkeypatch.setattr(
        clean.validation_artifacts,
        "cleanup_rebuildable_entries",
        lambda *a, **k: {
            "status": "cleaned_after_verified_archive",
            "removed_entries": sorted(k["entries"]),
            "bytes_removed": 1,
        },
    )

    result = clean.run_clean_replay(
        qualification_spec_path=tmp_path / "qualified.json",
        run_root=tmp_path / "runs",
        source_codex_home=tmp_path / "codex",
        artifact_root=tmp_path / "artifacts",
        execute_real=True,
        max_wall_seconds=100,
        worker_timeout_seconds=50,
    )

    run = calls["run"]
    assert run["run_id_prefix"] == "phase4g10-clean"
    assert run["workspace_ownership_canary"] is True
    assert run["evaluated_stop_policy"] == {
        "schema": p4g8_run.EVALUATED_STOP_POLICY_SCHEMA,
        "min_completed_evaluator_attempts": 3,
        "min_consumed_evaluator_feedback": 2,
        "reason": "Clean Replay 已完成两次 evaluator feedback remediation，并保留第三个固定已评估 candidate。",
    }
    assert result["artifact_archive"]["status"] == "verified"
    assert result["local_cleanup"]["status"] == "cleaned_after_verified_archive"
    assert (reports / "clean-replay-summary.md").is_file()
