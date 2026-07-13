"""Official SWE-EVO dataset and evaluator adapter for Phase 4G8."""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
from typing import Any, Optional
import types
import uuid

from agent.redact import redact_sensitive_text
from hermes_cli import kanban_runtime_phase4g8 as phase4g8


SWE_EVO_ADAPTER_SCHEMA = "hermes_phase4g8_swe_evo_adapter_v1"
SWE_EVO_DATASET_REVISION = "9b83d5af943ba7a17567336f5b18239f73960219"
SWE_EVO_ARROW_SHA256 = "74e7c63160ada4ceba71d5d89a9bb7c9794f4574b384458d546eb65cdb730520"
SWE_EVO_OFFICIAL_INSTANCE_IDS = (
    "pydantic__pydantic_v2.6.0b1_v2.6.0",
    "dask__dask_2022.9.2_2022.10.0",
    "iterative__dvc_1.0.0a1_1.0.0a2",
)


def load_swe_evo_rows(
    arrow_path: Path,
    instance_ids: list[str],
    *,
    expected_sha256: str = SWE_EVO_ARROW_SHA256,
) -> list[dict[str, Any]]:
    """Load only selected rows from the official fixed Arrow artifact."""

    arrow_path = arrow_path.expanduser().resolve()
    if not arrow_path.is_file():
        raise ValueError("SWE-EVO Arrow artifact does not exist")
    actual_hash = _sha256_file(arrow_path)
    if expected_sha256 and actual_hash != expected_sha256:
        raise ValueError(f"SWE-EVO Arrow SHA-256 mismatch: {actual_hash}")
    requested = [str(value).strip() for value in instance_ids if str(value).strip()]
    if not requested or len(set(requested)) != len(requested):
        raise ValueError("SWE-EVO instance ids must be non-empty and unique")
    try:
        import pyarrow as pa
        import pyarrow.ipc as ipc
    except ImportError as exc:
        raise RuntimeError("reading the SWE-EVO Arrow artifact requires pyarrow") from exc
    with pa.memory_map(str(arrow_path), "r") as source:
        table = ipc.open_stream(source).read_all()
    available = table["instance_id"].to_pylist()
    rows: list[dict[str, Any]] = []
    for instance_id in requested:
        if instance_id not in available:
            raise ValueError(f"SWE-EVO instance not found: {instance_id}")
        index = available.index(instance_id)
        row = {name: table[name][index].as_py() for name in table.column_names}
        _validate_dataset_row(row)
        rows.append(row)
    return rows


def prepare_swe_evo_specs(
    rows: list[dict[str, Any]],
    *,
    output_root: Path,
    local_mirrors: dict[str, Path],
    harness_python: Path,
    dataset_revision: str = SWE_EVO_DATASET_REVISION,
    dependency_modes: Optional[dict[str, str]] = None,
    evaluator_env: Optional[dict[str, str]] = None,
) -> list[dict[str, Any]]:
    """Materialize protected evaluator inputs and qualification specs."""

    if dataset_revision != SWE_EVO_DATASET_REVISION:
        raise ValueError("SWE-EVO dataset revision is not the Phase 4G8 locked revision")
    harness_python = harness_python.expanduser().absolute()
    if not harness_python.is_file() or not os.access(harness_python, os.X_OK):
        raise ValueError("official harness Python must be an executable file")
    outputs: list[dict[str, Any]] = []
    for row in rows:
        _validate_dataset_row(row)
        instance_id = row["instance_id"]
        mirror = Path(local_mirrors.get(instance_id, Path())).expanduser().resolve()
        if not (mirror / ".git").exists():
            raise ValueError(f"local mirror is missing for {instance_id}")
        mirror_head = _run_git(mirror, ["rev-parse", "HEAD"]).strip()
        if mirror_head != row["base_commit"]:
            raise ValueError(f"local mirror base commit mismatch for {instance_id}")
        if _run_git(mirror, ["status", "--porcelain=v1", "--untracked-files=all"]).strip():
            raise ValueError(f"local mirror must be clean for {instance_id}")
        layout = phase4g8.Phase4G8Layout(output_root.resolve(), instance_id)
        layout.prepare()
        gold_path = layout.protected / "gold.patch"
        test_patch_path = layout.protected / "test.patch"
        evaluator_instance_path = layout.protected / "swe-evo-instance.json"
        evaluator_root = layout.protected / "evaluator-runs"
        evaluator_root.mkdir(parents=True, exist_ok=True)
        _write_text(gold_path, row["patch"], mode=0o600)
        _write_text(test_patch_path, row["test_patch"], mode=0o600)
        dependency_mode = str((dependency_modes or {}).get(instance_id) or "locked_image")
        if dependency_mode not in {"locked_image", "official_install"}:
            raise ValueError(f"unsupported dependency mode for {instance_id}")
        evaluator_instance = _evaluator_instance(row, test_patch_path, dependency_mode=dependency_mode)
        _write_json(evaluator_instance_path, evaluator_instance, mode=0o600)
        spec = {
            "schema": phase4g8.QUALIFICATION_SPEC_SCHEMA,
            "instance_id": instance_id,
            "dataset_revision": dataset_revision,
            "repository": row["repo"],
            "base_commit": row["base_commit"],
            "srs": row["problem_statement"],
            "public_requirements": [],
            "source": {"local_mirror": str(mirror)},
            "gold": {"patch_path": str(gold_path)},
            "evaluator": {
                "argv": [
                    str(harness_python),
                    "-m",
                    "hermes_cli.phase4g8_swe_evo",
                    "evaluate",
                    "--instance",
                    str(evaluator_instance_path),
                    "--workspace",
                    "{workspace}",
                    "--output-root",
                    str(evaluator_root),
                ],
                "timeout_seconds": 7200,
                "env": {str(key): str(value) for key, value in (evaluator_env or {}).items()},
            },
            "benchmark": {
                "name": "SWE-EVO",
                "adapter_schema": SWE_EVO_ADAPTER_SCHEMA,
                "official_image": row["image"],
                "fail_to_pass_count": len(row["FAIL_TO_PASS"]),
                "pass_to_pass_count": len(row["PASS_TO_PASS"]),
                "test_patch_sha256": hashlib.sha256(row["test_patch"].encode("utf-8")).hexdigest(),
                "dependency_mode": dependency_mode,
            },
        }
        phase4g8.validate_qualification_spec(spec)
        spec_path = layout.protected / "qualification-spec.json"
        _write_json(spec_path, spec, mode=0o600)
        outputs.append({
            "instance_id": instance_id,
            "spec_path": str(spec_path),
            "layout": str(layout.instance_root),
            "fail_to_pass_count": len(row["FAIL_TO_PASS"]),
            "pass_to_pass_count": len(row["PASS_TO_PASS"]),
            "gold_patch_bytes": len(row["patch"].encode("utf-8")),
        })
    return outputs


def evaluate_swe_evo_workspace(instance_path: Path, workspace: Path, output_root: Path) -> dict[str, Any]:
    """Run the fixed SWE-EVO Docker evaluator without modifying the candidate workspace."""

    instance = json.loads(instance_path.read_text(encoding="utf-8"))
    _validate_evaluator_instance(instance)
    workspace = workspace.resolve()
    if not (workspace / ".git").exists():
        raise ValueError("candidate workspace must be a git repository")
    candidate_patch = collect_candidate_patch(workspace, instance["base_commit"])
    test_patch = Path(instance.pop("test_patch_path")).read_text(encoding="utf-8")
    benchmark_row = dict(instance)
    benchmark_row["test_patch"] = test_patch
    combined_patch = _merge_patches(test_patch, candidate_patch)
    run_id = f"phase4g8-{uuid.uuid4().hex[:12]}"
    run_root = output_root.resolve() / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    os.chmod(run_root, 0o700)
    log_path = run_root / "harness.log"
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log, redirect_stdout(log), redirect_stderr(log):
        try:
            report = _run_official_harness(benchmark_row, combined_patch, run_id, run_root)
        except Exception:
            traceback.print_exc(file=log)
            raise
    result = _standardize_report(report, benchmark_row)
    failure_diagnostics = _extract_pytest_failure_diagnostics(run_root / "test_output.txt")
    if failure_diagnostics:
        result["failure_diagnostics"] = failure_diagnostics
    result.update({
        "wall_time_seconds": round(time.monotonic() - started, 3),
        "harness_log_sha256": _sha256_file(log_path),
        "candidate_patch_sha256": hashlib.sha256(candidate_patch.encode("utf-8")).hexdigest(),
        "candidate_patch_bytes": len(candidate_patch.encode("utf-8")),
        "official_image": benchmark_row["image"],
        "dataset_revision": benchmark_row["dataset_revision"],
    })
    return result


def _extract_pytest_failure_diagnostics(path: Path, *, max_chars: int = 6_000) -> dict[str, Any]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    marker = "=================================== FAILURES ==================================="
    start = text.rfind(marker)
    if start < 0:
        return {}
    selected: list[str] = []
    for line in text[start + len(marker):].splitlines():
        stripped = line.strip()
        if stripped.startswith(("warnings summary", "short test summary info", "PASSES", "Summary of Failures")):
            break
        if (
            stripped.startswith("E ")
            or stripped.startswith("E\t")
            or stripped.startswith("assert ")
            or (stripped.startswith("tests/") and ": in " in stripped)
        ):
            selected.append(line.rstrip())
    diagnostic = redact_sensitive_text("\n".join(selected)).strip()
    if not diagnostic:
        return {}
    truncated = len(diagnostic) > int(max_chars)
    return {
        "schema": "hermes_phase4g8_pytest_failure_diagnostics_v1",
        "text": diagnostic[: int(max_chars)],
        "truncated": truncated,
        "source_sha256": hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest(),
    }


def collect_candidate_patch(workspace: Path, base_commit: str) -> str:
    """Create a binary patch including untracked files without changing the workspace."""

    head = _run_git(workspace, ["rev-parse", "HEAD"]).strip()
    if head != base_commit:
        raise ValueError(f"candidate HEAD mismatch: expected {base_commit}, got {head}")
    tracked = _run_git(workspace, ["diff", "--binary", "--no-ext-diff", base_commit])
    untracked_raw = _run_git(workspace, ["ls-files", "--others", "--exclude-standard", "-z"])
    parts = [tracked] if tracked else []
    for relative in [value for value in untracked_raw.split("\0") if value]:
        completed = subprocess.run(
            ["git", "diff", "--binary", "--no-index", "--", "/dev/null", relative],
            cwd=workspace,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode not in {0, 1}:
            raise RuntimeError(f"could not encode untracked file {relative}")
        if completed.stdout:
            parts.append(completed.stdout)
    return "\n".join(value.rstrip("\n") for value in parts if value) + ("\n" if parts else "")


def _run_official_harness(
    instance: dict[str, Any],
    combined_patch: str,
    run_id: str,
    run_root: Path,
) -> dict[str, Any]:
    try:
        _load_swebench_as_namespace()
        import docker
        from swebench.harness.constants import (
            DOCKER_PATCH,
            DOCKER_USER,
            DOCKER_WORKDIR,
            KEY_INSTANCE_ID,
            KEY_MODEL,
            KEY_PREDICTION,
            MAP_REPO_VERSION_TO_SPECS,
        )
        from swebench.harness.docker_utils import copy_to_container, exec_run_with_timeout
        from swebench.harness.grading import get_eval_report
        from swebench.harness.test_spec import test_spec as test_spec_module
    except ImportError as exc:
        raise RuntimeError("official SWE-EVO harness dependencies are unavailable") from exc

    client = docker.from_env()
    source_image = instance["image"]
    try:
        client.images.get(source_image)
    except docker.errors.ImageNotFound:
        client.images.pull(source_image)
    # The official instance image already contains the repository and environment.
    # Avoid unrelated network fetches used only to synthesize image build scripts.
    test_spec_module.make_repo_script_list = lambda *_args, **_kwargs: []
    test_spec_module.make_env_script_list = lambda *_args, **_kwargs: []
    test_spec = test_spec_module.make_test_spec(instance, namespace=None)
    container = client.containers.create(
        source_image,
        command=["tail", "-f", "/dev/null"],
        name=f"{run_id}-{hashlib.sha256(instance['instance_id'].encode()).hexdigest()[:8]}",
        platform="linux/amd64",
        user=DOCKER_USER,
        environment={
            key: os.environ[key]
            for key in ("PIP_INDEX_URL", "PIP_TRUSTED_HOST")
            if os.environ.get(key)
        },
    )
    patch_path = run_root / "combined.patch"
    eval_path = run_root / "eval.sh"
    test_output_path = run_root / "test_output.txt"
    report_path = run_root / "official-report.json"
    _write_text(patch_path, combined_patch, mode=0o600)
    install_command = MAP_REPO_VERSION_TO_SPECS[instance["repo"]][instance["version"]].get("install")
    eval_script = (
        test_spec.eval_script
        if instance["dependency_mode"] == "official_install"
        else _locked_image_eval_script(test_spec.eval_script, install_command)
    )
    _write_text(eval_path, eval_script, mode=0o600)
    prediction = {
        KEY_INSTANCE_ID: instance["instance_id"],
        KEY_MODEL: "hermes-phase4g8",
        KEY_PREDICTION: combined_patch,
    }
    try:
        container.start()
        reset = container.exec_run(
            ["bash", "-lc", f"git reset --hard {instance['base_commit']} && git clean -fdx"],
            workdir=DOCKER_WORKDIR,
            user=DOCKER_USER,
        )
        if reset.exit_code != 0:
            raise RuntimeError("official evaluator could not reset the testbed to base commit")
        if combined_patch:
            copy_to_container(container, patch_path, Path(DOCKER_PATCH))
            applied = container.exec_run(
                f"git apply --binary --verbose {DOCKER_PATCH}",
                workdir=DOCKER_WORKDIR,
                user=DOCKER_USER,
            )
            if applied.exit_code != 0:
                raise RuntimeError("candidate and hidden test patch did not apply")
        copy_to_container(container, eval_path, Path("/eval.sh"))
        test_output, timed_out, _runtime = exec_run_with_timeout(container, "/bin/bash /eval.sh", 7200)
        _write_text(test_output_path, test_output, mode=0o600)
        if timed_out:
            raise RuntimeError("official evaluator timed out")
        report = get_eval_report(test_spec, prediction, str(test_output_path), include_tests_status=True)
        instance_report = report[instance["instance_id"]]
        _write_json(report_path, instance_report, mode=0o600)
        return instance_report
    finally:
        try:
            container.remove(force=True)
        except Exception:
            pass


def _load_swebench_as_namespace() -> None:
    """Load harness submodules without importing SWE-bench's unrelated CLI surface."""

    if "swebench" in sys.modules:
        return
    spec = importlib.util.find_spec("swebench")
    locations = list(spec.submodule_search_locations or []) if spec is not None else []
    if not locations:
        raise ImportError("swebench package is not installed")
    package = types.ModuleType("swebench")
    package.__path__ = locations
    package.__package__ = "swebench"
    package.__version__ = "4.0.5"
    sys.modules["swebench"] = package
    root = Path(locations[0])
    for name, path in (
        ("swebench.harness", root / "harness"),
        ("swebench.harness.test_spec", root / "harness" / "test_spec"),
    ):
        namespace = types.ModuleType(name)
        namespace.__path__ = [str(path)]
        namespace.__package__ = name
        sys.modules[name] = namespace


def _standardize_report(report: dict[str, Any], instance: dict[str, Any]) -> dict[str, Any]:
    tests = report.get("tests_status") if isinstance(report.get("tests_status"), dict) else {}
    fail = tests.get("FAIL_TO_PASS") if isinstance(tests.get("FAIL_TO_PASS"), dict) else {}
    passed = tests.get("PASS_TO_PASS") if isinstance(tests.get("PASS_TO_PASS"), dict) else {}
    fail_success = list(fail.get("success") or [])
    fail_failure = list(fail["failure"]) if "failure" in fail else list(instance["FAIL_TO_PASS"])
    pass_success = list(passed.get("success") or [])
    pass_failure = list(passed["failure"]) if "failure" in passed else list(instance["PASS_TO_PASS"])
    return {
        "schema": phase4g8.EVALUATOR_RESULT_SCHEMA,
        "resolved": report.get("resolved") is True,
        "patch_applied": report.get("patch_successfully_applied") is True,
        "fail_to_pass": {
            "passed": len(fail_success),
            "failed": len(fail_failure),
            "total": len(fail_success) + len(fail_failure),
            "failed_tests": fail_failure[:20],
            "failed_tests_truncated": max(0, len(fail_failure) - 20),
        },
        "pass_to_pass": {
            "passed": len(pass_success),
            "failed": len(pass_failure),
            "total": len(pass_success) + len(pass_failure),
            "failed_tests": pass_failure[:20],
            "failed_tests_truncated": max(0, len(pass_failure) - 20),
        },
    }


def _locked_image_eval_script(eval_script: str, install_command: Any) -> str:
    """Keep official tests/parser while preventing dependency drift outside the image."""

    command = str(install_command or "").strip()
    if not command:
        return eval_script
    lines = eval_script.splitlines()
    removed = False
    output: list[str] = []
    for line in lines:
        if line.strip() == command:
            removed = True
            continue
        output.append(line)
    if not removed:
        raise RuntimeError("official evaluator install command could not be isolated")
    return "\n".join(output) + "\n"


def _evaluator_instance(
    row: dict[str, Any],
    test_patch_path: Path,
    *,
    dependency_mode: str,
) -> dict[str, Any]:
    keys = (
        "repo",
        "instance_id",
        "base_commit",
        "problem_statement",
        "FAIL_TO_PASS",
        "PASS_TO_PASS",
        "environment_setup_commit",
        "start_version",
        "end_version",
        "end_version_commit",
        "image",
        "version",
        "test_cmds",
        "log_parser",
    )
    payload = {key: row[key] for key in keys}
    payload.update({
        "schema": SWE_EVO_ADAPTER_SCHEMA,
        "dataset_revision": SWE_EVO_DATASET_REVISION,
        "test_patch_path": str(test_patch_path),
        "dependency_mode": dependency_mode,
    })
    return payload


def _validate_dataset_row(row: dict[str, Any]) -> None:
    required_strings = (
        "repo", "instance_id", "base_commit", "patch", "problem_statement",
        "environment_setup_commit", "image", "start_version", "end_version", "version",
    )
    for key in required_strings:
        if not isinstance(row.get(key), str) or not row[key].strip():
            raise ValueError(f"SWE-EVO row requires non-empty {key}")
    if not isinstance(row.get("test_patch"), str):
        raise ValueError("SWE-EVO row requires string test_patch")
    for key in ("FAIL_TO_PASS", "PASS_TO_PASS"):
        if not isinstance(row.get(key), list) or not row[key] or any(not isinstance(item, str) for item in row[key]):
            raise ValueError(f"SWE-EVO row requires non-empty {key}")


def _validate_evaluator_instance(instance: dict[str, Any]) -> None:
    if instance.get("schema") != SWE_EVO_ADAPTER_SCHEMA:
        raise ValueError("invalid SWE-EVO evaluator instance schema")
    if instance.get("dataset_revision") != SWE_EVO_DATASET_REVISION:
        raise ValueError("invalid SWE-EVO evaluator dataset revision")
    if instance.get("dependency_mode") not in {"locked_image", "official_install"}:
        raise ValueError("invalid SWE-EVO evaluator dependency mode")
    test_patch_path = Path(str(instance.get("test_patch_path") or "")).resolve()
    if not test_patch_path.is_file():
        raise ValueError("protected SWE-EVO test patch is missing")
    row = dict(instance)
    row["patch"] = "protected"
    row["test_patch"] = test_patch_path.read_text(encoding="utf-8")
    _validate_dataset_row(row)


def _merge_patches(test_patch: str, candidate_patch: str) -> str:
    parts = [value.rstrip("\n") for value in (test_patch, candidate_patch) if value]
    return "\n".join(parts) + ("\n" if parts else "")


def _run_git(workspace: Path, args: list[str]) -> str:
    completed = subprocess.run(
        ["git", "-c", f"safe.directory={workspace.resolve()}", *args],
        cwd=workspace,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed")
    return completed.stdout


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_text(path: Path, text: str, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)


def _write_json(path: Path, payload: dict[str, Any], *, mode: int) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n", mode=mode)


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m hermes_cli.phase4g8_swe_evo")
    sub = parser.add_subparsers(dest="action", required=True)
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--instance", required=True)
    evaluate.add_argument("--workspace", required=True)
    evaluate.add_argument("--output-root", required=True)
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        if args.action != "evaluate":
            raise ValueError(f"unsupported action {args.action}")
        result = evaluate_swe_evo_workspace(
            Path(args.instance).resolve(),
            Path(args.workspace).resolve(),
            Path(args.output_root).resolve(),
        )
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
