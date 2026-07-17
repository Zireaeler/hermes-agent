import json
from pathlib import Path
import tomllib

from hermes_cli import phase4g9_native_arm as arm1


def _source_codex_home(path: Path) -> Path:
    path.mkdir()
    (path / "config.toml").write_text(
        'model = "gpt-5.6-sol"\n'
        'model_reasoning_effort = "high"\n'
        'model_provider = "source"\n'
        '[model_providers.source]\n'
        'base_url = "https://model.example.invalid/v1"\n'
        'wire_api = "responses"\n'
        'supports_websockets = true\n'
        'stream_max_retries = 20\n'
        'websocket_connect_timeout_ms = 8000\n',
        encoding="utf-8",
    )
    (path / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "sk-phase4g9-test-secret"}) + "\n",
        encoding="utf-8",
    )
    (path / "sessions").mkdir()
    (path / "sessions" / "old.jsonl").write_text("old session\n", encoding="utf-8")
    return path


def test_native_codex_home_freezes_ultra_multi_agent_and_transport(tmp_path):
    source = _source_codex_home(tmp_path / "source")
    target = tmp_path / "isolated"

    audit = arm1.prepare_native_codex_home(
        source,
        target,
        proxy_base_url="http://10.203.20.1:41000/v1",
        worker_uid=0,
        worker_gid=0,
    )

    config = tomllib.loads((target / "config.toml").read_text(encoding="utf-8"))
    assert config["model"] == "gpt-5.6-sol"
    assert config["model_reasoning_effort"] == "ultra"
    assert config["model_context_window"] == 353_400
    assert config["model_auto_compact_token_limit"] == 230_000
    assert config["features"]["multi_agent_v2"] is True
    provider = config["model_providers"]["phase4g9_proxy"]
    assert provider["base_url"] == "http://10.203.20.1:41000/v1"
    assert provider["supports_websockets"] is True
    assert provider["stream_max_retries"] == 20
    assert provider["websocket_connect_timeout_ms"] == 8000
    assert not (target / "sessions").exists()
    assert audit["reasoning_effort"] == "ultra"
    assert audit["wire_reasoning_effort"] == "max"
    assert audit["multi_agent_mode"] == "proactive"
    assert audit["max_threads_including_parent"] == 4
    assert audit["copied_session_history"] is False
    assert "sk-phase4g9-test-secret" not in json.dumps(audit)


def test_parent_prompt_grants_native_allocation_before_iterative_feedback(tmp_path):
    prompt = arm1.build_parent_prompt(
        {"srs": "Requirement one.\nRequirement two."},
        toolchain=tmp_path / "toolchain",
    )

    assert "Proactively use native Codex\nsubagents" in prompt
    assert "Decide their count, roles, scopes" in prompt
    assert "No official evaluator result is available during the initial turn" in prompt
    assert "resume this exact parent thread" in prompt
    assert "Requirement one." in prompt
    assert "gold patch" in prompt
    assert "FAIL_TO_PASS" not in prompt
    assert "58/68" not in prompt


def test_native_codex_argv_resumes_exact_parent_thread(tmp_path):
    fresh = arm1.build_native_codex_argv(workspace=tmp_path)
    resumed = arm1.build_native_codex_argv(
        workspace=tmp_path,
        resume_session_id="thread-parent",
    )

    assert fresh[-3:] == ["exec", "--json", "-"]
    assert resumed[-5:] == ["exec", "resume", "--json", "thread-parent", "-"]


def test_evaluator_feedback_includes_every_current_failure():
    evaluator = {
        "resolved": False,
        "fail_to_pass": {
            "passed": 1,
            "failed": 2,
            "total": 3,
            "failed_tests": ["tests/test_a.py::test_a", "tests/test_b.py::test_b"],
        },
        "pass_to_pass": {
            "passed": 4,
            "failed": 0,
            "total": 4,
            "failed_tests": [],
        },
        "feedback_coverage": {
            "status": "current_failure_complete",
            "official_failed_test_count": 2,
            "required_case_count": 2,
            "covered_official_test_count": 2,
            "missing_test_ids": [],
            "unidentified_failed_test_count": 0,
        },
        "failure_diagnostics": {
            "source_sha256": "a" * 64,
            "cases": [
                {
                    "test_id": "tests/test_a.py::test_a",
                    "failure_kind": "assertion_comparison_failed",
                    "comparisons": [{
                        "operator": "==",
                        "left": "1",
                        "right": "2",
                        "required_relation": "equal",
                    }],
                    "diagnostic_excerpt": "assert 1 == 2",
                },
                {
                    "test_id": "tests/test_b.py::test_b",
                    "failure_kind": "exception_raised",
                    "exception_summary": ["ValueError: broken"],
                    "diagnostic_excerpt": "ValueError: broken",
                },
            ],
        },
    }

    prompt = arm1.build_evaluator_feedback_prompt(
        evaluator,
        candidate_round=3,
        best_round=2,
        best_fail_to_pass=2,
        best_pass_to_pass=4,
    )

    assert "tests/test_a.py::test_a" in prompt
    assert "tests/test_b.py::test_b" in prompt
    assert '"round": 2' in prompt
    assert "exact parent session" in prompt


def test_evaluator_feedback_rejects_incomplete_extraction():
    evaluator = {
        "resolved": False,
        "fail_to_pass": {
            "passed": 0,
            "failed": 1,
            "total": 1,
            "failed_tests": ["tests/test_a.py::test_a"],
        },
        "pass_to_pass": {"passed": 1, "failed": 0, "total": 1, "failed_tests": []},
        "feedback_coverage": {"status": "extraction_incomplete"},
        "failure_diagnostics": {},
    }

    try:
        arm1.build_evaluator_feedback_prompt(
            evaluator,
            candidate_round=1,
            best_round=1,
            best_fail_to_pass=0,
            best_pass_to_pass=1,
        )
    except ValueError as exc:
        assert "incomplete" in str(exc)
    else:
        raise AssertionError("incomplete evaluator feedback was accepted")


def test_candidate_quality_preserves_p2p_before_f2p_gain():
    clean = {
        "resolved": False,
        "fail_to_pass": {"passed": 5},
        "pass_to_pass": {"failed": 0},
    }
    regression = {
        "resolved": False,
        "fail_to_pass": {"passed": 10},
        "pass_to_pass": {"failed": 1},
    }

    assert arm1.candidate_quality_key(clean) > arm1.candidate_quality_key(regression)


def test_parent_thread_invariant_rejects_fresh_root_on_resume():
    assert arm1.validated_parent_thread_id(None, "parent") == "parent"
    assert arm1.validated_parent_thread_id("parent", None) == "parent"
    assert arm1.validated_parent_thread_id("parent", "parent") == "parent"

    try:
        arm1.validated_parent_thread_id("parent", "replacement")
    except RuntimeError as exc:
        assert "different root thread" in str(exc)
    else:
        raise AssertionError("replacement root thread was accepted")


def test_iterative_runner_resumes_same_parent_until_resolved(tmp_path, monkeypatch):
    protected = tmp_path / "qualification" / "protected"
    worker = tmp_path / "qualification" / "worker"
    protected.mkdir(parents=True)
    worker.mkdir()
    spec_path = protected / "qualification-spec.json"
    spec_path.write_text("{}\n", encoding="utf-8")
    (worker / "locked-task.json").write_text(
        json.dumps({"srs": "Implement the complete release."}) + "\n",
        encoding="utf-8",
    )
    run_root = tmp_path / "run"
    source_home = tmp_path / "source-home"
    source_home.mkdir()
    paths = {
        "root": run_root,
        "home": run_root / "home",
        "codex_home": run_root / "codex-home",
        "workspace": run_root / "workspace",
        "protected": run_root / "protected",
        "worker_events": run_root / "worker-events",
        "reports": run_root / "reports",
        "worker_toolchain": run_root / "toolchain",
    }

    def fake_layout(_root, _spec):
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
        return paths

    class FakeNetwork:
        proxy_base_url = "http://127.0.0.1:40000/v1"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def transport_audit(self):
            return {"requests": 2}

    spec = {
        "instance_id": arm1.FROZEN_INSTANCE_ID,
        "base_commit": arm1.FROZEN_BASE_COMMIT,
        "dataset_revision": "dataset",
    }
    monkeypatch.setattr(arm1.p4g8, "load_qualification_spec", lambda _path: spec)
    monkeypatch.setattr(arm1.p4g8_run, "_require_qualified", lambda *_args: None)
    monkeypatch.setattr(arm1, "_prepare_layout", fake_layout)
    monkeypatch.setattr(
        arm1.p4g8,
        "load_codex_model_source",
        lambda *_args, **_kwargs: {"explicit_base_url": "https://model.invalid/v1"},
    )
    monkeypatch.setattr(arm1.p4g8, "Phase4G8NetworkNamespace", lambda *_args: FakeNetwork())
    monkeypatch.setattr(
        arm1,
        "prepare_native_codex_home",
        lambda *_args, **_kwargs: {"source_hashes": {}},
    )
    calls = []

    def fake_turn(**kwargs):
        calls.append(kwargs)
        round_number = kwargs["candidate_round"]
        return {
            "candidate_round": round_number,
            "mode": "fresh" if round_number == 1 else "resume",
            "requested_session_id": kwargs["resume_session_id"],
            "observed_session_id": "parent",
            "return_code": 0,
            "timed_out": False,
            "wall_time_seconds": 1,
            "event_file": f"round-{round_number}.jsonl",
            "stderr_file": f"round-{round_number}.stderr",
            "event_summary": {"parent_thread_id": "parent", "terminal_message": "done"},
            "raw_lines": [],
            "run_id": kwargs["run_id"],
        }

    monkeypatch.setattr(arm1, "_run_native_codex_turn", fake_turn)
    monkeypatch.setattr(arm1, "_reclaim_workspace", lambda _path: None)

    def fake_candidate(_paths, *, candidate_round, terminal_message):
        patch = f"patch-{candidate_round}"
        return ({
            "round": candidate_round,
            "base_commit": arm1.FROZEN_BASE_COMMIT,
            "revision": f"patch-sha256:{candidate_round}",
            "patch_sha256": str(candidate_round),
            "patch_bytes": len(patch),
            "changed_files": [f"round-{candidate_round}.py"],
            "patch_ref": f"reports/candidates/round-{candidate_round:03d}.patch",
            "metadata_ref": f"reports/candidates/round-{candidate_round:03d}.json",
            "terminal_message": terminal_message,
        }, patch)

    monkeypatch.setattr(arm1, "_freeze_candidate_round", fake_candidate)
    evaluations = [
        {
            "resolved": False,
            "fail_to_pass": {
                "passed": 0,
                "failed": 1,
                "total": 1,
                "failed_tests": ["tests/test_x.py::test_x"],
            },
            "pass_to_pass": {"passed": 1, "failed": 0, "total": 1, "failed_tests": []},
            "feedback_coverage": {
                "status": "current_failure_complete",
                "official_failed_test_count": 1,
                "required_case_count": 1,
                "covered_official_test_count": 1,
                "missing_test_ids": [],
                "unidentified_failed_test_count": 0,
            },
            "failure_diagnostics": {
                "source_sha256": "b" * 64,
                "cases": [{
                    "test_id": "tests/test_x.py::test_x",
                    "failure_kind": "exception_raised",
                    "exception_summary": ["ValueError"],
                    "diagnostic_excerpt": "ValueError",
                }],
            },
        },
        {
            "resolved": True,
            "fail_to_pass": {"passed": 1, "failed": 0, "total": 1, "failed_tests": []},
            "pass_to_pass": {"passed": 1, "failed": 0, "total": 1, "failed_tests": []},
            "feedback_coverage": {"status": "current_failure_complete"},
        },
    ]

    def fake_evaluator(_spec, _paths, *, candidate, invocation_start, **_kwargs):
        result = evaluations.pop(0)
        invocation = invocation_start + 1
        return result, [{
            "invocation": invocation,
            "candidate_round": candidate["round"],
            "candidate_revision": candidate["revision"],
            "result": result,
        }]

    monkeypatch.setattr(arm1, "_run_evaluator_until_feedback_complete", fake_evaluator)
    monkeypatch.setattr(arm1.p4g8, "verify_codex_source_unchanged", lambda *_args: True)
    monkeypatch.setattr(arm1, "summarize_exec_events", lambda _lines: {"parent_thread_id": "parent"})
    monkeypatch.setattr(arm1, "summarize_rollout_sessions", lambda *_args, **_kwargs: {"sessions": []})
    monkeypatch.setattr(arm1, "_merge_rollout_identity", lambda event, _rollout: event)
    monkeypatch.setattr(arm1.swe_evo, "collect_candidate_patch", lambda *_args: "patch-2")
    monkeypatch.setattr(arm1, "_codex_version", lambda: "codex-test")
    monkeypatch.setattr(arm1.validation_artifacts, "archive_validation_run", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(arm1.validation_artifacts, "model_source_redactions", lambda *_args: {})

    report = arm1.run_native_arm1(
        qualification_spec_path=spec_path,
        run_root=run_root,
        source_codex_home=source_home,
        execute_real=True,
        max_wall_seconds=100,
        max_total_wall_seconds=1000,
    )

    assert report["classification"] == "resolved"
    assert report["candidate_round_count"] == 2
    assert report["evaluator_feedback_turn_count"] == 1
    assert report["best_candidate_round"] == 2
    assert calls[0]["resume_session_id"] is None
    assert calls[1]["resume_session_id"] == "parent"
    assert "tests/test_x.py::test_x" in calls[1]["prompt"]


def test_exec_event_summary_extracts_native_subagents_and_usage():
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "parent"}),
        json.dumps({
            "type": "item.completed",
            "item": {
                "id": "spawn-1",
                "type": "collab_tool_call",
                "tool": "spawn_agent",
                "sender_thread_id": "parent",
                "receiver_thread_ids": ["child-a", "child-b"],
                "prompt": "Inspect two independent modules",
                "agents_states": {"child-a": {"status": "running"}},
                "status": "completed",
            },
        }),
        json.dumps({
            "type": "item.completed",
            "item": {
                "id": "cmd",
                "type": "command_execution",
                "command": "pytest tests/unit",
                "exit_code": 0,
                "status": "completed",
            },
        }),
        json.dumps({
            "type": "item.completed",
            "item": {"id": "msg", "type": "agent_message", "text": "Candidate complete"},
        }),
        json.dumps({
            "type": "turn.completed",
            "usage": {
                "input_tokens": 1000,
                "cached_input_tokens": 750,
                "output_tokens": 200,
                "reasoning_output_tokens": 50,
            },
        }),
    ]

    summary = arm1.summarize_exec_events(lines)

    assert summary["parent_thread_id"] == "parent"
    assert summary["child_thread_ids"] == ["child-a", "child-b"]
    assert summary["subagent_count"] == 2
    assert summary["collaboration_calls"][0]["prompt"] == "Inspect two independent modules"
    assert summary["commands"] == [{
        "command": "pytest tests/unit",
        "exit_code": 0,
        "status": "completed",
    }]
    assert summary["terminal_message"] == "Candidate complete"
    assert summary["usage"]["cached_input_tokens"] == 750
    assert summary["cache_hit_ratio"] == 0.75


def test_execution_summary_states_one_shot_measurement():
    report = {
        "wall_time_seconds": 123.4,
        "candidate": {"patch_bytes": 99, "changed_files": ["a.py"]},
        "worker": {
            "parent_thread_id": "parent",
            "subagent_count": 1,
            "rollouts": {
                "sessions": [{
                    "thread_id": "child",
                    "kind": "orchestration_subagent",
                    "agent_path": "/root/unit_runner",
                    "agent_nickname": "Hubble",
                    "depth": 1,
                    "duration_seconds": 60,
                    "compaction_count": 0,
                }],
                "aggregate_usage": {
                    "input_tokens": 1000,
                    "cached_input_tokens": 800,
                    "output_tokens": 100,
                    "reasoning_output_tokens": 20,
                },
                "aggregate_cache_hit_ratio": 0.8,
                "peak_implementation_concurrency": 2,
                "implementation_compaction_count": 0,
                "collaboration_call_counts": {"spawn_agent": 1},
            },
            "terminal_message": "Tests pass locally.",
        },
        "evaluator": {
            "resolved": False,
            "fail_to_pass": {"passed": 60, "total": 68},
            "pass_to_pass": {"passed": 242, "total": 242},
        },
    }

    rendered = arm1.render_execution_summary(report)

    assert "FAIL_TO_PASS: `60/68`" in rendered
    assert "Native implementation/audit subagents：`1`" in rendered
    assert "official evaluator 运行一次" in rendered
    assert "unit_runner" in rendered
    assert "真实的 native orchestra baseline" in rendered


def test_execution_summary_renders_iterative_progression():
    report = {
        "schema": arm1.ARM1_REPORT_SCHEMA,
        "wall_time_seconds": 300,
        "termination_reason": "official_resolved",
        "candidate": {"patch_bytes": 120, "changed_files": ["a.py", "b.py"]},
        "candidate_round_count": 2,
        "evaluator_invocation_count": 2,
        "evaluator_feedback_turn_count": 1,
        "best_candidate_round": 2,
        "candidate_rounds": [
            {
                "round": 1,
                "worker_mode": "fresh",
                "fail_to_pass": {"passed": 4, "total": 8},
                "pass_to_pass": {"passed": 10, "total": 10},
                "is_best_after_round": True,
                "evaluator_invocations": [1],
            },
            {
                "round": 2,
                "worker_mode": "resume",
                "fail_to_pass": {"passed": 8, "total": 8},
                "pass_to_pass": {"passed": 10, "total": 10},
                "is_best_after_round": True,
                "evaluator_invocations": [2],
            },
        ],
        "worker": {
            "parent_thread_id": "parent",
            "rollouts": {"sessions": [], "collaboration_call_counts": {}},
            "terminal_message": "Resolved after feedback.",
        },
        "evaluator": {
            "resolved": True,
            "fail_to_pass": {"passed": 8, "total": 8},
            "pass_to_pass": {"passed": 10, "total": 10},
        },
    }

    rendered = arm1.render_execution_summary(report)

    assert "Candidate rounds：`2`" in rendered
    assert "Evaluator feedback turns：`1`" in rendered
    assert "| 2 | `resume` | 8/8 | 10/10 | `True` | `[2]` |" in rendered
    assert "同一个 parent thread" in rendered


def test_rollout_summary_aggregates_parent_and_child_tokens(tmp_path):
    sessions = tmp_path / "sessions" / "2026" / "07" / "17"
    sessions.mkdir(parents=True)
    parent = [
        {
            "timestamp": "2026-07-17T00:00:00Z",
            "type": "session_meta",
            "payload": {"id": "parent", "source": "exec"},
        },
        {
            "timestamp": "2026-07-17T00:02:00Z",
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {"total_token_usage": {
                "input_tokens": 1000,
                "cached_input_tokens": 700,
                "output_tokens": 100,
                "reasoning_output_tokens": 20,
            }}},
        },
    ]
    child = [
        {
            "timestamp": "2026-07-17T00:00:30Z",
            "type": "session_meta",
            "payload": {"id": "child", "source": {"subagent": {"thread_spawn": {
                "parent_thread_id": "parent",
                "depth": 1,
                "agent_path": "/root/unit_runner",
                "agent_nickname": "Hubble",
            }}}},
        },
        {
            "timestamp": "2026-07-17T00:00:30Z",
            "type": "session_meta",
            "payload": {"id": "parent", "source": "exec"},
        },
        {
            "timestamp": "2026-07-17T00:00:10Z",
            "type": "event_msg",
            "payload": {"type": "context_compacted"},
        },
        {
            "timestamp": "2026-07-17T00:01:00Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "namespace": "collaboration",
                "name": "send_message",
            },
        },
        {
            "timestamp": "2026-07-17T00:01:15Z",
            "type": "event_msg",
            "payload": {"type": "context_compacted"},
        },
        {
            "timestamp": "2026-07-17T00:01:30Z",
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {"total_token_usage": {
                "input_tokens": 500,
                "cached_input_tokens": 300,
                "output_tokens": 80,
                "reasoning_output_tokens": 10,
            }}},
        },
    ]
    (sessions / "parent.jsonl").write_text(
        "\n".join(json.dumps(event) for event in parent) + "\n", encoding="utf-8"
    )
    (sessions / "child.jsonl").write_text(
        "\n".join(json.dumps(event) for event in child) + "\n", encoding="utf-8"
    )

    summary = arm1.summarize_rollout_sessions(tmp_path, parent_thread_id="parent")

    assert summary["session_count"] == 2
    assert summary["aggregate_usage"] == {
        "input_tokens": 1500,
        "cached_input_tokens": 1000,
        "output_tokens": 180,
        "reasoning_output_tokens": 30,
    }
    assert summary["aggregate_cache_hit_ratio"] == 0.666667
    assert summary["peak_observed_concurrency"] == 2
    assert {session["kind"] for session in summary["sessions"]} == {
        "parent", "orchestration_subagent"
    }
    assert summary["orchestration_subagent_count"] == 1
    assert summary["guardian_count"] == 0
    assert summary["max_orchestration_depth"] == 1
    assert summary["implementation_compaction_count"] == 1
    assert summary["collaboration_call_counts"] == {"send_message": 1}
    child_summary = next(
        session for session in summary["sessions"]
        if session["kind"] == "orchestration_subagent"
    )
    assert child_summary["thread_id"] == "child"


def test_cleanup_worker_test_artifacts_preserves_candidate_files(tmp_path):
    (tmp_path / ".pytest-run").mkdir()
    (tmp_path / ".pytest-run" / "invalid-byte-fixture").write_bytes(b"\xbd")
    (tmp_path / ".pytest_cache").mkdir()
    candidate = tmp_path / "dvc" / "command" / "plots.py"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("candidate\n", encoding="utf-8")

    cleanup = arm1.cleanup_worker_test_artifacts(tmp_path)

    assert cleanup["removed_count"] == 2
    assert not (tmp_path / ".pytest-run").exists()
    assert not (tmp_path / ".pytest_cache").exists()
    assert candidate.read_text(encoding="utf-8") == "candidate\n"


def test_collaboration_result_does_not_treat_agent_failure_text_as_tool_failure():
    assert arm1._collaboration_result_status(
        '{"agents":[{"last_task_message":"13 tests failed"}]}'
    ) == "completed"
    assert arm1._collaboration_result_status(
        "collab spawn failed: agent thread limit reached"
    ) == "failed"
