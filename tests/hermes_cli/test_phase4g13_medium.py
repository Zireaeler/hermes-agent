import json
from pathlib import Path

from hermes_cli import kanban_runtime_kernel as rk
from hermes_cli import kanban_runtime_phase4g8_run as p4g8_run
from hermes_cli import phase4g13_medium as phase4g13


def _locked_task() -> dict:
    return {
        "srs": "Implement the public release requirements without topology hints.",
    }


def test_single_worker_prompt_owns_full_result_without_topology_answer(tmp_path):
    prompt = phase4g13.build_single_worker_prompt(
        _locked_task(), toolchain=tmp_path / "toolchain"
    )

    assert "one coherent worker session" in prompt
    assert "Do not delegate to subagents" in prompt
    assert "No evaluator result will be returned" in prompt
    assert "dask/cli.py" not in prompt
    assert "test_register_command_ep" not in prompt
    assert "responsibility_candidates" not in prompt


def test_runtime_arm_uses_natural_coordination_and_one_unconsumed_evaluation(
    tmp_path,
):
    kwargs = phase4g13.runtime_arm_call_kwargs(
        qualification_spec_path=tmp_path / "qualification.json",
        run_root=tmp_path / "runtime",
        source_codex_home=tmp_path / "codex",
        max_wall_seconds=123,
        worker_timeout_seconds=456,
    )

    policy = kwargs["orchestration_policy"]
    assert policy["schema"] == rk.RUNTIME_ORCHESTRATION_POLICY_SCHEMA
    assert policy["mode"] == "closed_loop_coordination"
    assert policy["max_child_nodes"] == 3
    assert "assessment_replay" not in policy
    assert kwargs["fault_profile"] == "none"
    assert kwargs["worker_multi_agent_enabled"] is False
    stop = kwargs["evaluated_stop_policy"]
    assert stop["schema"] == p4g8_run.EVALUATED_STOP_POLICY_SCHEMA
    assert stop["min_completed_evaluator_attempts"] == 1
    assert stop["min_consumed_evaluator_feedback"] == 0


def test_comparison_keeps_quality_and_runtime_cost_separate():
    single = {
        "instance_id": phase4g13.FROZEN_INSTANCE_ID,
        "run_id": "single",
        "worker_count": 1,
        "wall_time_seconds": 10,
        "evaluator_invocation_count": 1,
        "evaluator_feedback_consumed": 0,
        "quality": {"resolved": True},
        "worker": {"events": {"usage": {"input_tokens": 100}}},
        "integrity": {"candidate_key_or_file_partition_in_prompt": False},
    }
    runtime = {
        "instance_id": phase4g13.FROZEN_INSTANCE_ID,
        "run_id": "runtime",
        "worker_count": 3,
        "wall_time_seconds": 18,
        "evaluator_invocation_count": 1,
        "evaluator_feedback_consumed": 0,
        "quality": {"resolved": False},
        "runtime": {
            "worker_sessions": {"aggregate": {"input_tokens": 250}},
            "coordination_cost": {"invalid_resume_count": 0},
        },
        "integrity": {"candidate_key_or_file_partition_in_prompt": False},
    }

    report = phase4g13.build_comparison_report(single, runtime)

    assert report["arms"]["coherent_single_worker"]["quality"]["resolved"] is True
    assert report["arms"]["runtime_orchestra"]["quality"]["resolved"] is False
    assert report["arms"]["runtime_orchestra"]["wall_time_seconds"] == 18
    assert report["arms"]["runtime_orchestra"]["coordination_cost"] == {
        "invalid_resume_count": 0
    }
    assert all(report["integrity"].values())


def test_frozen_public_manifest_does_not_expose_oracle_content():
    root = Path(__file__).resolve().parents[2]
    manifest = json.loads(
        (root / "docs/validation/phase4g13/qualification-manifest.json").read_text(
            encoding="utf-8"
        )
    )

    assert manifest["instance_id"] == phase4g13.FROZEN_INSTANCE_ID
    assert manifest["srs_sha256"] == phase4g13.FROZEN_SRS_SHA256
    assert manifest["test_patch_sha256"] == phase4g13.FROZEN_TEST_PATCH_SHA256
    assert manifest["official_image_digest"] == phase4g13.FROZEN_IMAGE_DIGEST
    assert manifest["oracle_status"] == "qualified"
    assert manifest["worker_visibility"]["candidate_keys"] is False
    assert "FAIL_TO_PASS" not in manifest
    assert "PASS_TO_PASS" not in manifest
