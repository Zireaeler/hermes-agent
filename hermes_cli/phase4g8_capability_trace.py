"""Build auditable Phase 4G8 real-task capability process records."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Optional


CAPABILITY_TRACE_SCHEMA = "hermes_phase4g8_capability_trace_v1"


def build_capability_trace(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    run_id: str,
    instance_id: str,
    case_size: str,
    run_report: dict[str, Any],
) -> dict[str, Any]:
    job = conn.execute(
        "SELECT objective FROM runtime_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    goal_items = [dict(row) for row in conn.execute(
        """
        SELECT i.item_key, i.description, i.required, i.verifier_required, i.state
          FROM goal_items i
          JOIN goal_contracts c ON c.id = i.contract_id
         WHERE c.job_id = ?
         ORDER BY i.created_at, i.id
        """,
        (job_id,),
    ).fetchall()]
    nodes = [_node_trace(conn, dict(row)) for row in conn.execute(
        "SELECT * FROM execution_nodes WHERE job_id = ? ORDER BY created_at, id",
        (job_id,),
    ).fetchall()]
    patches = []
    for row in conn.execute(
        "SELECT * FROM graph_patches WHERE job_id = ? ORDER BY created_at, id",
        (job_id,),
    ).fetchall():
        patch = _loads(row["patch_json"])
        patches.append({
            "patch_id": row["id"],
            "decision_id": row["decision_id"],
            "status": row["status"],
            "reject_reason": row["reject_reason"],
            "rationale_summary": _bounded(patch.get("rationale_summary"), 1000),
            "operations": [
                {
                    "op": operation.get("op"),
                    "node_key": operation.get("node_key") or operation.get("verifier_node_key"),
                    "target_node_key": operation.get("target_node_key"),
                }
                for operation in patch.get("ops") or []
                if isinstance(operation, dict)
            ],
            "created_at": row["created_at"],
            "applied_at": row["applied_at"],
        })
    checkpoints = []
    for row in conn.execute(
        "SELECT * FROM decision_checkpoints WHERE job_id = ? ORDER BY created_at, id",
        (job_id,),
    ).fetchall():
        metadata = _loads(row["metadata_json"])
        checkpoints.append({
            "checkpoint_id": row["id"],
            "validator_status": row["validator_status"],
            "reason": row["reason"],
            "provider": metadata.get("provider_name"),
            "model": metadata.get("provider_model"),
            "provider_latency_ms": metadata.get("provider_latency_ms"),
            "fallback_used": bool(metadata.get("fallback_used")),
            "input_token_estimate": metadata.get("input_token_estimate"),
            "output_token_estimate": metadata.get("output_token_estimate"),
            "created_at": row["created_at"],
        })

    worker_nodes = [node for node in nodes if node["node_type"] != "verification"]
    evaluator_nodes = [node for node in nodes if node["node_type"] == "verification"]
    failed_test_sets = [
        set(node.get("official_evaluator_result", {}).get("fail_to_pass", {}).get("failed_tests") or [])
        for node in evaluator_nodes
        if node.get("official_evaluator_result")
    ]
    repeated_failed_tests = sorted(set.intersection(*failed_test_sets)) if failed_test_sets else []
    local_pass_then_evaluator_fail = sum(
        bool(node.get("local_verification", {}).get("reported"))
        for node in worker_nodes
    ) > 0 and any(
        node.get("official_evaluator_result", {}).get("resolved") is False
        for node in evaluator_nodes
    )
    runtime_validation = run_report.get("runtime_validation") or {}
    capability_validation = run_report.get("capability_validation") or {}
    metrics = run_report.get("metrics") or {}
    observations: list[dict[str, Any]] = []
    observations.append({
        "category": "runtime_orchestration",
        "assessment": "passed" if runtime_validation.get("passed") else "failed",
        "summary": (
            "Runtime 在真实进程、独立 evaluator、checkpoint 和 recovery 路径下保持一致性。"
            if runtime_validation.get("passed")
            else "Runtime correctness 仍有失败，不能只按任务结果评价本次运行。"
        ),
        "evidence": runtime_validation.get("failures") or [
            f"consistency={runtime_validation.get('consistency', {}).get('violation_count', 0)}/"
            f"{runtime_validation.get('consistency', {}).get('warning_count', 0)}",
            f"duplicate_ledger={runtime_validation.get('duplicate_ledger_fact_count', 0)}",
            f"compaction_fallback={runtime_validation.get('compaction_fallback_count', 0)}",
        ],
    })
    observations.append({
        "category": "task_capability",
        "assessment": "passed" if capability_validation.get("passed") else "failed",
        "summary": (
            "模型与 Runtime 共同完成任务并通过 official evaluator。"
            if capability_validation.get("passed")
            else (
                f"经过 {len(worker_nodes)} 个 durable worker responsibility 和 "
                f"{len(evaluator_nodes)} 次独立 evaluator 后仍未 resolved。"
            )
        ),
        "evidence": [
            f"classification={run_report.get('classification')}",
            f"official_resolved={capability_validation.get('official_resolved')}",
            f"evaluator_budget_exhausted={metrics.get('evaluator_budget_exhausted', False)}",
        ],
    })
    if local_pass_then_evaluator_fail:
        observations.append({
            "category": "verification_quality",
            "assessment": "insufficient",
            "summary": "Worker 的本地验证曾通过，但独立 evaluator 仍失败，说明局部测试不足以证明目标完成。",
            "evidence": repeated_failed_tests[:10],
        })
    if repeated_failed_tests:
        observations.append({
            "category": "recovery_convergence",
            "assessment": "not_converged" if not capability_validation.get("passed") else "converged",
            "summary": "同一 evaluator failure 在多轮 recovery 后仍重复出现。",
            "evidence": repeated_failed_tests[:10],
        })
    if len(worker_nodes) > 1:
        observations.append({
            "category": "context_isolation",
            "assessment": "enforced",
            "summary": "每个 durable execution node 使用独立 backend session；recovery 依赖显式 evidence，而非前序隐藏对话。",
            "evidence": [
                f"worker_nodes={len(worker_nodes)}",
                f"distinct_backend_sessions={len({session for node in worker_nodes for session in node['backend_session_keys']})}",
            ],
        })

    timeline: list[dict[str, Any]] = []
    for patch in patches:
        timeline.append({
            "kind": "decision_patch",
            "at": patch["created_at"],
            "title": f"Decision Provider patch {patch['status']}",
            "summary": patch["rationale_summary"],
            "details": patch["operations"],
            "evidence_refs": [f"patch:{patch['patch_id']}", f"decision:{patch['decision_id']}"],
        })
    for checkpoint in checkpoints:
        timeline.append({
            "kind": "checkpoint",
            "at": checkpoint["created_at"],
            "title": f"Real compaction checkpoint {checkpoint['validator_status']}",
            "summary": checkpoint["reason"],
            "details": checkpoint,
            "evidence_refs": [f"checkpoint:{checkpoint['checkpoint_id']}"],
        })
    for node in nodes:
        timeline.append({
            "kind": "evaluator" if node["node_type"] == "verification" else "worker",
            "at": node["created_at"],
            "title": node["node_key"],
            "summary": node.get("receipt_summary") or node.get("description"),
            "details": {
                "node_type": node["node_type"],
                "state": node["state"],
                "description": node["description"],
                "changed_files": node["changed_files"],
                "local_verification": node["local_verification"],
                "runtime_verification": node["verification"],
                "commands": node["commands"],
                "official_evaluator_result": node.get("official_evaluator_result"),
                "backend_session_keys": node["backend_session_keys"],
                "observable_updates": node["agent_messages"],
                "remaining_risks": node["remaining_risks"],
                "recommended_reviewer_action": node["recommended_reviewer_action"],
            },
            "evidence_refs": [f"node:{node['node_id']}", *node["materialization_refs"]],
        })
    timeline.sort(key=lambda item: (int(item.get("at") or 0), item["kind"], item["title"]))
    return {
        "schema": CAPABILITY_TRACE_SCHEMA,
        "run_id": run_id,
        "instance_id": instance_id,
        "case_size": case_size,
        "job_id": job_id,
        "objective": _bounded(job["objective"] if job is not None else "", 2000),
        "goal_items": goal_items,
        "conclusion": {
            "runtime_validation_passed": bool(runtime_validation.get("passed")),
            "capability_validation_passed": bool(capability_validation.get("passed")),
            "classification": run_report.get("classification"),
            "official_resolved": capability_validation.get("official_resolved"),
        },
        "counts": {
            "decision_patches": len(patches),
            "execution_nodes": len(nodes),
            "worker_nodes": len(worker_nodes),
            "recovery_nodes": sum(node["node_type"] == "strategy_update" for node in worker_nodes),
            "evaluator_attempts": len(evaluator_nodes),
            "accepted_checkpoints": sum(item["validator_status"] == "accepted" for item in checkpoints),
        },
        "observations": observations,
        "patches": patches,
        "checkpoints": checkpoints,
        "nodes": nodes,
        "timeline": timeline,
        "generated_at": int(time.time()),
    }


def write_capability_trace(report_dir: Path, trace: dict[str, Any]) -> dict[str, str]:
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "capability-trace.json"
    markdown_path = report_dir / "capability-trace.md"
    json_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_capability_trace_markdown(trace), encoding="utf-8")
    json_path.chmod(0o600)
    markdown_path.chmod(0o600)
    return {"json": str(json_path), "markdown": str(markdown_path)}


def render_capability_trace_markdown(trace: dict[str, Any]) -> str:
    conclusion = trace["conclusion"]
    lines = [
        f"# Phase 4G8 实际能力过程记录：{trace['instance_id']}",
        "",
        "## 结论",
        "",
        f"- Runtime Validation：{'通过' if conclusion['runtime_validation_passed'] else '失败'}",
        f"- End-to-End Capability Validation：{'通过' if conclusion['capability_validation_passed'] else '未通过'}",
        f"- 分类：`{conclusion['classification']}`",
        f"- Official evaluator resolved：`{conclusion['official_resolved']}`",
        "",
        "## 测试目标",
        "",
        _bounded(trace.get("objective"), 800) or "-",
        "",
    ]
    for item in trace.get("goal_items") or []:
        lines.append(
            f"- `{item['item_key']}`：{item['description']} "
            f"（state=`{item['state']}`，verifier_required=`{bool(item['verifier_required'])}`）"
        )
    lines.extend([
        "",
        "## 规模",
        "",
    ])
    for key, value in trace["counts"].items():
        lines.append(f"- `{key}`：{value}")
    lines.extend(["", "## 能力观察", ""])
    for observation in trace["observations"]:
        lines.append(f"### {observation['category']}：{observation['assessment']}")
        lines.extend(["", str(observation["summary"]), ""])
        for evidence in observation.get("evidence") or []:
            lines.append(f"- `{_bounded(evidence, 500)}`")
        lines.append("")
    lines.extend(["## 执行时间线", ""])
    for index, item in enumerate(trace["timeline"], 1):
        lines.append(f"### {index}. {item['title']}")
        lines.append("")
        lines.append(f"- 类型：`{item['kind']}`")
        lines.append(f"- 时间：`{_format_time(item.get('at'))}`")
        lines.append(f"- 结果：{_bounded(item.get('summary'), 1200) or '-'}")
        details = item.get("details") or {}
        if isinstance(details, dict):
            if details.get("state"):
                lines.append(f"- Node 状态：`{details['state']}`")
            if details.get("changed_files"):
                lines.append("- 修改文件：" + "、".join(f"`{path}`" for path in details["changed_files"][:30]))
            local_verification = details.get("local_verification")
            if isinstance(local_verification, dict) and local_verification.get("reported"):
                lines.append("- Worker 本地验证：`reported=True`（非权威完成证据）")
                if local_verification.get("summary"):
                    lines.append(f"- 本地验证摘要：{_bounded(local_verification.get('summary'), 800)}")
            runtime_verification = details.get("runtime_verification")
            if isinstance(runtime_verification, dict) and runtime_verification:
                lines.append(f"- Runtime verification：`passed={runtime_verification.get('passed')}`")
                if runtime_verification.get("adapter_requires_independent_verification"):
                    lines.append("- Runtime 判定：worker 自报不能满足 independent evaluator 要求")
            evaluator = details.get("official_evaluator_result")
            if isinstance(evaluator, dict):
                lines.append(f"- Official evaluator：`resolved={evaluator.get('resolved')}`")
                failed = evaluator.get("fail_to_pass") or {}
                passed = evaluator.get("pass_to_pass") or {}
                lines.append(
                    f"- Oracle：FAIL_TO_PASS `{failed.get('passed', 0)}/{failed.get('total', 0)}`；"
                    f"PASS_TO_PASS `{passed.get('passed', 0)}/{passed.get('total', 0)}`"
                )
            commands = details.get("commands") or []
            if commands:
                lines.append("- 代表性命令：")
                for command in commands[-8:]:
                    lines.append(
                        f"  - `{_bounded(command.get('command'), 300)}` -> "
                        f"`{command.get('status')}` / exit `{command.get('exit_code')}`"
                    )
            updates = details.get("observable_updates") or []
            if updates:
                lines.append("- 可观察过程更新：")
                for update in updates[-4:]:
                    lines.append(f"  - {_bounded(update, 700)}")
            if details.get("remaining_risks"):
                lines.append(f"- Worker 剩余风险：{_bounded(details['remaining_risks'], 800)}")
            if details.get("recommended_reviewer_action"):
                lines.append(
                    f"- Worker 建议复核：{_bounded(details['recommended_reviewer_action'], 800)}"
                )
        if item.get("evidence_refs"):
            lines.append("- Evidence：" + "、".join(f"`{ref}`" for ref in item["evidence_refs"]))
        lines.append("")
    lines.extend([
        "## 解释边界",
        "",
        "这份记录描述真实执行中可观察到的决策、修改、验证、恢复和失败。它不包含 gold patch、",
        "受保护 evaluator 实现、模型隐藏推理或其他 node 的私有 session 内容。Runtime correctness",
        "通过不等于任务能力通过；worker 自报测试通过也不等于 official evaluator resolved。",
        "",
    ])
    return "\n".join(lines)


def _node_trace(conn: sqlite3.Connection, node: dict[str, Any]) -> dict[str, Any]:
    materializations = [dict(row) for row in conn.execute(
        "SELECT * FROM node_materializations WHERE node_id = ? ORDER BY attempt, created_at",
        (node["id"],),
    ).fetchall()]
    run_rows = []
    for materialization in materializations:
        row = conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (materialization["task_id"],),
        ).fetchone()
        if row is not None:
            run_rows.append(dict(row))
    latest_metadata: dict[str, Any] = {}
    for run in run_rows:
        metadata = _loads(run.get("metadata"))
        if metadata:
            latest_metadata = metadata
    runtime_receipt = latest_metadata.get("runtime_receipt")
    latest_receipt = (
        runtime_receipt
        if isinstance(runtime_receipt, dict)
        else latest_metadata
    )
    commands = []
    messages = []
    task_ids = [str(materialization["task_id"]) for materialization in materializations]
    if task_ids:
        placeholders = ",".join("?" for _ in task_ids)
        rows = conn.execute(
            f"SELECT payload FROM task_events WHERE task_id IN ({placeholders}) "
            "AND kind = 'worker_codex_event' ORDER BY created_at, id",
            tuple(task_ids),
        ).fetchall()
        for row in rows:
            payload = _loads(row["payload"])
            item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
            if (
                payload.get("event_type") == "item.completed"
                and item.get("type") == "command_execution"
            ):
                commands.append({
                    "command": _bounded(item.get("command"), 500),
                    "status": item.get("status"),
                    "exit_code": item.get("exit_code"),
                    "output_tail": _bounded(item.get("output_tail"), 1000),
                })
            elif (
                payload.get("event_type") == "item.completed"
                and item.get("type") == "agent_message"
                and item.get("text_tail")
            ):
                messages.append(_bounded(item.get("text_tail"), 1000))
    sessions = [dict(row) for row in conn.execute(
        "SELECT * FROM backend_worker_sessions WHERE node_id = ? ORDER BY created_at, id",
        (node["id"],),
    ).fetchall()]
    verification = latest_receipt.get("verification")
    reported_verification = latest_metadata.get("verification")
    evaluator_result = latest_receipt.get("official_evaluator_result")
    git_evidence = latest_metadata.get("git")
    worker_receipt = latest_metadata.get("worker_receipt")
    if not isinstance(git_evidence, dict):
        git_evidence = {}
    if not isinstance(worker_receipt, dict):
        worker_receipt = {}
    changed_files = (
        latest_receipt.get("changed_files")
        or git_evidence.get("attempt_changed_files")
        or git_evidence.get("changed_files")
        or []
    )
    constraints = _loads(node.get("constraints_json"))
    return {
        "node_id": node["id"],
        "node_key": node["node_key"],
        "node_type": node["node_type"],
        "state": node["state"],
        "title": node["title"],
        "description": node["description"],
        "contract": constraints.get("contract") if isinstance(constraints.get("contract"), dict) else {},
        "receipt_verdict": latest_receipt.get("verdict"),
        "receipt_summary": _bounded(latest_receipt.get("summary") or node.get("output_summary"), 4000),
        "changed_files": list(changed_files)[:200],
        "verification": verification if isinstance(verification, dict) else {},
        "local_verification": {
            "reported": bool(
                isinstance(reported_verification, dict)
                and (
                    reported_verification.get("commands")
                    or reported_verification.get("summary")
                )
            ),
            "commands": (
                list(reported_verification.get("commands") or [])
                if isinstance(reported_verification, dict)
                else []
            ),
            "summary": (
                _bounded(reported_verification.get("summary"), 2000)
                if isinstance(reported_verification, dict)
                else ""
            ),
        },
        "official_evaluator_result": evaluator_result if isinstance(evaluator_result, dict) else None,
        "commands": commands[-30:],
        "command_count": len(commands),
        "agent_messages": messages[-5:],
        "remaining_risks": _bounded(worker_receipt.get("remaining_risks"), 2000),
        "recommended_reviewer_action": _bounded(
            worker_receipt.get("recommended_reviewer_action"),
            2000,
        ),
        "backend_session_keys": [
            str(session["backend_session_key"])
            for session in sessions
            if session.get("backend_session_key")
        ],
        "resume_count": sum(int(session.get("resume_count") or 0) for session in sessions),
        "materialization_refs": [f"materialization:{item['id']}" for item in materializations],
        "created_at": node["created_at"],
        "updated_at": node["updated_at"],
    }


def _loads(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _bounded(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= int(limit):
        return text
    return text[: max(0, int(limit) - 16)].rstrip() + "\n...[truncated]"


def _format_time(value: Any) -> str:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return str(value or "-")
    return datetime.fromtimestamp(timestamp).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
