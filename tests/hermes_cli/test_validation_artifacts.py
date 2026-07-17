import json
from pathlib import Path

import pytest

from hermes_cli import validation_artifacts as artifacts


def _validation_run(tmp_path: Path) -> Path:
    run = tmp_path / "runs" / "phase4g9-test-run"
    sessions = run / "codex-home" / "sessions"
    events = run / "worker-events"
    service = run / "service"
    reports = run / "reports"
    workspace = run / "workspace"
    for path in (sessions, events, service, reports, workspace):
        path.mkdir(parents=True, exist_ok=True)
    (run / "codex-home" / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "sk-test-secret"}) + "\n",
        encoding="utf-8",
    )
    (run / "codex-home" / "config.toml").write_text(
        'model = "gpt-test"\nbase_url = "http://local-proxy.invalid/v1"\n',
        encoding="utf-8",
    )
    (sessions / "rollout.jsonl").write_text(
        '{"message":"reasoning is retained","upstream":"https://source.example/v1"}\n',
        encoding="utf-8",
    )
    (events / "codex-exec.jsonl").write_text(
        '{"header":"Bearer sk-test-secret"}\n', encoding="utf-8"
    )
    (service / "kanban.db").write_bytes(b"runtime-state")
    (reports / "run-report.json").write_text('{"status":"done"}\n', encoding="utf-8")
    (workspace / "candidate.py").write_text("changed = True\n", encoding="utf-8")
    return run


def test_archive_validation_run_redacts_credentials_and_verifies_manifest(tmp_path):
    run = _validation_run(tmp_path)
    archive_root = tmp_path / "artifacts"

    manifest = artifacts.archive_validation_run(
        run,
        artifact_root=archive_root,
        phase="phase4g9",
        instance_id="fixture",
        redactions={"https://source.example/v1": "<redacted-model-source-base-url>"},
        expected_entries={"codex-home", "worker-events", "service", "reports"},
    )

    archived = Path(manifest["artifact_path"])
    assert manifest["status"] == "verified"
    assert manifest["file_count"] == 5
    assert not (archived / "codex-home" / "auth.json").exists()
    rollout = (archived / "codex-home" / "sessions" / "rollout.jsonl").read_text()
    outer = (archived / "worker-events" / "codex-exec.jsonl").read_text()
    assert "reasoning is retained" in rollout
    assert "source.example" not in rollout
    assert "sk-test-secret" not in outer
    assert "local-proxy.invalid" in (archived / "codex-home" / "config.toml").read_text()
    assert not (archived / "workspace").exists()
    assert artifacts.verify_artifact_manifest(archived / "manifest.json")["run_id"] == run.name


def test_archive_validation_run_is_idempotent_after_verified_copy(tmp_path):
    run = _validation_run(tmp_path)
    kwargs = {
        "artifact_root": tmp_path / "artifacts",
        "phase": "phase4g9",
        "instance_id": "fixture",
    }

    first = artifacts.archive_validation_run(run, **kwargs)
    second = artifacts.archive_validation_run(run, **kwargs)

    assert first["artifact_path"] == second["artifact_path"]
    assert first["files"] == second["files"]


def test_cleanup_requires_matching_verified_manifest_and_allowlisted_entries(tmp_path):
    run = _validation_run(tmp_path)
    manifest = artifacts.archive_validation_run(
        run,
        artifact_root=tmp_path / "artifacts",
        phase="phase4g9",
        instance_id="fixture",
    )
    manifest_path = Path(manifest["artifact_path"]) / "manifest.json"

    with pytest.raises(artifacts.ArtifactArchiveError, match="non-rebuildable"):
        artifacts.cleanup_rebuildable_entries(
            run,
            manifest_path=manifest_path,
            entries={"codex-home"},
        )

    cleanup = artifacts.cleanup_rebuildable_entries(
        run,
        manifest_path=manifest_path,
        entries={"workspace"},
    )

    assert cleanup["status"] == "cleaned_after_verified_archive"
    assert cleanup["removed_entries"] == ["workspace"]
    assert not (run / "workspace").exists()
    assert (run / "codex-home" / "sessions" / "rollout.jsonl").is_file()


def test_manifest_verification_detects_archive_tampering(tmp_path):
    run = _validation_run(tmp_path)
    manifest = artifacts.archive_validation_run(
        run,
        artifact_root=tmp_path / "artifacts",
        phase="phase4g9",
        instance_id="fixture",
    )
    archived = Path(manifest["artifact_path"])
    (archived / "reports" / "run-report.json").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(artifacts.ArtifactArchiveError, match="mismatch"):
        artifacts.verify_artifact_manifest(archived / "manifest.json")
