import json
from pathlib import Path

from hermes_cli import phase4g16_natural_calibration as phase4g16


def _treatment(*, actions=None, checkpoints=None, candidate_count=0):
    return {
        "oracle": {"passed": True},
        "candidate_count": candidate_count,
        "consistency": {"status": "passed"},
        "orchestration": {
            "coordination": {
                "actions": list(actions or []),
                "checkpoints": list(checkpoints or []),
                "terminal_responsibility_candidates": {
                    "expanded_count": 0,
                    "resolved_without_expansion_count": 0,
                },
            }
        },
    }


def test_frozen_cases_do_not_preannounce_topology_or_candidate_keys():
    cases = phase4g16._cases()

    assert [item.kind for item in cases] == [
        "coherent_negative_control",
        "shared_contract_medium",
        "durable_boundary_medium",
    ]
    for case in cases:
        text = case.objective.lower()
        assert "candidate_key" not in text
        assert "responsibility_candidate" not in text
        assert "create_node" not in text
        assert "src/" not in text
        assert "拆分" not in text

    durable = cases[2]
    assert "src/core/event.py" in durable.files
    assert "tests/test_legacy_plugin.py" in durable.files
    assert "tests/test_audit_jsonl.py" in durable.files


def test_each_frozen_repository_starts_with_a_failing_oracle(tmp_path):
    for case in phase4g16._cases():
        workspace = tmp_path / case.key
        revision = phase4g16._write_repository(workspace, case)
        result = phase4g16._oracle(workspace)

        assert len(revision) == 40
        assert result["passed"] is False
        assert result["test_count"] is not None


def test_changed_files_preserves_first_character_of_modified_path(tmp_path):
    case = phase4g16._cases()[0]
    workspace = tmp_path / "changed-files"
    phase4g16._write_repository(workspace, case)
    target = workspace / "src" / "retry.py"
    target.write_text(target.read_text(encoding="utf-8") + "\nVALUE = 2\n", encoding="utf-8")
    (workspace / "src" / "new.py").write_text("NEW = True\n", encoding="utf-8")

    assert phase4g16._changed_files(workspace) == [
        "src/new.py",
        "src/retry.py",
    ]


def test_negative_control_rejects_any_coordination_action():
    case = phase4g16._cases()[0]
    baseline = {"oracle": {"passed": True}}

    clean = phase4g16._acceptance(case, baseline, _treatment())
    noisy = phase4g16._acceptance(
        case,
        baseline,
        _treatment(actions=[{"id": "action-1"}]),
    )

    assert clean["no_false_coordination"] is True
    assert noisy["no_false_coordination"] is False


def test_shared_contract_accepts_natural_checkpoint_or_coherent_route():
    case = phase4g16._cases()[1]
    baseline = {"oracle": {"passed": True}}

    coherent = phase4g16._acceptance(case, baseline, _treatment())
    checkpointed = phase4g16._acceptance(
        case,
        baseline,
        _treatment(checkpoints=[{"event_id": 1}]),
    )

    assert coherent["checkpoint_or_coherent_route"] is True
    assert checkpointed["checkpoint_or_coherent_route"] is True


def test_durable_boundary_requires_natural_candidate_and_provider_consumption():
    case = phase4g16._cases()[2]
    baseline = {"oracle": {"passed": True}}
    action = {
        "id": "action-1",
        "route": "provider_required",
        "status": "applied",
    }

    missing = phase4g16._acceptance(case, baseline, _treatment())
    consumed = phase4g16._acceptance(
        case,
        baseline,
        _treatment(actions=[action], candidate_count=1),
    )

    assert missing["natural_candidate_observed"] is False
    assert missing["candidate_consumed_by_provider"] is False
    assert consumed["natural_candidate_observed"] is True
    assert consumed["candidate_consumed_by_provider"] is True


def test_missing_durable_candidate_is_a_fixture_gap_not_missed_coordination():
    case = phase4g16._cases()[2]
    baseline = {"oracle": {"passed": True}}
    treatment = _treatment()
    treatment["orchestration"]["structure_checkpoint"] = {"event_id": 21}

    observations = phase4g16._coordination_observations(
        case,
        baseline,
        treatment,
    )

    assert observations["missed_coordination_evidence_refs"] == []
    assert observations["coordination_protocol_failure_evidence_refs"] == []
    assert observations["calibration_fixture_gap_evidence_refs"] == [
        "execution_event:21",
        "report:durable-boundary-medium:candidate-not-observed",
    ]


def test_invalid_natural_candidate_checkpoint_is_protocol_failure_not_fixture_gap():
    case = phase4g16._cases()[2]
    baseline = {"oracle": {"passed": True}}
    treatment = _treatment()
    treatment["invalid_structural_checkpoints"] = [{
        "materialization_id": "mat-invalid",
        "event_id": 31,
        "checkpoint_schema": "runtime_worker_structure_checkpoint_v1",
        "recommendation": "defer_until_milestone",
        "proposed_node_count": 2,
        "responsibility_candidate_count": 0,
        "validation_error": (
            "milestone_contract artifact_scope must be within shared_integration_scope"
        ),
    }]

    observations = phase4g16._coordination_observations(
        case,
        baseline,
        treatment,
    )

    assert observations["calibration_fixture_gap_evidence_refs"] == []
    assert observations["coordination_protocol_failure_evidence_refs"] == [
        "execution_event:31",
        "materialization:mat-invalid",
        "report:durable-boundary-medium:natural-candidate-checkpoint-rejected",
    ]


def test_fixture_gap_conclusion_does_not_claim_runtime_or_quality_failure():
    conclusion = phase4g16._case_conclusion(
        passed=False,
        acceptance={
            "baseline_quality_passed": True,
            "treatment_quality_passed": True,
            "quality_non_regression": True,
            "runtime_consistency_passed": True,
            "natural_candidate_observed": False,
        },
        finding_categories=["calibration_fixture_gap"],
    )

    assert "校准夹具不足" in conclusion
    assert "不是 Runtime correctness 或任务质量失败" in conclusion


def test_protocol_failure_conclusion_separates_quality_from_orchestra():
    conclusion = phase4g16._case_conclusion(
        passed=False,
        acceptance={
            "baseline_quality_passed": True,
            "treatment_quality_passed": True,
            "quality_non_regression": True,
            "runtime_consistency_passed": True,
            "natural_candidate_observed": False,
        },
        finding_categories=["coordination_protocol_failure"],
    )

    assert "orchestration protocol failure" in conclusion
    assert "不能归类为校准夹具不足" in conclusion


def test_baseline_prompt_keeps_one_worker_and_no_runtime_answer():
    prompt = phase4g16._baseline_prompt(phase4g16._cases()[2])

    assert "唯一的 coherent implementation worker" in prompt
    assert "不要委派 subagent" in prompt
    assert "candidate_key" not in prompt
    assert "create_node" not in prompt


def test_isolated_config_summary_omits_paths_and_provider_endpoint(tmp_path):
    audit = {
        "source_home": str(Path.home() / ".codex"),
        "target_home": str(tmp_path / "codex"),
        "proxy_base_url": "https://sensitive.invalid/v1",
        "model": "model",
        "reasoning_effort": "max",
        "multi_agent_enabled": False,
        "provider_transport": {"supports_websockets": True},
    }

    summary = phase4g16._isolated_config_summary(audit)

    assert summary["model"] == "model"
    assert summary["provider_transport"] == {"supports_websockets": True}
    assert "source_home" not in summary
    assert "target_home" not in summary
    assert "proxy_base_url" not in summary


def test_archive_instance_is_unique_per_campaign_root(tmp_path):
    case = phase4g16._cases()[0]
    first = phase4g16.CalibrationConfig(
        root=tmp_path / "campaign-a",
        artifact_root=tmp_path / "artifacts",
        source_codex_home=tmp_path / "codex",
    )
    second = phase4g16.CalibrationConfig(
        root=tmp_path / "campaign-b",
        artifact_root=tmp_path / "artifacts",
        source_codex_home=tmp_path / "codex",
    )

    assert phase4g16._archive_instance_id(first, case) != phase4g16._archive_instance_id(
        second,
        case,
    )


def test_verified_baseline_reuse_skips_model_and_records_provenance(
    tmp_path,
    monkeypatch,
):
    case = phase4g16._cases()[2]
    archive = tmp_path / "archive"
    reports = archive / "reports"
    reports.mkdir(parents=True)
    manifest_path = archive / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    prior_report = {
        "schema": phase4g16.CASE_REPORT_SCHEMA,
        "case": {
            "key": case.key,
            "title": case.title,
            "kind": case.kind,
            "objective": case.objective,
            "fixture_sha256": phase4g16._case_fixture_sha256(case),
        },
        "baseline": {
            "transport_status": "completed",
            "oracle": {"passed": True, "test_count": 27},
            "changed_files": ["src/core/event.py"],
            "wall_time_seconds": 12.5,
        },
        "status": "failed",
    }
    (reports / "case-report.json").write_text(
        json.dumps(prior_report),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        phase4g16.validation_artifacts,
        "verify_artifact_manifest",
        lambda path: {
            "schema": "manifest",
            "status": "verified",
            "source_run_root": "/tmp/prior-run",
            "files": [{"path": "reports/case-report.json"}],
        },
    )

    baseline = phase4g16._load_verified_reused_baseline(manifest_path, case)

    assert baseline["oracle"]["passed"] is True
    assert baseline["reuse_provenance"]["status"] == "verified_archive_reused"
    assert baseline["reuse_provenance"]["fixture_identity_mode"] == "fixture_sha256"


def test_campaign_archives_first_infrastructure_failure_and_stops(
    tmp_path,
    monkeypatch,
):
    config = phase4g16.CalibrationConfig(
        root=tmp_path / "campaign",
        artifact_root=tmp_path / "artifacts",
        source_codex_home=tmp_path / "codex",
    )
    attempted = []
    archived = []

    def fail_case(_config, case):
        attempted.append(case.key)
        raise RuntimeError("broken calibration infrastructure")

    def archive_failure(_config, case, exc):
        archived.append((case.key, str(exc)))
        return {
            "case": {"key": case.key},
            "status": "infrastructure_invalid",
        }

    monkeypatch.setattr(phase4g16, "run_case", fail_case)
    monkeypatch.setattr(
        phase4g16,
        "_archive_infrastructure_invalid_case",
        archive_failure,
    )

    report = phase4g16.run_campaign(config)

    assert report["status"] == "failed"
    assert attempted == ["coherent-negative"]
    assert archived == [
        ("coherent-negative", "broken calibration infrastructure")
    ]
    assert report["cases"] == [
        {
            "key": "coherent-negative",
            "status": "infrastructure_invalid",
            "acceptance": None,
            "artifact_archive": None,
            "learning": None,
            "cleanup": None,
        }
    ]


def test_campaign_can_run_one_frozen_case(tmp_path, monkeypatch):
    config = phase4g16.CalibrationConfig(
        root=tmp_path / "campaign",
        artifact_root=tmp_path / "artifacts",
        source_codex_home=tmp_path / "codex",
    )
    attempted = []

    def pass_case(_config, case):
        attempted.append(case.key)
        return {
            "case": {"key": case.key},
            "status": "passed",
            "acceptance": {"passed": True},
        }

    monkeypatch.setattr(phase4g16, "run_case", pass_case)

    report = phase4g16.run_campaign(config, case_key="shared-contract-medium")

    assert report["status"] == "passed"
    assert report["selected_cases"] == ["shared-contract-medium"]
    assert attempted == ["shared-contract-medium"]


def test_campaign_rejects_unknown_case(tmp_path):
    config = phase4g16.CalibrationConfig(
        root=tmp_path / "campaign",
        artifact_root=tmp_path / "artifacts",
        source_codex_home=tmp_path / "codex",
    )

    try:
        phase4g16.run_campaign(config, case_key="missing")
    except ValueError as exc:
        assert str(exc) == "unknown calibration case: missing"
    else:
        raise AssertionError("unknown case must be rejected")


def test_treatment_waits_for_complete_worker_attempt(tmp_path, monkeypatch):
    case = phase4g16._cases()[0]
    captured = {}
    connections = []

    class FakeConnection:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self.close()
            return False

    def connect():
        conn = FakeConnection()
        connections.append(conn)
        return conn

    monkeypatch.setattr(phase4g16, "_create_runtime_job", lambda *args: "job-1")
    monkeypatch.setattr(phase4g16.kb, "connect", connect)

    def run_smoke(_conn, _job_id, **kwargs):
        captured.update(kwargs)
        return {"final_state": "done"}

    monkeypatch.setattr(phase4g16, "run_real_worker_lane_smoke", run_smoke)
    monkeypatch.setattr(
        phase4g16,
        "_runtime_evidence",
        lambda _conn, _job_id: {
            "status": {"job": {"state": "done"}},
            "consistency": {"status": "passed"},
            "orchestration": {},
            "nodes": [],
            "candidate_count": 0,
        },
    )
    monkeypatch.setattr(phase4g16, "_oracle", lambda _path: {"passed": True})
    monkeypatch.setattr(phase4g16, "_changed_files", lambda _path: [])

    phase4g16._run_treatment(
        case,
        tmp_path / "workspace",
        tmp_path,
        provider_source={"provider_name": "provider", "model": "model"},
        worker_timeout_seconds=321,
        decision_timeout_seconds=45,
    )

    assert captured["worker_wait_seconds"] == 321.0
    assert len(connections) == 2
    assert all(conn.closed for conn in connections)


def test_infrastructure_failure_retains_source_when_db_is_corrupt(
    tmp_path,
    monkeypatch,
):
    config = phase4g16.CalibrationConfig(
        root=tmp_path / "campaign",
        artifact_root=tmp_path / "artifacts",
        source_codex_home=tmp_path / "codex",
    )
    case = phase4g16._cases()[0]
    db_path = config.root / case.key / "hermes-home" / "kanban.db"
    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"SQLite format 3\x00broken")

    def corrupt_connect(*args, **kwargs):
        raise phase4g16.sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(phase4g16.kb, "connect", corrupt_connect)

    report = phase4g16._archive_infrastructure_invalid_case(
        config,
        case,
        RuntimeError("consistency read failed"),
    )

    assert report["status"] == "infrastructure_invalid"
    assert report["learning"]["status"] == "absorption_blocked_db_corruption"
    assert "artifact_archive" not in report
    assert db_path.is_file()
    assert (config.root / case.key / "reports" / "capability-trace.md").is_file()
