from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_kernel as rk
from hermes_cli import kanban_runtime_decision as rd
from hermes_cli import kanban_runtime_supervisor as rs


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _provider_first_job() -> str:
    with kb.connect() as conn:
        root = kb.create_task(conn, title="runtime supervisor root", initial_status="running")
        return rk.create_runtime_job(
            conn,
            root,
            "produce one verified runtime result",
            goal_items=[
                {
                    "item_key": "runtime-result",
                    "description": "Produce the runtime result",
                    "required": True,
                    "verifier_required": True,
                }
            ],
            initialization_mode="provider_first",
        )


def _daemon_config(tmp_path: Path, **overrides) -> rs.RuntimeSupervisorDaemonConfig:
    values = {
        "interval_seconds": 0.01,
        "limit": 10,
        "lock_ttl_seconds": 10,
        "max_consecutive_errors": 2,
        "error_backoff_max_seconds": 0.02,
        "max_polls": 1,
        "pidfile": tmp_path / "supervisor.pid",
        "state_file": tmp_path / "supervisor-state.json",
    }
    values.update(overrides)
    return rs.RuntimeSupervisorDaemonConfig(**values)


class _DaemonCompactionProvider:
    provider_name = "fake-real-compactor"
    model = "fake-real-model"

    def __init__(self):
        self.calls = []

    def compact(self, request):
        self.calls.append(request)
        with kb.connect() as conn:
            checkpoint = rd.build_deterministic_checkpoint(
                conn,
                request.job_id,
                request.source_segment["id"],
                profile_name=request.profile["profile_name"],
            )
        return rd.CompactionProviderResult(
            checkpoint=checkpoint,
            raw_output=checkpoint,
            provider_name=self.provider_name,
            model=self.model,
            profile_name=request.profile["profile_name"],
            profile_version=request.profile["profile_version"],
            profile_hash=request.profile["profile_hash"],
            request_ref="daemon-compaction-request",
            response_ref="daemon-compaction-response",
            parse_status="parsed",
        )


def test_daemon_config_rejects_remote_health_bind(tmp_path):
    config = _daemon_config(tmp_path, health_host="0.0.0.0", health_port=8791)

    with pytest.raises(ValueError, match="loopback"):
        config.validate()


def test_daemon_config_rejects_shared_pid_and_state_path(tmp_path):
    shared = tmp_path / "shared-state"
    config = _daemon_config(tmp_path, pidfile=shared, state_file=shared)

    with pytest.raises(ValueError, match="different paths"):
        config.validate()


def test_pidfile_claim_is_exclusive_and_replaces_stale_pid(tmp_path, monkeypatch):
    pidfile = tmp_path / "supervisor.pid"
    rs.claim_runtime_supervisor_pidfile(pidfile)
    try:
        with pytest.raises(rs.SupervisorAlreadyRunningError, match="live PID"):
            rs.claim_runtime_supervisor_pidfile(pidfile)
    finally:
        rs.release_runtime_supervisor_pidfile(pidfile)

    pidfile.write_text("99999999\n", encoding="utf-8")
    monkeypatch.setattr(rs, "_pid_is_alive", lambda _pid: False)
    rs.claim_runtime_supervisor_pidfile(pidfile, pid=4242)
    assert pidfile.read_text(encoding="utf-8") == "4242\n"
    rs.release_runtime_supervisor_pidfile(pidfile, pid=4242)
    assert not pidfile.exists()


def test_pidfile_conflict_does_not_overwrite_running_daemon_state(tmp_path):
    config = _daemon_config(tmp_path)
    running_state = {"status": "running", "owner": "existing-daemon"}
    config.pidfile.write_text(f"{os.getpid()}\n", encoding="utf-8")
    config.state_file.write_text(json.dumps(running_state), encoding="utf-8")

    with pytest.raises(rs.SupervisorAlreadyRunningError):
        rs.run_runtime_supervisor_daemon(config, poll_once=lambda _owner: {})

    assert json.loads(config.state_file.read_text(encoding="utf-8")) == running_state


def test_health_server_reports_liveness_readiness_and_bounded_state():
    state = rs.RuntimeSupervisorOperationalState("test-owner", readiness_timeout_seconds=30)
    server, thread = rs.start_runtime_supervisor_health_server("127.0.0.1", 0, state)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        live = json.loads(urllib.request.urlopen(base + "/health/live", timeout=2).read())
        assert live == {"live": True, "status": "ok"}
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(base + "/health/ready", timeout=2)
        assert error.value.code == 503

        state.poll_succeeded(
            {
                "job_count": 1,
                "advanced_count": 1,
                "provider_payload": "sk-this-must-not-appear-anywhere",
                "ticks": [{"status": "advanced", "secret": "hidden"}],
            }
        )
        ready = json.loads(urllib.request.urlopen(base + "/health/ready", timeout=2).read())
        health_text = urllib.request.urlopen(base + "/health", timeout=2).read().decode("utf-8")
        health = json.loads(health_text)

        assert ready["ready"] is True
        assert health["last_result"] == {
            "advanced_count": 1,
            "job_count": 1,
            "skip_reasons": {},
            "skipped_count": 0,
        }
        assert "sk-this-must-not-appear-anywhere" not in health_text
        assert "provider_payload" not in health_text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_daemon_runs_bounded_polls_and_writes_operational_state(tmp_path):
    owners = []

    def poll(owner):
        owners.append(owner)
        return {"owner": owner, "job_count": 0, "advanced_count": 0, "ticks": []}

    config = _daemon_config(tmp_path, max_polls=2)
    report = rs.run_runtime_supervisor_daemon(config, poll_once=poll)

    assert len(owners) == 2
    assert owners[0] == owners[1] == report["owner"]
    assert report["exit_reason"] == "max_polls"
    assert report["state"]["poll_count"] == 2
    assert report["state"]["status"] == "stopped"
    assert not config.pidfile.exists()
    persisted = json.loads(config.state_file.read_text(encoding="utf-8"))
    assert persisted["status"] == "stopped"
    assert persisted["last_result"]["job_count"] == 0
    assert not list(tmp_path.glob(".*.tmp"))


def test_daemon_redacts_poll_error_and_exits_after_error_budget(tmp_path):
    secret = "sk-abcdefghijklmnopqrstuvwxyz"

    def fail(_owner):
        raise RuntimeError(f"provider rejected key {secret}")

    config = _daemon_config(tmp_path, max_polls=0, max_consecutive_errors=2)
    report = rs.run_runtime_supervisor_daemon(config, poll_once=fail)
    state_text = config.state_file.read_text(encoding="utf-8")

    assert report["status"] == "failed"
    assert report["exit_reason"] == "max_consecutive_errors"
    assert report["state"]["poll_count"] == 2
    assert secret not in state_text
    assert "provider rejected key" in state_text
    assert "detail_sha256" in state_text
    assert not config.pidfile.exists()


def test_daemon_restart_does_not_duplicate_patch_or_materialization(kanban_home, tmp_path):
    job_id = _provider_first_job()
    first = rs.run_runtime_supervisor_daemon(
        _daemon_config(
            tmp_path,
            pidfile=tmp_path / "first.pid",
            state_file=tmp_path / "first.json",
        ),
        decision_provider=rk.fixture_decision_provider,
        owner="daemon-first",
    )
    second = rs.run_runtime_supervisor_daemon(
        _daemon_config(
            tmp_path,
            pidfile=tmp_path / "second.pid",
            state_file=tmp_path / "second.json",
        ),
        decision_provider=rk.fixture_decision_provider,
        owner="daemon-second",
    )

    assert first["owner"] != second["owner"]
    with kb.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM graph_patches WHERE job_id = ? AND status = 'applied'",
            (job_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM node_materializations WHERE job_id = ?",
            (job_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT advance_lock FROM runtime_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()[0] is None


def test_daemon_poll_uses_configured_compaction_provider_without_fallback(kanban_home, tmp_path):
    job_id = _provider_first_job()
    with kb.connect() as conn:
        rk.append_decision_segment_entry(
            conn,
            job_id,
            "delta_appended",
            {"reason": "force bounded daemon compaction"},
            payload_text="x" * 200,
        )
    provider = _DaemonCompactionProvider()

    rs.run_runtime_supervisor_daemon(
        _daemon_config(
            tmp_path,
            compaction_policy={"max_active_segment_tokens": 1},
            compaction_fallback_to_deterministic=False,
        ),
        compaction_provider=provider,
        owner="daemon-compaction",
    )

    assert len(provider.calls) == 1
    with kb.connect() as conn:
        checkpoint = conn.execute(
            "SELECT * FROM decision_checkpoints WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        assert checkpoint is not None
        metadata = json.loads(checkpoint["metadata_json"])
        assert metadata["provider_name"] == "fake-real-compactor"
        assert metadata["request_ref"] == "daemon-compaction-request"
        assert conn.execute(
            "SELECT COUNT(*) FROM decision_segment_entries WHERE job_id = ? AND entry_type = 'compaction_fallback'",
            (job_id,),
        ).fetchone()[0] == 0


def test_daemon_takes_over_expired_crash_lease_without_duplicate_work(kanban_home, tmp_path):
    job_id = _provider_first_job()
    with kb.connect() as conn:
        lock = rk.acquire_runtime_advance_lock(conn, job_id, owner="crashed-daemon", ttl_seconds=60)
        assert lock["acquired"] is True
        conn.execute("UPDATE runtime_jobs SET claim_expires_at = 0 WHERE id = ?", (job_id,))

    rs.run_runtime_supervisor_daemon(
        _daemon_config(tmp_path, max_polls=2),
        decision_provider=rk.fixture_decision_provider,
        owner="replacement-daemon",
    )

    with kb.connect() as conn:
        acquired = conn.execute(
            """
            SELECT payload_json
              FROM execution_events
             WHERE job_id = ? AND event_type = 'advance_lock_acquired'
             ORDER BY id
            """,
            (job_id,),
        ).fetchall()
        owners = [json.loads(row[0])["owner"] for row in acquired]
        assert owners[0] == "crashed-daemon"
        assert owners[1:] == ["replacement-daemon", "replacement-daemon"]
        assert conn.execute(
            "SELECT COUNT(*) FROM node_materializations WHERE job_id = ?",
            (job_id,),
        ).fetchone()[0] == 1


def test_daemon_respects_active_lease_owned_by_another_process(kanban_home, tmp_path):
    job_id = _provider_first_job()
    with kb.connect() as conn:
        lock = rk.acquire_runtime_advance_lock(conn, job_id, owner="active-daemon", ttl_seconds=60)
        assert lock["acquired"] is True

    report = rs.run_runtime_supervisor_daemon(
        _daemon_config(tmp_path),
        decision_provider=rk.fixture_decision_provider,
        owner="contending-daemon",
    )

    assert report["state"]["last_result"] == {
        "advanced_count": 0,
        "job_count": 1,
        "skip_reasons": {"locked": 1},
        "skipped_count": 1,
    }
    with kb.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM graph_patches WHERE job_id = ?",
            (job_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM node_materializations WHERE job_id = ?",
            (job_id,),
        ).fetchone()[0] == 0
        rk.release_runtime_advance_lock(conn, job_id, owner="active-daemon")


def test_daemon_does_not_rediscover_terminal_job(kanban_home, tmp_path):
    job_id = _provider_first_job()
    with kb.connect() as conn:
        conn.execute("UPDATE runtime_jobs SET state = 'done' WHERE id = ?", (job_id,))
        event_count = conn.execute(
            "SELECT COUNT(*) FROM execution_events WHERE job_id = ?",
            (job_id,),
        ).fetchone()[0]

    report = rs.run_runtime_supervisor_daemon(
        _daemon_config(tmp_path),
        decision_provider=rk.fixture_decision_provider,
    )

    assert report["state"]["last_result"]["job_count"] == 0
    with kb.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM execution_events WHERE job_id = ?",
            (job_id,),
        ).fetchone()[0] == event_count


def test_runtime_daemon_cli_runs_bounded_json_poll(kanban_home, tmp_path):
    output = kc.run_slash(
        "runtime daemon --max-polls 1 --interval 0.01 --error-backoff-max 0.02 "
        f"--pidfile {tmp_path / 'cli.pid'} --state-file {tmp_path / 'cli.json'} --json"
    )
    payload = json.loads(output)

    assert payload["exit_reason"] == "max_polls"
    assert payload["state"]["poll_count"] == 1
    assert payload["state"]["last_result"]["job_count"] == 0


def test_runtime_daemon_real_provider_requires_bounded_timeout(kanban_home):
    output = kc.run_slash(
        "runtime daemon --provider real --model-provider custom --model test-model --max-polls 1"
    )

    assert "requires a positive --timeout" in output


def test_runtime_daemon_real_provider_requires_ttl_for_retry_window(kanban_home):
    output = kc.run_slash(
        "runtime daemon --provider real --model-provider custom --model test-model "
        "--timeout 20 --max-retries 1 --lock-ttl 30 --max-polls 1"
    )

    assert "must exceed real decision and compaction provider retry windows" in output


def test_runtime_daemon_real_compaction_requires_bounded_timeout(kanban_home):
    output = kc.run_slash(
        "runtime daemon --compaction-provider real --model-provider custom --model test-model --max-polls 1"
    )

    assert "requires a positive --compaction-timeout" in output


@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGTERM process test")
def test_runtime_daemon_subprocess_handles_sigterm_and_cleans_pidfile(tmp_path):
    hermes_home = tmp_path / "hermes-home"
    home = tmp_path / "home"
    hermes_home.mkdir()
    home.mkdir()
    pidfile = tmp_path / "subprocess.pid"
    state_file = tmp_path / "subprocess.json"
    env = os.environ.copy()
    env.update(
        HERMES_HOME=str(hermes_home),
        HERMES_KANBAN_DB=str(tmp_path / "kanban.db"),
        HOME=str(home),
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import shlex, sys; from hermes_cli.kanban import run_slash; "
                "print(run_slash(' '.join(shlex.quote(value) for value in sys.argv[1:])))"
            ),
            "runtime",
            "daemon",
            "--interval",
            "60",
            "--pidfile",
            str(pidfile),
            "--state-file",
            str(state_file),
        ],
        cwd=Path.cwd(),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if state_file.exists():
                state = json.loads(state_file.read_text(encoding="utf-8"))
                if state.get("poll_count", 0) >= 1 and state.get("last_success_at"):
                    break
            if process.poll() is not None:
                break
            time.sleep(0.05)
        else:
            pytest.fail("runtime supervisor subprocess did not complete its first poll")
        assert process.poll() is None
        process.terminate()
        _stdout, stderr = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)

    assert process.returncode == 0, stderr
    assert not pidfile.exists()
    final_state = json.loads(state_file.read_text(encoding="utf-8"))
    assert final_state["status"] == "stopped"
    assert final_state["poll_count"] == 1


def test_systemd_unit_uses_runtime_daemon_without_embedded_credentials():
    unit = Path("plugins/kanban/systemd/hermes-kanban-runtime-supervisor.service").read_text(
        encoding="utf-8"
    )

    assert "hermes kanban runtime daemon" in unit
    assert "Restart=on-failure" in unit
    assert "KillSignal=SIGTERM" in unit
    assert "API_KEY" not in unit
    assert "api_key" not in unit
