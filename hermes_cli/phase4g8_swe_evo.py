"""Official SWE-EVO dataset and evaluator adapter for Phase 4G8."""

from __future__ import annotations

import argparse
import ast
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
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
WORKER_ENVIRONMENT_SETUP_SCHEMA = "hermes_phase4g8_worker_environment_setup_v1"
ENVIRONMENT_FINGERPRINT_SCHEMA = "hermes_phase4g8_environment_fingerprint_v1"
PYTEST_FAILURE_DIAGNOSTICS_SCHEMA = "hermes_phase4g8_pytest_failure_diagnostics_v3"
EVALUATOR_DIAGNOSTIC_BATCH_SIZE = 20
EVALUATOR_TEST_OUTPUT_MARKER = ">>>>> Start Test Output"
SWE_EVO_DATASET_REVISION = "9b83d5af943ba7a17567336f5b18239f73960219"
SWE_EVO_ARROW_SHA256 = "74e7c63160ada4ceba71d5d89a9bb7c9794f4574b384458d546eb65cdb730520"
EVALUATOR_RUN_LABEL = "hermes.phase4g8.run_id"
EVALUATOR_INVOCATION_LABEL = "hermes.phase4g8.evaluator_invocation_id"
EVALUATOR_OWNER_PID_LABEL = "hermes.phase4g8.owner_pid"
SWE_EVO_OFFICIAL_INSTANCE_IDS = (
    "pydantic__pydantic_v2.6.0b1_v2.6.0",
    "dask__dask_2022.9.2_2022.10.0",
    "iterative__dvc_1.0.0a1_1.0.0a2",
)

_ENVIRONMENT_FINGERPRINT_CODE = r"""
import hashlib
import importlib.metadata
import json
import platform

packages = []
for distribution in importlib.metadata.distributions():
    location = str(distribution.locate_file(""))
    if location.startswith(("/testbed", "/workspace")):
        continue
    name = str(distribution.metadata.get("Name") or "").strip().lower().replace("_", "-")
    if name:
        packages.append([name, str(distribution.version)])
packages.sort()
payload = {
    "python_implementation": platform.python_implementation(),
    "python_version": platform.python_version(),
    "packages": packages,
}
encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
selected_names = {"dask", "distributed", "numpy", "pandas", "pyarrow", "pytest", "sqlalchemy"}
selected = {name: version for name, version in packages if name in selected_names}
print(json.dumps({
    "schema": "hermes_phase4g8_environment_fingerprint_v1",
    "sha256": hashlib.sha256(encoded).hexdigest(),
    "python_implementation": payload["python_implementation"],
    "python_version": payload["python_version"],
    "package_count": len(packages),
    "selected_packages": selected,
}, sort_keys=True))
""".strip()


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
            "worker_environment": {
                "renderer_argv": [
                    str(harness_python),
                    "-m",
                    "hermes_cli.phase4g8_swe_evo",
                    "render-worker-environment",
                    "--instance",
                    str(evaluator_instance_path),
                ],
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


def render_worker_environment_setup(instance_path: Path) -> dict[str, Any]:
    """Render the trusted evaluator setup prefix without exposing its test command."""

    instance = json.loads(instance_path.read_text(encoding="utf-8"))
    _validate_evaluator_instance(instance)
    if instance["dependency_mode"] != "locked_image":
        raise RuntimeError("worker environment rendering currently requires locked_image mode")
    test_patch_path = Path(str(instance.pop("test_patch_path"))).resolve()
    benchmark_row = dict(instance)
    benchmark_row["test_patch"] = test_patch_path.read_text(encoding="utf-8")
    _test_spec, eval_script = _make_official_test_spec(benchmark_row)
    setup_script = _worker_environment_setup_script(eval_script)
    return {
        "schema": WORKER_ENVIRONMENT_SETUP_SCHEMA,
        "official_image": benchmark_row["image"],
        "dependency_mode": benchmark_row["dependency_mode"],
        "setup_script": setup_script,
        "setup_sha256": hashlib.sha256(setup_script.encode("utf-8")).hexdigest(),
        "eval_script_sha256": hashlib.sha256(eval_script.encode("utf-8")).hexdigest(),
    }


def evaluate_swe_evo_workspace(instance_path: Path, workspace: Path, output_root: Path) -> dict[str, Any]:
    """Run the fixed SWE-EVO Docker evaluator without modifying the candidate workspace."""

    instance = json.loads(instance_path.read_text(encoding="utf-8"))
    _validate_evaluator_instance(instance)
    workspace = workspace.resolve()
    if not (workspace / ".git").exists():
        raise ValueError("candidate workspace must be a git repository")
    candidate_patch = collect_candidate_patch(workspace, instance["base_commit"])
    test_patch = Path(instance.pop("test_patch_path")).read_text(encoding="utf-8")
    protected_test_paths = _patch_paths(test_patch, workspace)
    evaluator_candidate_patch = collect_candidate_patch(
        workspace,
        instance["base_commit"],
        exclude_paths=protected_test_paths,
    )
    benchmark_row = dict(instance)
    benchmark_row["test_patch"] = test_patch
    combined_patch = _merge_patches(test_patch, evaluator_candidate_patch)
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
    environment_fingerprint = report.get("_hermes_environment_fingerprint")
    if isinstance(environment_fingerprint, dict):
        result["environment_fingerprint"] = environment_fingerprint
    failed_test_ids = [
        str(test_id)
        for section in (result.get("fail_to_pass") or {}, result.get("pass_to_pass") or {})
        for test_id in section.get("failed_tests") or []
    ]
    expected_failed_count = sum(
        int(section.get("failed") or 0)
        for section in (result.get("fail_to_pass") or {}, result.get("pass_to_pass") or {})
    )
    failure_diagnostics = _extract_pytest_failure_diagnostics(
        run_root / "test_output.txt",
        failed_test_ids=failed_test_ids,
        batch_size=EVALUATOR_DIAGNOSTIC_BATCH_SIZE,
    )
    if failure_diagnostics:
        result["failure_diagnostics"] = failure_diagnostics
    feedback_coverage = _evaluator_feedback_coverage(
        failed_test_ids,
        failure_diagnostics,
        expected_failed_count=expected_failed_count,
    )
    result["feedback_coverage"] = feedback_coverage
    result.update({
        "wall_time_seconds": round(time.monotonic() - started, 3),
        "harness_log_sha256": _sha256_file(log_path),
        "candidate_patch_sha256": hashlib.sha256(candidate_patch.encode("utf-8")).hexdigest(),
        "candidate_patch_bytes": len(candidate_patch.encode("utf-8")),
        "evaluator_candidate_patch_sha256": hashlib.sha256(
            evaluator_candidate_patch.encode("utf-8")
        ).hexdigest(),
        "protected_test_path_count": len(protected_test_paths),
        "official_image": benchmark_row["image"],
        "dataset_revision": benchmark_row["dataset_revision"],
    })
    if feedback_coverage["status"] == "extraction_incomplete":
        result["error"] = "evaluator_feedback_extraction_incomplete"
    result["raw_artifact_cleanup"] = _finalize_evaluator_artifacts(
        run_root, feedback_coverage
    )
    return result


def _extract_pytest_failure_diagnostics(
    path: Path,
    *,
    failed_test_ids: Optional[list[str]] = None,
    batch_size: int = EVALUATOR_DIAGNOSTIC_BATCH_SIZE,
    max_chars_per_case: int = 4_000,
    max_cases: Optional[int] = None,
    max_total_chars: Optional[int] = None,
) -> dict[str, Any]:
    # Legacy limits now affect organization only; they must not drop failures.
    if max_cases is not None:
        batch_size = max(1, int(max_cases))
    del max_total_chars
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    marker = "=================================== FAILURES ==================================="
    start = text.rfind(marker)
    if start < 0:
        return {}
    failure_body = text[start + len(marker):]
    sections = _pytest_failure_sections(failure_body)
    known_test_ids = [str(value) for value in (failed_test_ids or []) if str(value)]
    relevant_sections: list[tuple[str, list[str], str]] = []
    for title, lines in sections:
        test_id = _match_pytest_test_id(title, lines, known_test_ids)
        if known_test_ids and test_id not in known_test_ids:
            continue
        relevant_sections.append((title, lines, test_id))

    # Select one diagnostic for every official failed test. Repeated pytest
    # sections do not add information and must never displace another test.
    ordered_sections: list[tuple[str, list[str], str]] = []
    selected_section_indexes: set[int] = set()
    for test_id in known_test_ids:
        for index, section in enumerate(relevant_sections):
            if index not in selected_section_indexes and section[2] == test_id:
                ordered_sections.append(section)
                selected_section_indexes.add(index)
                break
    if not known_test_ids:
        ordered_sections.extend(
            section
            for index, section in enumerate(relevant_sections)
            if index not in selected_section_indexes
        )

    cases: list[dict[str, Any]] = []
    duplicate_sections_omitted = max(0, len(relevant_sections) - len(ordered_sections))
    for title, lines, _test_id in ordered_sections:
        case = _structured_pytest_failure_case(
            title,
            lines,
            known_test_ids,
            max_chars=max(256, int(max_chars_per_case)),
            protected_root=path.parent,
        )
        if case is None:
            case = _pytest_test_id_only_failure_case(_test_id)
        case.pop("_content_chars", None)
        case["batch_index"] = len(cases) // max(1, int(batch_size))
        cases.append(case)
    selected_test_ids = {str(case.get("test_id") or "") for case in cases}
    for test_id in known_test_ids:
        if test_id in selected_test_ids:
            continue
        case = _pytest_test_id_only_failure_case(test_id)
        case["batch_index"] = len(cases) // max(1, int(batch_size))
        cases.append(case)
        selected_test_ids.add(test_id)
    if not cases and not known_test_ids:
        return {}
    missing_test_ids = [
        test_id for test_id in known_test_ids if test_id not in selected_test_ids
    ]
    rendered = _render_pytest_failure_cases(cases)
    return {
        "schema": PYTEST_FAILURE_DIAGNOSTICS_SCHEMA,
        "cases": cases,
        "case_count": len(cases),
        "batch_size": max(1, int(batch_size)),
        "batch_count": (
            (len(cases) + max(1, int(batch_size)) - 1) // max(1, int(batch_size))
        ),
        "duplicate_sections_omitted_count": duplicate_sections_omitted,
        "omitted_case_count": 0,
        "missing_test_ids": missing_test_ids,
        "text": rendered,
        "detail_bounded": any(case.get("truncated") for case in cases),
        "truncated": bool(missing_test_ids),
        "source_sha256": hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest(),
    }


def _evaluator_feedback_coverage(
    failed_test_ids: list[str],
    diagnostics: dict[str, Any],
    *,
    max_cases: Optional[int] = None,
    expected_failed_count: Optional[int] = None,
) -> dict[str, Any]:
    del max_cases
    official_test_ids = list(dict.fromkeys(
        str(test_id) for test_id in failed_test_ids if str(test_id).strip()
    ))
    required_test_ids = official_test_ids
    cases = diagnostics.get("cases") if isinstance(diagnostics, dict) else []
    covered_test_ids = {
        str(case.get("test_id") or "")
        for case in cases or []
        if isinstance(case, dict)
    }
    missing_test_ids = [
        test_id for test_id in required_test_ids if test_id not in covered_test_ids
    ]
    expected_count = max(
        len(official_test_ids),
        int(expected_failed_count) if expected_failed_count is not None else 0,
    )
    unidentified_failed_test_count = max(0, expected_count - len(official_test_ids))
    if missing_test_ids or unidentified_failed_test_count:
        status = "extraction_incomplete"
    else:
        status = "current_failure_complete"
    return {
        "official_failed_test_count": len(official_test_ids),
        "required_case_count": len(required_test_ids),
        "covered_official_test_count": sum(
            test_id in covered_test_ids for test_id in official_test_ids
        ),
        "status": status,
        "missing_test_ids": missing_test_ids,
        "unidentified_failed_test_count": unidentified_failed_test_count,
        "uncovered_due_to_budget_count": 0,
    }


def _pytest_test_id_only_failure_case(test_id: str) -> dict[str, Any]:
    return {
        "test_id": str(test_id),
        "failure_kind": "test_failed",
        "detail_status": "test_id_only",
        "comparisons": [],
        "conditions": [],
        "expected": [],
        "actual": [],
        "regex": [],
        "emitted_warnings": [],
        "exception_summary": [],
        "diagnostic_excerpt": (
            "Official pytest reported this test as failed but emitted no bounded "
            "failure detail; rerun this exact test in the parity environment."
        ),
        "truncated": False,
    }


def _pytest_failure_sections(body: str) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    title: Optional[str] = None
    lines: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        summary_label = stripped.strip("=_ ")
        if summary_label.startswith((
            "warnings summary",
            "short test summary info",
            "PASSES",
            "Summary of Failures",
        )):
            break
        heading = re.match(r"^_+\s*(?P<title>.*?)\s*_+$", stripped)
        heading_title = heading.group("title").strip() if heading else ""
        if heading_title and re.search(r"[A-Za-z0-9]", heading_title):
            if title is not None:
                sections.append((title, lines))
            title = heading_title
            lines = []
            continue
        if title is not None:
            lines.append(line)
    if title is not None:
        sections.append((title, lines))
    return sections


def _structured_pytest_failure_case(
    title: str,
    lines: list[str],
    known_test_ids: list[str],
    *,
    max_chars: int,
    protected_root: Path,
) -> Optional[dict[str, Any]]:
    test_id = _match_pytest_test_id(title, lines, known_test_ids)
    safe_lines = []
    for line in lines:
        stripped = line.lstrip()
        if not re.match(r"^E(?:\s|$)", stripped):
            continue
        value = re.sub(r"^E\s*", "", stripped, count=1)
        value = _sanitize_pytest_diagnostic_value(value, protected_root=protected_root)
        if value:
            safe_lines.append(value)
    if not safe_lines:
        return None

    comparisons = [
        comparison
        for line in safe_lines
        if (comparison := _pytest_assertion_comparison(line)) is not None
    ]
    conditions = _pytest_safe_call_conditions(lines, protected_root=protected_root)
    expected: list[str] = []
    actual: list[str] = []
    regex_values: list[str] = []
    emitted_warnings: list[str] = []
    exception_summary: list[str] = []
    for line in safe_lines:
        lowered = line.lower()
        if lowered.startswith("regex:"):
            _append_unique(regex_values, line.split(":", 1)[1].strip())
        elif lowered.startswith("input:"):
            _append_unique(actual, line.split(":", 1)[1].strip())
        elif lowered.startswith(("expected:", "right:")):
            _append_unique(expected, line.split(":", 1)[1].strip())
        elif lowered.startswith(("actual:", "obtained:", "left:")):
            _append_unique(actual, line.split(":", 1)[1].strip())
        elif lowered.startswith("[right]:"):
            _append_unique(expected, line.split(":", 1)[1].strip())
        elif lowered.startswith("[left]:"):
            _append_unique(actual, line.split(":", 1)[1].strip())
        elif lowered.startswith("emitted warnings:"):
            _append_unique(emitted_warnings, line.split(":", 1)[1].strip())
        elif line.startswith("- ") and not comparisons:
            _append_unique(expected, line[2:].strip())
        elif line.startswith("+ ") and not comparisons:
            _append_unique(actual, line[2:].strip())
        if re.match(
            r"^(?:[A-Za-z_][\w.]*\.)*[A-Za-z_][\w]*(?:Error|Exception|Warning):",
            line,
        ) or line.startswith("Failed:"):
            _append_unique(exception_summary, line)

    result: dict[str, Any] = {
        "test_id": test_id,
        "failure_kind": _pytest_failure_kind(safe_lines, comparisons),
        "detail_status": "extracted",
        "comparisons": [],
        "conditions": [],
        "expected": [],
        "actual": [],
        "regex": [],
        "emitted_warnings": [],
        "exception_summary": [],
        "diagnostic_excerpt": "",
        "truncated": False,
    }
    remaining = max(256, int(max_chars))
    for comparison in comparisons:
        if remaining <= 0:
            result["truncated"] = True
            break
        bounded = {
            "operator": comparison["operator"],
            "left": comparison["left"][: min(len(comparison["left"]), remaining, 1_000)],
            "right": comparison["right"][: min(len(comparison["right"]), remaining, 1_000)],
            "required_relation": comparison["required_relation"],
        }
        result["comparisons"].append(bounded)
        remaining -= len(bounded["left"]) + len(bounded["right"]) + 32
    for condition in conditions:
        if remaining <= 0:
            result["truncated"] = True
            break
        bounded = condition[: min(len(condition), remaining, 500)]
        result["conditions"].append(bounded)
        remaining -= len(bounded)
    for key, values in (
        ("expected", expected),
        ("actual", actual),
        ("regex", regex_values),
        ("emitted_warnings", emitted_warnings),
        ("exception_summary", exception_summary),
    ):
        for value in values:
            if remaining <= 0:
                result["truncated"] = True
                break
            bounded = value[: min(len(value), remaining, 2_000)]
            result[key].append(bounded)
            remaining -= len(bounded)
            if len(bounded) < len(value):
                result["truncated"] = True
    excerpt = "\n".join(safe_lines)
    if remaining > 0:
        result["diagnostic_excerpt"] = excerpt[:remaining]
        remaining -= len(result["diagnostic_excerpt"])
    if len(result["diagnostic_excerpt"]) < len(excerpt):
        result["truncated"] = True
    result["_content_chars"] = max(256, int(max_chars)) - max(0, remaining)
    return result


def _match_pytest_test_id(
    title: str,
    lines: list[str],
    known_test_ids: list[str],
) -> str:
    normalized = re.sub(r"^ERROR at (?:setup|teardown) of ", "", title).strip()
    exact = [value for value in known_test_ids if value.rsplit("::", 1)[-1] == normalized]
    if len(exact) == 1:
        return exact[0]
    for line in lines:
        match = re.search(r"(?P<path>(?:[A-Za-z0-9_.-]+/)+test[^:\s]+\.py):\d+", line)
        if match:
            candidate = f"{match.group('path')}::{normalized}"
            if not known_test_ids or candidate in known_test_ids:
                return candidate
    return exact[0] if exact else normalized


def _pytest_assertion_comparison(line: str) -> Optional[dict[str, str]]:
    marker = "assert "
    index = line.find(marker)
    if index < 0:
        return None
    source = line[index:]
    try:
        statement = ast.parse(source).body[0]
    except (SyntaxError, ValueError):
        return None
    test = getattr(statement, "test", None)
    if not (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
    ):
        return None
    return {
        "operator": "==",
        "left": ast.unparse(test.left),
        "right": ast.unparse(test.comparators[0]),
        "required_relation": "equal",
    }


def _pytest_safe_call_conditions(lines: list[str], *, protected_root: Path) -> list[str]:
    """Extract scalar call kwargs without forwarding protected test source."""

    conditions: list[str] = []
    sensitive_key = re.compile(
        r"(?i)(?:secret|token|password|credential|api_?key|auth|url|uri|path|file)"
    )
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("E"):
            continue
        source = re.sub(r"^>\s*", "", stripped)
        if "(" not in source or len(source) > 2_000:
            continue
        parse_source = source
        if source.startswith(("with ", "async with ")) and source.endswith(":"):
            parse_source += "\n    pass"
        try:
            tree = ast.parse(parse_source)
        except (SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if not keyword.arg or sensitive_key.search(keyword.arg):
                    continue
                if not isinstance(keyword.value, ast.Constant) or not isinstance(
                    keyword.value.value, (str, int, float, bool, type(None))
                ):
                    continue
                rendered = _sanitize_pytest_diagnostic_value(
                    f"{keyword.arg}={keyword.value.value!r}",
                    protected_root=protected_root,
                )
                if rendered:
                    _append_unique(conditions, rendered)
    return conditions


def _pytest_failure_kind(
    safe_lines: list[str], comparisons: list[dict[str, str]]
) -> str:
    joined = "\n".join(safe_lines)
    if "DID NOT RAISE" in joined:
        return "expected_exception_not_raised"
    if "DID NOT WARN" in joined:
        return "expected_warning_not_emitted"
    if comparisons:
        return "assertion_comparison_failed"
    if any(
        re.match(r"^(?:[A-Za-z_][\w.]*\.)*[A-Za-z_][\w]*(?:Error|Exception):", line)
        for line in safe_lines
    ):
        return "exception_raised"
    return "test_failed"


def _sanitize_pytest_diagnostic_value(value: str, *, protected_root: Path) -> str:
    sanitized = redact_sensitive_text(str(value))
    protected_patterns = [
        re.escape(str(protected_root.resolve())),
        r"/testbed",
        r"/workspace",
    ]
    for prefix in protected_patterns:
        sanitized = re.sub(prefix + r"(?:/[^\s:'\"]+)*", "<protected-path>", sanitized)
    sanitized = re.sub(
        r"(?i)\b(?:gold|test)\.patch\b|\bhidden test (?:source|patch)\b",
        "<protected-artifact>",
        sanitized,
    )
    return sanitized.strip()


def _append_unique(values: list[str], value: str) -> None:
    selected = str(value or "").strip()
    if selected and selected not in values:
        values.append(selected)


def _render_pytest_failure_cases(cases: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for case in cases:
        lines.append(f"[{case['test_id']}]")
        lines.append(f"Failure kind: {case.get('failure_kind') or 'test_failed'}")
        for comparison in case.get("comparisons") or []:
            lines.append(
                "Failed comparison: "
                f"{comparison.get('left')} {comparison.get('operator')} "
                f"{comparison.get('right')} (required: {comparison.get('required_relation')})"
            )
        for condition in case.get("conditions") or []:
            lines.append(f"Call condition: {condition}")
        for label, key in (
            ("Expected", "expected"),
            ("Actual", "actual"),
            ("Regex", "regex"),
            ("Emitted warnings", "emitted_warnings"),
            ("Exceptions", "exception_summary"),
        ):
            for value in case.get(key) or []:
                lines.append(f"{label}: {value}")
        if case.get("diagnostic_excerpt"):
            lines.append("Diagnostics:")
            lines.append(str(case["diagnostic_excerpt"]))
    return "\n".join(lines).strip()


def _remove_completed_evaluator_artifacts(run_root: Path) -> dict[str, Any]:
    """Remove raw protected evaluator output after bounded evidence is extracted."""

    bytes_removed = 0
    try:
        for path in run_root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                bytes_removed += path.stat().st_size
        shutil.rmtree(run_root)
    except OSError as exc:
        return {
            "status": "retained_after_cleanup_error",
            "bytes_removed": 0,
            "error": type(exc).__name__,
        }
    return {
        "status": "removed_after_evidence_extraction",
        "bytes_removed": bytes_removed,
    }


def _finalize_evaluator_artifacts(
    run_root: Path,
    feedback_coverage: dict[str, Any],
) -> dict[str, Any]:
    if feedback_coverage.get("status") == "extraction_incomplete":
        return {
            "status": "retained_for_incomplete_feedback",
            "bytes_retained": _path_tree_size(run_root),
            "protected": True,
        }
    return _remove_completed_evaluator_artifacts(run_root)


def _path_tree_size(path: Path) -> int:
    total = 0
    try:
        for child in path.rglob("*"):
            if child.is_file() and not child.is_symlink():
                total += child.stat().st_size
    except OSError:
        return total
    return total


def collect_candidate_patch(
    workspace: Path,
    base_commit: str,
    *,
    exclude_paths: Optional[set[str]] = None,
) -> str:
    """Create a binary patch including untracked files without changing the workspace."""

    excluded = {str(path) for path in (exclude_paths or set())}
    head = _run_git(workspace, ["rev-parse", "HEAD"]).strip()
    if head != base_commit:
        raise ValueError(f"candidate HEAD mismatch: expected {base_commit}, got {head}")
    tracked_args = ["diff", "--binary", "--no-ext-diff", base_commit]
    if excluded:
        tracked_args.extend(["--", "."])
        tracked_args.extend(f":(exclude,literal){path}" for path in sorted(excluded))
    tracked = _run_git(workspace, tracked_args)
    untracked_raw = _run_git(workspace, ["ls-files", "--others", "--exclude-standard", "-z"])
    parts = [tracked] if tracked else []
    for relative in [value for value in untracked_raw.split("\0") if value]:
        if relative in excluded:
            continue
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


def _make_official_test_spec(instance: dict[str, Any]) -> tuple[Any, str]:
    """Build the exact official test spec and its dependency-locked eval script."""

    try:
        _load_swebench_as_namespace()
        from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
        from swebench.harness.test_spec import test_spec as test_spec_module
    except ImportError as exc:
        raise RuntimeError("official SWE-EVO harness dependencies are unavailable") from exc
    test_spec_module.make_repo_script_list = lambda *_args, **_kwargs: []
    test_spec_module.make_env_script_list = lambda *_args, **_kwargs: []
    test_spec = test_spec_module.make_test_spec(instance, namespace=None)
    install_command = MAP_REPO_VERSION_TO_SPECS[instance["repo"]][instance["version"]].get("install")
    eval_script = (
        test_spec.eval_script
        if instance["dependency_mode"] == "official_install"
        else _locked_image_eval_script(test_spec.eval_script, install_command)
    )
    return test_spec, eval_script


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
        )
        from swebench.harness.docker_utils import copy_to_container, exec_run_with_timeout
        from swebench.harness.grading import get_eval_report
    except ImportError as exc:
        raise RuntimeError("official SWE-EVO harness dependencies are unavailable") from exc

    client = docker.from_env()
    source_image = instance["image"]
    try:
        client.images.get(source_image)
    except docker.errors.ImageNotFound:
        client.images.pull(source_image)
    test_spec, eval_script = _make_official_test_spec(instance)
    eval_script = _with_pytest_diagnostic_verbosity(eval_script)
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
        labels=_evaluator_container_labels(run_id),
    )
    patch_path = run_root / "combined.patch"
    eval_path = run_root / "eval.sh"
    test_output_path = run_root / "test_output.txt"
    report_path = run_root / "official-report.json"
    _write_text(patch_path, combined_patch, mode=0o600)
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
        environment_fingerprint = _fingerprint_container_environment(container)
        expected_environment = str(
            os.environ.get("HERMES_PHASE4G8_EXPECTED_ENVIRONMENT_SHA256") or ""
        ).strip()
        if expected_environment and environment_fingerprint["sha256"] != expected_environment:
            raise RuntimeError("official evaluator environment does not match worker toolchain")
        instance_report["_hermes_environment_fingerprint"] = environment_fingerprint
        _write_json(report_path, instance_report, mode=0o600)
        return instance_report
    finally:
        try:
            container.remove(force=True)
        except Exception:
            pass


def cleanup_phase4g8_evaluator_containers(
    run_id: str,
    *,
    include_active: bool = False,
    client: Any = None,
) -> dict[str, Any]:
    """Remove evaluator containers whose owning evaluator process no longer exists."""

    selected_run_id = str(run_id or "").strip()
    if not selected_run_id:
        raise ValueError("run_id is required for evaluator container cleanup")
    if client is None:
        return _cleanup_phase4g8_evaluator_containers_cli(
            selected_run_id,
            include_active=include_active,
        )
    containers = client.containers.list(
        all=True,
        filters={"label": f"{EVALUATOR_RUN_LABEL}={selected_run_id}"},
    )
    removed: list[str] = []
    retained: list[str] = []
    errors: list[str] = []
    for container in containers:
        labels = getattr(container, "labels", None)
        if not isinstance(labels, dict):
            attrs = getattr(container, "attrs", {})
            labels = ((attrs.get("Config") or {}).get("Labels") or {}) if isinstance(attrs, dict) else {}
        owner_pid = _positive_int(labels.get(EVALUATOR_OWNER_PID_LABEL))
        container_id = str(getattr(container, "id", "unknown"))
        if not include_active and owner_pid is not None and _pid_exists(owner_pid):
            retained.append(container_id)
            continue
        try:
            container.remove(force=True)
            removed.append(container_id)
        except Exception as exc:
            errors.append(f"{container_id}:{type(exc).__name__}")
    return {
        "run_id": selected_run_id,
        "removed": removed,
        "retained": retained,
        "errors": errors,
    }


def _evaluator_container_labels(evaluator_invocation_id: str) -> dict[str, str]:
    outer_run_id = str(os.environ.get("HERMES_PHASE4G8_RUN_ID") or evaluator_invocation_id)
    return {
        EVALUATOR_RUN_LABEL: outer_run_id,
        EVALUATOR_INVOCATION_LABEL: str(evaluator_invocation_id),
        EVALUATOR_OWNER_PID_LABEL: str(os.getpid()),
    }


def _cleanup_phase4g8_evaluator_containers_cli(
    run_id: str,
    *,
    include_active: bool,
) -> dict[str, Any]:
    listed = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label={EVALUATOR_RUN_LABEL}={run_id}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if listed.returncode != 0:
        raise RuntimeError("could not list Phase 4G8 evaluator containers")
    removed: list[str] = []
    retained: list[str] = []
    errors: list[str] = []
    for container_id in [line.strip() for line in listed.stdout.splitlines() if line.strip()]:
        inspected = subprocess.run(
            [
                "docker", "inspect", "--format",
                f'{{{{ index .Config.Labels "{EVALUATOR_OWNER_PID_LABEL}" }}}}',
                container_id,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        owner_pid = _positive_int(inspected.stdout.strip()) if inspected.returncode == 0 else None
        if not include_active and owner_pid is not None and _pid_exists(owner_pid):
            retained.append(container_id)
            continue
        removed_process = subprocess.run(
            ["docker", "rm", "-f", container_id],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if removed_process.returncode == 0:
            removed.append(container_id)
        else:
            errors.append(f"{container_id}:docker_rm_failed")
    return {
        "run_id": run_id,
        "removed": removed,
        "retained": retained,
        "errors": errors,
    }


def fingerprint_python_environment(python: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [str(python), "-c", _ENVIRONMENT_FINGERPRINT_CODE],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        cwd="/",
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError("worker environment fingerprint command failed")
    return _parse_environment_fingerprint(completed.stdout)


def environment_fingerprint_code() -> str:
    return _ENVIRONMENT_FINGERPRINT_CODE


def parse_environment_fingerprint(text: str) -> dict[str, Any]:
    return _parse_environment_fingerprint(text)


def _fingerprint_container_environment(container: Any) -> dict[str, Any]:
    completed = container.exec_run(
        ["/opt/miniconda3/envs/testbed/bin/python", "-c", _ENVIRONMENT_FINGERPRINT_CODE],
        workdir="/",
    )
    if int(completed.exit_code) != 0:
        raise RuntimeError("official evaluator environment fingerprint command failed")
    output = (
        completed.output.decode("utf-8", errors="replace")
        if isinstance(completed.output, bytes)
        else str(completed.output)
    )
    return _parse_environment_fingerprint(output)


def _parse_environment_fingerprint(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("environment fingerprint output is not valid JSON") from exc
    if payload.get("schema") != ENVIRONMENT_FINGERPRINT_SCHEMA:
        raise RuntimeError("environment fingerprint schema is invalid")
    if len(str(payload.get("sha256") or "")) != 64:
        raise RuntimeError("environment fingerprint SHA-256 is invalid")
    if not isinstance(payload.get("selected_packages"), dict):
        raise RuntimeError("environment fingerprint selected_packages is invalid")
    return payload


def _positive_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
            "failed_tests": fail_failure,
            "failed_tests_truncated": 0,
        },
        "pass_to_pass": {
            "passed": len(pass_success),
            "failed": len(pass_failure),
            "total": len(pass_success) + len(pass_failure),
            "failed_tests": pass_failure,
            "failed_tests_truncated": 0,
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


def _worker_environment_setup_script(eval_script: str) -> str:
    """Return the official eval prefix that mutates dependencies before tests start."""

    lines = eval_script.splitlines()
    marker_indexes = [
        index for index, line in enumerate(lines) if EVALUATOR_TEST_OUTPUT_MARKER in line
    ]
    if len(marker_indexes) != 1:
        raise RuntimeError("official evaluator script must contain one test output marker")
    setup_lines = lines[: marker_indexes[0]]
    if not setup_lines or not setup_lines[0].startswith("#!"):
        raise RuntimeError("official evaluator setup prefix must retain its shell interpreter")
    setup_script = "\n".join(setup_lines).rstrip() + "\n"
    if EVALUATOR_TEST_OUTPUT_MARKER in setup_script:
        raise RuntimeError("worker environment setup unexpectedly contains the test marker")
    return setup_script


def _with_pytest_diagnostic_verbosity(eval_script: str) -> str:
    """Increase pytest diff detail without changing the protected test command."""

    if not re.search(r"(?:^|[;&|\s])(?:python\s+-m\s+)?pytest(?:\s|$)", eval_script):
        return eval_script
    lines = eval_script.splitlines()
    verbosity = 'export PYTEST_ADDOPTS="${PYTEST_ADDOPTS:-} -vv"'
    insert_at = 1 if lines and lines[0].startswith("#!") else 0
    lines.insert(insert_at, verbosity)
    return "\n".join(lines) + ("\n" if eval_script.endswith("\n") else "")


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
    parts = [value.rstrip("\n") for value in (candidate_patch, test_patch) if value]
    return "\n".join(parts) + ("\n" if parts else "")


def _patch_paths(patch: str, workspace: Path) -> set[str]:
    if not patch:
        return set()
    completed = subprocess.run(
        ["git", "apply", "--numstat", "-z", "-"],
        cwd=workspace,
        input=patch.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("protected test patch paths could not be parsed")
    paths: set[str] = set()
    for record in completed.stdout.split(b"\0"):
        if not record:
            continue
        fields = record.split(b"\t", 2)
        if len(fields) != 3:
            raise RuntimeError("protected test patch numstat is malformed")
        paths.add(fields[2].decode("utf-8", errors="surrogateescape"))
    return paths


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
    render = sub.add_parser("render-worker-environment")
    render.add_argument("--instance", required=True)
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        if args.action == "evaluate":
            result = evaluate_swe_evo_workspace(
                Path(args.instance).resolve(),
                Path(args.workspace).resolve(),
                Path(args.output_root).resolve(),
            )
        elif args.action == "render-worker-environment":
            result = render_worker_environment_setup(Path(args.instance).resolve())
        else:
            raise ValueError(f"unsupported action {args.action}")
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
