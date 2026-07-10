"""Bounded real-model smoke orchestration for the Kanban runtime kernel.

This module does not introduce a new provider path.  It composes the existing
provider request renderer, RuntimeDecisionProvider, RuntimeCompactionProvider,
patch validator, and production advance function into a bounded report.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from hermes_cli import kanban_runtime_decision as rd
from hermes_cli import kanban_runtime_kernel as rk


def run_real_model_smoke(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    provider_source: Optional[dict[str, Any]] = None,
    execute_decision: bool = False,
    apply_decision: bool = False,
    compact: bool = False,
    profile_name: str = "graph_patch_decision",
    compaction_profile_name: str = "token_budget_compaction",
    max_retries: int = 1,
    timeout_seconds: Optional[float] = None,
    fallback_to_deterministic: bool = True,
) -> dict[str, Any]:
    """Run Phase 4G1 real provider smoke steps and return a bounded report.

    Dry-run rendering is always performed and never calls a model.  Real model
    calls are only made when execute_decision, apply_decision, or compact is set
    and a provider_source is supplied by the caller.
    """

    rk.ensure_runtime_schema(conn)
    source = provider_source or {}
    requires_source = execute_decision or apply_decision or compact
    if requires_source and (not source.get("provider_name") or not source.get("model")):
        raise ValueError("real model smoke requires an explicit model source")

    before = _state_counts(conn, job_id)
    delta = rk.build_decision_delta(conn, job_id)
    request = rd.build_decision_provider_request(conn, job_id, delta)
    messages, rendered, profile = rd.render_decision_messages(request, profile_name=profile_name)
    dry_run = {
        "called_model": False,
        "applied": False,
        "graph_revision_before": before["graph_revision"],
        "graph_revision_after": int(rk._job(conn, job_id)["graph_revision"]),
        "kernel_decisions_before": before["kernel_decisions"],
        "kernel_decisions_after": _count(conn, "kernel_decisions", job_id),
        "graph_patches_before": before["graph_patches"],
        "graph_patches_after": _count(conn, "graph_patches", job_id),
        "profile": _profile_summary(profile),
        "provider_call": {
            **_source_summary(source),
            "mode": "dry_run",
            "no_tools": True,
            "single_shot": True,
            "message_count": len(messages),
            "input_token_estimate": rd.estimate_decision_input_tokens(rendered, profile["content"]),
        },
    }

    decision_execute = None
    if execute_decision:
        provider = _decision_provider(source, profile_name=profile_name, max_retries=max_retries, timeout_seconds=timeout_seconds)
        provider_result = provider.decide(request)
        validation = _validate_provider_result(conn, job_id, provider_result)
        decision_execute = {
            "called_model": True,
            "applied": False,
            "provider_result": _provider_result_summary(provider_result),
            "delegation": _delegation_summary(provider_result.patch),
            "validation": validation,
            "graph_revision_after": int(rk._job(conn, job_id)["graph_revision"]),
            "graph_patches_after": _count(conn, "graph_patches", job_id),
            "kernel_decisions_after": _count(conn, "kernel_decisions", job_id),
        }

    one_step_apply = None
    if apply_decision:
        current_state = rk.reduce_runtime_job(conn, job_id)["state"]
        if current_state != "waiting_decision":
            one_step_apply = {
                "called_model": False,
                "applied": False,
                "skipped": True,
                "reason": f"job_state:{current_state}",
                "required_state": "waiting_decision",
            }
        else:
            provider = _decision_provider(source, profile_name=profile_name, max_retries=max_retries, timeout_seconds=timeout_seconds)
            before_apply = _state_counts(conn, job_id)
            result = rk.advance_runtime_job(
                conn,
                job_id,
                create_tasks=False,
                decision_provider=provider,
                max_patches=1,
                auto_compact=False,
            )
            after_apply = _state_counts(conn, job_id)
            one_step_apply = {
                "called_model": True,
                "applied": result.patch_status == "applied",
                "patch_status": result.patch_status,
                "job_state": result.job_state,
                "graph_revision_before": before_apply["graph_revision"],
                "graph_revision_after": after_apply["graph_revision"],
                "graph_patches_before": before_apply["graph_patches"],
                "graph_patches_after": after_apply["graph_patches"],
                "kernel_decisions_before": before_apply["kernel_decisions"],
                "kernel_decisions_after": after_apply["kernel_decisions"],
            }

    real_compaction = None
    if compact:
        provider = _compaction_provider(
            source,
            profile_name=compaction_profile_name,
            max_retries=max_retries,
            timeout_seconds=timeout_seconds,
        )
        result = rd.compact_decision_session(
            conn,
            job_id,
            profile_name=compaction_profile_name,
            reason="phase4g1_real_model_smoke",
            compaction_provider=provider,
            fallback_to_deterministic=fallback_to_deterministic,
        )
        real_compaction = {
            "called_model": True,
            "status": result.get("status"),
            "fallback_used": bool(result.get("fallback_used")),
            "provider_name": result.get("provider_name") or (result.get("provider_result") or {}).get("provider_name"),
            "parse_status": result.get("parse_status") or (result.get("provider_result") or {}).get("parse_status"),
            "provider_validation": result.get("provider_validation") or (result.get("provider_result") or {}).get("provider_validation"),
            "checkpoint_id": result.get("checkpoint_id"),
            "source_segment_id": result.get("source_segment_id"),
            "new_segment_id": result.get("new_segment_id"),
            "active_segment_preserved": bool(result.get("active_segment_preserved")),
            "reason": result.get("reason"),
        }

    consistency = rk.check_runtime_consistency(conn, job_id, write_events=False)
    report = {
        "job_id": job_id,
        "provider": _source_summary(source),
        "decision_dry_run": dry_run,
        "decision_execute": decision_execute,
        "one_step_advance": one_step_apply,
        "real_compaction": real_compaction,
        "consistency": {
            "status": consistency["status"],
            "violation_count": consistency["violation_count"],
            "warning_count": consistency["warning_count"],
            "violations": consistency["violations"][:20],
            "warnings": consistency["warnings"][:20],
        },
        "secrets_leaked": False,
    }
    report["secrets_leaked"] = _secrets_leaked(report, source)
    if execute_decision or apply_decision or compact:
        rk._event(
            conn,
            job_id,
            "real_model_smoke_completed",
            {
                "execute_decision": bool(execute_decision),
                "apply_decision": bool(apply_decision),
                "compact": bool(compact),
                "decision_parse_status": (decision_execute or {}).get("provider_result", {}).get("parse_status"),
                "one_step_patch_status": (one_step_apply or {}).get("patch_status"),
                "compaction_status": (real_compaction or {}).get("status"),
                "consistency_status": report["consistency"]["status"],
                "secrets_leaked": report["secrets_leaked"],
            },
        )
    return report


def _decision_provider(source: dict[str, Any], *, profile_name: str, max_retries: int, timeout_seconds: Optional[float]) -> Any:
    return rd.RuntimeDecisionProvider(
        provider_name=source["provider_name"],
        model=source["model"],
        profile_name=profile_name,
        max_retries=max_retries,
        timeout_seconds=timeout_seconds,
        explicit_base_url=source.get("explicit_base_url"),
        explicit_api_key=source.get("explicit_api_key"),
    )


def _compaction_provider(source: dict[str, Any], *, profile_name: str, max_retries: int, timeout_seconds: Optional[float]) -> Any:
    return rd.RuntimeCompactionProvider(
        provider_name=source["provider_name"],
        model=source["model"],
        profile_name=profile_name,
        max_retries=max_retries,
        timeout_seconds=timeout_seconds,
        explicit_base_url=source.get("explicit_base_url"),
        explicit_api_key=source.get("explicit_api_key"),
    )


def _validate_provider_result(conn: sqlite3.Connection, job_id: str, result: rd.DecisionProviderResult) -> dict[str, Any]:
    if result.patch is None:
        return {
            "status": "skipped",
            "would_apply": False,
            "reason": result.error or result.parse_status,
        }
    return rk.validate_graph_patch(conn, job_id, result.patch)


def _provider_result_summary(result: rd.DecisionProviderResult) -> dict[str, Any]:
    return {
        "provider_name": result.provider_name,
        "model": result.model,
        "profile_name": result.profile_name,
        "profile_version": result.profile_version,
        "profile_hash": result.profile_hash,
        "request_ref": result.request_ref,
        "response_ref": result.response_ref,
        "parse_status": result.parse_status,
        "retry_count": result.retry_count,
        "provider_latency_ms": result.provider_latency_ms,
        "input_token_estimate": result.input_token_estimate,
        "output_token_estimate": result.output_token_estimate,
        "error": result.error,
        "has_patch": result.patch is not None,
    }


def _delegation_summary(patch: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Return bounded delegation metrics without exposing provider-authored text."""

    if patch is None:
        return None
    ops = patch.get("ops") if isinstance(patch.get("ops"), list) else []
    execution_ops = [
        op for op in ops
        if isinstance(op, dict) and op.get("op") in rk.EXECUTION_NODE_OPS
    ]
    immediate_ops = [op for op in execution_ops if not (op.get("depends_on") or [])]
    contracted_ops = [op for op in execution_ops if isinstance(op.get("contract"), dict)]
    decomposition = patch.get("decomposition")
    justifications = (
        decomposition.get("justifications")
        if isinstance(decomposition, dict) and isinstance(decomposition.get("justifications"), list)
        else []
    )
    return {
        "operation_count": len(ops),
        "operation_types": [str(op.get("op") or "") for op in ops if isinstance(op, dict)],
        "execution_node_count": len(execution_ops),
        "immediate_execution_node_count": len(immediate_ops),
        "typed_contract_count": len(contracted_ops),
        "all_execution_nodes_have_typed_contract": bool(execution_ops) and len(contracted_ops) == len(execution_ops),
        "decomposition_present": isinstance(decomposition, dict),
        "decomposition_reason_types": [
            str(item.get("type") or "")
            for item in justifications
            if isinstance(item, dict)
        ],
    }


def _profile_summary(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "profile_name": profile["profile_name"],
        "profile_version": profile["profile_version"],
        "profile_hash": profile["profile_hash"],
        "profile_path": profile["profile_path"],
    }


def _source_summary(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": source.get("source") or "unspecified",
        "model_provider": source.get("display_provider") or source.get("provider_name"),
        "model": source.get("model") or None,
        "explicit_base_url": bool(source.get("explicit_base_url")),
        "explicit_api_key": bool(source.get("explicit_api_key")),
    }


def _state_counts(conn: sqlite3.Connection, job_id: str) -> dict[str, int]:
    return {
        "graph_revision": int(rk._job(conn, job_id)["graph_revision"]),
        "kernel_decisions": _count(conn, "kernel_decisions", job_id),
        "graph_patches": _count(conn, "graph_patches", job_id),
        "decision_entries": _count(conn, "decision_segment_entries", job_id),
    }


def _count(conn: sqlite3.Connection, table: str, job_id: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE job_id = ?", (job_id,)).fetchone()[0] or 0)


def _secrets_leaked(report: dict[str, Any], source: dict[str, Any]) -> bool:
    secret = source.get("explicit_api_key")
    if not secret:
        return False
    return str(secret) in json.dumps(report, ensure_ascii=False, sort_keys=True)
