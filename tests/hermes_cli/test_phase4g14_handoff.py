from pathlib import Path

from hermes_cli import phase4g14_handoff as phase4g14
from hermes_cli import validation_artifacts


def test_phase4g14_controlled_handoff_preserves_and_integrates_both_patches(
    tmp_path,
):
    run_root = tmp_path / "run"
    artifact_root = tmp_path / "artifacts"

    report = phase4g14.run_phase4g14_handoff(
        phase4g14.HandoffRunConfig(
            root=run_root,
            artifact_root=artifact_root,
        )
    )

    handoff = report["orchestration"]["contribution_handoff"]
    assert report["final_state"] == "done"
    assert report["official_evaluator_used"] is False
    assert report["model_worker_used"] is False
    assert report["process_boundaries"] == 1
    assert handoff["attempt_patch_captured_count"] == 2
    assert handoff["promoted_contribution_count"] == 2
    assert handoff["receipt_repair_count"] == 1
    assert handoff["implementation_reexecution_due_to_receipt_count"] == 0
    assert handoff["integrated_contribution_count"] == 2
    assert handoff["contribution_preservation_ratio"] == 1.0
    assert report["receipt_repair"]["shell_commands_run"] == 0
    assert report["receipt_repair"]["workspace_modified"] is False
    assert report["integration"]["test"]["returncode"] == 0
    assert report["consistency"]["status"] == "passed"
    assert report["cleanup"]["status"] == "cleaned"
    assert not run_root.exists()
    manifest = validation_artifacts.verify_artifact_manifest(
        Path(report["artifact_archive"]["manifest_path"])
    )
    assert manifest["status"] == "verified"
    assert manifest["file_count"] > 0
