from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_kernel as rk
from hermes_cli import kanban_runtime_learning as learning
from hermes_cli import validation_artifacts


@pytest.fixture
def runtime_conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kb.connect() as conn:
        rk.ensure_runtime_schema(conn)
        root_task = kb.create_task(conn, title="learning root", initial_status="running")
        job_id = rk.create_runtime_job(
            conn,
            root_task,
            "verify every orchestra run is absorbed",
            goal_items=[
                {
                    "item_key": "learning-result",
                    "description": "learning evidence exists",
                    "required": True,
                    "verifier_required": False,
                }
            ],
            initialization_mode="fixture",
        )
        yield conn, job_id


def test_learning_bundle_is_absorbed_before_archive_and_cleanup(
    runtime_conn,
    tmp_path,
):
    conn, job_id = runtime_conn
    run_root = tmp_path / "run-1"
    (run_root / "workspace").mkdir(parents=True)
    (run_root / "workspace" / "cache.bin").write_bytes(b"cache")
    artifact_root = tmp_path / "artifacts"
    registry = artifact_root / "orchestration-learning" / "registry.sqlite3"

    finalized = learning.finalize_learning_bundle(
        conn,
        job_id,
        run_root=run_root,
        registry_path=registry,
        phase="phase4g15",
        instance_id="controlled",
        run_id="run-1",
        source_db_ref="runtime-state/kanban.db",
        quality={"status": "passed", "score": 1.0},
    )

    assert finalized["receipt"]["status"] == "absorbed"
    markdown = Path(finalized["markdown_path"]).read_text(encoding="utf-8")
    assert "过程学习报告" in markdown
    assert "发现与吸收" in markdown
    manifest = validation_artifacts.archive_validation_run(
        run_root,
        artifact_root=artifact_root,
        phase="phase4g15",
        instance_id="controlled",
        expected_entries=("reports",),
        orchestration_learning_required=True,
    )
    assert manifest["orchestration_learning"]["status"] == "absorbed"
    cleaned = validation_artifacts.cleanup_rebuildable_entries(
        run_root,
        manifest_path=Path(manifest["artifact_path"]) / "manifest.json",
        entries=("workspace",),
        orchestration_learning_required=True,
    )
    assert cleaned["removed_entries"] == ["workspace"]


def test_candidate_promotion_requires_quality_and_metric_improvement(
    runtime_conn,
    tmp_path,
):
    conn, job_id = runtime_conn
    bundle = learning.build_learning_bundle(
        conn,
        job_id,
        phase="phase4g15",
        instance_id="promotion",
        run_id="run-promotion",
        source_db_ref="runtime-state/kanban.db",
    )
    finding = bundle["findings"][0]
    finding.update(
        {
            "category": "coordination_overhead",
            "absorption_disposition": "candidate_created",
        }
    )
    candidate = {
        "candidate_key": "candidate_coordination_overhead",
        "category": "coordination_overhead",
        "scope": "runtime_coordination",
        "symptom": "unnecessary decision",
        "root_cause": "local reducer could decide",
        "evidence_refs": finding["evidence_refs"],
        "proposed_change": "route locally",
        "expected_effect": "lower decision count",
        "regression_scenario_key": "regression-coordination-overhead",
        "status": "candidate",
    }
    bundle["improvement_candidates"] = [candidate]
    bundle["regression_scenarios"] = [
        {
            "scenario_key": candidate["regression_scenario_key"],
            "candidate_key": candidate["candidate_key"],
            "required_assertion": candidate["expected_effect"],
        }
    ]
    bundle["absorption"]["candidate_count"] = 1
    bundle_path = tmp_path / "candidate.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    registry = tmp_path / "registry.sqlite3"
    receipt = learning.absorb_learning_bundle(
        bundle,
        bundle_path=bundle_path,
        registry_path=registry,
    )
    learning.record_candidate_evaluation(
        registry,
        candidate_key=candidate["candidate_key"],
        arm="baseline",
        bundle_sha256_value="baseline-sha",
        quality_score=1.0,
        target_metric=5.0,
    )
    learning.record_candidate_evaluation(
        registry,
        candidate_key=candidate["candidate_key"],
        arm="treatment",
        bundle_sha256_value=receipt["bundle_sha256"],
        quality_score=1.0,
        target_metric=2.0,
    )
    promoted = learning.promote_candidate(
        registry,
        candidate_key=candidate["candidate_key"],
        approved_by="release:test",
    )
    assert promoted["status"] == "promoted"


def test_learning_gate_rejects_unabsorbed_bundle(runtime_conn, tmp_path):
    conn, job_id = runtime_conn
    run_root = tmp_path / "unabsorbed"
    reports = run_root / "reports"
    reports.mkdir(parents=True)
    bundle = learning.build_learning_bundle(
        conn,
        job_id,
        phase="phase4g15",
        instance_id="invalid",
        run_id="run-invalid",
        source_db_ref="runtime-state/kanban.db",
    )
    (reports / "orchestration-learning.json").write_text(
        json.dumps(bundle),
        encoding="utf-8",
    )
    with pytest.raises(validation_artifacts.ArtifactArchiveError, match="receipt"):
        validation_artifacts.archive_validation_run(
            run_root,
            artifact_root=tmp_path / "artifacts",
            phase="phase4g15",
            instance_id="invalid",
            orchestration_learning_required=True,
        )


def test_terminal_pending_live_delivery_creates_improvement_candidate():
    live = {
        "deliveries": [
            {
                "delivery_id": "rld-pending",
                "status": "pending",
                "target_terminal_at": 42,
            }
        ]
    }

    findings = learning._findings("job-pending", {}, live)
    candidates = learning._candidates(findings)

    assert [item["category"] for item in findings] == [
        "live_delivery_unresolved"
    ]
    assert candidates[0]["category"] == "live_delivery_unresolved"


def test_natural_calibration_findings_cover_false_missed_and_overhead():
    orchestration = {
        "coordination": {
            "actions": [
                {
                    "id": "rcact-negative",
                    "route": "local_context_route",
                    "status": "applied",
                }
            ],
            "cost": {},
        }
    }
    quality = {
        "case_kind": "coherent_negative_control",
        "coordination_observations": {
            "missed_coordination_evidence_refs": ["artifact:stale-child"],
            "coordination_overhead_evidence_refs": ["pair:wall-time"],
        },
    }

    findings = learning._findings(
        "job-natural-calibration",
        orchestration,
        {"deliveries": []},
        quality,
    )

    assert {item["category"] for item in findings} == {
        "false_coordination",
        "missed_coordination",
        "coordination_overhead",
    }


def test_calibration_fixture_gap_does_not_create_runtime_coordination_candidate():
    quality = {
        "case_kind": "durable_boundary_medium",
        "coordination_observations": {
            "missed_coordination_evidence_refs": [],
            "calibration_fixture_gap_evidence_refs": [
                "report:durable-boundary-medium:candidate-not-observed"
            ],
        },
    }

    findings = learning._findings(
        "job-calibration-gap",
        {"coordination": {"actions": [], "cost": {}}},
        {"deliveries": []},
        quality,
    )
    candidates = learning._candidates(findings)

    assert [item["category"] for item in findings] == [
        "calibration_fixture_gap"
    ]
    assert candidates[0]["scope"] == "validation_campaign"
    assert "冻结任务" in candidates[0]["proposed_change"]


def test_checkpoint_protocol_failure_creates_runtime_coordination_candidate():
    quality = {
        "case_kind": "durable_boundary_medium",
        "coordination_observations": {
            "coordination_protocol_failure_evidence_refs": [
                "materialization:mat-invalid"
            ],
            "calibration_fixture_gap_evidence_refs": [],
        },
    }

    findings = learning._findings(
        "job-checkpoint-protocol-failure",
        {"coordination": {"actions": [], "cost": {}}},
        {"deliveries": []},
        quality,
    )
    candidates = learning._candidates(findings)

    assert [item["category"] for item in findings] == [
        "coordination_protocol_failure"
    ]
    assert candidates[0]["scope"] == "runtime_coordination"
    assert "reducer/transport" in candidates[0]["proposed_change"]
