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


def test_parent_prompt_grants_native_allocation_without_oracle_feedback(tmp_path):
    prompt = arm1.build_parent_prompt(
        {"srs": "Requirement one.\nRequirement two."},
        toolchain=tmp_path / "toolchain",
    )

    assert "Proactively use native Codex\nsubagents" in prompt
    assert "Decide their count, roles, scopes" in prompt
    assert "No official evaluator result will be provided" in prompt
    assert "Requirement one." in prompt
    assert "gold patch" in prompt
    assert "FAIL_TO_PASS" not in prompt
    assert "58/68" not in prompt


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
