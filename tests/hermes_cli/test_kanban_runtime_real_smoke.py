from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_decision as rd
from hermes_cli import kanban_runtime_kernel as rk
from hermes_cli import kanban_runtime_real_smoke as rs


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


def _root_task(conn) -> str:
    return kb.create_task(conn, title="root goal", initial_status="running")


def _job(conn) -> str:
    return rk.create_runtime_job(
        conn,
        _root_task(conn),
        "phase4g1 real provider smoke fixture",
        goal_items=[
            {
                "item_key": "runtime-result",
                "description": "runtime result evidence exists",
                "required": True,
                "verifier_required": True,
            }
        ],
    )


def _node(conn, job_id: str, node_key: str):
    return conn.execute(
        "SELECT * FROM execution_nodes WHERE job_id = ? AND node_key = ?",
        (job_id, node_key),
    ).fetchone()


def _provider_source(secret: str = "real-smoke-secret") -> dict:
    return {
        "source": "explicit",
        "provider_name": "fake-real",
        "display_provider": "fake-real",
        "model": "fake-real-model",
        "explicit_base_url": "http://127.0.0.1:1/v1",
        "explicit_api_key": secret,
    }


class AcceptingDecisionProvider:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def decide(self, request):
        patch = {
            "schema": rk.PATCH_SCHEMA,
            "expected_revision": request.db_revision,
            "rationale_summary": "fake real provider creates linked work",
            "ops": [
                {
                    "op": "create_node",
                    "node_key": f"real-smoke-fix-{request.db_revision}",
                    "node_type": "implementation",
                    "title": "Real smoke fix",
                    "description": "Create a linked runtime node from fake real provider.",
                    "goal_item_keys": ["runtime-result"],
                    "gap_keys": ["runtime-result:missing_evidence"],
                }
            ],
        }
        return rd.DecisionProviderResult(
            patch=patch,
            raw_output=json.dumps(patch),
            provider_name=self.kwargs["provider_name"],
            model=self.kwargs["model"],
            profile_name=self.kwargs["profile_name"],
            request_ref="req_fake_real",
            response_ref="resp_fake_real",
            parse_status="parsed",
        )


class ProviderErrorDecisionProvider:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def decide(self, request):
        return rd.DecisionProviderResult(
            patch=None,
            raw_output=None,
            provider_name=self.kwargs["provider_name"],
            model=self.kwargs["model"],
            profile_name=self.kwargs["profile_name"],
            request_ref="req_error",
            parse_status="provider_error",
            error="synthetic provider error",
        )


class AcceptingCompactionProvider:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def compact(self, request):
        revision = int(request.db_state["job"]["graph_revision"])
        checkpoint = {
            "objective_summary": request.db_state["job"]["objective"],
            "goal_contract_revision": request.db_state["goal_contract"]["version"],
            "satisfied_goal_items": [],
            "open_goal_gaps": [
                {
                    "gap_key": gap["gap_key"],
                    "gap_type": gap["gap_type"],
                    "summary": gap["summary"],
                    "source_refs": [{"gap_key": gap["gap_key"]}],
                }
                for gap in request.db_state["open_gaps"]
            ],
            "open_blockers": [],
            "graph_frontier": [
                {
                    "node_key": node["node_key"],
                    "node_type": node["node_type"],
                    "state": node["state"],
                    "summary": node["summary"],
                    "source_refs": [{"node_key": node["node_key"]}],
                }
                for node in request.db_state["frontier_nodes"]
            ],
            "metadata": {
                "source_segment_id": request.source_segment["id"],
                "db_revision": revision,
                "graph_revision": revision,
                "ledger_revision": revision,
            },
        }
        return rd.CompactionProviderResult(
            checkpoint=checkpoint,
            raw_output=checkpoint,
            provider_name=self.kwargs["provider_name"],
            model=self.kwargs["model"],
            profile_name=request.profile["profile_name"],
            profile_version=request.profile["profile_version"],
            profile_hash=request.profile["profile_hash"],
            request_ref="req_compact",
            response_ref="resp_compact",
            parse_status="parsed",
        )


def _prepare_waiting_decision(conn, job_id: str) -> None:
    rk.advance_runtime_job(conn, job_id, create_tasks=True)
    node = _node(conn, job_id, "understand-scope")
    assert kb.complete_task(
        conn,
        node["latest_task_id"],
        result="synthetic failure",
        summary="synthetic failure",
        metadata={
            "verdict": "failed",
            "summary": "synthetic failure",
            "unmet_goal_items": ["runtime-result"],
            "verification": {"passed": False},
        },
    )
    rk.advance_runtime_job(conn, job_id, create_tasks=False)
    assert rk._job(conn, job_id)["state"] == "waiting_decision"


def test_real_smoke_dry_run_does_not_require_source_or_write_decisions(conn):
    job_id = _job(conn)
    before = {
        "revision": rk._job(conn, job_id)["graph_revision"],
        "patches": conn.execute("SELECT COUNT(*) FROM graph_patches WHERE job_id = ?", (job_id,)).fetchone()[0],
        "decisions": conn.execute("SELECT COUNT(*) FROM kernel_decisions WHERE job_id = ?", (job_id,)).fetchone()[0],
        "events": conn.execute("SELECT COUNT(*) FROM execution_events WHERE job_id = ?", (job_id,)).fetchone()[0],
    }

    report = rs.run_real_model_smoke(conn, job_id)

    assert report["decision_dry_run"]["called_model"] is False
    assert report["decision_execute"] is None
    assert report["one_step_advance"] is None
    assert report["real_compaction"] is None
    assert report["secrets_leaked"] is False
    assert rk._job(conn, job_id)["graph_revision"] == before["revision"]
    assert conn.execute("SELECT COUNT(*) FROM graph_patches WHERE job_id = ?", (job_id,)).fetchone()[0] == before["patches"]
    assert conn.execute("SELECT COUNT(*) FROM kernel_decisions WHERE job_id = ?", (job_id,)).fetchone()[0] == before["decisions"]
    assert conn.execute("SELECT COUNT(*) FROM execution_events WHERE job_id = ?", (job_id,)).fetchone()[0] == before["events"]


def test_real_smoke_execute_validates_without_apply_and_redacts_secret(conn, monkeypatch):
    job_id = _job(conn)
    monkeypatch.setattr(rd, "RuntimeDecisionProvider", AcceptingDecisionProvider)

    report = rs.run_real_model_smoke(
        conn,
        job_id,
        provider_source=_provider_source(secret="super-secret-real-smoke-key"),
        execute_decision=True,
    )

    assert report["decision_execute"]["called_model"] is True
    assert report["decision_execute"]["provider_result"]["parse_status"] == "parsed"
    assert report["decision_execute"]["validation"]["status"] == "accepted"
    assert report["decision_execute"]["applied"] is False
    assert report["decision_execute"]["graph_patches_after"] == 0
    assert report["decision_execute"]["kernel_decisions_after"] == 0
    assert report["provider"]["explicit_api_key"] is True
    assert report["secrets_leaked"] is False
    assert "super-secret-real-smoke-key" not in json.dumps(report, ensure_ascii=False)


def test_real_smoke_execute_provider_error_is_classified(conn, monkeypatch):
    job_id = _job(conn)
    monkeypatch.setattr(rd, "RuntimeDecisionProvider", ProviderErrorDecisionProvider)

    report = rs.run_real_model_smoke(conn, job_id, provider_source=_provider_source(), execute_decision=True)

    assert report["decision_execute"]["provider_result"]["parse_status"] == "provider_error"
    assert report["decision_execute"]["validation"]["status"] == "skipped"
    assert report["decision_execute"]["validation"]["would_apply"] is False
    assert report["consistency"]["status"] == "passed"


def test_real_smoke_apply_and_compaction_use_runtime_boundaries(conn, monkeypatch):
    job_id = _job(conn)
    _prepare_waiting_decision(conn, job_id)
    monkeypatch.setattr(rd, "RuntimeDecisionProvider", AcceptingDecisionProvider)
    monkeypatch.setattr(rd, "RuntimeCompactionProvider", AcceptingCompactionProvider)

    report = rs.run_real_model_smoke(
        conn,
        job_id,
        provider_source=_provider_source(),
        execute_decision=True,
        apply_decision=True,
        compact=True,
    )

    assert report["decision_execute"]["validation"]["status"] == "accepted"
    assert report["one_step_advance"]["called_model"] is True
    assert report["one_step_advance"]["patch_status"] == "applied"
    assert report["one_step_advance"]["kernel_decisions_after"] == report["one_step_advance"]["kernel_decisions_before"] + 1
    assert report["real_compaction"]["called_model"] is True
    assert report["real_compaction"]["status"] == "compacted"
    assert report["real_compaction"]["fallback_used"] is False
    assert report["consistency"]["status"] == "passed"
