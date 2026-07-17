from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_decision as rd
from hermes_cli import kanban_runtime_kernel as rk
from hermes_cli import kanban_runtime_phase4g8 as p4g8
from hermes_cli import kanban_runtime_phase4g8_run as p4g8_run
from hermes_cli import phase4g8_swe_evo as swe_evo
from hermes_cli import phase4g8_capability_trace as capability_trace
from hermes_cli import phase4g8_evaluator
from hermes_cli.worker_lanes import clear_worker_lanes, get_worker_lane, register_worker_lane


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _run(*argv: str, cwd: Path) -> str:
    completed = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, check=True)
    return completed.stdout.strip()


def _qualified_fixture(tmp_path: Path) -> tuple[dict, str]:
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    _run("git", "init", "--quiet", cwd=mirror)
    _run("git", "config", "user.email", "phase4g8@example.invalid", cwd=mirror)
    _run("git", "config", "user.name", "Phase4G8 Test", cwd=mirror)
    (mirror / "result.txt").write_text("base\n", encoding="utf-8")
    _run("git", "add", "result.txt", cwd=mirror)
    _run("git", "commit", "--quiet", "-m", "base", cwd=mirror)
    base_commit = _run("git", "rev-parse", "HEAD", cwd=mirror)
    (mirror / "result.txt").write_text("gold\n", encoding="utf-8")
    gold_patch = tmp_path / "protected-answer.patch"
    gold_patch.write_text(_run("git", "diff", "--binary", "HEAD", cwd=mirror) + "\n", encoding="utf-8")
    _run("git", "restore", "result.txt", cwd=mirror)

    evaluator = tmp_path / "evaluator.py"
    evaluator.write_text(
        "import argparse, json, pathlib\n"
        "p=argparse.ArgumentParser(); p.add_argument('--workspace'); a=p.parse_args()\n"
        "gold=(pathlib.Path(a.workspace)/'result.txt').read_text().strip() == 'gold'\n"
        "print(json.dumps({'schema':'hermes_phase4g8_evaluator_result_v1','resolved':gold,"
        "'fail_to_pass':{'passed':1 if gold else 0,'failed':0 if gold else 1,'total':1},"
        "'pass_to_pass':{'passed':2,'failed':0,'total':2}}))\n",
        encoding="utf-8",
    )
    spec = {
        "schema": p4g8.QUALIFICATION_SPEC_SCHEMA,
        "instance_id": "toy-evolution",
        "dataset_revision": "fixture-v1",
        "repository": "fixture/toy",
        "base_commit": base_commit,
        "srs": "Change result.txt from base to gold.",
        "public_requirements": ["result.txt contains gold"],
        "source": {"local_mirror": str(mirror)},
        "gold": {"patch_path": str(gold_patch)},
        "evaluator": {
            "argv": [sys.executable, str(evaluator), "--workspace", "{workspace}"],
            "timeout_seconds": 30,
        },
    }
    return spec, gold_patch.read_text(encoding="utf-8")


def _swe_evo_row(base_commit: str) -> dict:
    return {
        "repo": "fixture/toy",
        "instance_id": "fixture__toy_1.0_2.0",
        "base_commit": base_commit,
        "patch": "diff --git a/result.txt b/result.txt\n",
        "test_patch": "diff --git a/test_result.py b/test_result.py\n",
        "problem_statement": "Evolve the fixture from 1.0 to 2.0.",
        "FAIL_TO_PASS": ["test_result.py::test_new_result"],
        "PASS_TO_PASS": ["test_result.py::test_existing_result"],
        "environment_setup_commit": base_commit,
        "end_version_commit": base_commit,
        "image": "example.invalid/swe-evo-fixture:latest",
        "start_version": "1.0",
        "end_version": "2.0",
        "version": "1.0",
        "test_cmds": "pytest -rA",
        "log_parser": "parse_log_pytest",
    }


def _insert_running_phase4g8_worker(conn, *, started_at: int, worker_pid: int | None = None):
    root = kb.create_task(conn, title="phase4g8 worker root", initial_status="running")
    job_id = rk.create_runtime_job(
        conn,
        root,
        "phase4g8 worker lifecycle",
        initialization_mode="provider_first",
    )
    task_id = kb.create_task(
        conn,
        title="phase4g8 codex worker",
        assignee="phase4g8-codex",
        initial_status="running",
        tenant=f"runtime:{job_id}",
    )
    now = int(started_at)
    conn.execute(
        """
        INSERT INTO execution_nodes (
            id, job_id, node_key, node_type, state, title, description, assignee,
            latest_task_id, assumptions_json, constraints_json, metadata_json, created_at, updated_at
        ) VALUES ('rnode_phase4g8_worker', ?, 'phase4g8-worker', 'implementation', 'running',
                  'worker', 'worker', 'phase4g8-codex', ?, '{}', '{}', '{}', ?, ?)
        """,
        (job_id, task_id, now, now),
    )
    cursor = conn.execute(
        """
        INSERT INTO task_runs (task_id, profile, status, worker_pid, started_at)
        VALUES (?, 'phase4g8-codex', 'running', ?, ?)
        """,
        (task_id, worker_pid, now),
    )
    run_id = int(cursor.lastrowid)
    conn.execute("UPDATE tasks SET worker_pid = ?, current_run_id = ? WHERE id = ?", (worker_pid, run_id, task_id))
    return job_id, task_id, run_id


def test_oracle_qualification_writes_protected_report_and_gold_free_manifest(tmp_path):
    spec, gold_content = _qualified_fixture(tmp_path)
    output = p4g8.run_oracle_qualification(spec, output_root=tmp_path / "phase4g8", min_free_bytes=0)

    assert output["report"]["oracle"]["status"] == "qualified"
    assert output["report"]["base_result"]["fail_to_pass"]["failed"] == 1
    assert output["report"]["gold_result"]["resolved"] is True
    locked_path = Path(output["layout"]) / "worker" / "locked-task.json"
    protected_path = Path(output["layout"]) / "protected" / "qualification-report.json"
    locked_text = locked_path.read_text(encoding="utf-8")
    locked = json.loads(locked_text)
    assert locked["schema"] == p4g8.LOCKED_MANIFEST_SCHEMA
    assert "gold" not in locked
    assert "evaluator" not in locked
    assert gold_content not in locked_text
    assert json.loads(protected_path.read_text(encoding="utf-8"))["gold_patch_sha256"]
    assert protected_path.stat().st_mode & 0o077 == 0


def test_oracle_qualification_rejects_invalid_base_oracle(tmp_path):
    spec, _ = _qualified_fixture(tmp_path)
    evaluator = Path(spec["evaluator"]["argv"][1])
    evaluator.write_text(
        "import json\n"
        "print(json.dumps({'schema':'hermes_phase4g8_evaluator_result_v1','resolved':True,"
        "'fail_to_pass':{'passed':1,'failed':0,'total':1},"
        "'pass_to_pass':{'passed':1,'failed':0,'total':1}}))\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="base_fail_to_pass_fails"):
        p4g8.run_oracle_qualification(spec, output_root=tmp_path / "phase4g8", min_free_bytes=0)


def test_oracle_validation_rejects_base_gold_environment_drift():
    base = {
        "resolved": False,
        "fail_to_pass": {"passed": 0, "failed": 1, "total": 1},
        "pass_to_pass": {"passed": 2, "failed": 0, "total": 2},
        "environment_fingerprint": {"sha256": "a" * 64},
    }
    gold = {
        "resolved": True,
        "fail_to_pass": {"passed": 1, "failed": 0, "total": 1},
        "pass_to_pass": {"passed": 2, "failed": 0, "total": 2},
        "environment_fingerprint": {"sha256": "b" * 64},
    }

    with pytest.raises(ValueError, match="base_gold_environment_match"):
        p4g8._validate_oracle_results(base, gold)


def test_swe_evo_adapter_writes_protected_inputs_and_spec(tmp_path):
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    _run("git", "init", "--quiet", cwd=mirror)
    _run("git", "config", "user.email", "phase4g8@example.invalid", cwd=mirror)
    _run("git", "config", "user.name", "Phase4G8 Test", cwd=mirror)
    (mirror / "result.txt").write_text("base\n", encoding="utf-8")
    _run("git", "add", "result.txt", cwd=mirror)
    _run("git", "commit", "--quiet", "-m", "base", cwd=mirror)
    base_commit = _run("git", "rev-parse", "HEAD", cwd=mirror)
    harness_python = Path(sys.executable).resolve()

    result = swe_evo.prepare_swe_evo_specs(
        [_swe_evo_row(base_commit)],
        output_root=tmp_path / "phase4g8",
        local_mirrors={"fixture__toy_1.0_2.0": mirror},
        harness_python=harness_python,
    )

    assert len(result) == 1
    spec_path = Path(result[0]["spec_path"])
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    protected = spec_path.parent
    assert spec["benchmark"]["adapter_schema"] == swe_evo.SWE_EVO_ADAPTER_SCHEMA
    assert Path(spec["gold"]["patch_path"]).parent == protected
    evaluator_instance = json.loads((protected / "swe-evo-instance.json").read_text(encoding="utf-8"))
    assert evaluator_instance["test_patch_path"] == str(protected / "test.patch")
    assert "patch" not in evaluator_instance
    assert spec["worker_environment"]["renderer_argv"][-2:] == [
        "--instance",
        str(protected / "swe-evo-instance.json"),
    ]
    assert spec_path.stat().st_mode & 0o077 == 0
    assert (protected / "gold.patch").stat().st_mode & 0o077 == 0
    assert (protected / "test.patch").stat().st_mode & 0o077 == 0

    (mirror / "dirty.txt").write_text("not allowed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be clean"):
        swe_evo.prepare_swe_evo_specs(
            [_swe_evo_row(base_commit)],
            output_root=tmp_path / "second-run",
            local_mirrors={"fixture__toy_1.0_2.0": mirror},
            harness_python=harness_python,
        )


def test_pytest_failure_diagnostics_are_case_bounded_and_exclude_protected_source(tmp_path):
    output = tmp_path / "test_output.txt"
    output.write_text(
        "git apply output\n+def hidden_fixture():\n+    return 'do-not-leak'\n"
        "=================================== FAILURES ===================================\n"
        "____________________________ test_index_name ____________________________\n"
        "tests/test_hidden.py:42: in test_index_name\n"
        "    assert actual == expected\n"
        "E   AssertionError: assert 'getitem' == 'from_pandas-index'\n"
        "_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _\n"
        "E   - from_pandas-index\n"
        "E   + getitem\n"
        f"E   evaluator artifact: {tmp_path}/gold.patch\n"
        "____________________________ test_warning_text ____________________________\n"
        "tests/test_hidden.py:77: in test_warning_text\n"
        "E   Failed: DID NOT WARN. No warnings of type (<class 'FutureWarning'>,) were emitted.\n"
        "E   Regex: 'must use shuffl'\n"
        "E   Input: 'Median must use a shuffle operation'\n"
        "E   Emitted warnings: [UserWarning('different warning')]\n"
        "E   /testbed/tests/test_hidden.py contains protected source\n"
        "________________________ test_shuffle_false_rejected ________________________\n"
        "tests/test_hidden.py:91: in test_shuffle_false_rejected\n"
        "    with pytest.raises(ValueError, match='must use shuffl'):\n"
        "        grouped.aggregate(spec, shuffle=False)\n"
        "E   Failed: DID NOT RAISE <class 'ValueError'>\n"
        "=========================== short test summary info ============================\n",
        encoding="utf-8",
    )

    failed_test_ids = [
        "tests/test_hidden.py::test_index_name",
        "tests/test_hidden.py::test_warning_text",
        "tests/test_hidden.py::test_shuffle_false_rejected",
    ]
    diagnostic = swe_evo._extract_pytest_failure_diagnostics(
        output,
        failed_test_ids=failed_test_ids,
        max_chars_per_case=700,
    )

    assert diagnostic["schema"] == swe_evo.PYTEST_FAILURE_DIAGNOSTICS_SCHEMA
    assert [case["test_id"] for case in diagnostic["cases"]] == failed_test_ids
    first, second, third = diagnostic["cases"]
    assert first["failure_kind"] == "assertion_comparison_failed"
    assert first["comparisons"] == [{
        "operator": "==",
        "left": "'getitem'",
        "right": "'from_pandas-index'",
        "required_relation": "equal",
    }]
    assert first["expected"] == []
    assert first["actual"] == []
    assert second["failure_kind"] == "expected_warning_not_emitted"
    assert second["regex"] == ["'must use shuffl'"]
    assert second["actual"] == ["'Median must use a shuffle operation'"]
    assert second["emitted_warnings"] == ["[UserWarning('different warning')]"]
    assert second["exception_summary"]
    assert third["failure_kind"] == "expected_exception_not_raised"
    assert third["conditions"] == ["match='must use shuffl'", "shuffle=False"]
    encoded = json.dumps(diagnostic)
    assert "assert actual == expected" not in encoded
    assert "hidden_fixture" not in encoded
    assert "do-not-leak" not in encoded
    assert str(tmp_path) not in encoded
    assert "gold.patch" not in encoded
    assert "/testbed" not in encoded


def test_pytest_failure_diagnostics_prioritize_every_official_failed_test(tmp_path):
    output = tmp_path / "test_output.txt"
    unrelated = "".join(
        f"____________________________ test_unrelated_{index} ____________________________\n"
        f"tests/test_unrelated.py:{index + 1}: in test_unrelated_{index}\n"
        f"E   AssertionError: unrelated failure {index}\n"
        for index in range(30)
    )
    cli_failures = (
        "____________________________ test_register_command_ep ____________________________\n"
        "dask/tests/test_cli.py:80: in test_register_command_ep\n"
        "E   Failed: DID NOT WARN. No warnings were emitted.\n"
        "E   Regex: 'must be instances of'\n"
        "______________________ test_repeated_name_registration_warn ______________________\n"
        "dask/tests/test_cli.py:108: in test_repeated_name_registration_warn\n"
        "E   Failed: DID NOT WARN. No warnings were emitted.\n"
        "E   Regex: 'While registering the command with name'\n"
        "________________________________ test_version _________________________________\n"
        "dask/tests/test_cli.py:20: in test_version\n"
        "E   AssertionError: assert 'dask, version 1.0' == 'cli, version 1.0'\n"
    )
    output.write_text(
        "=================================== FAILURES ===================================\n"
        + unrelated
        + cli_failures
        + "=========================== short test summary info ============================\n",
        encoding="utf-8",
    )
    failed_test_ids = [
        "dask/tests/test_cli.py::test_register_command_ep",
        "dask/tests/test_cli.py::test_repeated_name_registration_warn",
        "dask/tests/test_cli.py::test_version",
    ]

    extracted = swe_evo._extract_pytest_failure_diagnostics(
        output,
        failed_test_ids=failed_test_ids,
        max_cases=3,
    )
    projected = rk._safe_evaluator_failure_diagnostics(
        extracted,
        allowed_test_ids=set(failed_test_ids),
        policy={
            "max_diagnostic_cases": 3,
            "max_diagnostics_chars_per_case": 2500,
            "max_diagnostics_chars": 7500,
        },
    )

    assert [case["test_id"] for case in extracted["cases"]] == failed_test_ids
    assert extracted["missing_test_ids"] == []
    assert extracted["omitted_case_count"] == 0
    assert [case["test_id"] for case in projected["cases"]] == failed_test_ids
    assert projected["missing_test_ids"] == []
    encoded = json.dumps(projected)
    assert "test_unrelated" not in encoded
    assert "must be instances of" in encoded
    assert "While registering the command with name" in encoded
    assert "'dask, version 1.0'" in encoded
    assert "'cli, version 1.0'" in encoded


def test_pytest_failure_diagnostics_preserve_test_id_when_detail_is_missing(tmp_path):
    output = tmp_path / "test_output.txt"
    output.write_text(
        "=================================== FAILURES ===================================\n"
        "____________________________ test_other ____________________________\n"
        "tests/test_other.py:1: in test_other\n"
        "E   AssertionError: other\n"
        "=========================== short test summary info ============================\n",
        encoding="utf-8",
    )
    missing = "dask/tests/test_cli.py::test_version"

    diagnostic = swe_evo._extract_pytest_failure_diagnostics(
        output,
        failed_test_ids=[missing],
    )

    assert [case["test_id"] for case in diagnostic["cases"]] == [missing]
    assert diagnostic["cases"][0]["detail_status"] == "test_id_only"
    assert diagnostic["missing_test_ids"] == []
    assert diagnostic["truncated"] is False

    coverage = swe_evo._evaluator_feedback_coverage(
        [missing], diagnostic, expected_failed_count=1
    )
    assert coverage == {
        "official_failed_test_count": 1,
        "required_case_count": 1,
        "covered_official_test_count": 1,
        "status": "current_failure_complete",
        "missing_test_ids": [],
        "unidentified_failed_test_count": 0,
        "uncovered_due_to_budget_count": 0,
    }


def test_evaluator_feedback_is_incomplete_when_official_ids_are_truncated(tmp_path):
    failed_test_id = "tests/test_feature.py::test_known_failure"
    output = tmp_path / "test_output.txt"
    output.write_text(
        "=================================== FAILURES ===================================\n"
        "________________ test_known_failure ________________\n"
        "tests/test_feature.py:1: in test_known_failure\n"
        "E   AssertionError: known failure\n"
        "================ short test summary info ================\n",
        encoding="utf-8",
    )
    diagnostics = swe_evo._extract_pytest_failure_diagnostics(
        output,
        failed_test_ids=[failed_test_id],
    )

    coverage = swe_evo._evaluator_feedback_coverage(
        [failed_test_id],
        diagnostics,
        expected_failed_count=2,
    )

    assert coverage["status"] == "extraction_incomplete"
    assert coverage["missing_test_ids"] == []
    assert coverage["unidentified_failed_test_count"] == 1


def test_evaluator_feedback_coverage_requires_every_current_failure():
    failed_test_ids = [f"tests/test_feature.py::test_{index}" for index in range(4)]
    diagnostics = {
        "cases": [{"test_id": test_id} for test_id in failed_test_ids[:3]],
    }

    coverage = swe_evo._evaluator_feedback_coverage(
        failed_test_ids,
        diagnostics,
        max_cases=3,
    )

    assert coverage["status"] == "extraction_incomplete"
    assert coverage["required_case_count"] == 4
    assert coverage["covered_official_test_count"] == 3
    assert coverage["missing_test_ids"] == [failed_test_ids[-1]]
    assert coverage["unidentified_failed_test_count"] == 0
    assert coverage["uncovered_due_to_budget_count"] == 0


def test_evaluator_preserves_and_batches_more_than_twenty_current_failures(tmp_path):
    failed_test_ids = [f"tests/test_feature.py::test_{index}" for index in range(65)]
    sections = "".join(
        "________________ test_{index} ________________\n"
        "tests/test_feature.py:{line}: in test_{index}\n"
        "E   AssertionError: assert {index} == {expected}\n".format(
            index=index,
            line=index + 1,
            expected=index + 1,
        )
        for index in range(65)
    )
    output = tmp_path / "test_output.txt"
    output.write_text(
        "=================================== FAILURES ===================================\n"
        + sections
        + "================ short test summary info ================\n",
        encoding="utf-8",
    )

    diagnostics = swe_evo._extract_pytest_failure_diagnostics(
        output,
        failed_test_ids=failed_test_ids,
        batch_size=20,
    )
    coverage = swe_evo._evaluator_feedback_coverage(failed_test_ids, diagnostics)
    projected = rk._safe_evaluator_failure_diagnostics(
        diagnostics,
        allowed_test_ids=set(failed_test_ids),
        policy={
            "diagnostic_batch_size": 20,
            "max_diagnostics_chars_per_case": 4000,
        },
    )

    assert diagnostics["case_count"] == 65
    assert diagnostics["batch_count"] == 4
    assert diagnostics["missing_test_ids"] == []
    assert coverage["status"] == "current_failure_complete"
    assert coverage["covered_official_test_count"] == 65
    assert projected["case_count"] == 65
    assert projected["batch_count"] == 4
    assert projected["missing_test_ids"] == []
    assert [case["test_id"] for case in projected["cases"]] == failed_test_ids


def test_swe_evo_pytest_verbosity_preserves_protected_test_command():
    script = (
        "#!/bin/bash\n"
        "set -uxo pipefail\n"
        "pytest -rA tests/test_hidden.py::test_behavior\n"
    )

    verbose = swe_evo._with_pytest_diagnostic_verbosity(script)

    assert 'export PYTEST_ADDOPTS="${PYTEST_ADDOPTS:-} -vv"' in verbose
    assert verbose.count("pytest -rA tests/test_hidden.py::test_behavior") == 1
    assert verbose.index("PYTEST_ADDOPTS") < verbose.index("pytest -rA")
    assert swe_evo._with_pytest_diagnostic_verbosity("#!/bin/bash\nnose tests\n") == (
        "#!/bin/bash\nnose tests\n"
    )


def test_swe_evo_candidate_patch_includes_tracked_and_untracked_files(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _run("git", "init", "--quiet", cwd=workspace)
    _run("git", "config", "user.email", "phase4g8@example.invalid", cwd=workspace)
    _run("git", "config", "user.name", "Phase4G8 Test", cwd=workspace)
    (workspace / "tracked.txt").write_text("base\n", encoding="utf-8")
    _run("git", "add", "tracked.txt", cwd=workspace)
    _run("git", "commit", "--quiet", "-m", "base", cwd=workspace)
    base_commit = _run("git", "rev-parse", "HEAD", cwd=workspace)
    (workspace / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (workspace / "new.txt").write_text("new\n", encoding="utf-8")

    patch = swe_evo.collect_candidate_patch(workspace, base_commit)

    assert "tracked.txt" in patch
    assert "new.txt" in patch
    status = _run("git", "status", "--short", cwd=workspace)
    assert "tracked.txt" in status
    assert "?? new.txt" in status


def test_swe_evo_evaluator_patch_excludes_protected_test_paths(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _run("git", "init", "--quiet", cwd=workspace)
    _run("git", "config", "user.email", "phase4g8@example.invalid", cwd=workspace)
    _run("git", "config", "user.name", "Phase4G8 Test", cwd=workspace)
    (workspace / "source.py").write_text("VALUE = 'base'\n", encoding="utf-8")
    tests = workspace / "tests"
    tests.mkdir()
    (tests / "test_feature.py").write_text("def test_base(): pass\n", encoding="utf-8")
    _run("git", "add", ".", cwd=workspace)
    _run("git", "commit", "--quiet", "-m", "base", cwd=workspace)
    base_commit = _run("git", "rev-parse", "HEAD", cwd=workspace)

    (workspace / "source.py").write_text("VALUE = 'candidate'\n", encoding="utf-8")
    (tests / "test_feature.py").write_text("def test_candidate(): pass\n", encoding="utf-8")
    (tests / "test_hidden.py").write_text("def test_candidate_hidden(): pass\n", encoding="utf-8")
    hidden_patch = (
        "diff --git a/tests/test_feature.py b/tests/test_feature.py\n"
        "index 3d4a24f..8d6c38f 100644\n"
        "--- a/tests/test_feature.py\n"
        "+++ b/tests/test_feature.py\n"
        "@@ -1 +1 @@\n"
        "-def test_base(): pass\n"
        "+def test_hidden(): pass\n"
        "diff --git a/tests/test_hidden.py b/tests/test_hidden.py\n"
        "new file mode 100644\n"
        "index 0000000..38dc9a5\n"
        "--- /dev/null\n"
        "+++ b/tests/test_hidden.py\n"
        "@@ -0,0 +1 @@\n"
        "+def test_hidden_only(): pass\n"
    )

    protected_paths = swe_evo._patch_paths(hidden_patch, workspace)
    candidate_patch = swe_evo.collect_candidate_patch(
        workspace,
        base_commit,
        exclude_paths=protected_paths,
    )
    combined = swe_evo._merge_patches(hidden_patch, candidate_patch)

    assert protected_paths == {"tests/test_feature.py", "tests/test_hidden.py"}
    assert "source.py" in candidate_patch
    assert "tests/test_feature.py" not in candidate_patch
    assert "tests/test_hidden.py" not in candidate_patch
    assert combined.index("source.py") < combined.index("tests/test_feature.py")


def test_swe_evo_evaluator_container_cleanup_removes_only_orphans(monkeypatch):
    class FakeContainer:
        def __init__(self, container_id, owner_pid):
            self.id = container_id
            self.labels = {
                swe_evo.EVALUATOR_RUN_LABEL: "phase4g8-medium-test",
                swe_evo.EVALUATOR_OWNER_PID_LABEL: str(owner_pid),
            }
            self.removed = False

        def remove(self, *, force):
            assert force is True
            self.removed = True

    active = FakeContainer("active", 101)
    orphan = FakeContainer("orphan", 202)

    class FakeContainers:
        def list(self, *, all, filters):
            assert all is True
            assert filters == {
                "label": f"{swe_evo.EVALUATOR_RUN_LABEL}=phase4g8-medium-test"
            }
            return [active, orphan]

    class FakeClient:
        containers = FakeContainers()

    monkeypatch.setattr(swe_evo, "_pid_exists", lambda pid: pid == 101)

    result = swe_evo.cleanup_phase4g8_evaluator_containers(
        "phase4g8-medium-test",
        client=FakeClient(),
    )

    assert result == {
        "run_id": "phase4g8-medium-test",
        "removed": ["orphan"],
        "retained": ["active"],
        "errors": [],
    }
    assert active.removed is False
    assert orphan.removed is True


def test_swe_evo_evaluator_container_labels_preserve_outer_run(monkeypatch):
    monkeypatch.setenv("HERMES_PHASE4G8_RUN_ID", "phase4g8-medium-outer")

    labels = swe_evo._evaluator_container_labels("phase4g8-internal-evaluator")

    assert labels == {
        swe_evo.EVALUATOR_RUN_LABEL: "phase4g8-medium-outer",
        swe_evo.EVALUATOR_INVOCATION_LABEL: "phase4g8-internal-evaluator",
        swe_evo.EVALUATOR_OWNER_PID_LABEL: str(os.getpid()),
    }


def test_swe_evo_locked_image_script_removes_only_install_command():
    script = "#!/bin/bash\nconda activate testbed\npython -m pip install -e .\npytest -rA\n"

    locked = swe_evo._locked_image_eval_script(script, "python -m pip install -e .")

    assert "pip install" not in locked
    assert "conda activate testbed" in locked
    assert "pytest -rA" in locked


def test_swe_evo_worker_environment_setup_keeps_hotfix_and_excludes_test_command():
    script = (
        "#!/bin/bash\n"
        "set -uxo pipefail\n"
        "conda activate testbed\n"
        "pip install 'pandas<2.0'\n"
        ": '>>>>> Start Test Output'\n"
        "pytest -rA tests/test_hidden.py\n"
        ": '>>>>> End Test Output'\n"
    )

    setup = swe_evo._worker_environment_setup_script(script)

    assert "pip install 'pandas<2.0'" in setup
    assert "pytest" not in setup
    assert "test_hidden.py" not in setup
    assert "Start Test Output" not in setup


def test_worker_toolchain_cache_identity_includes_image_setup_and_environment():
    image_identity = {
        "content_identity": "sha256:image-a",
        "image_id": "sha256:image-a",
        "repo_digests": [],
    }

    baseline, baseline_env = p4g8_run._worker_toolchain_cache_identity(
        "example/image:tag",
        image_identity,
        "a" * 64,
        {"PIP_INDEX_URL": "https://index-a.invalid/simple"},
    )
    changed_setup, _ = p4g8_run._worker_toolchain_cache_identity(
        "example/image:tag",
        image_identity,
        "b" * 64,
        {"PIP_INDEX_URL": "https://index-a.invalid/simple"},
    )
    changed_env, changed_env_hash = p4g8_run._worker_toolchain_cache_identity(
        "example/image:tag",
        image_identity,
        "a" * 64,
        {"PIP_INDEX_URL": "https://index-b.invalid/simple"},
    )
    changed_image, _ = p4g8_run._worker_toolchain_cache_identity(
        "example/image:tag",
        {**image_identity, "content_identity": "sha256:image-b"},
        "a" * 64,
        {"PIP_INDEX_URL": "https://index-a.invalid/simple"},
    )
    changed_resolution, _ = p4g8_run._worker_toolchain_cache_identity(
        "example/image:tag",
        image_identity,
        "a" * 64,
        {"PIP_INDEX_URL": "https://index-a.invalid/simple"},
        "resolved-environment-b",
    )

    assert len(baseline) == 20
    assert baseline != changed_setup
    assert baseline != changed_env
    assert baseline != changed_image
    assert baseline != changed_resolution
    assert baseline_env != changed_env_hash


def test_worker_isolation_preflight_executes_toolchain_and_compares_fingerprint(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    toolchain = tmp_path / "toolchain"
    home = tmp_path / "home"
    codex_home = tmp_path / "codex-home"
    for path in (workspace, toolchain / "bin", home, codex_home):
        path.mkdir(parents=True)
    (toolchain / "bin" / "python").write_text("fixture\n", encoding="utf-8")
    fingerprint = {
        "schema": swe_evo.ENVIRONMENT_FINGERPRINT_SCHEMA,
        "sha256": "a" * 64,
        "python_implementation": "CPython",
        "python_version": "3.10.14",
        "package_count": 1,
        "selected_packages": {"pandas": "1.5.3"},
    }
    (toolchain / ".hermes-phase4g8-toolchain.json").write_text(
        json.dumps({
            "schema": p4g8_run.WORKER_TOOLCHAIN_MANIFEST_SCHEMA,
            "parity_status": "passed",
            "resolved_environment_sha256": fingerprint["sha256"],
            "environment_fingerprint": fingerprint,
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("PHASE4G8_WORKER_TOOLCHAIN", str(toolchain))
    captured = {}

    def fake_wrap(argv, *_args, **_kwargs):
        captured["argv"] = argv
        return argv

    def fake_run(argv, **_kwargs):
        captured["run_argv"] = argv
        return type("Completed", (), {
            "returncode": 0,
            "stdout": json.dumps(fingerprint),
            "stderr": "",
        })()

    monkeypatch.setattr(p4g8_run, "wrap_codex_network_argv", fake_wrap)
    monkeypatch.setattr(p4g8_run.subprocess, "run", fake_run)

    result = p4g8_run._assert_worker_filesystem_isolation(
        {
            "workspace": workspace,
            "worker_toolchain": toolchain,
        },
        namespace="h4g8-12345678",
        worker_uid=1234,
        worker_gid=1234,
        qualification_spec_path=tmp_path / "protected" / "spec.json",
        source_mirror=tmp_path / "protected" / "mirror",
    )

    assert result == fingerprint
    assert "/opt/miniconda3/envs/testbed/bin/python -c" in captured["argv"][2]


def test_swe_evo_standard_report_preserves_empty_failure_lists():
    row = _swe_evo_row("a" * 40)
    report = {
        "resolved": True,
        "patch_successfully_applied": True,
        "tests_status": {
            "FAIL_TO_PASS": {"success": row["FAIL_TO_PASS"], "failure": []},
            "PASS_TO_PASS": {"success": row["PASS_TO_PASS"], "failure": []},
        },
    }

    result = swe_evo._standardize_report(report, row)

    assert result["resolved"] is True
    assert result["fail_to_pass"] == {
        "passed": 1,
        "failed": 0,
        "total": 1,
        "failed_tests": [],
        "failed_tests_truncated": 0,
    }
    assert result["pass_to_pass"] == {
        "passed": 1,
        "failed": 0,
        "total": 1,
        "failed_tests": [],
        "failed_tests_truncated": 0,
    }


def test_swe_evo_standard_report_preserves_all_failed_test_identifiers():
    row = _swe_evo_row("a" * 40)
    failures = [f"tests/test_feature.py::test_{index}" for index in range(25)]
    row["FAIL_TO_PASS"] = failures
    report = {
        "resolved": False,
        "patch_successfully_applied": True,
        "tests_status": {
            "FAIL_TO_PASS": {"success": [], "failure": failures},
            "PASS_TO_PASS": {"success": row["PASS_TO_PASS"], "failure": []},
        },
    }

    result = swe_evo._standardize_report(report, row)

    assert result["fail_to_pass"]["failed_tests"] == failures
    assert result["fail_to_pass"]["failed_tests_truncated"] == 0


def test_phase4g8_real_case_requires_explicit_execution(tmp_path):
    with pytest.raises(ValueError, match="execute_real"):
        p4g8_run.run_phase4g8_real_case(
            qualification_spec_path=tmp_path / "missing.json",
            run_root=tmp_path / "runs",
            source_codex_home=tmp_path / "codex",
            case_size="small",
            execute_real=False,
        )


def test_phase4g8_real_case_requires_exactly_one_run_target(tmp_path):
    with pytest.raises(ValueError, match="exactly one of run_root or resume_run"):
        p4g8_run.run_phase4g8_real_case(
            qualification_spec_path=tmp_path / "missing.json",
            run_root=None,
            resume_run=None,
            source_codex_home=tmp_path / "codex",
            case_size="small",
            execute_real=True,
        )


def test_completed_evaluator_raw_artifacts_are_removed_after_extraction(tmp_path):
    run_root = tmp_path / "protected" / "evaluator-runs" / "eval-1"
    run_root.mkdir(parents=True)
    (run_root / "combined.patch").write_bytes(b"patch-data")
    (run_root / "test_output.txt").write_bytes(b"test-output")

    cleanup = swe_evo._remove_completed_evaluator_artifacts(run_root)

    assert cleanup["status"] == "removed_after_evidence_extraction"
    assert cleanup["bytes_removed"] == len(b"patch-data") + len(b"test-output")
    assert not run_root.exists()


def test_incomplete_evaluator_feedback_retains_protected_raw_artifacts(tmp_path):
    run_root = tmp_path / "protected" / "evaluator-runs" / "eval-incomplete"
    run_root.mkdir(parents=True)
    (run_root / "combined.patch").write_bytes(b"protected-patch")
    (run_root / "test_output.txt").write_bytes(b"unparsed-output")

    cleanup = swe_evo._finalize_evaluator_artifacts(
        run_root,
        {"status": "extraction_incomplete"},
    )

    assert cleanup == {
        "status": "retained_for_incomplete_feedback",
        "bytes_retained": len(b"protected-patch") + len(b"unparsed-output"),
        "protected": True,
    }
    assert (run_root / "combined.patch").is_file()
    assert (run_root / "test_output.txt").is_file()
    assert phase4g8_evaluator._is_evaluator_infrastructure_invalid({
        "feedback_coverage": {"status": "extraction_incomplete"},
    }) is True


def test_fresh_run_compacts_reported_prior_runs_but_preserves_active_runs(tmp_path):
    instance_root = tmp_path / "dask-instance"
    completed = instance_root / "phase4g8-medium-completed"
    active = instance_root / "phase4g8-medium-active"
    reports = completed / "reports"
    reports.mkdir(parents=True)
    (completed / "workspace").mkdir()
    (completed / "workspace" / "large.bin").write_bytes(b"x" * 4096)
    (completed / "hermes-home").mkdir()
    (completed / "hermes-home" / "kanban.db").write_bytes(b"db")
    (completed / "codex-homes").mkdir()
    (completed / "codex-homes" / "state.sqlite").write_bytes(b"session")
    (reports / "run-report.json").write_text(
        json.dumps({
            "schema": p4g8_run.REAL_CASE_REPORT_SCHEMA,
            "termination": {"reason": "runtime_terminal"},
            "run_report": {"classification": "runtime-correct/task-failed"},
        }),
        encoding="utf-8",
    )
    (reports / "capability-trace.md").write_text("summary\n", encoding="utf-8")
    (active / "workspace").mkdir(parents=True)
    (active / "workspace" / "keep.bin").write_bytes(b"active")

    first = p4g8_run._compact_completed_phase4g8_runs(instance_root)
    second = p4g8_run._compact_completed_phase4g8_runs(instance_root)

    assert len(first) == 1
    assert first[0]["status"] == "compacted_after_report_persisted"
    assert first[0]["bytes_removed"] >= 4096
    assert not (completed / "workspace").exists()
    assert not (completed / "hermes-home").exists()
    assert not (completed / "codex-homes").exists()
    assert (reports / "run-report.json").is_file()
    assert (reports / "capability-trace.md").is_file()
    retention = json.loads((reports / "retention.json").read_text(encoding="utf-8"))
    assert retention["preserved_entries"] == ["reports"]
    assert (active / "workspace" / "keep.bin").is_file()
    assert second == []


def test_candidate_patch_evidence_survives_completed_run_compaction(tmp_path):
    instance_root = tmp_path / "dask-instance"
    completed = instance_root / "phase4g8-medium-completed"
    workspace = completed / "workspace"
    reports = completed / "reports"
    workspace.mkdir(parents=True)
    reports.mkdir()
    _run("git", "init", "--quiet", cwd=workspace)
    _run("git", "config", "user.email", "phase4g8@example.invalid", cwd=workspace)
    _run("git", "config", "user.name", "Phase4G8 Test", cwd=workspace)
    (workspace / "tracked.txt").write_text("base\n", encoding="utf-8")
    _run("git", "add", "tracked.txt", cwd=workspace)
    _run("git", "commit", "--quiet", "-m", "base", cwd=workspace)
    base_commit = _run("git", "rev-parse", "HEAD", cwd=workspace)
    (workspace / "tracked.txt").write_text("candidate\n", encoding="utf-8")
    (workspace / "new.txt").write_text("new candidate file\n", encoding="utf-8")

    evidence = p4g8_run._archive_candidate_evidence(
        reports,
        workspace,
        base_commit=base_commit,
    )
    (reports / "run-report.json").write_text(
        json.dumps({
            "schema": p4g8_run.REAL_CASE_REPORT_SCHEMA,
            "termination": {"reason": "runtime_terminal"},
            "candidate_evidence": evidence,
        }),
        encoding="utf-8",
    )

    compacted = p4g8_run._compact_completed_phase4g8_runs(instance_root)

    patch = (reports / "candidate.patch").read_bytes()
    persisted = json.loads((reports / "candidate-evidence.json").read_text(encoding="utf-8"))
    assert compacted[0]["status"] == "compacted_after_report_persisted"
    assert not workspace.exists()
    assert b"tracked.txt" in patch
    assert b"new.txt" in patch
    assert persisted["patch_sha256"] == evidence["patch_sha256"]
    assert persisted["patch_bytes"] == len(patch)
    assert persisted["protected_oracle_included"] is False


def test_refresh_existing_node_codex_homes_preserves_session_state(tmp_path):
    seed = tmp_path / "seed"
    root = tmp_path / "nodes"
    node = root / "node-existing"
    (seed / "rules").mkdir(parents=True)
    node.mkdir(parents=True)
    (node / "rules").mkdir()
    (seed / "config.toml").write_text('base_url = "new-proxy"\n', encoding="utf-8")
    (seed / "auth.json").write_text('{"OPENAI_API_KEY":"new"}\n', encoding="utf-8")
    (seed / "rules" / "default.rules").write_text("new policy\n", encoding="utf-8")
    (node / ".execution-node").write_text("rnode_test\n", encoding="utf-8")
    (node / "config.toml").write_text('base_url = "old-proxy"\n', encoding="utf-8")
    (node / "auth.json").write_text('{"OPENAI_API_KEY":"old"}\n', encoding="utf-8")
    (node / "rules" / "default.rules").write_text("old policy\n", encoding="utf-8")
    state = node / "state_5.sqlite"
    state.write_bytes(b"durable-session-state")

    audit = p4g8_run._refresh_existing_node_codex_homes(
        {"codex_home": seed, "node_codex_homes": root},
        worker_uid=os.geteuid(),
        worker_gid=os.getegid(),
    )

    assert audit == {
        "refreshed": ["node-existing"],
        "refreshed_count": 1,
        "preserved_state_file_count": 1,
    }
    assert (node / "config.toml").read_text(encoding="utf-8") == 'base_url = "new-proxy"\n'
    assert (node / "auth.json").read_text(encoding="utf-8") == '{"OPENAI_API_KEY":"new"}\n'
    assert (node / "rules" / "default.rules").read_text(encoding="utf-8") == "new policy\n"
    assert state.read_bytes() == b"durable-session-state"


def test_reconstruct_resume_state_recovers_dead_worker_and_session(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="phase4g8 resume", initial_status="running")
        job_id = rk.create_runtime_job(
            conn,
            root,
            "resume one coherent worker",
            goal_items=[{"item_key": "result", "description": "result", "required": True}],
            initialization_mode="fixture",
        )
        rk.advance_runtime_job(conn, job_id, create_tasks=True)
        node = conn.execute(
            "SELECT * FROM execution_nodes WHERE job_id = ? ORDER BY created_at LIMIT 1",
            (job_id,),
        ).fetchone()
        materialization = conn.execute(
            "SELECT * FROM node_materializations WHERE node_id = ?",
            (node["id"],),
        ).fetchone()
        conn.execute(
            "UPDATE tasks SET status = 'running', worker_pid = 99999999, claim_lock = ? WHERE id = ?",
            (kb._claimer_id(), materialization["task_id"]),
        )
        conn.execute(
            "UPDATE node_materializations SET status = 'crashed' WHERE id = ?",
            (materialization["id"],),
        )
        now = int(time.time())
        conn.execute(
            """
            INSERT INTO backend_worker_sessions (
                id, job_id, node_id, backend_kind, backend_session_key, status,
                initial_materialization_id, latest_materialization_id,
                capability_fingerprint, node_contract_fingerprint,
                resume_count, created_at, updated_at
            ) VALUES (?, ?, ?, 'codex_cli', ?, 'active', ?, ?, 'cap', 'contract', 1, ?, ?)
            """,
            (
                "bws_resume",
                job_id,
                node["id"],
                "codex-session-resume",
                materialization["id"],
                materialization["id"],
                now,
                now,
            ),
        )

    recovered = p4g8_run._reconstruct_resume_state(job_id, case_size="medium")

    assert recovered["worker_interrupted"] is True
    assert recovered["dead_running_task_id"] == materialization["task_id"]
    assert recovered["prior_materialization_count"] == 1
    assert recovered["prior_session_resume_count"] == 1
    assert recovered["boundaries"]["daemon_restarted"] is True
    assert recovered["boundaries"]["worker_process_interrupted"] is True
    assert recovered["boundaries"]["worker_backend_session_resumed"] is True

    recovery = p4g8_run._prepare_resumed_runtime_job(job_id)

    assert recovery["detected_crashed_tasks"] == [materialization["task_id"]]
    assert recovery["resumed_nodes"] == [node["node_key"]]
    with kb.connect() as conn:
        refreshed_node = conn.execute(
            "SELECT state, latest_task_id FROM execution_nodes WHERE id = ?",
            (node["id"],),
        ).fetchone()
        refreshed_task = conn.execute(
            "SELECT status FROM tasks WHERE id = ?",
            (materialization["task_id"],),
        ).fetchone()
        refreshed_materialization = conn.execute(
            "SELECT status FROM node_materializations WHERE id = ?",
            (materialization["id"],),
        ).fetchone()
        refreshed_session = conn.execute(
            "SELECT status FROM backend_worker_sessions WHERE id = 'bws_resume'",
        ).fetchone()
    assert dict(refreshed_node) == {"state": "ready", "latest_task_id": None}
    assert refreshed_task["status"] == "blocked"
    assert refreshed_materialization["status"] == "crashed"
    assert refreshed_session["status"] == "interrupted"


def test_resume_requeues_incomplete_fixed_target_evaluator(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="phase4g8 evaluator resume", initial_status="running")
        job_id = rk.create_runtime_job(
            conn,
            root,
            "retry evaluator feedback extraction",
            initialization_mode="provider_first",
        )
        task_id = kb.create_task(
            conn,
            title="fixed target evaluator",
            assignee="phase4g8-evaluator",
            initial_status="blocked",
            tenant=f"runtime:{job_id}",
        )
        now = int(time.time())
        receipt = {
            "infrastructure_invalid": True,
            "official_evaluator_result": {
                "error": "evaluator_feedback_extraction_incomplete",
                "feedback_coverage": {"status": "extraction_incomplete"},
            },
        }
        run = conn.execute(
            """
            INSERT INTO task_runs (
                task_id, status, started_at, ended_at, outcome, metadata
            ) VALUES (?, 'done', ?, ?, 'completed', ?)
            """,
            (task_id, now, now, json.dumps(receipt)),
        )
        conn.execute(
            "UPDATE tasks SET status = 'done', current_run_id = ?, completed_at = ? WHERE id = ?",
            (int(run.lastrowid), now, task_id),
        )
        conn.execute(
            """
            INSERT INTO execution_nodes (
                id, job_id, node_key, node_type, state, title, description,
                assignee, latest_task_id, assumptions_json, constraints_json,
                metadata_json, created_at, updated_at, completed_at
            ) VALUES (
                'rnode_incomplete_evaluator', ?, 'verify-fixed-target', 'verification',
                'blocked', 'verify', 'verify fixed candidate', 'phase4g8-evaluator',
                ?, '{}', '{}', '{}', ?, ?, ?
            )
            """,
            (job_id, task_id, now, now, now),
        )
        conn.execute(
            "UPDATE runtime_jobs SET state = 'waiting_decision' WHERE id = ?",
            (job_id,),
        )

    recovery = p4g8_run._prepare_resumed_runtime_job(job_id)

    assert recovery["requeued_incomplete_evaluators"] == ["verify-fixed-target"]
    with kb.connect() as conn:
        node = conn.execute(
            "SELECT state, latest_task_id, completed_at FROM execution_nodes WHERE id = 'rnode_incomplete_evaluator'"
        ).fetchone()
        job = conn.execute("SELECT state FROM runtime_jobs WHERE id = ?", (job_id,)).fetchone()
        event = conn.execute(
            """
            SELECT 1 FROM execution_events
             WHERE job_id = ? AND event_type = 'phase4g8_incomplete_evaluator_requeued'
            """,
            (job_id,),
        ).fetchone()
    assert dict(node) == {"state": "ready", "latest_task_id": None, "completed_at": None}
    assert job["state"] == "active"
    assert event is not None


def test_resume_repairs_receipt_budget_mixed_with_prior_infra_failure(
    kanban_home,
    tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _run("git", "init", "--quiet", cwd=workspace)
    _run("git", "config", "user.email", "phase4g8@example.invalid", cwd=workspace)
    _run("git", "config", "user.name", "Phase4G8 Test", cwd=workspace)
    (workspace / "result.txt").write_text("base\n", encoding="utf-8")
    _run("git", "add", "result.txt", cwd=workspace)
    _run("git", "commit", "--quiet", "-m", "base", cwd=workspace)
    (workspace / "result.txt").write_text("candidate\n", encoding="utf-8")
    candidate_revision = p4g8_run.collect_git_evidence(str(workspace))[
        "workspace_revision"
    ]
    with kb.connect() as conn:
        root = kb.create_task(
            conn,
            title="phase4g8 mixed receipt budget",
            initial_status="running",
        )
        job_id = rk.create_runtime_job(
            conn,
            root,
            "recover a valid workspace after malformed receipt",
            goal_items=[
                {
                    "item_key": "result",
                    "description": "produce the result",
                    "required": True,
                    "verifier_required": True,
                }
            ],
            initialization_mode="fixture",
            workspace_path=str(workspace),
            runtime_metadata={"phase4g8_run_id": "phase4g8-receipt-repair"},
        )
        rk.advance_runtime_job(conn, job_id, create_tasks=True)
        node = conn.execute(
            "SELECT * FROM execution_nodes WHERE job_id = ? ORDER BY created_at LIMIT 1",
            (job_id,),
        ).fetchone()
        first = conn.execute(
            "SELECT * FROM node_materializations WHERE node_id = ?",
            (node["id"],),
        ).fetchone()
        now = int(time.time())
        conn.execute(
            "UPDATE node_materializations SET status = 'crashed', completed_at = ? WHERE id = ?",
            (now - 10, first["id"]),
        )
        conn.execute(
            "UPDATE tasks SET status = 'blocked', completed_at = ? WHERE id = ?",
            (now - 10, first["task_id"]),
        )
        second_task = kb.create_task(
            conn,
            title="receipt recovery attempt",
            assignee="phase4g8-codex",
            initial_status="blocked",
            tenant=f"runtime:{job_id}",
        )
        second_run = conn.execute(
            """
            INSERT INTO task_runs (
                task_id, status, started_at, ended_at, outcome, metadata
            ) VALUES (?, 'blocked', ?, ?, 'blocked', ?)
            """,
            (
                second_task,
                now,
                now,
                json.dumps(
                    {
                        "runtime_receipt": {
                            "schema": "runtime_worker_receipt_v1",
                            "status": "completed",
                            "workspace_revision": candidate_revision,
                        }
                    }
                ),
            ),
        )
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?",
            (int(second_run.lastrowid), second_task),
        )
        second_materialization_id = "mat_receipt_invalid"
        conn.execute(
            """
            INSERT INTO node_materializations (
                id, job_id, node_id, attempt, task_id, worker_lane, status,
                created_at, completed_at, metadata_json
            ) VALUES (?, ?, ?, 2, ?, 'phase4g8-codex', 'receipt_invalid', ?, ?, '{}')
            """,
            (
                second_materialization_id,
                job_id,
                node["id"],
                second_task,
                now,
                now,
            ),
        )
        conn.execute(
            """
            UPDATE execution_nodes
               SET state = 'failed', latest_task_id = ?,
                   output_summary = 'Runtime recovery marked node failed: receipt_invalid',
                   completed_at = ?, updated_at = ?
             WHERE id = ?
            """,
            (second_task, now, now, node["id"]),
        )
        conn.execute(
            "UPDATE runtime_jobs SET state = 'waiting_decision' WHERE id = ?",
            (job_id,),
        )
        rk._event(
            conn,
            job_id,
            "node_recovery_not_retryable",
            {
                "node_key": node["node_key"],
                "materialization_id": second_materialization_id,
                "attempt": 2,
                "task_id": second_task,
                "recovery_reason": "receipt_invalid",
                "retryable": False,
                "policy_decision": "mark_failed",
                "retry_limit": 1,
            },
            node_id=node["id"],
            task_id=second_task,
        )
        strategy_task = kb.create_task(
            conn,
            title="speculative strategy branch",
            assignee="phase4g8-codex",
            initial_status="blocked",
            tenant=f"runtime:{job_id}",
        )
        conn.execute(
            """
            INSERT INTO execution_nodes (
                id, job_id, node_key, node_type, state, title, description,
                assignee, latest_task_id, assumptions_json, constraints_json,
                metadata_json, created_at, updated_at
            ) VALUES (
                'rnode_speculative_receipt_branch', ?, 'speculative-repair',
                'strategy_update', 'running', 'speculative repair',
                'incorrectly created from receipt failure', 'phase4g8-codex',
                ?, '{}', '{}', '{}', ?, ?
            )
            """,
            (job_id, strategy_task, now + 1, now + 1),
        )
        conn.execute(
            """
            INSERT INTO node_materializations (
                id, job_id, node_id, attempt, task_id, worker_lane, status,
                created_at, metadata_json
            ) VALUES (
                'mat_speculative_receipt_branch', ?,
                'rnode_speculative_receipt_branch', 1, ?,
                'phase4g8-codex', 'running', ?, '{}'
            )
            """,
            (job_id, strategy_task, now + 1),
        )

    recovery = p4g8_run._prepare_resumed_runtime_job(job_id)

    assert recovery["requeued_receipt_recoveries"] == [node["node_key"]]
    assert recovery["receipt_branch_repair"]["repaired"] is True
    assert recovery["receipt_branch_repair"]["superseded_nodes"] == [
        "speculative-repair"
    ]
    with kb.connect() as conn:
        refreshed_node = conn.execute(
            "SELECT state, latest_task_id, completed_at FROM execution_nodes WHERE id = ?",
            (node["id"],),
        ).fetchone()
        job = conn.execute(
            "SELECT state FROM runtime_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        event = conn.execute(
            """
            SELECT payload_json FROM execution_events
             WHERE job_id = ? AND event_type = 'phase4g8_receipt_recovery_requeued'
            """,
            (job_id,),
        ).fetchone()
        strategy = conn.execute(
            """
            SELECT state, latest_task_id, output_summary
              FROM execution_nodes
             WHERE id = 'rnode_speculative_receipt_branch'
            """
        ).fetchone()
        strategy_task_status = conn.execute(
            "SELECT status FROM tasks WHERE id = ?",
            (strategy_task,),
        ).fetchone()["status"]
    assert dict(refreshed_node) == {
        "state": "ready",
        "latest_task_id": None,
        "completed_at": None,
    }
    assert job["state"] == "active"
    assert json.loads(event["payload_json"])["reason"] == (
        "recovery_budget_category_repair"
    )
    assert dict(strategy) == {
        "state": "superseded",
        "latest_task_id": None,
        "output_summary": "Superseded after receipt recovery branch repair.",
    }
    assert strategy_task_status == "archived"
    with kb.connect() as conn:
        advanced = rk.advance_runtime_job(conn, job_id, create_tasks=True)
        assert advanced.materialized_nodes == [node["node_key"]]
        retried = conn.execute(
            "SELECT body FROM tasks WHERE id = (SELECT latest_task_id FROM execution_nodes WHERE id = ?)",
            (node["id"],),
        ).fetchone()
    assert "Receipt protocol recovery" in retried["body"]
    assert "verdict=candidate_ready" in retried["body"]


def test_phase4g8_adapts_legacy_candidate_shape_without_granting_completion(
    kanban_home,
    tmp_path,
):
    workspace = tmp_path / "candidate-workspace"
    workspace.mkdir()
    _run("git", "init", "--quiet", cwd=workspace)
    _run("git", "config", "user.email", "phase4g8@example.invalid", cwd=workspace)
    _run("git", "config", "user.name", "Phase4G8 Test", cwd=workspace)
    (workspace / "result.txt").write_text("candidate\n", encoding="utf-8")
    _run("git", "add", "result.txt", cwd=workspace)
    _run("git", "commit", "--quiet", "-m", "candidate", cwd=workspace)
    revision = p4g8_run.collect_git_evidence(str(workspace))["workspace_revision"]
    with kb.connect() as conn:
        root = kb.create_task(
            conn,
            title="phase4g8 legacy candidate receipt",
            initial_status="running",
        )
        job_id = rk.create_runtime_job(
            conn,
            root,
            "adapt a candidate receipt but require independent evaluation",
            goal_items=[
                {
                    "item_key": "result",
                    "description": "produce the result",
                    "required": True,
                    "verifier_required": True,
                }
            ],
            initialization_mode="fixture",
            workspace_path=str(workspace),
            runtime_metadata={
                "phase4g8_run_id": "phase4g8-candidate-adapter",
                "verification_policy": {
                    "mode": "required_evaluator",
                    "assignee": "phase4g8-evaluator",
                    "require_workspace_revision": True,
                },
            },
        )
        rk.advance_runtime_job(conn, job_id, create_tasks=True)
        node = conn.execute(
            "SELECT * FROM execution_nodes WHERE job_id = ? ORDER BY created_at LIMIT 1",
            (job_id,),
        ).fetchone()
        kb.complete_task(
            conn,
            node["latest_task_id"],
            result="legacy candidate shape",
            summary="legacy candidate shape",
            metadata={
                "worker_lane": {"kind": "codex_cli", "name": "phase4g8-codex"},
                "runtime_receipt": {
                    "schema": "runtime_worker_receipt_v1",
                    "status": "completed",
                    "outcome": "implementation_ready",
                    "verification": [
                        {
                            "name": "unit",
                            "result": "passed",
                            "details": "12 passed",
                        },
                        {"name": "lint", "result": "passed"},
                    ],
                    "changed_files": ["result.txt"],
                    "workspace_revision": revision,
                    "independent_evaluation_run": False,
                    "structure_request": None,
                },
            },
        )

        now = int(time.time())
        conn.execute(
            """
            UPDATE node_materializations
               SET status = 'receipt_invalid', completed_at = ?
             WHERE node_id = ? AND task_id = ?
            """,
            (now, node["id"], node["latest_task_id"]),
        )
        conn.execute(
            """
            UPDATE execution_nodes
               SET state = 'failed', output_summary = 'receipt invalid',
                   completed_at = ?, updated_at = ?
             WHERE id = ?
            """,
            (now, now, node["id"]),
        )
        conn.execute(
            "UPDATE runtime_jobs SET state = 'waiting_decision' WHERE id = ?",
            (job_id,),
        )

    recovery = p4g8_run._prepare_resumed_runtime_job(job_id)

    assert recovery["adapted_candidate_receipts"] == [node["node_key"]]
    with kb.connect() as conn:
        refreshed = conn.execute(
            "SELECT state, output_summary FROM execution_nodes WHERE id = ?",
            (node["id"],),
        ).fetchone()
        job = conn.execute(
            "SELECT state FROM runtime_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        adapter_event = conn.execute(
            """
            SELECT payload_json FROM execution_events
             WHERE job_id = ? AND event_type = 'runtime_receipt_adapted'
            """,
            (job_id,),
        ).fetchone()
        ledger = conn.execute(
            "SELECT satisfaction, verification_state FROM progress_ledger WHERE node_id = ?",
            (node["id"],),
        ).fetchone()

    assert refreshed["state"] == "candidate_ready"
    assert "candidate reported ready" in refreshed["output_summary"]
    assert job["state"] != "done"
    assert dict(ledger) == {
        "satisfaction": "full",
        "verification_state": "implementation_verified",
    }
    payload = json.loads(adapter_event["payload_json"])
    assert payload["adapter"] == "phase4g8_candidate_shape_v1"
    assert payload["resulting_verdict"] == "candidate_ready"
    assert payload["independent_evaluator_still_required"] is True


def test_phase4g8_adapts_legacy_structure_request_as_blocked(
    kanban_home,
    tmp_path,
):
    workspace = tmp_path / "structure-request-workspace"
    workspace.mkdir()
    _run("git", "init", "--quiet", cwd=workspace)
    _run("git", "config", "user.email", "phase4g8@example.invalid", cwd=workspace)
    _run("git", "config", "user.name", "Phase4G8 Test", cwd=workspace)
    (workspace / "result.txt").write_text("blocked\n", encoding="utf-8")
    _run("git", "add", "result.txt", cwd=workspace)
    _run("git", "commit", "--quiet", "-m", "blocked", cwd=workspace)
    revision = p4g8_run.collect_git_evidence(str(workspace))["workspace_revision"]
    with kb.connect() as conn:
        root = kb.create_task(
            conn,
            title="phase4g8 legacy structure request",
            initial_status="running",
        )
        job_id = rk.create_runtime_job(
            conn,
            root,
            "adapt a bounded structure request",
            goal_items=[
                {
                    "item_key": "result",
                    "description": "produce the result",
                    "required": True,
                    "verifier_required": True,
                }
            ],
            initialization_mode="fixture",
            workspace_path=str(workspace),
            runtime_metadata={
                "phase4g8_run_id": "phase4g8-structure-request-adapter",
                "verification_policy": {
                    "mode": "required_evaluator",
                    "assignee": "phase4g8-evaluator",
                    "require_workspace_revision": True,
                },
            },
        )
        rk.advance_runtime_job(conn, job_id, create_tasks=True)
        node = conn.execute(
            "SELECT * FROM execution_nodes WHERE job_id = ? ORDER BY created_at LIMIT 1",
            (job_id,),
        ).fetchone()
        kb.complete_task(
            conn,
            node["latest_task_id"],
            result="legacy structure request",
            summary="legacy structure request",
            metadata={
                "worker_lane": {"kind": "codex_cli", "name": "phase4g8-codex"},
                "runtime_receipt": {
                    "schema": "runtime_worker_receipt_v1",
                    "status": "structure_request",
                    "outcome": "blocked_independent_verification",
                    "verification": [
                        {
                            "name": "focused",
                            "result": "passed",
                            "details": "30 passed",
                        },
                        {"name": "lint", "result": "passed"},
                    ],
                    "changed_files": ["result.txt"],
                    "workspace_revision": revision,
                    "independent_evaluation_run": False,
                    "structure_request": {
                        "type": "independent_verification",
                        "reason": "Evaluator diagnostics are test-id only.",
                        "failure_signature": "efsig_test",
                        "no_progress_streak": 2,
                        "requested_evidence": [
                            "One bounded assertion or traceback",
                        ],
                        "protected_source_access_requested": False,
                    },
                },
            },
        )
        now = int(time.time())
        conn.execute(
            """
            UPDATE node_materializations
               SET status = 'receipt_invalid', completed_at = ?
             WHERE node_id = ? AND task_id = ?
            """,
            (now, node["id"], node["latest_task_id"]),
        )
        conn.execute(
            """
            UPDATE execution_nodes
               SET state = 'failed', output_summary = 'receipt invalid',
                   completed_at = ?, updated_at = ?
             WHERE id = ?
            """,
            (now, now, node["id"]),
        )
        conn.execute(
            "UPDATE runtime_jobs SET state = 'waiting_decision' WHERE id = ?",
            (job_id,),
        )
        strategy_task = kb.create_task(
            conn,
            title="speculative structure-request strategy",
            assignee="phase4g8-codex",
            initial_status="blocked",
            tenant=f"runtime:{job_id}",
        )
        conn.execute(
            "UPDATE tasks SET status = 'ready' WHERE id = ?",
            (strategy_task,),
        )
        conn.execute(
            """
            INSERT INTO execution_nodes (
                id, job_id, node_key, node_type, state, title, description,
                assignee, latest_task_id, assumptions_json, constraints_json,
                metadata_json, created_at, updated_at
            ) VALUES (
                'rnode_speculative_structure_branch', ?,
                'speculative-structure-repair', 'strategy_update', 'running',
                'speculative structure repair',
                'created before the structure receipt was adapted',
                'phase4g8-codex', ?, '{}', '{}', '{}', ?, ?
            )
            """,
            (job_id, strategy_task, now + 1, now + 1),
        )

    recovery = p4g8_run._prepare_resumed_runtime_job(job_id)

    assert recovery["adapted_structure_request_receipts"] == [node["node_key"]]
    assert recovery["structure_request_branch_repair"] == {
        "repaired": True,
        "consumed": True,
        "reason": "accepted_structure_request",
        "superseded_nodes": ["speculative-structure-repair"],
    }
    with kb.connect() as conn:
        refreshed = conn.execute(
            "SELECT state, output_summary FROM execution_nodes WHERE id = ?",
            (node["id"],),
        ).fetchone()
        job = conn.execute(
            "SELECT state FROM runtime_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        adapter_event = conn.execute(
            """
            SELECT payload_json FROM execution_events
             WHERE job_id = ? AND event_type = 'runtime_receipt_adapted'
            """,
            (job_id,),
        ).fetchone()
        request_event = conn.execute(
            """
            SELECT payload_json FROM execution_events
             WHERE job_id = ? AND event_type = 'worker_structure_requested'
            """,
            (job_id,),
        ).fetchone()
        strategy = conn.execute(
            """
            SELECT state, latest_task_id, output_summary
              FROM execution_nodes
             WHERE id = 'rnode_speculative_structure_branch'
            """
        ).fetchone()

    assert refreshed["state"] == "blocked"
    assert refreshed["output_summary"] == "Evaluator diagnostics are test-id only."
    assert job["state"] == "active"
    adapter_payload = json.loads(adapter_event["payload_json"])
    assert adapter_payload["adapter"] == "phase4g8_structure_request_shape_v1"
    assert adapter_payload["resulting_verdict"] == "blocked"
    request_payload = json.loads(request_event["payload_json"])
    assert request_payload["structure_request"]["reason_type"] == (
        "independent_verification"
    )
    assert request_payload["structure_request"]["failure_signature"] == (
        "efsig_test"
    )
    assert dict(strategy) == {
        "state": "superseded",
        "latest_task_id": None,
        "output_summary": (
            "Superseded after worker structure request acceptance."
        ),
    }


def test_resume_reclaims_only_dead_phase4g8_daemon_advance_lock(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="phase4g8 dead lock", initial_status="running")
        job_id = rk.create_runtime_job(
            conn,
            root,
            "recover dead daemon lock",
            initialization_mode="provider_first",
            runtime_metadata={"phase4g8_run_id": "phase4g8-lock-test"},
        )
        conn.execute(
            """
            UPDATE runtime_jobs
               SET advance_lock = 'runtime-daemon:test-host:99999999:test-token',
                   claim_expires_at = ?
             WHERE id = ?
            """,
            (int(time.time()) + 900, job_id),
        )

    recovery = p4g8_run._prepare_resumed_runtime_job(job_id)

    assert recovery["reclaimed_dead_advance_lock"]["reclaimed"] is True
    assert recovery["reclaimed_dead_advance_lock"]["owner_pid"] == 99999999
    with kb.connect() as conn:
        job = conn.execute(
            "SELECT advance_lock, claim_expires_at FROM runtime_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        event = conn.execute(
            """
            SELECT 1 FROM execution_events
             WHERE job_id = ? AND event_type = 'phase4g8_dead_advance_lock_reclaimed'
            """,
            (job_id,),
        ).fetchone()
    assert dict(job) == {"advance_lock": None, "claim_expires_at": None}
    assert event is not None


def test_resume_preserves_live_phase4g8_daemon_advance_lock(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="phase4g8 live lock", initial_status="running")
        job_id = rk.create_runtime_job(
            conn,
            root,
            "preserve live daemon lock",
            initialization_mode="provider_first",
            runtime_metadata={"phase4g8_run_id": "phase4g8-lock-test"},
        )
        owner = f"runtime-daemon:test-host:{os.getpid()}:test-token"
        conn.execute(
            "UPDATE runtime_jobs SET advance_lock = ?, claim_expires_at = ? WHERE id = ?",
            (owner, int(time.time()) + 900, job_id),
        )

    recovery = p4g8_run._prepare_resumed_runtime_job(job_id)

    assert recovery["reclaimed_dead_advance_lock"] == {
        "reclaimed": False,
        "reason": "lock_owner_alive",
        "owner_pid": os.getpid(),
    }
    with kb.connect() as conn:
        assert conn.execute(
            "SELECT advance_lock FROM runtime_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()["advance_lock"] == owner


def test_phase4g8_real_workspace_contains_only_shallow_base_without_target_refs(tmp_path):
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    _run("git", "init", "--quiet", cwd=mirror)
    _run("git", "config", "user.email", "phase4g8@example.invalid", cwd=mirror)
    _run("git", "config", "user.name", "Phase4G8 Test", cwd=mirror)
    (mirror / "result.txt").write_text("base\n", encoding="utf-8")
    _run("git", "add", "result.txt", cwd=mirror)
    _run("git", "commit", "--quiet", "-m", "base", cwd=mirror)
    base_commit = _run("git", "rev-parse", "HEAD", cwd=mirror)
    (mirror / "result.txt").write_text("gold\n", encoding="utf-8")
    _run("git", "commit", "--quiet", "-am", "target", cwd=mirror)
    target_commit = _run("git", "rev-parse", "HEAD", cwd=mirror)
    _run("git", "tag", "target-release", cwd=mirror)
    _run("git", "checkout", "--quiet", "--detach", base_commit, cwd=mirror)

    paths = p4g8_run._prepare_real_layout(
        tmp_path / "run",
        {"source": {"local_mirror": str(mirror)}, "base_commit": base_commit},
    )
    workspace = paths["workspace"]

    safe = f"safe.directory={workspace}"
    assert _run("git", "-c", safe, "rev-parse", "HEAD", cwd=workspace) == base_commit
    assert _run("git", "-c", safe, "tag", "--list", cwd=workspace) == ""
    assert _run("git", "-c", safe, "remote", cwd=workspace) == ""
    hidden_target = subprocess.run(
        ["git", "-c", safe, "cat-file", "-e", f"{target_commit}^{{commit}}"],
        cwd=workspace,
        check=False,
    )
    assert hidden_target.returncode != 0
    from hermes_cli.codex_worker import collect_git_evidence

    evidence = collect_git_evidence(str(workspace))
    assert evidence["head_revision"] == base_commit
    assert evidence["workspace_revision"] == f"git:{base_commit}"
    assert swe_evo.collect_candidate_patch(workspace, base_commit) == ""


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() != 0 or shutil.which("setpriv") is None,
    reason="run identity isolation requires POSIX root and setpriv",
)
def test_phase4g8_real_runs_use_owner_only_distinct_worker_identities():
    test_root = Path(tempfile.mkdtemp(prefix="hermes-phase4g8-identity-", dir="/tmp"))
    try:
        mirror = test_root / "mirror"
        mirror.mkdir()
        _run("git", "init", "--quiet", cwd=mirror)
        _run("git", "config", "user.email", "phase4g8@example.invalid", cwd=mirror)
        _run("git", "config", "user.name", "Phase4G8 Test", cwd=mirror)
        (mirror / "result.txt").write_text("base\n", encoding="utf-8")
        _run("git", "add", "result.txt", cwd=mirror)
        _run("git", "commit", "--quiet", "-m", "base", cwd=mirror)
        base_commit = _run("git", "rev-parse", "HEAD", cwd=mirror)
        spec = {"source": {"local_mirror": str(mirror)}, "base_commit": base_commit}

        run_a = "phase4g8-small-identity-a"
        run_b = "phase4g8-small-identity-b"
        uid_a, gid_a = p4g8_run._derive_run_identity(run_a)
        uid_b, gid_b = p4g8_run._derive_run_identity(run_b)
        assert (uid_a, gid_a) != (uid_b, gid_b)

        os.chmod(test_root, 0o711)
        paths_a = p4g8_run._prepare_real_layout(
            test_root / run_a,
            spec,
            worker_uid=uid_a,
            worker_gid=gid_a,
        )
        paths_b = p4g8_run._prepare_real_layout(
            test_root / run_b,
            spec,
            worker_uid=uid_b,
            worker_gid=gid_b,
        )
        protected = test_root / "protected"
        protected.mkdir(mode=0o700)
        (protected / "gold.patch").write_text("secret\n", encoding="utf-8")
        os.chmod(protected / "gold.patch", 0o600)

        def as_worker(uid, gid, *argv):
            return subprocess.run(
                [
                    "setpriv",
                    f"--reuid={uid}",
                    f"--regid={gid}",
                    "--clear-groups",
                    *map(str, argv),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

        own_file = paths_a["workspace"] / "result.txt"
        old_environment = p4g8_run._install_isolated_environment(paths_a)
        try:
            assert as_worker(uid_a, gid_a, "test", "-r", own_file).returncode == 0
            assert as_worker(uid_a, gid_a, "sh", "-c", f"printf changed > {own_file}").returncode == 0
            assert own_file.read_text(encoding="utf-8") == "changed"
            assert as_worker(uid_a, gid_a, "test", "-r", paths_b["workspace"] / "result.txt").returncode != 0
            assert as_worker(uid_a, gid_a, "test", "-r", protected / "gold.patch").returncode != 0
            assert as_worker(uid_a, gid_a, "test", "-r", paths_a["db"]).returncode != 0
            assert as_worker(uid_a, gid_a, "test", "-r", mirror / ".git" / "HEAD").returncode != 0
            assert (paths_b["workspace"] / "result.txt").read_text(encoding="utf-8") == "base\n"
            assert paths_a["db"].is_file()
            assert paths_a["db"].parent == paths_a["hermes_home"]
            assert mirror.stat().st_mode & 0o077 == 0
            assert paths_a["workspace"].stat().st_mode & 0o077 == 0
            assert own_file.stat().st_mode & 0o077 == 0
            assert paths_a["service"].stat().st_mode & 0o077 == 0
        finally:
            p4g8_run._restore_environment(old_environment)
    finally:
        shutil.rmtree(test_root)


def test_fault_trigger_requires_exact_receipt_before_ingest_state(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="phase4g8 trigger", initial_status="running")
        job_id = rk.create_runtime_job(
            conn,
            root,
            "exercise exact fault trigger",
            goal_items=[{"item_key": "result", "description": "result", "required": True}],
            initialization_mode="fixture",
        )
        rk.advance_runtime_job(conn, job_id, create_tasks=True)
        node = conn.execute(
            "SELECT * FROM execution_nodes WHERE job_id = ? AND node_key = 'understand-scope'",
            (job_id,),
        ).fetchone()
        assert p4g8.evaluate_fault_trigger(conn, job_id, "worker_running")["ready"] is True
        kb.complete_task(
            conn,
            node["latest_task_id"],
            result="terminal receipt exists",
            summary="terminal receipt exists",
            metadata={
                "verdict": "succeeded",
                "summary": "terminal receipt exists",
                "claimed_goal_items": ["result"],
                "verification": {"passed": True},
            },
        )
        trigger = p4g8.evaluate_fault_trigger(conn, job_id, "receipt_before_ingest")
        assert trigger["ready"] is True
        assert trigger["facts"]["node_id"] == node["id"]
        assert rk.ingest_runtime_node_evidence(conn, node["id"])
        assert p4g8.evaluate_fault_trigger(conn, job_id, "receipt_before_ingest")["ready"] is False


def test_receipt_before_ingest_trigger_and_counts_are_materialization_scoped(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="phase4g8 retry trigger", initial_status="running")
        job_id = rk.create_runtime_job(
            conn,
            root,
            "retry the same runtime node",
            goal_items=[{"item_key": "result", "description": "result", "required": True}],
            initialization_mode="fixture",
        )
        rk.advance_runtime_job(conn, job_id, create_tasks=True)
        node = conn.execute(
            "SELECT * FROM execution_nodes WHERE job_id = ? AND node_key = 'understand-scope'",
            (job_id,),
        ).fetchone()
        kb.complete_task(
            conn,
            node["latest_task_id"],
            result="first receipt",
            summary="first receipt",
            metadata={
                "verdict": "succeeded",
                "summary": "first receipt",
                "claimed_goal_items": ["result"],
                "verification": {"passed": True},
            },
        )
        assert rk.ingest_runtime_node_evidence(conn, node["id"])
        conn.execute(
            """
            UPDATE execution_nodes
               SET state = 'ready', latest_task_id = NULL, latest_run_id = NULL,
                   completed_at = NULL
             WHERE id = ?
            """,
            (node["id"],),
        )
        retry_node = conn.execute(
            "SELECT * FROM execution_nodes WHERE id = ?",
            (node["id"],),
        ).fetchone()
        retry_task = rk.materialize_runtime_node(conn, dict(retry_node))
        retry_node = conn.execute(
            "SELECT * FROM execution_nodes WHERE id = ?",
            (node["id"],),
        ).fetchone()
        retry_materialization = conn.execute(
            "SELECT * FROM node_materializations WHERE task_id = ?",
            (retry_task,),
        ).fetchone()
        kb.complete_task(
            conn,
            retry_task,
            result="second receipt",
            summary="second receipt",
            metadata={
                "verdict": "succeeded",
                "summary": "second receipt",
                "claimed_goal_items": ["result"],
                "verification": {"passed": True},
            },
        )

        trigger = p4g8.evaluate_fault_trigger(conn, job_id, "receipt_before_ingest")
        before = p4g8.runtime_fact_counts(
            conn,
            job_id,
            node["id"],
            materialization_id=retry_materialization["id"],
        )

        assert trigger["ready"] is True
        assert trigger["facts"]["materialization_id"] == retry_materialization["id"]
        assert before == {"ledger": 0, "terminal_events": 0, "terminal_materializations": 0}
        assert rk.ingest_runtime_node_evidence(conn, retry_node["id"])
        after = p4g8.runtime_fact_counts(
            conn,
            job_id,
            node["id"],
            materialization_id=retry_materialization["id"],
        )
        assert after == {"ledger": 1, "terminal_events": 1, "terminal_materializations": 1}


def test_fault_trigger_detects_only_expired_lease(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="phase4g8 lease", initial_status="running")
        job_id = rk.create_runtime_job(conn, root, "lease trigger", initialization_mode="provider_first")
        assert rk.acquire_runtime_advance_lock(conn, job_id, owner="phase4g8-daemon", ttl_seconds=60)["acquired"]
        assert p4g8.evaluate_fault_trigger(conn, job_id, "lease_expired", now=1)["ready"] is False
        conn.execute("UPDATE runtime_jobs SET claim_expires_at = 0 WHERE id = ?", (job_id,))
        assert p4g8.evaluate_fault_trigger(conn, job_id, "lease_expired", now=1)["ready"] is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX crashed lease-holder test")
def test_phase4g8_crashed_lease_holder_expires_before_takeover(kanban_home, tmp_path):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="phase4g8 crashed lease", initial_status="running")
        job_id = rk.create_runtime_job(
            conn,
            root,
            "exercise orphaned lease expiry",
            initialization_mode="provider_first",
        )

    assert p4g8_run._exercise_expired_lease_takeover(
        job_id,
        run_id="phase4g8-lease-control",
        marker_path=tmp_path / "lease-ready",
        ttl_seconds=1,
    ) is True
    with kb.connect() as conn:
        assert p4g8.evaluate_fault_trigger(conn, job_id, "lease_expired")["ready"] is True


def test_phase4g8_checkpoint_helper_compacts_actual_runtime_delta_without_fallback(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="phase4g8 real checkpoint", initial_status="running")
        job_id = rk.create_runtime_job(
            conn,
            root,
            "compact current runtime state",
            initialization_mode="provider_first",
        )
        provider = rd.DeterministicCompactionProvider(conn)

        result = p4g8_run._append_and_compact_real_checkpoint(
            conn,
            job_id,
            provider,
            reason="phase4g8-control-boundary",
        )

        assert result["status"] == "compacted"
        assert result["fallback_used"] is False
        assert p4g8_run._accepted_checkpoint_count(conn, job_id) == 1
        assert rd.validate_decision_context_chain(conn, job_id)["status"] == "valid"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group ownership test")
def test_fault_injector_terminates_only_owned_process_group(tmp_path):
    run_id = "phase4g8-owned-test"
    env = os.environ.copy()
    env[p4g8.PROCESS_OWNER_ENV] = run_id
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env=env,
        start_new_session=True,
    )
    try:
        result = p4g8.terminate_owned_process_group(process.pid, run_id=run_id, hard=True)
        assert result["signal"] == "SIGKILL"
        assert process.wait(timeout=5) != 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group ownership test")
def test_fault_injector_rejects_unowned_process_group():
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        time.sleep(0.05)
        with pytest.raises(ValueError, match="not owned"):
            p4g8.terminate_owned_process_group(process.pid, run_id="different-run", hard=True)
    finally:
        os.killpg(process.pid, 9)
        process.wait(timeout=5)


def test_worker_event_stall_detects_wrapper_heartbeat_without_codex_event(kanban_home):
    with kb.connect() as conn:
        job_id, task_id, run_id = _insert_running_phase4g8_worker(conn, started_at=100, worker_pid=12345)
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, 'heartbeat', '{}', ?)",
            (task_id, run_id, 390),
        )

        result = p4g8_run._worker_event_stall(
            conn,
            job_id,
            startup_timeout_seconds=300,
            stall_timeout_seconds=3600,
            now=401,
        )

    assert result["stalled"] is True
    assert result["reason"] == "no_worker_codex_event_after_startup_timeout"
    assert result["worker_pid"] == 12345


def test_worker_event_stall_uses_codex_event_not_wrapper_heartbeat(kanban_home):
    with kb.connect() as conn:
        job_id, task_id, run_id = _insert_running_phase4g8_worker(conn, started_at=100, worker_pid=12345)
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, 'worker_codex_event', '{}', ?)",
            (task_id, run_id, 350),
        )

        result = p4g8_run._worker_event_stall(
            conn,
            job_id,
            startup_timeout_seconds=300,
            stall_timeout_seconds=100,
            now=401,
        )

    assert result["stalled"] is False
    assert result["last_codex_event_at"] == 350


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group ownership test")
def test_phase4g8_cleanup_terminates_only_job_owned_worker(kanban_home):
    run_id = "phase4g8-cleanup-owned"
    env = os.environ.copy()
    env[p4g8.PROCESS_OWNER_ENV] = run_id
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env=env,
        start_new_session=True,
    )
    try:
        with kb.connect() as conn:
            job_id, _task_id, _task_run_id = _insert_running_phase4g8_worker(
                conn,
                started_at=int(time.time()),
                worker_pid=process.pid,
            )

        result = p4g8_run._terminate_owned_job_workers(job_id, run_id=run_id)

        assert result["terminated_pids"] == [process.pid]
        assert process.wait(timeout=5) != 0
    finally:
        if process.poll() is None:
            os.killpg(process.pid, 9)
            process.wait(timeout=5)


def test_phase4g8_model_profile_uses_effective_shared_context_window():
    policy = p4g8_run.PHASE4G8_COMPACTION_POLICY

    assert p4g8_run.PHASE4G8_CONTEXT_WINDOW_TOKENS == 353_400
    assert policy["compaction_trigger_ratio"] == 0.65
    assert int(policy["context_window_tokens"] * policy["compaction_trigger_ratio"]) == 229_710
    assert int(policy["context_window_tokens"] * policy["max_compaction_input_ratio"]) == 194_370
    assert policy["max_active_segment_tokens"] is None


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() != 0 or not all(shutil.which(command) for command in ("ip", "nft", "setpriv", "curl")),
    reason="network namespace integration requires POSIX root and ip/nft/setpriv/curl",
)
def test_network_namespace_allows_only_model_proxy():
    class UpstreamHandler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{upstream.server_address[1]}/v1"
        with p4g8.Phase4G8NetworkNamespace("network-canary", base_url, timeout_seconds=5) as namespace:
            allowed = subprocess.run(
                namespace.wrap_argv([
                    "curl", "--silent", "--show-error", "--max-time", "5",
                    "-X", "POST", f"{namespace.proxy_base_url}/responses", "-d", "{}",
                ]),
                text=True,
                capture_output=True,
                check=False,
            )
            assert allowed.returncode == 0, allowed.stderr
            assert json.loads(allowed.stdout) == {"ok": True}
            denied = subprocess.run(
                namespace.wrap_argv([
                    "curl", "--silent", "--show-error", "--max-time", "2", "https://example.com/",
                ]),
                text=True,
                capture_output=True,
                check=False,
            )
            assert denied.returncode != 0
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=5)


def test_model_proxy_forwards_websocket_upgrade_and_bidirectional_bytes():
    observed = {}

    class WebSocketUpstreamHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802
            observed["path"] = self.path
            observed["upgrade"] = self.headers.get("Upgrade")
            observed["connection"] = self.headers.get("Connection")
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.end_headers()
            self.wfile.flush()
            frame = self.rfile.read(6)
            observed["frame"] = frame
            self.wfile.write(frame)
            self.wfile.flush()

        def log_message(self, _format, *_args):
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), WebSocketUpstreamHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    proxy = p4g8._ModelProxyServer(
        ("127.0.0.1", 0),
        f"http://127.0.0.1:{upstream.server_address[1]}/v1",
        5,
    )
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    try:
        with socket.create_connection(proxy.server_address, timeout=5) as client:
            client.sendall(
                b"GET /v1/responses HTTP/1.1\r\n"
                b"Host: phase4g8-proxy\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"Sec-WebSocket-Version: 13\r\n"
                b"Sec-WebSocket-Key: dGVzdC1rZXk=\r\n\r\n"
            )
            handshake = p4g8._read_http_header(client, max_bytes=64 * 1024)
            assert handshake.startswith(b"HTTP/1.1 101 Switching Protocols\r\n")
            frame = b"\x81\x04ping"
            client.sendall(frame)
            assert client.recv(len(frame)) == frame
        assert observed == {
            "path": "/v1/responses",
            "upgrade": "websocket",
            "connection": "Upgrade",
            "frame": b"\x81\x04ping",
        }
        assert proxy.transport_audit() == {
            "schema": "hermes_phase4g8_model_transport_audit_v1",
            "http_request_count": 0,
            "websocket_upgrade_attempt_count": 1,
            "websocket_101_count": 1,
            "websocket_failure_count": 0,
        }
    finally:
        proxy.shutdown()
        proxy.server_close()
        proxy_thread.join(timeout=5)
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=5)


def test_websocket_relay_does_not_treat_idle_poll_as_connection_ttl():
    client_peer, proxy_client = socket.socketpair()
    upstream_peer, proxy_upstream = socket.socketpair()
    relay = threading.Thread(
        target=p4g8._relay_bidirectional,
        args=(proxy_client, proxy_upstream),
        kwargs={"idle_timeout_seconds": 0.05},
        daemon=True,
    )
    relay.start()
    try:
        time.sleep(0.15)
        assert relay.is_alive()
        client_peer.sendall(b"after-idle")
        assert upstream_peer.recv(10) == b"after-idle"
    finally:
        client_peer.close()
        upstream_peer.close()
        proxy_client.close()
        proxy_upstream.close()
        relay.join(timeout=2)


def test_isolated_codex_home_copies_only_model_source_and_preserves_source(tmp_path):
    source = tmp_path / "source-codex"
    target = tmp_path / "isolated-codex"
    source.mkdir()
    (source / "config.toml").write_text(
        'model = "gpt-5.4"\n'
        'model_reasoning_effort = "high"\n'
        'model_provider = "private"\n'
        '[model_providers.private]\n'
        'name = "Private"\n'
        'base_url = "https://model.example.invalid/v1"\n'
        'wire_api = "responses"\n'
        'supports_websockets = true\n'
        'stream_max_retries = 20\n'
        'websocket_connect_timeout_ms = 8000\n',
        encoding="utf-8",
    )
    (source / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "sk-phase4g8-isolation-secret"}),
        encoding="utf-8",
    )
    (source / "sessions").mkdir()
    (source / "sessions" / "history.jsonl").write_text("secret history\n", encoding="utf-8")

    report = p4g8.prepare_isolated_codex_home(
        source,
        target,
        proxy_base_url="http://10.203.20.1:43210/v1",
    )

    isolated_config = (target / "config.toml").read_text(encoding="utf-8")
    parsed_config = tomllib.loads(isolated_config)
    isolated_auth = json.loads((target / "auth.json").read_text(encoding="utf-8"))
    assert "http://10.203.20.1:43210/v1" in isolated_config
    assert "https://model.example.invalid/v1" not in isolated_config
    assert 'model_reasoning_effort = "high"' in isolated_config
    assert parsed_config["approval_policy"] == "on-request"
    assert parsed_config["approvals_reviewer"] == "auto_review"
    assert parsed_config["features"]["guardian_approval"] is True
    isolated_provider = parsed_config["model_providers"]["phase4g8_proxy"]
    assert isolated_provider["supports_websockets"] is True
    assert isolated_provider["stream_max_retries"] == 20
    assert isolated_provider["websocket_connect_timeout_ms"] == 8000
    assert "protected benchmark oracle material" in parsed_config["auto_review"]["policy"]
    exec_policy = (target / "rules" / "default.rules").read_text(encoding="utf-8")
    assert 'pattern = [["sudo", "su", "doas", "pkexec"]]' in exec_policy
    assert 'pattern = [["rm", "chmod", "chown"' in exec_policy
    assert 'decision = "prompt"' in exec_policy
    assert isolated_auth["OPENAI_API_KEY"] == "sk-phase4g8-isolation-secret"
    assert not (target / "sessions").exists()
    assert (target / "config.toml").stat().st_mode & 0o077 == 0
    assert (target / "auth.json").stat().st_mode & 0o077 == 0
    assert report["copied_session_history"] is False
    assert report["provider_transport"] == {
        "supports_websockets": True,
        "stream_max_retries": 20,
        "websocket_connect_timeout_ms": 8000,
    }
    assert report["approval"] == p4g8.audit_phase4g8_codex_auto_review(target / "config.toml")
    assert report["approval"]["configured"] is True
    assert report["approval"]["policy"] == "on-request"
    assert report["approval"]["reviewer"] == "auto_review"
    assert report["approval"]["auto_review_policy_version"] == "phase4g8-dangerous-operations-v1"
    assert report["approval"]["exec_policy_version"] == "phase4g8-exec-policy-v1"
    assert len(report["approval"]["exec_policy_sha256"]) == 64
    assert "protected benchmark oracle material" not in json.dumps(report["approval"])
    assert p4g8.verify_codex_source_unchanged(source, report["source_hashes"]) is True

    model_source = p4g8.load_codex_model_source(source)
    assert model_source["model"] == "gpt-5.4"
    assert model_source["reasoning_effort"] == "high"
    assert model_source["summary"]["reasoning_effort"] == "high"
    assert model_source["explicit_base_url"] == "https://model.example.invalid/v1"
    assert model_source["explicit_api_key"] == "sk-phase4g8-isolation-secret"
    assert "sk-phase4g8-isolation-secret" not in json.dumps(model_source["summary"])


def test_real_case_codex_lane_enables_isolated_auto_review(tmp_path):
    evaluator_spec = tmp_path / "qualification.json"
    evaluator_spec.write_text("{}\n", encoding="utf-8")
    codex_seed = tmp_path / "codex-seed"
    codex_seed.mkdir()
    codex_root = tmp_path / "codex-homes"

    try:
        p4g8_run._register_real_case_lanes(
            run_id="phase4g8-small-1234567890",
            model="gpt-5.4",
            namespace="h4g8-1234abcd",
            worker_timeout_seconds=60,
            evaluator_spec=evaluator_spec,
            expected_environment_sha256="a" * 64,
            worker_uid=os.geteuid(),
            worker_gid=os.getegid(),
            codex_home_seed=codex_seed,
            codex_home_root=codex_root,
        )

        lane = get_worker_lane("phase4g8-codex")
        assert lane is not None
        assert lane.config["sandbox"] == "danger-full-access"
        assert lane.config["approval"] == "on-request"
        assert lane.config["isolated_codex_home_seed"] == str(codex_seed)
    finally:
        clear_worker_lanes()


def test_run_report_separates_runtime_correctness_from_task_quality(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="phase4g8 report", initial_status="running")
        job_id = rk.create_runtime_job(
            conn,
            root,
            "report task quality separately",
            goal_items=[{
                "item_key": "result",
                "description": "result",
                "required": True,
                "verifier_required": True,
            }],
            initialization_mode="provider_first",
        )
        evaluator = {
            "schema": p4g8.EVALUATOR_RESULT_SCHEMA,
            "resolved": False,
            "fail_to_pass": {"passed": 0, "failed": 1, "total": 1},
            "pass_to_pass": {"passed": 2, "failed": 0, "total": 2},
        }
        report = p4g8.build_phase4g8_run_report(
            conn,
            job_id,
            instance_id="small",
            evaluator_result=evaluator,
            process_boundaries={"daemon_restart": True, "independent_evaluator": True},
        )

    assert report["runtime_validation"]["passed"] is True
    assert report["capability_validation"]["passed"] is False
    assert report["classification"] == "runtime-correct/task-failed"


def test_run_report_separates_resource_exhaustion(kanban_home):
    with kb.connect() as conn:
        job_id = rk.create_runtime_job(
            conn,
            kb.create_task(conn, title="resource exhaustion root", initial_status="running"),
            "report resource exhaustion separately",
            goal_items=[{
                "item_key": "result",
                "description": "result",
                "required": True,
                "verifier_required": True,
            }],
            initialization_mode="provider_first",
        )
        evaluator = {
            "schema": p4g8.EVALUATOR_RESULT_SCHEMA,
            "resolved": False,
            "fail_to_pass": {"passed": 0, "failed": 1, "total": 1},
            "pass_to_pass": {"passed": 2, "failed": 0, "total": 2},
        }
        report = p4g8.build_phase4g8_run_report(
            conn,
            job_id,
            instance_id="medium",
            evaluator_result=evaluator,
            process_boundaries={"daemon_restart": True, "independent_evaluator": True},
            metrics={"resource_exhausted": True},
        )

    assert report["runtime_validation"]["passed"] is True
    assert report["classification"] == "runtime-correct/resource-exhausted"


def test_fixed_evaluator_attempt_budget_is_ignored_while_feedback_can_progress():
    failures = [
        {"result": {"resolved": False}},
        {"result": {"resolved": False}},
        {"result": {"resolved": False}},
    ]

    status = p4g8_run._evaluator_failure_budget_status(
        failures,
        max_unresolved_evaluator_attempts=3,
    )

    assert status["attempt_count"] == 3
    assert status["failure_count"] == 3
    assert status["max_unresolved_evaluator_attempts"] == 3
    assert status["latest_resolved"] is False
    assert status["latest_feedback_extraction_incomplete"] is False
    assert status["deprecated_fixed_attempt_budget_ignored"] is True
    assert status["exhausted"] is False
    resolved = p4g8_run._evaluator_failure_budget_status(
        [*failures, {"result": {"resolved": True}}],
        max_unresolved_evaluator_attempts=3,
    )
    assert resolved["failure_count"] == 3
    assert resolved["latest_resolved"] is True
    assert resolved["exhausted"] is False
    infrastructure_invalid = p4g8_run._evaluator_failure_budget_status(
        [
            {"result": {"resolved": False}},
            {"result": {"resolved": False, "error": "stale_target_revision"}},
        ],
        max_unresolved_evaluator_attempts=2,
    )
    assert infrastructure_invalid["attempt_count"] == 2
    assert infrastructure_invalid["failure_count"] == 1
    assert infrastructure_invalid["exhausted"] is False
    incomplete = p4g8_run._evaluator_failure_budget_status(
        [{
            "result": {
                "resolved": False,
                "feedback_coverage": {"status": "extraction_incomplete"},
            },
        }],
        max_unresolved_evaluator_attempts=1,
    )
    assert incomplete["failure_count"] == 0
    assert incomplete["exhausted"] is False
    assert incomplete["latest_feedback_extraction_incomplete"] is True


def test_evaluator_progress_tracks_deeper_signature_and_feedback_consumption():
    test_id = "tests/test_feature.py::test_contract"

    def attempt(passed, expected, *, consumed):
        return {
            "node_id": f"verifier-{expected}",
            "feedback_consumed": consumed,
            "result": {
                "resolved": False,
                "fail_to_pass": {
                    "passed": passed,
                    "failed": 1,
                    "total": passed + 1,
                    "failed_tests": [test_id],
                },
                "pass_to_pass": {
                    "passed": 10,
                    "failed": 0,
                    "total": 10,
                    "failed_tests": [],
                },
                "failure_diagnostics": {
                    "cases": [{
                        "test_id": test_id,
                        "failure_kind": "assertion_comparison_failed",
                        "expected": [expected],
                    }],
                },
            },
        }

    status = p4g8_run._evaluator_progress_status([
        attempt(0, "first assertion", consumed=True),
        attempt(0, "deeper assertion", consumed=True),
        attempt(0, "deeper assertion", consumed=False),
    ])

    assert status["history"][1]["signature_changed"] is True
    assert status["history"][1]["progress"] is True
    assert status["history"][2]["progress"] is False
    assert status["no_progress_streak"] == 1
    assert status["latest_feedback_consumed"] is False


def test_official_evaluator_heartbeat_loop_stops_when_run_is_superseded(monkeypatch):
    calls = []
    stop_event = threading.Event()
    lost_run = threading.Event()

    class ConnectionContext:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(phase4g8_evaluator.kb, "connect", lambda **_kwargs: ConnectionContext())

    def fake_heartbeat(_conn, **kwargs):
        calls.append(kwargs)
        return len(calls) < 3

    monkeypatch.setattr(phase4g8_evaluator, "_heartbeat_evaluator", fake_heartbeat)
    thread = threading.Thread(
        target=phase4g8_evaluator._evaluator_heartbeat_loop,
        kwargs={
            "stop_event": stop_event,
            "lost_run": lost_run,
            "task_id": "task-evaluator",
            "task_run_id": 7,
            "session_id": "official-evaluator:test",
            "board": "default",
            "interval_seconds": 0.01,
        },
    )

    thread.start()
    assert lost_run.wait(timeout=1)
    stop_event.set()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert len(calls) == 3
    assert all(call["task_run_id"] == 7 for call in calls)


def test_aggregate_requires_runtime_three_of_three_separately_from_resolved_three_of_three():
    reports = []
    for index in range(3):
        reports.append({
            "schema": p4g8.RUN_REPORT_SCHEMA,
            "instance_id": f"case-{index}",
            "classification": "resolved" if index < 2 else "runtime-correct/task-failed",
            "runtime_validation": {"passed": True},
            "capability_validation": {"passed": index < 2},
        })

    aggregate = p4g8.aggregate_phase4g8_reports(reports)

    assert aggregate["runtime_validation"] == {"passed": True, "passed_instances": 3, "total_instances": 3}
    assert aggregate["capability_validation"] == {"passed": False, "resolved_instances": 2, "total_instances": 3}


def test_official_evaluator_lane_forwards_expected_environment_fingerprint(tmp_path, monkeypatch):
    spec_path = tmp_path / "qualification-spec.json"
    spec_path.write_text("{}\n", encoding="utf-8")
    expected = "a" * 64
    captured = {}

    class FakeProcess:
        pid = 12345

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(p4g8.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(kb, "worker_log_path", lambda *_args, **_kwargs: tmp_path / "evaluator.log")
    lane = p4g8.make_phase4g8_evaluator_lane({
        "name": "phase4g8-evaluator",
        "spec_path": str(spec_path),
        "run_id": "phase4g8-environment-test",
        "expected_environment_sha256": expected,
    })
    task = type("Task", (), {"id": "task-environment", "current_run_id": None})()

    assert lane.spawn_fn(task, str(tmp_path), board="default") == 12345
    assert captured["env"]["HERMES_PHASE4G8_EXPECTED_ENVIRONMENT_SHA256"] == expected


def test_official_evaluator_lane_completes_goal_through_task_receipt(
    kanban_home,
    tmp_path,
    monkeypatch,
):
    clear_worker_lanes()
    spec, _ = _qualified_fixture(tmp_path)
    spec_path = tmp_path / "protected-spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    workspace = tmp_path / "candidate"
    _run("git", "clone", "--quiet", spec["source"]["local_mirror"], str(workspace), cwd=tmp_path)
    _run("git", "config", "user.email", "phase4g8@example.invalid", cwd=workspace)
    _run("git", "config", "user.name", "Phase4G8 Test", cwd=workspace)
    (workspace / "result.txt").write_text("gold\n", encoding="utf-8")
    _run("git", "add", "result.txt", cwd=workspace)
    _run("git", "commit", "--quiet", "-m", "candidate", cwd=workspace)
    from hermes_cli.codex_worker import collect_git_evidence

    candidate_revision = collect_git_evidence(str(workspace))["workspace_revision"]
    lane_name = "phase4g8-evaluator"
    register_worker_lane(p4g8.make_phase4g8_evaluator_lane({
        "name": lane_name,
        "spec_path": str(spec_path),
        "run_id": "official-evaluator-control",
    }))
    try:
        with kb.connect() as conn:
            root = kb.create_task(conn, title="official evaluator control", initial_status="running")
            job_id = rk.create_runtime_job(
                conn,
                root,
                "complete only after official evaluator",
                workspace_path=str(workspace),
                goal_items=[{
                    "item_key": "result",
                    "description": "candidate passes official evaluator",
                    "required": True,
                    "verifier_required": True,
                }],
                initialization_mode="provider_first",
                runtime_metadata={
                    "phase4g8_run_id": "official-evaluator-control",
                    "verification_policy": {
                        "mode": "required_evaluator",
                        "assignee": lane_name,
                        "require_workspace_revision": True,
                    }
                },
            )
            patch = {
                "schema": rk.PATCH_SCHEMA,
                "expected_revision": 0,
                "rationale_summary": "create one implementation responsibility",
                "ops": [{
                    "op": "create_node",
                    "node_key": "implementation",
                    "node_type": "implementation",
                    "title": "Implementation",
                    "description": "Produce candidate revision.",
                    "goal_item_keys": ["result"],
                    "contract": {
                        "outcome": "Candidate exists.",
                        "acceptance_criteria": ["candidate revision exists"],
                        "success_evidence": ["workspace_revision"],
                        "declared_write_scope": ["**"],
                        "prohibited_actions": ["production_deployment"],
                    },
                }],
            }
            assert rk.apply_graph_patch(conn, job_id, patch)["status"] == "applied"
            rk.reduce_runtime_job(conn, job_id)
            implementation = conn.execute(
                "SELECT * FROM execution_nodes WHERE job_id = ? AND node_key = 'implementation'",
                (job_id,),
            ).fetchone()
            rk.materialize_runtime_node(conn, dict(implementation))
            implementation = conn.execute("SELECT * FROM execution_nodes WHERE id = ?", (implementation["id"],)).fetchone()
            implementation_task = kb.get_task(conn, implementation["latest_task_id"])
            assert "Phase 4G8 trusted-evaluator boundary" in implementation_task.body
            assert "Do not inspect Hermes databases" in implementation_task.body
            kb.complete_task(
                conn,
                implementation["latest_task_id"],
                result="candidate complete",
                summary="candidate complete",
                metadata={
                    "verdict": "succeeded",
                    "summary": "candidate complete",
                    "claimed_goal_items": ["result"],
                    "workspace_revision": candidate_revision,
                    "verification": {"passed": True},
                },
            )
            advanced = rk.advance_runtime_job(conn, job_id, create_tasks=True)
            verifier_key = next(key for key in advanced.materialized_nodes if key.startswith("verify-result-"))
            verifier = conn.execute(
                "SELECT * FROM execution_nodes WHERE job_id = ? AND node_key = ?",
                (job_id, verifier_key),
            ).fetchone()
            dispatch = kb.dispatch_once(conn, only_task_ids=[verifier["latest_task_id"]])
            assert dispatch.spawned

        deadline = time.time() + 10
        while time.time() < deadline:
            with kb.connect() as conn:
                task = kb.get_task(conn, verifier["latest_task_id"])
                if task and task.status in {"done", "blocked"}:
                    break
            time.sleep(0.05)
        else:
            pytest.fail("official evaluator subprocess did not finish")

        with kb.connect() as conn:
            assert p4g8_run._official_evaluator_attempts(conn, job_id) == []
            result = rk.advance_runtime_job(conn, job_id, create_tasks=False)
            status = rk.status_runtime_job(conn, job_id)
            assert result.job_state == "done"
            assert status["goal_items"][0]["state"] == "satisfied"
            verifier_ledger = next(row for row in status["progress_ledger"] if row["node_id"] == verifier["id"])
            assert verifier_ledger["verification_state"] == "independently_verified"
            assert verifier_ledger["metadata"]["official_evaluator_result"]["resolved"] is True
            evaluator_result, provenance = p4g8_run._official_evaluator_result(conn, job_id)
            attempts = p4g8_run._official_evaluator_attempts(conn, job_id)
            assert evaluator_result["resolved"] is True
            assert provenance["producer_kind"] == "official_evaluator"
            assert len(attempts) == 1
            assert attempts[0]["task_id"] == verifier["latest_task_id"]
            assert conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'official_evaluator_completed'",
                (verifier["latest_task_id"],),
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'worker_heartbeat'",
                (verifier["latest_task_id"],),
            ).fetchone()[0] >= 1
            assert conn.execute(
                "SELECT last_heartbeat_at FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (verifier["latest_task_id"],),
            ).fetchone()[0] is not None
            session = conn.execute(
                "SELECT * FROM backend_worker_sessions WHERE node_id = ?",
                (verifier["id"],),
            ).fetchone()
            assert session["status"] == "completed"
            durable_counts_before = {
                "patches": conn.execute(
                    "SELECT COUNT(*) FROM graph_patches WHERE job_id = ?",
                    (job_id,),
                ).fetchone()[0],
                "nodes": conn.execute(
                    "SELECT COUNT(*) FROM execution_nodes WHERE job_id = ?",
                    (job_id,),
                ).fetchone()[0],
                "materializations": conn.execute(
                    "SELECT COUNT(*) FROM node_materializations WHERE job_id = ?",
                    (job_id,),
                ).fetchone()[0],
            }

            sync = rk.sync_runtime_backend_sessions(conn, job_id)

            session = conn.execute(
                "SELECT * FROM backend_worker_sessions WHERE id = ?",
                (session["id"],),
            ).fetchone()
            assert session["status"] == "completed"
            assert session["id"] in sync["updated"]
            assert durable_counts_before == {
                "patches": conn.execute(
                    "SELECT COUNT(*) FROM graph_patches WHERE job_id = ?",
                    (job_id,),
                ).fetchone()[0],
                "nodes": conn.execute(
                    "SELECT COUNT(*) FROM execution_nodes WHERE job_id = ?",
                    (job_id,),
                ).fetchone()[0],
                "materializations": conn.execute(
                    "SELECT COUNT(*) FROM node_materializations WHERE job_id = ?",
                    (job_id,),
                ).fetchone()[0],
            }
            original_node_trace = capability_trace._node_trace

            def node_trace_with_resume(*args, **kwargs):
                node_trace = original_node_trace(*args, **kwargs)
                if node_trace["node_type"] != "verification":
                    node_trace["resume_count"] = 2
                    node_trace["backend_session_keys"] = ["worker-thread"]
                return node_trace

            monkeypatch.setattr(capability_trace, "_node_trace", node_trace_with_resume)
            trace = capability_trace.build_capability_trace(
                conn,
                job_id,
                run_id="official-evaluator-control",
                instance_id="toy-evolution",
                case_size="small",
                run_report={
                    "classification": "resolved",
                    "runtime_validation": {
                        "passed": True,
                        "failures": [],
                        "consistency": {"violation_count": 0, "warning_count": 0},
                        "duplicate_ledger_fact_count": 0,
                        "compaction_fallback_count": 0,
                    },
                    "capability_validation": {"passed": True, "official_resolved": True},
                    "metrics": {},
                },
            )
            assert trace["schema"] == capability_trace.CAPABILITY_TRACE_SCHEMA
            assert trace["counts"]["worker_nodes"] == 1
            assert trace["counts"]["superseded_worker_nodes"] == 0
            assert trace["counts"]["evaluator_attempts"] == 1
            continuity = next(
                item for item in trace["observations"]
                if item["category"] == "context_continuity"
            )
            assert continuity["assessment"] == "preserved"
            assert "session_resume_count=2" in continuity["evidence"]
            assert any(item["kind"] == "evaluator" for item in trace["timeline"])

            def node_trace_as_superseded(*args, **kwargs):
                node_trace = original_node_trace(*args, **kwargs)
                if node_trace["node_type"] != "verification":
                    node_trace["state"] = "superseded"
                return node_trace

            monkeypatch.setattr(capability_trace, "_node_trace", node_trace_as_superseded)
            superseded_trace = capability_trace.build_capability_trace(
                conn,
                job_id,
                run_id="official-evaluator-control",
                instance_id="toy-evolution",
                case_size="small",
                run_report={
                    "classification": "resolved",
                    "runtime_validation": {"passed": True},
                    "capability_validation": {"passed": True, "official_resolved": True},
                    "metrics": {},
                },
            )
            assert superseded_trace["counts"]["worker_nodes"] == 0
            assert superseded_trace["counts"]["superseded_worker_nodes"] == 1
            assert not any(
                item["category"] == "context_continuity"
                for item in superseded_trace["observations"]
            )
            rendered = capability_trace.render_capability_trace_markdown(trace)
            assert "实际能力过程记录" in rendered
            assert "Runtime Validation：通过" in rendered
            paths = capability_trace.write_capability_trace(tmp_path / "reports", trace)
            assert Path(paths["json"]).is_file()
            assert Path(paths["markdown"]).is_file()
    finally:
        clear_worker_lanes()
