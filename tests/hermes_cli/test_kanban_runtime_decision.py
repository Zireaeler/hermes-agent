from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_decision as rd
from hermes_cli import kanban_runtime_kernel as rk


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kb.connect() as db:
        rk.ensure_runtime_schema(db)
        yield db


def _job(conn) -> str:
    root = kb.create_task(conn, title="root", initial_status="running")
    return rk.create_runtime_job(
        conn,
        root,
        "ship phase2b decision layer",
        goal_items=[
            {
                "item_key": "b-item",
                "description": "B item",
                "required": True,
                "verifier_required": True,
            },
            {
                "item_key": "a-item",
                "description": "A item",
                "required": True,
                "verifier_required": True,
            },
        ],
        initialization_mode="fixture",
    )


def _revision(conn, job_id: str) -> int:
    return int(conn.execute("SELECT graph_revision FROM runtime_jobs WHERE id = ?", (job_id,)).fetchone()[0])


def _contract() -> dict:
    return {
        "outcome": "Produce the complete verified runtime result.",
        "acceptance_criteria": ["The result exists", "Verification passes"],
        "success_evidence": ["changed_files", "verification", "worker_summary"],
        "declared_write_scope": [],
        "prohibited_actions": ["production_deployment"],
    }


class _FakeCompletions:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.outputs:
            raise AssertionError("fake client exhausted")
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=output),
                )
            ]
        )


class _FakeClient:
    def __init__(self, outputs):
        self.completions = _FakeCompletions(outputs)
        self.chat = SimpleNamespace(completions=self.completions)
        self.options_calls = []

    def with_options(self, **kwargs):
        self.options_calls.append(kwargs)
        return self


class _StaticCompactionProvider:
    provider_name = "fake-compactor"
    model = "fake-checkpoint-model"

    def __init__(self, conn, *, checkpoint_factory=None, checkpoint=None, error=None):
        self.conn = conn
        self.checkpoint_factory = checkpoint_factory
        self.checkpoint = checkpoint
        self.error = error
        self.calls = []

    def compact(self, request):
        self.calls.append(request)
        if self.error:
            return rd.CompactionProviderResult(
                checkpoint=None,
                raw_output="bad checkpoint",
                provider_name=self.provider_name,
                model=self.model,
                profile_name=request.profile["profile_name"],
                profile_version=request.profile["profile_version"],
                profile_hash=request.profile["profile_hash"],
                parse_status="parse_failed",
                error=self.error,
            )
        checkpoint = self.checkpoint
        if self.checkpoint_factory is not None:
            checkpoint = self.checkpoint_factory(request)
        return rd.CompactionProviderResult(
            checkpoint=checkpoint,
            raw_output=checkpoint,
            provider_name=self.provider_name,
            model=self.model,
            profile_name=request.profile["profile_name"],
            profile_version=request.profile["profile_version"],
            profile_hash=request.profile["profile_hash"],
            request_ref="fake-request-ref",
            response_ref="fake-response-ref",
            parse_status="parsed",
            input_token_estimate=123,
            output_token_estimate=45,
        )


def test_decision_checkpoint_schema_and_creation(conn):
    job_id = _job(conn)
    checkpoint = rd.create_decision_checkpoint(conn, job_id, reason="test")

    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert "decision_checkpoints" in tables
    assert checkpoint["reason"] == "test"
    assert checkpoint["checkpoint"]["job"]["id"] == job_id
    assert [item["item_key"] for item in checkpoint["checkpoint"]["goal_items"]] == ["a-item", "b-item"]
    assert conn.execute("SELECT COUNT(*) FROM decision_checkpoints WHERE job_id = ?", (job_id,)).fetchone()[0] == 1


def test_job_creation_creates_active_decision_segment(conn):
    job_id = _job(conn)
    segment = rk.ensure_decision_segment(conn, job_id)

    assert segment["state"] == "active"
    assert segment["segment_index"] == 0
    session = conn.execute("SELECT * FROM decision_sessions WHERE job_id = ?", (job_id,)).fetchone()
    assert session["active_segment_id"] == segment["id"]


def test_decision_delta_and_patch_entries_preserve_order(conn):
    job_id = _job(conn)
    conn.execute("UPDATE execution_nodes SET state = 'failed' WHERE job_id = ? AND node_key = 'understand-scope'", (job_id,))
    before = _revision(conn, job_id)
    patch = {
        "schema": rk.PATCH_SCHEMA,
        "expected_revision": before,
        "rationale_summary": "create implementation",
        "ops": [
                        {
                            "op": "create_node",
                "node_key": "implement-a",
                "node_type": "implementation",
                "title": "Implement A",
                "description": "Produce A evidence.",
                "goal_item_keys": ["a-item"],
            }
        ],
    }

    result = rk.advance_runtime_job(conn, job_id, create_tasks=False, decision_provider=lambda session, delta: patch)

    assert result.patch_status == "applied"
    entries = [
        row["entry_type"]
        for row in conn.execute(
            "SELECT entry_type FROM decision_segment_entries WHERE job_id = ? ORDER BY id",
            (job_id,),
        ).fetchall()
    ]
    assert entries == [
        "delta_appended",
        "provider_output",
        "patch_parsed",
        "validator_result",
        "patch_applied",
    ]


def test_manual_compaction_archives_old_segment_and_creates_new_active_segment(conn):
    job_id = _job(conn)
    old_segment = rk.ensure_decision_segment(conn, job_id)
    rk.append_decision_segment_entry(conn, job_id, "delta_appended", {"old": "context"})

    result = rd.compact_decision_session(conn, job_id, profile_name="token_budget_compaction", reason="test")

    assert result["status"] == "compacted"
    old_row = conn.execute("SELECT * FROM decision_session_segments WHERE id = ?", (old_segment["id"],)).fetchone()
    assert old_row["state"] == "compacted"
    assert old_row["compacted_checkpoint_id"] == result["checkpoint_id"]
    new_row = conn.execute("SELECT * FROM decision_session_segments WHERE id = ?", (result["new_segment_id"],)).fetchone()
    assert new_row["state"] == "active"
    session = conn.execute("SELECT * FROM decision_sessions WHERE job_id = ?", (job_id,)).fetchone()
    assert session["active_segment_id"] == result["new_segment_id"]
    assert session["latest_checkpoint_id"] == result["checkpoint_id"]


def test_checkpoint_records_profile_hash_and_revision_binding(conn):
    job_id = _job(conn)
    result = rd.compact_decision_session(conn, job_id, profile_name="token_budget_compaction", reason="test")
    checkpoint = conn.execute("SELECT * FROM decision_checkpoints WHERE id = ?", (result["checkpoint_id"],)).fetchone()

    assert checkpoint["profile_name"] == "token_budget_compaction"
    assert checkpoint["profile_version"] == "1"
    assert checkpoint["profile_hash"]
    assert checkpoint["profile_path"].endswith("token_budget_compaction.md")
    assert checkpoint["graph_revision"] == _revision(conn, job_id)
    assert checkpoint["ledger_revision"] == _revision(conn, job_id)
    assert checkpoint["validator_status"] == "accepted"


def test_compaction_profile_loader_reads_markdown_profile():
    profile = rd.load_compaction_profile("validator_boundary_compaction")

    assert profile["profile_name"] == "validator_boundary_compaction"
    assert profile["profile_version"] == "1"
    assert profile["profile_hash"]
    assert profile["profile_path"].endswith("validator_boundary_compaction.md")
    assert "Validator Boundary Compaction" in profile["content"]


def test_decision_profile_loader_reads_markdown_profile():
    profile = rd.load_decision_profile("graph_patch_decision")

    assert profile["profile_name"] == "graph_patch_decision"
    assert profile["profile_version"] == "7"
    assert "外部调研本身不构成独立 Runtime node 的理由" in profile["content"]
    assert "最多创建一个新的 runnable worker node" in profile["content"]
    assert "整个 workspace 使用 `**`" in profile["content"]
    assert "不得输出为单个字符串" in profile["content"]
    assert "每个 `waiting_coordination` target node 恰好接收一条" in profile["content"]
    assert "target node 集合必须与 snapshot" in profile["content"]
    assert "Evidence-driven coordination epoch" in profile["content"]
    assert "source_responsibility_ref" in profile["content"]
    assert "execution_discovered_gap" in profile["content"]
    assert "receipt:<node_key>:attempt-<n>" in profile["content"]
    assert profile["profile_hash"]
    assert profile["profile_path"].endswith("graph_patch_decision.md")
    assert "Graph Patch 决策 Profile" in profile["content"]


def test_should_compact_uses_token_telemetry(conn):
    job_id = _job(conn)
    rk.append_decision_segment_entry(conn, job_id, "delta_appended", {"large": "x" * 200})

    result = rd.should_compact_decision_session(
        conn,
        job_id,
        {"max_active_segment_tokens": 1},
    )

    assert result["should_compact"] is True
    assert result["reason"] == "token_threshold"
    assert result["profile_name"] == "token_budget_compaction"
    assert result["telemetry"]["active_segment_tokens"] >= 1


def test_compaction_profile_uses_configured_effective_context_ratio(conn):
    job_id = _job(conn)

    telemetry = rd.build_compaction_telemetry(
        conn,
        job_id,
        policy={
            "context_window_tokens": 353_400,
            "compaction_trigger_ratio": 0.65,
            "max_compaction_input_ratio": 0.55,
        },
    )

    assert telemetry["context_window_tokens"] == 353_400
    assert telemetry["compaction_trigger_tokens"] == 229_710
    assert telemetry["policy"]["max_compaction_input_ratio"] == 0.55


def test_compaction_source_excludes_recursive_audit_payloads(conn):
    job_id = _job(conn)
    segment = rk.ensure_decision_segment(conn, job_id)
    rk.append_decision_segment_entry(conn, job_id, "delta_appended", {"signal": "eligible"})
    sentinel = "recursive-compaction-sentinel-" + "x" * 100_000
    rk.append_decision_segment_entry(
        conn,
        job_id,
        "compaction_provider_input",
        {"rendered": sentinel},
        payload_text=sentinel,
    )

    request = rd.build_compaction_provider_request(conn, job_id, source_segment=segment)
    messages, _rendered, _profile = rd.render_compaction_messages(request)
    metrics = rd.estimate_segment_tokens(conn, segment["id"])

    assert [entry["entry_type"] for entry in request.segment_entries] == ["delta_appended"]
    assert sentinel not in json.dumps(messages)
    assert metrics["audit_segment_tokens"] > metrics["active_segment_tokens"]


def test_compaction_budget_rejection_skips_provider_and_suppresses_same_fingerprint(conn):
    job_id = _job(conn)
    rk.append_decision_segment_entry(conn, job_id, "delta_appended", {"signal": "eligible"})

    class CountingProvider:
        provider_name = "counting"
        model = "counting-model"

        def __init__(self):
            self.calls = 0

        def compact(self, _request):
            self.calls += 1
            raise AssertionError("provider must not be called for local budget rejection")

    provider = CountingProvider()
    result = rd.compact_decision_session(
        conn,
        job_id,
        compaction_provider=provider,
        fallback_to_deterministic=False,
        budget={"max_compaction_input_tokens": 1},
    )

    assert result["status"] == "rejected"
    assert result["provider_called"] is False
    assert result["provider_result"]["parse_status"] == "input_budget_rejected"
    assert provider.calls == 0
    suppressed = rd.should_compact_decision_session(
        conn,
        job_id,
        {"max_active_segment_tokens": 1},
    )
    assert suppressed["should_compact"] is False
    assert suppressed["reason"] == "unchanged_after_rejection"

    rk.append_decision_segment_entry(conn, job_id, "delta_appended", {"new_evidence": True})
    retry = rd.should_compact_decision_session(conn, job_id, {"max_active_segment_tokens": 1})
    assert retry["should_compact"] is True


def test_compaction_provider_input_audit_is_bounded_and_does_not_store_messages(conn):
    job_id = _job(conn)
    segment = rk.ensure_decision_segment(conn, job_id)
    rk.append_decision_segment_entry(conn, job_id, "delta_appended", {"old": "provider path"})
    provider = _StaticCompactionProvider(
        conn,
        checkpoint_factory=lambda request: rd.build_deterministic_checkpoint(
            conn,
            request.job_id,
            request.source_segment["id"],
            profile_name=request.profile["profile_name"],
        ),
    )

    result = rd.compact_decision_session(conn, job_id, compaction_provider=provider)

    assert result["status"] == "compacted"
    row = conn.execute(
        "SELECT payload_json, payload_text FROM decision_segment_entries "
        "WHERE segment_id = ? AND entry_type = 'compaction_provider_input'",
        (segment["id"],),
    ).fetchone()
    payload = json.loads(row["payload_json"])
    assert row["payload_text"] is None
    assert "rendered" not in payload
    assert "request" not in payload
    assert payload["request_ref"]
    assert payload["rendered_input_tokens"] <= payload["max_compaction_input_tokens"]
    assert len(row["payload_json"]) < 10_000


def test_should_compact_rejection_threshold_selects_validator_profile(conn):
    job_id = _job(conn)
    rk.append_decision_segment_entry(conn, job_id, "patch_rejected", {"status": "rejected", "reason": "bad"})

    result = rd.should_compact_decision_session(
        conn,
        job_id,
        {"max_active_segment_tokens": 999999, "rejected_patch_threshold": 1},
    )

    assert result["should_compact"] is True
    assert result["reason"] == "rejection_threshold"
    assert result["profile_name"] == "validator_boundary_compaction"


def test_advance_runtime_job_auto_compacts_when_policy_triggers(conn):
    job_id = _job(conn)
    old_segment = rk.ensure_decision_segment(conn, job_id)
    rk.append_decision_segment_entry(conn, job_id, "delta_appended", {"large": "x" * 200})

    rk.advance_runtime_job(
        conn,
        job_id,
        create_tasks=False,
        compaction_policy={"max_active_segment_tokens": 1},
    )

    old_row = conn.execute("SELECT * FROM decision_session_segments WHERE id = ?", (old_segment["id"],)).fetchone()
    assert old_row["state"] == "compacted"
    context = rd.decision_context_status(conn, job_id)
    assert context["latest_checkpoint"]["profile_name"] == "token_budget_compaction"
    assert context["active_segment"]["id"] != old_segment["id"]


def test_new_provider_input_uses_checkpoint_not_old_transcript(conn):
    job_id = _job(conn)
    rk.append_decision_segment_entry(
        conn,
        job_id,
        "provider_output",
        {"raw_output": "OLD_TRANSCRIPT_SHOULD_NOT_RETURN"},
    )
    rd.compact_decision_session(conn, job_id, profile_name="token_budget_compaction", reason="test")
    delta = rk.build_decision_delta(conn, job_id)
    request = rd.build_decision_provider_request(conn, job_id, delta)
    rendered = rd.render_decision_prompt(request)

    assert rendered["checkpoint"]["metadata"]["deterministic"] is True
    assert "OLD_TRANSCRIPT_SHOULD_NOT_RETURN" not in json.dumps(rendered, ensure_ascii=False)
    assert all(entry["entry_type"] != "provider_output" for entry in rendered["short_tail"])


def test_checkpoint_validator_rejects_unknown_node_reference(conn):
    job_id = _job(conn)
    segment = rk.ensure_decision_segment(conn, job_id)
    payload = rd.build_deterministic_checkpoint(conn, job_id, segment["id"])
    payload["graph_frontier"].append(
        {
            "node_key": "missing",
            "node_type": "implementation",
            "state": "ready",
            "summary": "missing node",
            "source_refs": [{"node_key": "missing"}],
        }
    )

    result = rd.validate_decision_checkpoint(conn, job_id, payload)

    assert result["status"] == "rejected"
    assert "unknown node_key" in result["reason"]


def test_checkpoint_validator_rejects_unknown_gap_reference(conn):
    job_id = _job(conn)
    segment = rk.ensure_decision_segment(conn, job_id)
    payload = rd.build_deterministic_checkpoint(conn, job_id, segment["id"])
    payload["open_goal_gaps"][0]["source_refs"] = [{"gap_key": "invented-gap"}]

    result = rd.validate_decision_checkpoint(conn, job_id, payload)

    assert result == {"status": "rejected", "reason": "unknown gap_key 'invented-gap'"}


def test_checkpoint_validator_rejects_mismatched_existing_gap_reference(conn):
    job_id = _job(conn)
    segment = rk.ensure_decision_segment(conn, job_id)
    payload = rd.build_deterministic_checkpoint(conn, job_id, segment["id"])
    assert len(payload["open_goal_gaps"]) >= 2
    first, second = payload["open_goal_gaps"][:2]
    first["source_refs"] = [{"gap_key": second["gap_key"]}]

    result = rd.validate_decision_checkpoint(conn, job_id, payload)

    assert result == {
        "status": "rejected",
        "reason": "open_goal_gaps gap_key does not match its provenance",
    }


def test_checkpoint_validator_enforces_fact_item_schema(conn):
    job_id = _job(conn)
    segment = rk.ensure_decision_segment(conn, job_id)
    payload = rd.build_deterministic_checkpoint(conn, job_id, segment["id"])
    del payload["open_goal_gaps"][0]["gap_type"]

    result = rd.validate_decision_checkpoint(conn, job_id, payload)

    assert result == {
        "status": "rejected",
        "reason": "open_goal_gaps item missing required fields: gap_type",
    }


def test_checkpoint_validator_rejects_failed_verifier_as_confirmed(conn):
    job_id = _job(conn)
    segment = rk.ensure_decision_segment(conn, job_id)
    payload = rd.build_deterministic_checkpoint(conn, job_id, segment["id"])
    payload["satisfied_goal_items"].append(
        {
            "goal_item_key": "a-item",
            "state": "satisfied",
            "summary": "failed verifier was misread as satisfied",
            "verification_state": "failed",
            "source_refs": [{"goal_item_key": "a-item"}],
        }
    )

    result = rd.validate_decision_checkpoint(conn, job_id, payload)

    assert result["status"] == "rejected"
    assert "verified or waived" in result["reason"]


def test_decision_prompt_layout_is_canonical(conn):
    job_id = _job(conn)
    delta = rk.build_decision_delta(conn, job_id)
    first = rd.render_decision_prompt(rd.build_decision_provider_request(conn, job_id, delta))
    second = rd.render_decision_prompt(rd.build_decision_provider_request(conn, job_id, delta))

    assert first == second
    assert "delta" not in first["stable_prefix"]
    assert first["stable_prefix"]["runtime_contract"]["forbidden_ops"] == ["release_node", "complete_job"]
    assert [item["item_key"] for item in first["checkpoint"]["goal_items"]] == ["a-item", "b-item"]


def test_provider_patch_parser_accepts_strict_json_object():
    patch = {
        "schema": rk.PATCH_SCHEMA,
        "expected_revision": 7,
        "rationale_summary": "noop",
        "ops": [],
    }
    assert rd.parse_provider_patch(json.dumps(patch), 7) == patch
    assert rd.parse_provider_patch(f"```json\n{json.dumps(patch)}\n```", 7) == patch


@pytest.mark.parametrize(
    "raw,reason",
    [
        ("Here is a patch: {}", "not valid JSON"),
        ({"schema": rk.PATCH_SCHEMA, "expected_revision": 0, "rationale_summary": "bad", "ops": [{"op": "release_node"}]}, "unsupported"),
        ({"schema": rk.PATCH_SCHEMA, "expected_revision": 0, "rationale_summary": "bad", "ops": [{"op": "complete_job"}]}, "unsupported"),
        ({"schema": "other", "expected_revision": 0, "rationale_summary": "bad", "ops": []}, "schema"),
        ({"schema": rk.PATCH_SCHEMA, "rationale_summary": "bad", "ops": []}, "expected_revision"),
        ({"schema": rk.PATCH_SCHEMA, "expected_revision": 99, "rationale_summary": "bad", "ops": []}, "does not match"),
    ],
)
def test_provider_patch_parser_rejects_free_text_and_unknown_ops(raw, reason):
    with pytest.raises(rd.ProviderPatchParseError, match=reason):
        rd.parse_provider_patch(raw, 0)


def test_runtime_decision_provider_is_no_tools_single_shot(conn):
    job_id = _job(conn)
    delta = rk.build_decision_delta(conn, job_id)
    request = rd.build_decision_provider_request(conn, job_id, delta)
    patch = {
        "schema": rk.PATCH_SCHEMA,
        "expected_revision": _revision(conn, job_id),
        "rationale_summary": "noop",
        "ops": [],
    }
    fake = _FakeClient([json.dumps(patch)])
    provider = rd.RuntimeDecisionProvider(
        provider_name="fake",
        model="fake-model",
        client=fake,
        max_retries=0,
        reasoning_effort="high",
    )

    result = provider.decide(request)

    assert result.patch == patch
    assert result.provider_name == "fake"
    assert result.model == "fake-model"
    assert result.profile_name == "graph_patch_decision"
    assert result.parse_status == "parsed"
    assert len(fake.completions.calls) == 1
    call = fake.completions.calls[0]
    assert "tools" not in call
    assert "tool_choice" not in call
    assert "web_search" not in call
    assert call["reasoning_effort"] == "high"
    assert fake.options_calls == [{"max_retries": 0}]


def test_decision_profiles_require_typed_contract_for_strategy_update():
    graph_profile = rd.load_decision_profile("graph_patch_decision")["content"]
    recovery_profile = rd.load_decision_profile("validator_recovery_decision")["content"]

    for content in (graph_profile, recovery_profile):
        assert "strategy_update" in content
        assert "typed `contract`" in content
        assert "declared_write_scope" in content
    assert "graph expansion requires `decomposition`" in recovery_profile
    assert "`context_or_runtime_limit`" in recovery_profile
    assert "another execution node remains nonterminal" in recovery_profile


def test_runtime_decision_provider_parse_retry_stays_schema_only(conn):
    job_id = _job(conn)
    delta = rk.build_decision_delta(conn, job_id)
    request = rd.build_decision_provider_request(conn, job_id, delta)
    patch = {
        "schema": rk.PATCH_SCHEMA,
        "expected_revision": _revision(conn, job_id),
        "rationale_summary": "retry fixes json",
        "ops": [],
    }
    fake = _FakeClient(["not json", json.dumps(patch)])
    provider = rd.RuntimeDecisionProvider(
        provider_name="fake",
        model="fake-model",
        client=fake,
        max_retries=1,
    )

    result = provider.decide(request)

    assert result.patch == patch
    assert result.retry_count == 1
    assert len(fake.completions.calls) == 2
    assert "tools" not in fake.completions.calls[1]
    retry_messages = fake.completions.calls[1]["messages"]
    assert "corrected JSON object" in retry_messages[-1]["content"]


def test_runtime_compaction_provider_is_no_tools_checkpoint_only(conn):
    job_id = _job(conn)
    segment = rk.ensure_decision_segment(conn, job_id)
    request = rd.build_compaction_provider_request(conn, job_id, source_segment=segment)
    checkpoint = rd.build_deterministic_checkpoint(conn, job_id, segment["id"])
    fake = _FakeClient([json.dumps(checkpoint)])
    provider = rd.RuntimeCompactionProvider(
        provider_name="fake",
        model="fake-model",
        client=fake,
        max_retries=0,
    )

    result = provider.compact(request)

    assert result.checkpoint["metadata"]["provider_generated"] is True
    assert result.provider_name == "fake"
    assert result.model == "fake-model"
    assert len(fake.completions.calls) == 1
    call = fake.completions.calls[0]
    assert "tools" not in call
    assert "tool_choice" not in call
    assert "web_search" not in call
    assert "Every non-empty checkpoint fact item" in call["messages"][0]["content"]


def test_compaction_request_exposes_bounded_provenance_contract(conn):
    job_id = _job(conn)
    request = rd.build_compaction_provider_request(conn, job_id)
    rendered = rd.render_compaction_prompt(request)

    assert request.provenance_catalog["goal_items"] == [
        {"goal_item_key": "a-item", "verified_evidence_refs": []},
        {"goal_item_key": "b-item", "verified_evidence_refs": []},
    ]
    assert {item["gap_key"] for item in request.provenance_catalog["goal_gaps"]} == {
        item["gap_key"] for item in request.db_state["open_gaps"]
    }
    assert {item["node_key"] for item in request.provenance_catalog["execution_nodes"]} == {
        item["node_key"] for item in request.db_state["frontier_nodes"]
    }
    assert request.provenance_catalog["artifacts"] == []
    assert request.provenance_catalog["validator_revisions"] == []
    source_contract = request.checkpoint_fact_schema["source_ref_contract"]
    assert source_contract["required_for_every_non_empty_fact_item"] is True
    assert source_contract["invented_references_forbidden"] is True
    assert rendered["provenance_catalog"] == request.provenance_catalog
    assert rendered["checkpoint_fact_schema"] == request.checkpoint_fact_schema
    assert rendered["stable_compaction_contract"]["inventing_source_refs_is_forbidden"] is True
    assert rendered["stable_compaction_contract"]["omit_a_fact_item_when_no_catalog_reference_exists"] is True


def test_compaction_parser_does_not_repair_missing_provenance(conn):
    job_id = _job(conn)
    request = rd.build_compaction_provider_request(conn, job_id)
    gap = request.db_state["open_gaps"][0]
    checkpoint = {
        "objective_summary": request.db_state["job"]["objective"],
        "goal_contract_revision": request.db_state["goal_contract"]["version"],
        "satisfied_goal_items": [],
        "open_goal_gaps": [
            {
                "gap_key": gap["gap_key"],
                "gap_type": gap["gap_type"],
                "summary": gap["summary"],
            }
        ],
        "open_blockers": [],
        "graph_frontier": [],
        "metadata": {},
    }

    parsed = rd.parse_compaction_checkpoint(json.dumps(checkpoint), request)
    validation = rd.validate_decision_checkpoint(conn, job_id, parsed)

    assert "source_refs" not in parsed["open_goal_gaps"][0]
    assert validation == {"status": "rejected", "reason": "open_goal_gaps item lacks provenance"}


def test_provider_candidate_with_catalog_refs_compacts_without_fallback(conn):
    job_id = _job(conn)
    old_segment = rk.ensure_decision_segment(conn, job_id)

    def candidate(request):
        return {
            "objective_summary": request.db_state["job"]["objective"],
            "goal_contract_revision": request.db_state["goal_contract"]["version"],
            "satisfied_goal_items": [],
            "open_goal_gaps": [
                {
                    **gap,
                    "source_refs": [{"gap_key": gap["gap_key"]}],
                }
                for gap in request.db_state["open_gaps"]
            ],
            "open_blockers": [],
            "graph_frontier": [
                {
                    **node,
                    "source_refs": [{"node_key": node["node_key"]}],
                }
                for node in request.db_state["frontier_nodes"]
            ],
            "metadata": {
                "source_segment_id": request.source_segment["id"],
                "db_revision": request.db_state["job"]["graph_revision"],
                "graph_revision": request.db_state["job"]["graph_revision"],
                "ledger_revision": request.db_state["job"]["graph_revision"],
            },
        }

    result = rd.compact_decision_session(
        conn,
        job_id,
        compaction_provider=_StaticCompactionProvider(conn, checkpoint_factory=candidate),
        fallback_to_deterministic=False,
    )

    assert result["status"] == "compacted"
    assert result["fallback_used"] is False
    assert result["provider_name"] == "fake-compactor"
    assert result["parse_status"] == "parsed"
    assert result["provider_validation"] == {"status": "accepted"}
    assert conn.execute(
        "SELECT state FROM decision_session_segments WHERE id = ?", (old_segment["id"],)
    ).fetchone()[0] == "compacted"


def test_compaction_provider_rejects_graph_patch_output(conn):
    job_id = _job(conn)
    request = rd.build_compaction_provider_request(conn, job_id)
    patch = {
        "schema": rk.PATCH_SCHEMA,
        "expected_revision": _revision(conn, job_id),
        "rationale_summary": "wrong output kind",
        "ops": [],
    }

    with pytest.raises(rd.ProviderPatchParseError, match="not graph patch"):
        rd.parse_compaction_checkpoint(patch, request)


def test_provider_compaction_records_audit_and_replaces_segment(conn):
    job_id = _job(conn)
    old_segment = rk.ensure_decision_segment(conn, job_id)
    rk.append_decision_segment_entry(conn, job_id, "delta_appended", {"old": "provider path"})
    provider = _StaticCompactionProvider(
        conn,
        checkpoint_factory=lambda request: rd.build_deterministic_checkpoint(
            conn,
            request.job_id,
            request.source_segment["id"],
            profile_name=request.profile["profile_name"],
        ),
    )

    result = rd.compact_decision_session(
        conn,
        job_id,
        profile_name="token_budget_compaction",
        reason="provider-test",
        compaction_provider=provider,
    )

    assert result["status"] == "compacted"
    assert result["provider_name"] == "fake-compactor"
    assert result["request_ref"] == "fake-request-ref"
    old_row = conn.execute("SELECT * FROM decision_session_segments WHERE id = ?", (old_segment["id"],)).fetchone()
    assert old_row["state"] == "compacted"
    checkpoint = conn.execute("SELECT * FROM decision_checkpoints WHERE id = ?", (result["checkpoint_id"],)).fetchone()
    metadata = json.loads(checkpoint["metadata_json"])
    assert metadata["provider_name"] == "fake-compactor"
    assert metadata["request_ref"] == "fake-request-ref"
    entries = [
        row["entry_type"]
        for row in conn.execute("SELECT entry_type FROM decision_segment_entries WHERE job_id = ? ORDER BY id", (job_id,))
    ]
    assert "compaction_provider_input" in entries
    assert "compaction_provider_output" in entries


def test_provider_compaction_rejection_preserves_active_segment_without_fallback(conn):
    job_id = _job(conn)
    old_segment = rk.ensure_decision_segment(conn, job_id)
    bad_checkpoint = rd.build_deterministic_checkpoint(conn, job_id, old_segment["id"])
    bad_checkpoint["satisfied_goal_items"].append(
        {
            "goal_item_key": "a-item",
            "state": "satisfied",
            "summary": "self reported only",
            "verification_state": "self_reported",
            "source_refs": [{"goal_item_key": "a-item"}],
        }
    )
    provider = _StaticCompactionProvider(conn, checkpoint=bad_checkpoint)

    result = rd.compact_decision_session(
        conn,
        job_id,
        compaction_provider=provider,
        fallback_to_deterministic=False,
    )

    assert result["status"] == "rejected"
    assert result["active_segment_preserved"] is True
    assert result["fallback_used"] is False
    row = conn.execute("SELECT * FROM decision_session_segments WHERE id = ?", (old_segment["id"],)).fetchone()
    assert row["state"] == "active"
    session = conn.execute("SELECT * FROM decision_sessions WHERE job_id = ?", (job_id,)).fetchone()
    assert session["active_segment_id"] == old_segment["id"]
    assert conn.execute("SELECT COUNT(*) FROM decision_checkpoints WHERE job_id = ?", (job_id,)).fetchone()[0] == 0


def test_provider_compaction_failure_falls_back_to_deterministic(conn):
    job_id = _job(conn)
    old_segment = rk.ensure_decision_segment(conn, job_id)
    provider = _StaticCompactionProvider(conn, error="invalid checkpoint json")

    result = rd.compact_decision_session(conn, job_id, compaction_provider=provider)

    assert result["status"] == "compacted"
    assert result["fallback_used"] is True
    row = conn.execute("SELECT * FROM decision_session_segments WHERE id = ?", (old_segment["id"],)).fetchone()
    assert row["state"] == "compacted"
    checkpoint = conn.execute("SELECT * FROM decision_checkpoints WHERE id = ?", (result["checkpoint_id"],)).fetchone()
    metadata = json.loads(checkpoint["metadata_json"])
    assert metadata["fallback_used"] is True
    assert metadata["provider_name"] == "deterministic"


def test_decision_request_uses_authoritative_goal_contract_when_real_checkpoint_omits_copy(conn):
    job_id = _job(conn)
    checkpoint = rd.build_deterministic_checkpoint(
        conn,
        job_id,
        rk.ensure_decision_segment(conn, job_id)["id"],
        profile_name="token_budget_compaction",
    )
    checkpoint.pop("goal_contract", None)
    result = rd.compact_decision_session(
        conn,
        job_id,
        compaction_provider=_StaticCompactionProvider(conn, checkpoint=checkpoint),
        fallback_to_deterministic=False,
    )
    assert result["status"] == "compacted"

    request = rd.build_decision_provider_request(conn, job_id, rk.build_decision_delta(conn, job_id))

    contract = conn.execute("SELECT * FROM goal_contracts WHERE job_id = ? AND state = 'active'", (job_id,)).fetchone()
    assert request.goal_contract["id"] == contract["id"]
    assert "goal_contract" not in request.checkpoint


def test_compaction_health_tracks_fallback_degradation_and_recovery(conn):
    job_id = _job(conn)

    first = rd.compact_decision_session(
        conn,
        job_id,
        compaction_provider=_StaticCompactionProvider(conn, error="first provider failure"),
    )
    second = rd.compact_decision_session(
        conn,
        job_id,
        compaction_provider=_StaticCompactionProvider(conn, error="second provider failure"),
    )
    recovered = rd.compact_decision_session(
        conn,
        job_id,
        compaction_provider=_StaticCompactionProvider(
            conn,
            checkpoint_factory=lambda request: rd.build_deterministic_checkpoint(
                conn,
                request.job_id,
                request.source_segment["id"],
                profile_name=request.profile["profile_name"],
            ),
        ),
        fallback_to_deterministic=False,
    )

    assert first["compaction_health"]["fallback_streak"] == 1
    assert second["compaction_health"]["status"] == "degraded"
    assert second["compaction_health"]["fallback_streak"] == 2
    assert recovered["compaction_health"]["status"] == "healthy"
    assert recovered["compaction_health"]["fallback_streak"] == 0
    assert recovered["compaction_health"]["fallback_count"] == 2
    events = [
        row["event_type"]
        for row in conn.execute(
            "SELECT event_type FROM execution_events WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()
    ]
    assert events.count("compaction_quality_degraded") == 1
    assert events.count("compaction_quality_recovered") == 1


def test_context_chain_selects_prior_checkpoint_when_latest_is_corrupt(conn):
    job_id = _job(conn)
    first = rd.compact_decision_session(conn, job_id, reason="first")
    second = rd.compact_decision_session(conn, job_id, reason="second")
    assert rd.latest_decision_checkpoint(conn, job_id)["id"] == second["checkpoint_id"]
    conn.execute(
        "UPDATE decision_session_segments SET compacted_checkpoint_id = 'broken' WHERE id = ?",
        (second["source_segment_id"],),
    )

    validation = rd.validate_decision_context_chain(conn, job_id)
    delta = rk.build_decision_delta(conn, job_id)
    first_request = rd.build_decision_provider_request(conn, job_id, delta)
    second_request = rd.build_decision_provider_request(conn, job_id, delta)

    assert validation["status"] == "degraded"
    assert validation["selection_mode"] == "prior_checkpoint"
    assert validation["latest_checkpoint_id"] == second["checkpoint_id"]
    assert validation["selected_checkpoint_id"] == first["checkpoint_id"]
    assert first_request.checkpoint["metadata"]["source_segment_id"] == first["source_segment_id"]
    assert second_request.checkpoint == first_request.checkpoint
    invalid_events = conn.execute(
        "SELECT COUNT(*) FROM execution_events WHERE job_id = ? AND event_type = 'decision_context_checkpoint_invalid'",
        (job_id,),
    ).fetchone()[0]
    assert invalid_events == 1


def test_context_chain_accepts_checkpoint_older_than_current_graph_revision(conn):
    job_id = _job(conn)
    compacted = rd.compact_decision_session(conn, job_id, reason="historical-revision")
    conn.execute("UPDATE runtime_jobs SET graph_revision = graph_revision + 1 WHERE id = ?", (job_id,))

    validation = rd.validate_decision_context_chain(conn, job_id)

    assert validation["status"] == "valid"
    assert validation["selected_checkpoint_id"] == compacted["checkpoint_id"]


def test_context_chain_invalid_checkpoint_falls_back_without_restoring_truth(conn):
    job_id = _job(conn)
    compacted = rd.compact_decision_session(conn, job_id, reason="broken-source")
    conn.execute(
        "UPDATE decision_checkpoints SET source_segment_id = 'missing-segment' WHERE id = ?",
        (compacted["checkpoint_id"],),
    )
    before_revision = _revision(conn, job_id)
    before_ledger = conn.execute(
        "SELECT COUNT(*) FROM progress_ledger WHERE job_id = ?", (job_id,)
    ).fetchone()[0]

    request = rd.build_decision_provider_request(conn, job_id, rk.build_decision_delta(conn, job_id))
    validation = rd.validate_decision_context_chain(conn, job_id)

    assert validation["status"] == "invalid"
    assert validation["selection_mode"] == "db_derived"
    assert validation["selected_checkpoint_id"] is None
    assert request.checkpoint["job"]["id"] == job_id
    assert _revision(conn, job_id) == before_revision
    assert conn.execute(
        "SELECT COUNT(*) FROM progress_ledger WHERE job_id = ?", (job_id,)
    ).fetchone()[0] == before_ledger


def test_checkpoint_validator_rejects_stale_revision(conn):
    job_id = _job(conn)
    segment = rk.ensure_decision_segment(conn, job_id)
    payload = rd.build_deterministic_checkpoint(conn, job_id, segment["id"])
    conn.execute("UPDATE runtime_jobs SET graph_revision = graph_revision + 1 WHERE id = ?", (job_id,))

    result = rd.validate_decision_checkpoint(conn, job_id, payload)

    assert result["status"] == "rejected"
    assert "conflicts with current revision" in result["reason"]


def test_advance_runtime_job_uses_runtime_decision_provider_interface(conn):
    job_id = _job(conn)
    conn.execute("UPDATE execution_nodes SET state = 'failed' WHERE job_id = ? AND node_key = 'understand-scope'", (job_id,))
    patch = {
        "schema": rk.PATCH_SCHEMA,
        "expected_revision": _revision(conn, job_id),
        "rationale_summary": "create implementation through runtime provider",
        "ops": [
            {
                "op": "create_node",
                "node_key": "implement-through-runtime-provider",
                "node_type": "implementation",
                "title": "Implement through runtime provider",
                "description": "Produce evidence through the new provider interface.",
                "goal_item_keys": ["a-item"],
            }
        ],
    }
    fake = _FakeClient([json.dumps(patch)])
    provider = rd.RuntimeDecisionProvider(
        provider_name="fake",
        model="fake-model",
        client=fake,
        max_retries=0,
    )

    result = rk.advance_runtime_job(conn, job_id, create_tasks=False, decision_provider=provider)

    assert result.patch_status == "applied"
    entries = [
        row["entry_type"]
        for row in conn.execute(
            "SELECT entry_type FROM decision_segment_entries WHERE job_id = ? ORDER BY id",
            (job_id,),
        ).fetchall()
    ]
    assert entries == [
        "delta_appended",
        "provider_input",
        "provider_output",
        "patch_parsed",
        "validator_result",
        "patch_applied",
    ]
    decision = conn.execute("SELECT * FROM kernel_decisions WHERE job_id = ?", (job_id,)).fetchone()
    decision_json = json.loads(decision["decision_json"])
    assert decision_json["profile_name"] == "graph_patch_decision"
    assert decision_json["model"] == "fake-model"


def test_advance_runtime_job_records_parse_retry_entry(conn):
    job_id = _job(conn)
    conn.execute("UPDATE execution_nodes SET state = 'failed' WHERE job_id = ? AND node_key = 'understand-scope'", (job_id,))
    patch = {
        "schema": rk.PATCH_SCHEMA,
        "expected_revision": _revision(conn, job_id),
        "rationale_summary": "retry then create implementation",
        "ops": [
            {
                "op": "create_node",
                "node_key": "implement-after-retry",
                "node_type": "implementation",
                "title": "Implement after retry",
                "description": "Produce evidence after parser retry.",
                "goal_item_keys": ["a-item"],
            }
        ],
    }
    fake = _FakeClient(["not json", json.dumps(patch)])
    provider = rd.RuntimeDecisionProvider(
        provider_name="fake",
        model="fake-model",
        client=fake,
        max_retries=1,
    )

    result = rk.advance_runtime_job(conn, job_id, create_tasks=False, decision_provider=provider)

    assert result.patch_status == "applied"
    entries = [
        row["entry_type"]
        for row in conn.execute(
            "SELECT entry_type FROM decision_segment_entries WHERE job_id = ? ORDER BY id",
            (job_id,),
        ).fetchall()
    ]
    assert "parse_retry" in entries
    decision = conn.execute("SELECT * FROM kernel_decisions WHERE job_id = ?", (job_id,)).fetchone()
    assert json.loads(decision["decision_json"])["retry_count"] == 1


def test_runtime_decision_provider_error_records_recoverable_event(conn):
    job_id = _job(conn)
    conn.execute("UPDATE execution_nodes SET state = 'failed' WHERE job_id = ? AND node_key = 'understand-scope'", (job_id,))
    before = _revision(conn, job_id)
    provider = rd.RuntimeDecisionProvider(
        provider_name="fake",
        model="fake-model",
        client=_FakeClient([RuntimeError("network down")]),
        max_retries=0,
    )

    result = rk.advance_runtime_job(conn, job_id, create_tasks=False, decision_provider=provider)

    assert result.patch_status == "provider_error"
    assert _revision(conn, job_id) == before
    decision = conn.execute("SELECT * FROM kernel_decisions WHERE job_id = ?", (job_id,)).fetchone()
    assert decision["status"] == "provider_error"
    assert "network down" in decision["error"]
    events = [row["event_type"] for row in conn.execute("SELECT event_type FROM execution_events WHERE job_id = ?", (job_id,))]
    assert "decision_provider_error" in events
    entries = [
        row["entry_type"]
        for row in conn.execute(
            "SELECT entry_type FROM decision_segment_entries WHERE job_id = ? ORDER BY id",
            (job_id,),
        ).fetchall()
    ]
    assert "provider_error" in entries


def test_runtime_decision_provider_parse_failure_after_retry_is_classified(conn):
    job_id = _job(conn)
    conn.execute("UPDATE execution_nodes SET state = 'failed' WHERE job_id = ? AND node_key = 'understand-scope'", (job_id,))
    before = _revision(conn, job_id)
    provider = rd.RuntimeDecisionProvider(
        provider_name="fake",
        model="fake-model",
        client=_FakeClient(["not json", "still not json"]),
        max_retries=1,
    )

    result = rk.advance_runtime_job(conn, job_id, create_tasks=False, decision_provider=provider)

    assert result.patch_status == "parse_failed"
    assert _revision(conn, job_id) == before
    decision = conn.execute("SELECT * FROM kernel_decisions WHERE job_id = ?", (job_id,)).fetchone()
    decision_json = json.loads(decision["decision_json"])
    assert decision["status"] == "parse_failed"
    assert decision_json["retry_count"] == 1
    events = [row["event_type"] for row in conn.execute("SELECT event_type FROM execution_events WHERE job_id = ?", (job_id,))]
    assert "decision_parse_failed" in events


def test_provider_parse_failure_records_decision_without_graph_change(conn):
    job_id = _job(conn)
    conn.execute("UPDATE execution_nodes SET state = 'failed' WHERE job_id = ? AND node_key = 'understand-scope'", (job_id,))
    before = _revision(conn, job_id)

    result = rk.advance_runtime_job(
        conn,
        job_id,
        create_tasks=False,
        decision_provider=lambda session, delta: "not a patch",
    )

    assert result.patch_status == "parse_failed"
    assert _revision(conn, job_id) == before
    decision = conn.execute("SELECT * FROM kernel_decisions WHERE job_id = ?", (job_id,)).fetchone()
    assert decision["status"] == "parse_failed"
    assert "not valid JSON" in decision["error"]
    events = [row["event_type"] for row in conn.execute("SELECT event_type FROM execution_events WHERE job_id = ?", (job_id,))]
    assert "decision_parse_failed" in events


def test_replay_provider_can_drive_existing_runtime_advance(conn):
    job_id = _job(conn)
    conn.execute("UPDATE execution_nodes SET state = 'failed' WHERE job_id = ? AND node_key = 'understand-scope'", (job_id,))
    patch = {
        "schema": rk.PATCH_SCHEMA,
        "expected_revision": _revision(conn, job_id),
        "rationale_summary": "replay creates implementation",
        "ops": [
            {
                "op": "create_node",
                "node_key": "implement-a-item",
                "node_type": "implementation",
                "title": "Implement A",
                "description": "Produce evidence for A.",
                "goal_item_keys": ["a-item"],
            }
        ],
    }

    provider = rd.ReplayDecisionProvider([json.dumps(patch)])
    result = rk.advance_runtime_job(conn, job_id, create_tasks=False, decision_provider=provider)

    assert result.patch_status == "applied"
    assert conn.execute(
        "SELECT node_type FROM execution_nodes WHERE job_id = ? AND node_key = 'implement-a-item'",
        (job_id,),
    ).fetchone()["node_type"] == "implementation"
    assert len(provider.calls) == 1


def test_provider_patch_rejected_records_decision_without_graph_change(conn):
    job_id = _job(conn)
    conn.execute("UPDATE execution_nodes SET state = 'failed' WHERE job_id = ? AND node_key = 'understand-scope'", (job_id,))
    before = _revision(conn, job_id)
    patch = {
        "schema": rk.PATCH_SCHEMA,
        "expected_revision": before,
        "rationale_summary": "invalid unlinked node",
        "ops": [
            {
                "op": "create_node",
                "node_key": "unlinked-node",
                "node_type": "implementation",
                "title": "Unlinked",
                "description": "No goal or gap linkage.",
            }
        ],
    }

    result = rk.advance_runtime_job(conn, job_id, create_tasks=False, decision_provider=lambda session, delta: patch)

    assert result.patch_status == "rejected"
    assert _revision(conn, job_id) == before
    decision = conn.execute("SELECT * FROM kernel_decisions WHERE job_id = ?", (job_id,)).fetchone()
    validator = json.loads(decision["validator_result_json"])
    assert validator["status"] == "rejected"
    assert "goal_item_keys" in validator["reason"]
    events = [row["event_type"] for row in conn.execute("SELECT event_type FROM execution_events WHERE job_id = ?", (job_id,))]
    assert "decision_patch_rejected" in events


def test_runtime_prompt_cli_outputs_provider_request_json(kanban_home):
    created = json.loads(kc.run_slash("runtime create 'phase2b prompt' --json"))
    payload = json.loads(kc.run_slash(f"runtime prompt {created['id']} --profile graph_patch_decision --json"))

    assert payload["request"]["job_id"] == created["id"]
    assert payload["rendered"]["stable_prefix"]["runtime_contract"]["db_is_authoritative"] is True
    assert payload["profile"]["profile_name"] == "graph_patch_decision"
    assert payload["profile"]["profile_hash"]
    assert payload["provider_call"]["no_tools"] is True
    assert payload["provider_call"]["single_shot"] is True
    assert payload["provider_call"]["input_token_estimate"] > 0
    assert "worker_log_tail" not in json.dumps(payload)


def test_runtime_provider_smoke_cli_dry_run_does_not_require_model_source(kanban_home):
    created = json.loads(kc.run_slash("runtime create 'phase3 provider smoke' --json"))
    payload = json.loads(kc.run_slash(f"runtime provider-smoke {created['id']} --json"))

    assert payload["job_id"] == created["id"]
    assert payload["dry_run"] is True
    assert payload["profile"]["profile_name"] == "graph_patch_decision"
    assert payload["provider_call"]["mode"] == "dry_run"
    assert payload["provider_call"]["model_provider"] is None
    assert payload["provider_call"]["no_tools"] is True
    assert payload["provider_call"]["input_token_estimate"] > 0
    assert "provider_result" not in payload


def test_runtime_advance_real_provider_requires_explicit_model_source(kanban_home):
    created = json.loads(kc.run_slash("runtime create 'phase3 real provider args' --json"))

    out = kc.run_slash(f"runtime advance {created['id']} --provider real --json")

    assert "--provider real requires --model-provider and --model" in out


def test_runtime_provider_smoke_execute_validates_without_apply(kanban_home, monkeypatch):
    created = json.loads(kc.run_slash("runtime create 'phase3 provider smoke execute' --json"))

    class SmokeProvider:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def decide(self, request):
            patch = {
                "schema": rk.PATCH_SCHEMA,
                "expected_revision": request.db_revision,
                "rationale_summary": "invalid unlinked node from smoke",
                "ops": [
                    {
                        "op": "create_node",
                        "node_key": "smoke-unlinked-node",
                        "node_type": "implementation",
                        "title": "Smoke unlinked node",
                        "description": "Missing goal/gap linkage.",
                    }
                ],
            }
            return rd.DecisionProviderResult(
                patch=patch,
                raw_output=json.dumps(patch),
                provider_name=self.kwargs["provider_name"],
                model=self.kwargs["model"],
                profile_name=self.kwargs["profile_name"],
                parse_status="parsed",
            )

    monkeypatch.setattr(rd, "RuntimeDecisionProvider", SmokeProvider)
    with kb.connect() as conn:
        before_patches = conn.execute("SELECT COUNT(*) FROM graph_patches WHERE job_id = ?", (created["id"],)).fetchone()[0]
        before_decisions = conn.execute("SELECT COUNT(*) FROM kernel_decisions WHERE job_id = ?", (created["id"],)).fetchone()[0]

    payload = json.loads(
        kc.run_slash(
            f"runtime provider-smoke {created['id']} --execute "
            "--model-provider fake --model fake-model --json"
        )
    )

    assert payload["dry_run"] is False
    assert payload["provider_result"]["parse_status"] == "parsed"
    assert payload["validation"]["status"] == "rejected"
    assert payload["validation"]["would_apply"] is False
    assert "goal_item_keys" in payload["validation"]["reason"]
    assert payload["applied"] is False
    with kb.connect() as conn:
        after_patches = conn.execute("SELECT COUNT(*) FROM graph_patches WHERE job_id = ?", (created["id"],)).fetchone()[0]
        after_decisions = conn.execute("SELECT COUNT(*) FROM kernel_decisions WHERE job_id = ?", (created["id"],)).fetchone()[0]
    assert after_patches == before_patches
    assert after_decisions == before_decisions


def test_runtime_provider_smoke_validator_recovery_retries_without_apply(kanban_home, monkeypatch):
    created = json.loads(kc.run_slash("runtime create 'phase3b provider recovery' --json"))

    class RecoveryProvider:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def decide(self, request):
            patch = {
                "schema": rk.PATCH_SCHEMA,
                "expected_revision": request.db_revision,
                "rationale_summary": "invalid verifier without target",
                "ops": [
                    {
                        "op": "insert_verifier",
                        "verifier_node_key": "verify-without-target",
                        "title": "Verify without target",
                        "goal_item_keys": ["initial-runtime-result"],
                    }
                ],
            }
            return rd.DecisionProviderResult(
                patch=patch,
                raw_output=json.dumps(patch),
                provider_name=self.kwargs["provider_name"],
                model=self.kwargs["model"],
                profile_name=self.kwargs["profile_name"],
                parse_status="parsed",
            )

        def decide_with_validator_feedback(self, request, *, rejected_patch, validation):
            assert validation["status"] == "rejected"
            assert "insert_verifier" in json.dumps(rejected_patch)
            patch = {
                "schema": rk.PATCH_SCHEMA,
                "expected_revision": request.db_revision,
                "rationale_summary": "recover by creating a goal-linked implementation node",
                "ops": [
                    {
                        "op": "create_node",
                        "node_key": "implement-recovered-runtime-result",
                        "node_type": "implementation",
                        "title": "Implement recovered runtime result",
                            "description": "Create evidence for the initial runtime result.",
                            "goal_item_keys": ["initial-runtime-result"],
                            "contract": _contract(),
                        }
                ],
            }
            return rd.DecisionProviderResult(
                patch=patch,
                raw_output=json.dumps(patch),
                provider_name=self.kwargs["provider_name"],
                model=self.kwargs["model"],
                profile_name="validator_recovery_decision",
                parse_status="parsed",
            )

    monkeypatch.setattr(rd, "RuntimeDecisionProvider", RecoveryProvider)
    with kb.connect() as conn:
        before_revision = _revision(conn, created["id"])
        before_patches = conn.execute("SELECT COUNT(*) FROM graph_patches WHERE job_id = ?", (created["id"],)).fetchone()[0]
        before_decisions = conn.execute("SELECT COUNT(*) FROM kernel_decisions WHERE job_id = ?", (created["id"],)).fetchone()[0]

    payload = json.loads(
        kc.run_slash(
            f"runtime provider-smoke {created['id']} --execute "
            "--model-provider fake --model fake-model --validator-retries 1 --json"
        )
    )

    assert payload["validation"]["status"] == "accepted"
    assert payload["validation"]["would_apply"] is True
    assert payload["provider_result"]["profile_name"] == "validator_recovery_decision"
    assert len(payload["recovery_attempts"]) == 2
    assert payload["recovery_attempts"][0]["validation"]["status"] == "rejected"
    assert payload["recovery_attempts"][1]["validation"]["status"] == "accepted"
    assert payload["applied"] is False
    with kb.connect() as conn:
        assert _revision(conn, created["id"]) == before_revision
        after_patches = conn.execute("SELECT COUNT(*) FROM graph_patches WHERE job_id = ?", (created["id"],)).fetchone()[0]
        after_decisions = conn.execute("SELECT COUNT(*) FROM kernel_decisions WHERE job_id = ?", (created["id"],)).fetchone()[0]
    assert after_patches == before_patches
    assert after_decisions == before_decisions


def test_runtime_provider_smoke_execute_can_use_codex_config(kanban_home, monkeypatch, tmp_path):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(
        """
model = "codex-test-model"
model_provider = "LocalCodexProvider"

[model_providers.LocalCodexProvider]
name = "LocalCodexProvider"
base_url = "http://127.0.0.1:9999/v1"
""".strip(),
        encoding="utf-8",
    )
    (codex_dir / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "test-codex-key"}),
        encoding="utf-8",
    )
    created = json.loads(kc.run_slash("runtime create 'phase3 codex config smoke' --json"))
    seen = {}

    class SmokeProvider:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def decide(self, request):
            patch = {
                "schema": rk.PATCH_SCHEMA,
                "expected_revision": request.db_revision,
                "rationale_summary": "codex config smoke noop",
                "ops": [],
            }
            return rd.DecisionProviderResult(
                patch=patch,
                raw_output=json.dumps(patch),
                provider_name=seen["provider_name"],
                model=seen["model"],
                profile_name=seen["profile_name"],
                parse_status="parsed",
            )

    monkeypatch.setattr(rd, "RuntimeDecisionProvider", SmokeProvider)

    payload = json.loads(
        kc.run_slash(
            f"runtime provider-smoke {created['id']} --execute "
            "--codex-config --profile graph_patch_decision --json"
        )
    )

    assert seen["provider_name"] == "custom"
    assert seen["model"] == "codex-test-model"
    assert seen["explicit_base_url"] == "http://127.0.0.1:9999/v1"
    assert seen["explicit_api_key"] == "test-codex-key"
    assert payload["provider_call"]["source"] == "codex_config"
    assert payload["provider_call"]["model_provider"] == "codex:LocalCodexProvider"
    assert payload["provider_call"]["explicit_base_url"] is True
    assert payload["provider_call"]["explicit_api_key"] is True
    assert payload["validation"]["status"] == "accepted"


def _runtime_metadata_arg(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"))


def _independent_verifier_evidence(job_id: str, node_key: str, payload: dict) -> dict:
    result = dict(payload)
    with kb.connect() as conn:
        node = conn.execute(
            "SELECT * FROM execution_nodes WHERE job_id = ? AND node_key = ?",
            (job_id, node_key),
        ).fetchone()
        assert node is not None
        result["verification_provenance"] = rk.build_independent_verification_provenance(conn, node["id"])
    return result


def test_runtime_complete_node_records_kanban_evidence_without_ingest(kanban_home):
    created = json.loads(kc.run_slash("runtime create 'phase3c complete-node bridge' --json"))
    job_id = created["id"]
    first = json.loads(kc.run_slash(f"runtime advance {job_id} --loop --fake-provider --json"))
    assert first["state"] == "waiting_worker"
    assert any(step["patch_status"] == "applied" for step in first["steps"])
    assert any("implement-initial-runtime-result" in step["materialized_nodes"] for step in first["steps"])

    evidence = {
        "verdict": "succeeded",
        "summary": "primary worker found remaining implementation work",
        "unmet_goal_items": ["initial-runtime-result"],
        "verification": {"passed": False},
    }
    payload = json.loads(
        kc.run_slash(
            f"runtime complete-node {job_id} implement-initial-runtime-result "
            "--summary 'primary worker found remaining implementation work' "
            f"--metadata '{_runtime_metadata_arg(evidence)}' --json"
        )
    )
    assert payload["ingest_required"] is True
    assert payload["graph_revision_before"] == payload["graph_revision_after"]
    assert payload["ledger_count_before"] == 0
    assert payload["ledger_count_after"] == 0

    status = json.loads(kc.run_slash(f"runtime status {job_id} --json"))
    node = next(item for item in status["nodes"] if item["node_key"] == "implement-initial-runtime-result")
    assert node["state"] == "running"
    assert status["progress_ledger"] == []


def test_runtime_cli_completes_worker_owned_goal_with_evidence_bridge(kanban_home):
    created = json.loads(kc.run_slash("runtime create 'phase3c worker-owned runtime loop' --json"))
    job_id = created["id"]

    first = json.loads(kc.run_slash(f"runtime advance {job_id} --loop --fake-provider --json"))
    assert first["state"] == "waiting_worker"
    assert any(step["patch_status"] == "applied" for step in first["steps"])
    assert any("implement-initial-runtime-result" in step["materialized_nodes"] for step in first["steps"])

    implementation_evidence = {
        "verdict": "succeeded",
        "summary": "implementation produced and locally verified goal evidence",
        "claimed_goal_items": ["initial-runtime-result"],
        "verification": {"commands": ["pytest"], "passed": True, "summary": "passed"},
    }
    json.loads(
        kc.run_slash(
            f"runtime complete-node {job_id} implement-initial-runtime-result "
            "--summary 'implementation produced and locally verified goal evidence' "
            f"--metadata '{_runtime_metadata_arg(implementation_evidence)}' --json"
        )
    )
    final = json.loads(kc.run_slash(f"runtime advance {job_id} --loop --fake-provider --json"))
    assert final["state"] == "done"
    assert any("implement-initial-runtime-result" in step["ingested_nodes"] for step in final["steps"])

    status = json.loads(kc.run_slash(f"runtime status {job_id} --json"))
    assert status["job"]["state"] == "done"
    assert status["goal_items"][0]["state"] == "satisfied"
    assert any(row["verification_state"] == "implementation_verified" for row in status["progress_ledger"])
    assert all(node["node_type"] != "verification" for node in status["nodes"])
    assert len(status["materializations"]) == 1

    decisions = json.loads(kc.run_slash(f"runtime decision {job_id} --json"))
    assert len(decisions) == 1
    assert any(row["validator_result"]["status"] == "applied" for row in decisions)


def test_runtime_cli_resumes_and_completes_after_goal_waiver(kanban_home):
    created = json.loads(
        kc.run_slash(
            "runtime create 'phase3d waiver resume loop' "
            "--goal-item core-result:Core-result "
            "--goal-item stretch-result:Stretch-result --json"
        )
    )
    job_id = created["id"]
    first = json.loads(kc.run_slash(f"runtime advance {job_id} --loop --fake-provider --json"))
    assert first["state"] == "waiting_worker"
    assert any("implement-core-result" in step["materialized_nodes"] for step in first["steps"])

    core_evidence = {
        "verdict": "succeeded",
        "summary": "core result verified",
        "claimed_goal_items": ["core-result"],
        "verification": {"commands": ["pytest"], "passed": True, "summary": "passed"},
    }
    json.loads(
        kc.run_slash(
            f"runtime complete-node {job_id} implement-core-result "
            "--summary 'core result verified' "
            f"--metadata '{_runtime_metadata_arg(core_evidence)}' --json"
        )
    )
    resumed = json.loads(kc.run_slash(f"runtime advance {job_id} --loop --provider none --json"))
    assert resumed["state"] == "waiting_decision"
    assert any("implement-core-result" in step["ingested_nodes"] for step in resumed["steps"])

    waived = json.loads(
        kc.run_slash(
            f"runtime waive-goal {job_id} stretch-result "
            "--reason 'user deferred stretch goal for this runtime slice' --source user --json"
        )
    )
    assert waived["state"] == "waived"
    assert waived["job_state"] == "done"

    status = json.loads(kc.run_slash(f"runtime status {job_id} --json"))
    states = {item["item_key"]: item["state"] for item in status["goal_items"]}
    assert states == {"core-result": "satisfied", "stretch-result": "waived"}
    assert status["job"]["state"] == "done"
    assert any(row["satisfaction"] == "waived" for row in status["progress_ledger"])
    event_types = [event["event_type"] for event in status["recent_events"]]
    assert "goal_item_waived" in event_types
    assert "human_decision_received" in event_types


def test_runtime_checkpoint_cli_outputs_db_derived_checkpoint(kanban_home):
    created = json.loads(kc.run_slash("runtime create 'phase2b checkpoint' --json"))
    payload = json.loads(kc.run_slash(f"runtime checkpoint {created['id']} --create --json"))

    assert payload["job_id"] == created["id"]
    assert payload["checkpoint"]["job"]["id"] == created["id"]
    assert payload["checkpoint"]["goal_items"][0]["item_key"] == "initial-runtime-result"


def test_runtime_compact_cli_outputs_segment_replacement(kanban_home):
    created = json.loads(kc.run_slash("runtime create 'phase2d compact' --json"))
    payload = json.loads(kc.run_slash(f"runtime compact {created['id']} --profile token_budget_compaction --json"))

    assert payload["status"] == "compacted"
    assert payload["checkpoint_id"]
    assert payload["source_segment_id"] != payload["new_segment_id"]


def test_runtime_compact_cli_fake_provider_outputs_provider_audit(kanban_home):
    created = json.loads(kc.run_slash("runtime create 'phase4 fake compact' --json"))
    payload = json.loads(kc.run_slash(f"runtime compact {created['id']} --provider fake --json"))

    assert payload["status"] == "compacted"
    assert payload["provider_mode"] == "fake"
    assert payload["provider_name"] == "deterministic"
    assert payload["request_ref"]
    assert payload["response_ref"]
    context = json.loads(kc.run_slash(f"runtime context {created['id']} --json"))
    assert context["latest_checkpoint"]["metadata"]["provider_name"] == "deterministic"
    assert context["latest_checkpoint"]["metadata"]["fallback_used"] is False


def test_runtime_context_cli_outputs_active_segment_and_checkpoint(kanban_home):
    created = json.loads(kc.run_slash("runtime create 'phase2d context' --json"))
    json.loads(kc.run_slash(f"runtime compact {created['id']} --json"))
    payload = json.loads(kc.run_slash(f"runtime context {created['id']} --json"))

    assert payload["job_id"] == created["id"]
    assert payload["active_segment"]["state"] == "active"
    assert payload["latest_checkpoint"]["validator_status"] == "accepted"
    assert "strict_short_tail" in payload["provider_input_composition"]
    assert "compaction_policy" in payload


def test_runtime_inspect_cli_outputs_observability_snapshot(kanban_home):
    created = json.loads(kc.run_slash("runtime create 'phase4 inspect' --json"))
    json.loads(kc.run_slash(f"runtime compact {created['id']} --provider fake --json"))

    payload = json.loads(kc.run_slash(f"runtime inspect {created['id']} --json"))

    assert payload["job"]["id"] == created["id"]
    assert {"goals", "progress_ledger", "graph", "events", "patches", "decisions"}.issubset(payload)
    assert {"decision_session", "checkpoints", "compactions", "human_gates", "liveness"}.issubset(payload)
    assert payload["operator_actions"]["read_only"] is True
    assert payload["compactions"]["checkpoints"][0]["profile_hash"]
    assert payload["compactions"]["health"]["status"] == "healthy"
    assert payload["compactions"]["context_chain_validation"]["status"] == "valid"


def test_runtime_supervise_cli_runs_leased_tick(kanban_home):
    created = json.loads(kc.run_slash("runtime create 'phase4 supervise' --json"))
    payload = json.loads(
        kc.run_slash(
            f"runtime supervise --job-id {created['id']} --owner test-supervisor --json"
        )
    )

    assert payload["status"] == "advanced"
    assert payload["lock"]["owner"] == "test-supervisor"
    assert payload["result"]["materialized_nodes"] == []
    assert payload["result"]["decision_requested"] is True
    status = json.loads(kc.run_slash(f"runtime status {created['id']} --json"))
    assert status["job"]["state"] == "waiting_decision"
    assert status["liveness"]["legal_wait"] is True
    assert status["job"]["advance_lock"] is None


def test_runtime_decision_cli_outputs_parse_failure_record(kanban_home):
    created = json.loads(kc.run_slash("runtime create 'phase2b decision list' --json"))
    with kb.connect() as conn:
        conn.execute(
            "UPDATE execution_nodes SET state = 'failed' WHERE job_id = ? AND node_key = 'understand-scope'",
            (created["id"],),
        )
        rk.advance_runtime_job(
            conn,
            created["id"],
            create_tasks=False,
            decision_provider=lambda session, delta: "not a patch",
        )

    rows = json.loads(kc.run_slash(f"runtime decision {created['id']} --json"))
    assert rows[0]["status"] == "parse_failed"
    assert rows[0]["validator_result"]["status"] == "parse_failed"


def test_runtime_decision_cli_outputs_provider_audit_fields(kanban_home):
    created = json.loads(kc.run_slash("runtime create 'phase3 decision audit' --json"))
    with kb.connect() as conn:
        conn.execute(
            "UPDATE execution_nodes SET state = 'failed' WHERE job_id = ? AND node_key = 'understand-scope'",
            (created["id"],),
        )
        revision = _revision(conn, created["id"])
        patch = {
            "schema": rk.PATCH_SCHEMA,
            "expected_revision": revision,
            "rationale_summary": "audit noop",
            "ops": [],
        }
        provider = rd.RuntimeDecisionProvider(
            provider_name="fake",
            model="fake-model",
            client=_FakeClient([json.dumps(patch)]),
            max_retries=0,
        )
        rk.advance_runtime_job(
            conn,
            created["id"],
            create_tasks=False,
            decision_provider=provider,
        )

    rows = json.loads(kc.run_slash(f"runtime decision {created['id']} --json"))
    assert rows[0]["provider"] == "fake"
    assert rows[0]["model"] == "fake-model"
    assert rows[0]["profile_name"] == "graph_patch_decision"
    assert rows[0]["profile_hash"]
    assert rows[0]["request_ref"]
    assert rows[0]["response_ref"]
    assert rows[0]["parse_status"] == "parsed"
    assert rows[0]["retry_count"] == 0

    text = kc.run_slash(f"runtime decision {created['id']}")
    assert "provider=fake" in text
    assert "model=fake-model" in text
    assert "profile=graph_patch_decision" in text
