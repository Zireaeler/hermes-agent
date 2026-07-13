from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_decision as rd
from hermes_cli import kanban_runtime_kernel as rk
from hermes_cli import kanban_runtime_phase4g8 as p4g8
from hermes_cli import kanban_runtime_phase4g8_run as p4g8_run
from hermes_cli import phase4g8_swe_evo as swe_evo
from hermes_cli import phase4g8_capability_trace as capability_trace
from hermes_cli.worker_lanes import clear_worker_lanes, register_worker_lane


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


def test_pytest_failure_diagnostics_excludes_patch_source_and_keeps_assertion_diff(tmp_path):
    output = tmp_path / "test_output.txt"
    output.write_text(
        "git apply output\n+def hidden_fixture():\n+    return 'do-not-leak'\n"
        "=================================== FAILURES ===================================\n"
        "_ test_schema _\n"
        "tests/test_hidden.py:42: in test_schema\n"
        "    assert actual == expected\n"
        "E   AssertionError: assert {'oneOf': []} == {'discriminator': {'propertyName': 'type'}}\n"
        "E   Differing items:\n"
        "=========================== short test summary info ============================\n",
        encoding="utf-8",
    )

    diagnostic = swe_evo._extract_pytest_failure_diagnostics(output)

    assert diagnostic["schema"] == "hermes_phase4g8_pytest_failure_diagnostics_v1"
    assert "assert actual == expected" in diagnostic["text"]
    assert "discriminator" in diagnostic["text"]
    assert "hidden_fixture" not in diagnostic["text"]
    assert "do-not-leak" not in diagnostic["text"]


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


def test_swe_evo_locked_image_script_removes_only_install_command():
    script = "#!/bin/bash\nconda activate testbed\npython -m pip install -e .\npytest -rA\n"

    locked = swe_evo._locked_image_eval_script(script, "python -m pip install -e .")

    assert "pip install" not in locked
    assert "conda activate testbed" in locked
    assert "pytest -rA" in locked


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


def test_swe_evo_standard_report_bounds_failed_test_identifiers():
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

    assert result["fail_to_pass"]["failed_tests"] == failures[:20]
    assert result["fail_to_pass"]["failed_tests_truncated"] == 5


def test_phase4g8_real_case_requires_explicit_execution(tmp_path):
    with pytest.raises(ValueError, match="execute_real"):
        p4g8_run.run_phase4g8_real_case(
            qualification_spec_path=tmp_path / "missing.json",
            run_root=tmp_path / "runs",
            source_codex_home=tmp_path / "codex",
            case_size="small",
            execute_real=False,
        )


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
        'wire_api = "responses"\n',
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
    isolated_auth = json.loads((target / "auth.json").read_text(encoding="utf-8"))
    assert "http://10.203.20.1:43210/v1" in isolated_config
    assert "https://model.example.invalid/v1" not in isolated_config
    assert 'model_reasoning_effort = "high"' in isolated_config
    assert isolated_auth["OPENAI_API_KEY"] == "sk-phase4g8-isolation-secret"
    assert not (target / "sessions").exists()
    assert (target / "config.toml").stat().st_mode & 0o077 == 0
    assert (target / "auth.json").stat().st_mode & 0o077 == 0
    assert report["copied_session_history"] is False
    assert p4g8.verify_codex_source_unchanged(source, report["source_hashes"]) is True

    model_source = p4g8.load_codex_model_source(source)
    assert model_source["model"] == "gpt-5.4"
    assert model_source["reasoning_effort"] == "high"
    assert model_source["summary"]["reasoning_effort"] == "high"
    assert model_source["explicit_base_url"] == "https://model.example.invalid/v1"
    assert model_source["explicit_api_key"] == "sk-phase4g8-isolation-secret"
    assert "sk-phase4g8-isolation-secret" not in json.dumps(model_source["summary"])


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


def test_evaluator_failure_budget_stops_only_after_latest_unresolved_attempt():
    failures = [
        {"result": {"resolved": False}},
        {"result": {"resolved": False}},
        {"result": {"resolved": False}},
    ]

    status = p4g8_run._evaluator_failure_budget_status(
        failures,
        max_unresolved_evaluator_attempts=3,
    )

    assert status == {
        "attempt_count": 3,
        "failure_count": 3,
        "max_unresolved_evaluator_attempts": 3,
        "latest_resolved": False,
        "exhausted": True,
    }
    resolved = p4g8_run._evaluator_failure_budget_status(
        [*failures, {"result": {"resolved": True}}],
        max_unresolved_evaluator_attempts=3,
    )
    assert resolved["failure_count"] == 3
    assert resolved["latest_resolved"] is True
    assert resolved["exhausted"] is False


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


def test_official_evaluator_lane_completes_goal_through_task_receipt(kanban_home, tmp_path):
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
            session = conn.execute(
                "SELECT * FROM backend_worker_sessions WHERE node_id = ?",
                (verifier["id"],),
            ).fetchone()
            assert session["status"] == "active"
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
            assert trace["counts"]["evaluator_attempts"] == 1
            assert any(item["kind"] == "evaluator" for item in trace["timeline"])
            rendered = capability_trace.render_capability_trace_markdown(trace)
            assert "实际能力过程记录" in rendered
            assert "Runtime Validation：通过" in rendered
            paths = capability_trace.write_capability_trace(tmp_path / "reports", trace)
            assert Path(paths["json"]).is_file()
            assert Path(paths["markdown"]).is_file()
    finally:
        clear_worker_lanes()
