from pathlib import Path

from hermes_cli import phase4g16_natural_calibration as phase4g16


def _treatment(*, actions=None, checkpoints=None, candidate_count=0):
    return {
        "oracle": {"passed": True},
        "candidate_count": candidate_count,
        "consistency": {"status": "passed"},
        "orchestration": {
            "coordination": {
                "actions": list(actions or []),
                "checkpoints": list(checkpoints or []),
                "terminal_responsibility_candidates": {
                    "expanded_count": 0,
                    "resolved_without_expansion_count": 0,
                },
            }
        },
    }


def test_frozen_cases_do_not_preannounce_topology_or_candidate_keys():
    cases = phase4g16._cases()

    assert [item.kind for item in cases] == [
        "coherent_negative_control",
        "shared_contract_medium",
        "durable_boundary_medium",
    ]
    for case in cases:
        text = case.objective.lower()
        assert "candidate_key" not in text
        assert "responsibility_candidate" not in text
        assert "create_node" not in text
        assert "src/" not in text
        assert "拆分" not in text


def test_each_frozen_repository_starts_with_a_failing_oracle(tmp_path):
    for case in phase4g16._cases():
        workspace = tmp_path / case.key
        revision = phase4g16._write_repository(workspace, case)
        result = phase4g16._oracle(workspace)

        assert len(revision) == 40
        assert result["passed"] is False
        assert result["test_count"] is not None


def test_negative_control_rejects_any_coordination_action():
    case = phase4g16._cases()[0]
    baseline = {"oracle": {"passed": True}}

    clean = phase4g16._acceptance(case, baseline, _treatment())
    noisy = phase4g16._acceptance(
        case,
        baseline,
        _treatment(actions=[{"id": "action-1"}]),
    )

    assert clean["no_false_coordination"] is True
    assert noisy["no_false_coordination"] is False


def test_shared_contract_accepts_natural_checkpoint_or_coherent_route():
    case = phase4g16._cases()[1]
    baseline = {"oracle": {"passed": True}}

    coherent = phase4g16._acceptance(case, baseline, _treatment())
    checkpointed = phase4g16._acceptance(
        case,
        baseline,
        _treatment(checkpoints=[{"event_id": 1}]),
    )

    assert coherent["checkpoint_or_coherent_route"] is True
    assert checkpointed["checkpoint_or_coherent_route"] is True


def test_durable_boundary_requires_natural_candidate_and_provider_consumption():
    case = phase4g16._cases()[2]
    baseline = {"oracle": {"passed": True}}
    action = {
        "id": "action-1",
        "route": "provider_required",
        "status": "applied",
    }

    missing = phase4g16._acceptance(case, baseline, _treatment())
    consumed = phase4g16._acceptance(
        case,
        baseline,
        _treatment(actions=[action], candidate_count=1),
    )

    assert missing["natural_candidate_observed"] is False
    assert missing["candidate_consumed_by_provider"] is False
    assert consumed["natural_candidate_observed"] is True
    assert consumed["candidate_consumed_by_provider"] is True


def test_baseline_prompt_keeps_one_worker_and_no_runtime_answer():
    prompt = phase4g16._baseline_prompt(phase4g16._cases()[2])

    assert "唯一的 coherent implementation worker" in prompt
    assert "不要委派 subagent" in prompt
    assert "candidate_key" not in prompt
    assert "create_node" not in prompt


def test_isolated_config_summary_omits_paths_and_provider_endpoint(tmp_path):
    audit = {
        "source_home": str(Path.home() / ".codex"),
        "target_home": str(tmp_path / "codex"),
        "proxy_base_url": "https://sensitive.invalid/v1",
        "model": "model",
        "reasoning_effort": "max",
        "multi_agent_enabled": False,
        "provider_transport": {"supports_websockets": True},
    }

    summary = phase4g16._isolated_config_summary(audit)

    assert summary["model"] == "model"
    assert summary["provider_transport"] == {"supports_websockets": True}
    assert "source_home" not in summary
    assert "target_home" not in summary
    assert "proxy_base_url" not in summary
