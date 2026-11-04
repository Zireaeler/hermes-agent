"""Codex 后代并发硬限制、研究网络和无单轮时限的机械配置。"""

from types import SimpleNamespace

import pytest

import hermes_cli.orchestra_v1_codex as codex
from tests.test_orchestra_v1_codex import (
    FakeClient,
    completed_notifications,
    compaction_notifications,
)


@pytest.mark.parametrize("resume", [None, "thread-existing"])
def test_configures_three_descendant_slots_with_network(tmp_path, resume):
    created = []
    client = FakeClient(notifications=completed_notifications())

    def factory(**kwargs):
        created.append(kwargs)
        return client

    result = codex.run_codex_turn(
        prompt="研究", workspace=tmp_path, resume_thread_id=resume,
        max_subagents=3, research_network=True, client_factory=factory,
    )

    assert result.status == "completed"
    args = created[0]["extra_args"]
    assert "--strict-config" in args
    overrides = {args[i + 1] for i, value in enumerate(args) if value == "-c"}
    assert {
        "agents.enabled=true",
        "features.multi_agent=true",
        "agents.max_concurrent_threads_per_session=3",
        "features.multi_agent_v2={enabled=true,max_concurrent_threads_per_session=4}",
        "sandbox_workspace_write.writable_roots=[]",
        "sandbox_workspace_write.network_access=true",
        'web_search="live"',
    } <= overrides


def test_zero_subagents_disables_both_backends(tmp_path):
    created = []

    def factory(**kwargs):
        created.append(kwargs)
        return FakeClient(notifications=completed_notifications())

    codex.run_codex_turn(
        prompt="任务", workspace=tmp_path, max_subagents=0, client_factory=factory,
    )
    args = created[0]["extra_args"]
    assert "features.multi_agent=false" in args
    assert "features.multi_agent_v2=false" in args
    assert "agents.enabled=false" in args
    assert not any("max_concurrent_threads" in arg for arg in args)


@pytest.mark.parametrize("limit", [-1, 1.5, True])
def test_rejects_invalid_child_limit_before_starting_process(tmp_path, limit):
    def factory(**_kwargs):
        raise AssertionError("非法参数不得创建服务")

    with pytest.raises(ValueError, match="max_subagents"):
        codex.run_codex_turn(
            prompt="任务", workspace=tmp_path, max_subagents=limit,
            client_factory=factory,
        )


def test_no_timeout_can_pass_two_hours_without_interrupt(tmp_path, monkeypatch):
    ticks = iter([0.0, 7201.0, 7202.0, 7203.0])
    monkeypatch.setattr(codex, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    client = FakeClient(notifications=completed_notifications())
    result = codex.run_codex_turn(
        prompt="长期研究", workspace=tmp_path, timeout_seconds=None,
        client_factory=lambda **_kwargs: client,
    )
    assert result.status == "completed"
    assert not any(method == "turn/interrupt" for method, _ in client.requests)


def test_no_timeout_still_interrupts_when_project_requests_control(tmp_path):
    class ControlledClient(FakeClient):
        def request(self, method, params=None, timeout=30):
            response = super().request(method, params, timeout)
            if method == "turn/interrupt":
                self.notifications.append({
                    "method": "turn/completed",
                    "params": {"turn": {"id": "turn-1", "status": "interrupted"}},
                })
            return response

    client = ControlledClient()
    result = codex.run_codex_turn(
        prompt="长期研究", workspace=tmp_path, timeout_seconds=None,
        on_poll=lambda: "project_budget", client_factory=lambda **_kwargs: client,
    )
    assert result.status == "interrupted"
    assert result.control_reason == "project_budget"
    assert result.termination_confirmed


def test_unbounded_compaction_obeys_project_cancellation(tmp_path):
    class CompactClient(FakeClient):
        def request(self, method, params=None, timeout=30):
            response = super().request(method, params, timeout)
            if method == "turn/interrupt":
                self.notifications.append({
                    "method": "turn/completed",
                    "params": {"turn": {"id": "turn-compact", "status": "interrupted"}},
                })
            return response

    client = CompactClient(notifications=[compaction_notifications()[0]])
    result = codex.run_codex_turn(
        prompt="继续", workspace=tmp_path, timeout_seconds=None,
        compact_before_turn=True, resume_thread_id="thread-existing",
        on_poll=lambda: "project_budget", client_factory=lambda **_kwargs: client,
    )
    assert result.control_reason == "project_budget"
    assert result.termination_confirmed
    assert not any(method == "turn/start" for method, _ in client.requests)


def descendant_notifications():
    return [{
        "method": "item/completed", "params": {
            "threadId": "thread-new", "item": {
                "type": "subAgentActivity", "kind": "started",
                "agentThreadId": "child-1",
            },
        },
    }, {
        "method": "turn/started", "params": {
            "threadId": "child-1", "turn": {"id": "child-turn"},
        },
    }]


def test_root_completion_collects_active_descendant_termination(tmp_path):
    class ChildClient(FakeClient):
        def request(self, method, params=None, timeout=30):
            response = super().request(method, params, timeout)
            if method == "turn/interrupt" and params["threadId"] == "child-1":
                self.notifications.append({
                    "method": "turn/completed", "params": {
                        "threadId": "child-1",
                        "turn": {"id": "child-turn", "status": "interrupted"},
                    },
                })
            return response

    client = ChildClient(
        notifications=descendant_notifications() + completed_notifications(),
    )
    result = codex.run_codex_turn(
        prompt="研究", workspace=tmp_path, timeout_seconds=None,
        client_factory=lambda **_kwargs: client,
    )
    assert result.status == "completed"
    assert result.termination_confirmed
    assert ("turn/interrupt", {
        "threadId": "child-1", "turnId": "child-turn",
    }) in client.requests


def test_unconfirmed_descendant_prevents_claiming_all_execution_stopped(tmp_path):
    client = FakeClient(
        notifications=descendant_notifications() + completed_notifications(),
    )
    result = codex.run_codex_turn(
        prompt="研究", workspace=tmp_path, timeout_seconds=None,
        interrupt_timeout_seconds=0.01,
        client_factory=lambda **_kwargs: client,
    )
    assert result.status == "completed"
    assert not result.termination_confirmed
    assert "descendant termination unconfirmed" in result.error
