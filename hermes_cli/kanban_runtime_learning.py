"""Deterministic Phase 4G15 orchestration run analysis and learning registry."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Optional

from hermes_cli import kanban_runtime_kernel as rk


LEARNING_BUNDLE_SCHEMA = "hermes_runtime_orchestration_learning_bundle_v1"
LEARNING_REGISTRY_SCHEMA = "hermes_runtime_orchestration_learning_registry_v1"
LEARNING_RECEIPT_SCHEMA = "hermes_runtime_orchestration_learning_receipt_v1"
ABSORPTION_DISPOSITIONS = {
    "candidate_created",
    "no_action_required",
    "duplicate_of_existing_candidate",
    "covered_by_existing_regression",
    "infrastructure_invalid",
}


class LearningBundleError(RuntimeError):
    pass


def _loads(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _stable_id(prefix: str, *parts: Any) -> str:
    digest = hashlib.sha256(
        "\0".join(str(part) for part in parts).encode("utf-8")
    ).hexdigest()[:20]
    return f"{prefix}_{digest}"


def _bundle_fingerprint_payload(bundle: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(json.dumps(bundle))
    absorption = payload.get("absorption")
    if isinstance(absorption, dict):
        absorption.pop("bundle_sha256", None)
        absorption.pop("registry_receipt", None)
        absorption.pop("absorbed_at", None)
        absorption["status"] = "validated"
    return payload


def bundle_sha256(bundle: dict[str, Any]) -> str:
    encoded = json.dumps(
        _bundle_fingerprint_payload(bundle),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _event_summary(event_type: str, payload: dict[str, Any]) -> str:
    for key in ("summary", "reason", "node_key", "directive_id", "status"):
        value = payload.get(key)
        if value not in {None, ""}:
            return f"{event_type}: {value}"
    checkpoint = payload.get("checkpoint")
    if isinstance(checkpoint, dict) and checkpoint.get("summary"):
        return f"{event_type}: {checkpoint['summary']}"
    return event_type


def _timeline(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    node_keys = {
        str(row["id"]): str(row["node_key"])
        for row in conn.execute(
            "SELECT id, node_key FROM execution_nodes WHERE job_id = ?",
            (job_id,),
        ).fetchall()
    }
    rows: list[dict[str, Any]] = []
    for row in conn.execute(
        """
        SELECT id, node_id, task_id, run_id, event_type, payload_json,
               source, created_at
          FROM execution_events
         WHERE job_id = ? ORDER BY created_at, id
        """,
        (job_id,),
    ).fetchall():
        payload = _loads(row["payload_json"])
        rows.append(
            {
                "timestamp": int(row["created_at"]),
                "order_key": f"runtime:{int(row['id']):012d}",
                "source": str(row["source"]),
                "event_type": str(row["event_type"]),
                "node_key": node_keys.get(str(row["node_id"])),
                "task_id": row["task_id"],
                "run_id": row["run_id"],
                "summary": _event_summary(str(row["event_type"]), payload),
                "evidence_ref": f"execution_event:{int(row['id'])}",
            }
        )
    task_ids = [
        str(row["task_id"])
        for row in conn.execute(
            "SELECT DISTINCT task_id FROM node_materializations WHERE job_id = ? AND task_id IS NOT NULL",
            (job_id,),
        ).fetchall()
    ]
    if task_ids:
        placeholders = ",".join("?" for _ in task_ids)
        for row in conn.execute(
            f"""
            SELECT id, task_id, run_id, kind, payload, created_at
              FROM task_events WHERE task_id IN ({placeholders})
             ORDER BY created_at, id
            """,
            task_ids,
        ).fetchall():
            if str(row["kind"]) not in {
                "worker_started",
                "worker_app_server_turn_started",
                "worker_backend_session_started",
                "worker_backend_session_resumed",
                "worker_review_required",
                "worker_failed",
                "worker_timed_out",
            }:
                continue
            payload = _loads(row["payload"])
            rows.append(
                {
                    "timestamp": int(row["created_at"]),
                    "order_key": f"task:{int(row['id']):012d}",
                    "source": "kanban_worker",
                    "event_type": str(row["kind"]),
                    "node_key": None,
                    "task_id": row["task_id"],
                    "run_id": row["run_id"],
                    "summary": _event_summary(str(row["kind"]), payload),
                    "evidence_ref": f"task_event:{int(row['id'])}",
                }
            )
    rows.sort(key=lambda item: (item["timestamp"], item["order_key"]))
    return rows


def _live_coordination(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    deliveries = [
        dict(row)
        for row in conn.execute(
            """
            SELECT delivery.*, node.node_key
              FROM runtime_live_directive_deliveries delivery
              JOIN execution_nodes node ON node.id = delivery.target_node_id
             WHERE delivery.job_id = ? ORDER BY delivery.created_at, delivery.id
            """,
            (job_id,),
        ).fetchall()
    ]
    terminal_by_materialization = {
        str(row["id"]): row["completed_at"]
        for row in conn.execute(
            "SELECT id, completed_at FROM node_materializations WHERE job_id = ?",
            (job_id,),
        ).fetchall()
    }
    normalized = []
    stale_avoided = 0
    stale_not_avoided = 0
    for item in deliveries:
        completed_at = terminal_by_materialization.get(str(item["materialization_id"]))
        accepted_before_terminal = bool(
            item.get("accepted_at")
            and (completed_at is None or int(item["accepted_at"]) <= int(completed_at))
        )
        acknowledged_before_terminal = bool(
            item.get("acknowledged_at")
            and (completed_at is None or int(item["acknowledged_at"]) <= int(completed_at))
        )
        if accepted_before_terminal and acknowledged_before_terminal:
            stale_avoided += 1
        elif item["status"] in {
            "queued_fallback",
            "stale_turn",
            "not_steerable",
            "transport_failed",
        } or (item["status"] == "pending" and completed_at is not None):
            stale_not_avoided += 1
        normalized.append(
            {
                "delivery_id": item["id"],
                "directive_id": item["directive_id"],
                "target_node_key": item["node_key"],
                "materialization_id": item["materialization_id"],
                "thread_id": item["thread_id"],
                "turn_id": item["turn_id"],
                "status": item["status"],
                "attempt_count": int(item["attempt_count"] or 0),
                "created_at": item["created_at"],
                "accepted_at": item["accepted_at"],
                "acknowledged_at": item["acknowledged_at"],
                "target_terminal_at": completed_at,
                "accepted_before_terminal": accepted_before_terminal,
                "acknowledged_before_terminal": acknowledged_before_terminal,
                "error_code": item["error_code"],
            }
        )
    return {
        "deliveries": normalized,
        "attempted": sum(int(item["attempt_count"]) > 0 for item in deliveries),
        "accepted": sum(item["status"] in {"accepted", "acknowledged"} for item in deliveries),
        "acknowledged": sum(item["status"] == "acknowledged" for item in deliveries),
        "fallback": sum(item["status"] == "queued_fallback" for item in deliveries),
        "stale_work_avoided_count": stale_avoided,
        "stale_work_not_avoided_count": stale_not_avoided,
    }


def _finding(
    job_id: str,
    category: str,
    severity: str,
    summary: str,
    root_cause: str,
    evidence_refs: list[str],
    disposition: str,
) -> dict[str, Any]:
    return {
        "finding_id": _stable_id("finding", job_id, category, *evidence_refs),
        "category": category,
        "severity": severity,
        "summary": summary,
        "root_cause": root_cause,
        "evidence_refs": evidence_refs,
        "absorption_disposition": disposition,
    }


def _findings(
    job_id: str,
    orchestration: dict[str, Any],
    live: dict[str, Any],
    quality: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    quality = quality or {}
    coordination = orchestration.get("coordination") or {}
    actions = coordination.get("actions") or []
    observations = quality.get("coordination_observations") or {}
    if not isinstance(observations, dict):
        observations = {}
    if quality.get("case_kind") == "coherent_negative_control" and actions:
        findings.append(
            _finding(
                job_id,
                "false_coordination",
                "high",
                "Coherent negative control 中出现了不需要的 coordination action。",
                "Runtime 或 worker 把单责任局部事实升级成了全局协调。",
                [f"coordination_action:{item['id']}" for item in actions],
                "candidate_created",
            )
        )
    missed_refs = [
        str(value)
        for value in observations.get("missed_coordination_evidence_refs") or []
        if str(value).strip()
    ]
    if missed_refs:
        findings.append(
            _finding(
                job_id,
                "missed_coordination",
                "high",
                "跨节点新事实没有在 sibling 产生过时工作前进入 coordination 路径。",
                "Worker 未提交可路由 checkpoint，或 Runtime 未在事实仍有时效时处理它。",
                missed_refs,
                "candidate_created",
            )
        )
    calibration_gap_refs = [
        str(value)
        for value in observations.get("calibration_fixture_gap_evidence_refs") or []
        if str(value).strip()
    ]
    if calibration_gap_refs:
        findings.append(
            _finding(
                job_id,
                "calibration_fixture_gap",
                "medium",
                "冻结校准任务没有暴露预期的 durable responsibility candidate。",
                "Repository evidence 表明任务仍可由一个 coherent worker 低成本完成；"
                "该结果不足以证明 Runtime 错过了跨节点协调。",
                calibration_gap_refs,
                "candidate_created",
            )
        )
    overhead_refs = [
        str(value)
        for value in observations.get("coordination_overhead_evidence_refs") or []
        if str(value).strip()
    ]
    if overhead_refs:
        findings.append(
            _finding(
                job_id,
                "coordination_overhead",
                "medium",
                "Coordination 增加了可测成本，但没有对应的质量或过时工作收益。",
                "Paired baseline/treatment 证明额外 checkpoint、resume、decision 或 token 没有产生净收益。",
                overhead_refs,
                "candidate_created",
            )
        )
    rejected_actions = [
        f"coordination_action:{item['id']}"
        for item in actions
        if item.get("status") == "rejected"
    ]
    if rejected_actions:
        findings.append(
            _finding(
                job_id,
                "ineffective_coordination",
                "high",
                "至少一个 provider-required coordination action 未形成有效结构结果。",
                "Decision Provider 输出缺失、解析失败或 graph patch 被 validator 拒绝。",
                rejected_actions,
                "candidate_created",
            )
        )
    fallback_ids = [
        f"live_delivery:{item['delivery_id']}"
        for item in live["deliveries"]
        if item["status"] == "queued_fallback"
    ]
    if fallback_ids:
        findings.append(
            _finding(
                job_id,
                "live_delivery_fallback",
                "high",
                "至少一条 active-turn directive 未能实时交付。",
                "app-server turn 不可 steer、identity 过期或 transport 失败。",
                fallback_ids,
                "candidate_created",
            )
        )
    unacked = [
        f"live_delivery:{item['delivery_id']}"
        for item in live["deliveries"]
        if item["status"] == "accepted"
    ]
    if unacked:
        findings.append(
            _finding(
                job_id,
                "accepted_without_ack",
                "high",
                "Runtime 已 steer，但 worker 没有在 canonical output 中确认消费。",
                "transport acceptance 被保留为非权威状态，缺少 worker 语义 ACK。",
                unacked,
                "candidate_created",
            )
        )
    unresolved = [
        f"live_delivery:{item['delivery_id']}"
        for item in live["deliveries"]
        if item["status"] == "pending" and item.get("target_terminal_at") is not None
    ]
    if unresolved:
        findings.append(
            _finding(
                job_id,
                "live_delivery_unresolved",
                "high",
                "Target 已终态，但 live directive 仍停留在 pending。",
                "新 directive 未绑定已注册的 active turn，且没有进入可观察的 durable fallback。",
                unresolved,
                "candidate_created",
            )
        )
    handoff = orchestration.get("contribution_handoff") or {}
    reexecution = int(handoff.get("implementation_reexecution_due_to_receipt_count") or 0)
    if reexecution:
        findings.append(
            _finding(
                job_id,
                "handoff_reexecution",
                "high",
                "receipt/handoff 问题导致已完成实现被重新执行。",
                "durable artifact 与 metadata repair 边界没有完全生效。",
                ["orchestration:contribution_handoff"],
                "candidate_created",
            )
        )
    cost = (coordination.get("cost") or {})
    ineffective = int(cost.get("structural_decision_count") or 0) - int(
        cost.get("effective_structural_decision_count") or 0
    )
    if ineffective > 0:
        findings.append(
            _finding(
                job_id,
                "ineffective_structural_decision",
                "medium",
                f"{ineffective} 次 structural decision 未产生可观察的有效结构结果。",
                "Provider 被调用后 patch 没有生效，或 directive 未得到 ACK。",
                ["orchestration:coordination.cost"],
                "candidate_created",
            )
        )
    if not findings:
        evidence = (
            [f"live_delivery:{item['delivery_id']}" for item in live["deliveries"]]
            or ["orchestration:summary"]
        )
        findings.append(
            _finding(
                job_id,
                "no_regression_signal",
                "info",
                "本 run 未发现需要形成 policy candidate 的确定性异常。",
                "已观测的 coordination 与 handoff facts 均未命中回归规则。",
                evidence,
                "no_action_required",
            )
        )
    return findings


def _candidates(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = []
    for finding in findings:
        if finding["absorption_disposition"] != "candidate_created":
            continue
        category = str(finding["category"])
        candidate_key = _stable_id("candidate", category)
        if category == "calibration_fixture_gap":
            scope = "validation_campaign"
            proposed_change = (
                "修订 candidate-required 冻结任务，使 repository 自然呈现可独立验收、"
                "写域不重叠且有明确集成 owner 的 durable responsibility。"
            )
            expected_effect = (
                "让正向校准覆盖自然 candidate 的 Provider 消费，同时不通过 prompt 预告拆分答案。"
            )
        else:
            scope = "runtime_coordination"
            proposed_change = f"为 {category} 增加 reducer/transport 回归约束。"
            expected_effect = "减少无效工作、重做或无效结构决策。"
        candidates.append(
            {
                "candidate_key": candidate_key,
                "category": category,
                "scope": scope,
                "symptom": finding["summary"],
                "root_cause": finding["root_cause"],
                "evidence_refs": finding["evidence_refs"],
                "proposed_change": proposed_change,
                "expected_effect": expected_effect,
                "regression_scenario_key": f"regression-{category}",
                "status": "candidate",
            }
        )
    return candidates


def build_learning_bundle(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    phase: str,
    instance_id: str,
    run_id: str,
    source_db_ref: str,
    quality: Optional[dict[str, Any]] = None,
    baseline_bundle_ref: Optional[str] = None,
) -> dict[str, Any]:
    rk.ensure_runtime_schema(conn)
    job = dict(
        conn.execute("SELECT * FROM runtime_jobs WHERE id = ?", (job_id,)).fetchone()
    )
    orchestration = rk.summarize_runtime_orchestration(conn, job_id)
    live = _live_coordination(conn, job_id)
    normalized_quality = quality or {"status": "unknown"}
    findings = _findings(job_id, orchestration, live, normalized_quality)
    candidates = _candidates(findings)
    nodes = [
        {
            "node_key": row["node_key"],
            "node_type": row["node_type"],
            "state": row["state"],
            "contract_revision": int(row["contract_revision"] or 1),
        }
        for row in conn.execute(
            "SELECT node_key, node_type, state, contract_revision FROM execution_nodes WHERE job_id = ? ORDER BY created_at, node_key",
            (job_id,),
        ).fetchall()
    ]
    patch_counts = {
        str(row["status"]): int(row["count"])
        for row in conn.execute(
            "SELECT status, COUNT(*) AS count FROM graph_patches WHERE job_id = ? GROUP BY status",
            (job_id,),
        ).fetchall()
    }
    bundle = {
        "schema": LEARNING_BUNDLE_SCHEMA,
        "run_identity": {
            "phase": phase,
            "instance_id": instance_id,
            "run_id": run_id,
            "job_id": job_id,
            "job_state": job["state"],
            "objective": job["objective"],
            "generated_at": int(time.time()),
        },
        "source_evidence": {
            "runtime_db_ref": source_db_ref,
            "job_id": job_id,
            "baseline_bundle_ref": baseline_bundle_ref,
            "authoritative_inputs": [
                "runtime_jobs",
                "execution_nodes",
                "execution_events",
                "task_events",
                "graph_patches",
                "runtime_node_directives",
                "runtime_coordination_actions",
                "runtime_live_directive_deliveries",
                "runtime_active_worker_turns",
                "node_materializations",
                "node_artifacts",
            ],
        },
        "timeline": _timeline(conn, job_id),
        "graph_evolution": {
            "nodes": nodes,
            "node_count_by_role": {
                role: sum(item["node_type"] == role for item in nodes)
                for role in sorted({item["node_type"] for item in nodes})
            },
            "graph_patch_status_counts": patch_counts,
        },
        "coordination": {
            "summary": orchestration.get("coordination") or {},
            "live": live,
        },
        "handoff": orchestration.get("contribution_handoff") or {},
        "cost": ((orchestration.get("coordination") or {}).get("cost") or {}),
        "quality": normalized_quality,
        "findings": findings,
        "improvement_candidates": candidates,
        "regression_scenarios": [
            {
                "scenario_key": item["regression_scenario_key"],
                "candidate_key": item["candidate_key"],
                "required_assertion": item["expected_effect"],
            }
            for item in candidates
        ],
        "absorption": {
            "status": "validated",
            "finding_count": len(findings),
            "candidate_count": len(candidates),
            "dispositions": {
                disposition: sum(
                    finding["absorption_disposition"] == disposition
                    for finding in findings
                )
                for disposition in sorted(ABSORPTION_DISPOSITIONS)
            },
        },
    }
    validate_learning_bundle(bundle)
    return bundle


def validate_learning_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    required = {
        "run_identity",
        "source_evidence",
        "timeline",
        "graph_evolution",
        "coordination",
        "handoff",
        "cost",
        "quality",
        "findings",
        "improvement_candidates",
        "regression_scenarios",
        "absorption",
    }
    if not isinstance(bundle, dict) or bundle.get("schema") != LEARNING_BUNDLE_SCHEMA:
        raise LearningBundleError("invalid orchestration learning bundle schema")
    missing = sorted(required - set(bundle))
    if missing:
        raise LearningBundleError(f"learning bundle missing fields: {missing}")
    if not bundle["timeline"]:
        raise LearningBundleError("learning bundle timeline is empty")
    findings = bundle.get("findings") or []
    if not findings:
        raise LearningBundleError("learning bundle has no absorption finding")
    for finding in findings:
        if not finding.get("evidence_refs"):
            raise LearningBundleError("learning finding lacks evidence refs")
        if finding.get("absorption_disposition") not in ABSORPTION_DISPOSITIONS:
            raise LearningBundleError("learning finding has invalid disposition")
    return bundle


def init_learning_registry(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS learning_runs (
                bundle_sha256 TEXT PRIMARY KEY,
                phase TEXT NOT NULL,
                instance_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                bundle_path TEXT NOT NULL,
                status TEXT NOT NULL,
                absorbed_at INTEGER NOT NULL,
                UNIQUE(phase, instance_id, run_id)
            );
            CREATE TABLE IF NOT EXISTS learning_findings (
                finding_id TEXT NOT NULL,
                bundle_sha256 TEXT NOT NULL,
                category TEXT NOT NULL,
                disposition TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(finding_id, bundle_sha256)
            );
            CREATE TABLE IF NOT EXISTS improvement_candidates (
                candidate_key TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                first_bundle_sha256 TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS candidate_evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_key TEXT NOT NULL,
                arm TEXT NOT NULL,
                bundle_sha256 TEXT NOT NULL,
                quality_score REAL NOT NULL,
                target_metric REAL NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS candidate_promotions (
                candidate_key TEXT PRIMARY KEY,
                approved_by TEXT NOT NULL,
                baseline_bundle_sha256 TEXT NOT NULL,
                treatment_bundle_sha256 TEXT NOT NULL,
                promoted_at INTEGER NOT NULL
            );
            """
        )


def absorb_learning_bundle(
    bundle: dict[str, Any],
    *,
    bundle_path: Path,
    registry_path: Path,
) -> dict[str, Any]:
    validate_learning_bundle(bundle)
    init_learning_registry(registry_path)
    digest = bundle_sha256(bundle)
    identity = bundle["run_identity"]
    now = int(time.time())
    with sqlite3.connect(registry_path) as conn:
        existing = conn.execute(
            "SELECT bundle_sha256 FROM learning_runs WHERE phase = ? AND instance_id = ? AND run_id = ?",
            (identity["phase"], identity["instance_id"], identity["run_id"]),
        ).fetchone()
        if existing is not None and str(existing[0]) != digest:
            raise LearningBundleError("run identity already absorbed with another bundle hash")
        conn.execute(
            """
            INSERT OR IGNORE INTO learning_runs (
                bundle_sha256, phase, instance_id, run_id, job_id,
                bundle_path, status, absorbed_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'absorbed', ?)
            """,
            (
                digest,
                identity["phase"],
                identity["instance_id"],
                identity["run_id"],
                identity["job_id"],
                str(bundle_path.resolve()),
                now,
            ),
        )
        for finding in bundle["findings"]:
            conn.execute(
                """
                INSERT OR IGNORE INTO learning_findings (
                    finding_id, bundle_sha256, category, disposition, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    finding["finding_id"],
                    digest,
                    finding["category"],
                    finding["absorption_disposition"],
                    json.dumps(finding, ensure_ascii=False, sort_keys=True),
                ),
            )
        for candidate in bundle["improvement_candidates"]:
            conn.execute(
                """
                INSERT INTO improvement_candidates (
                    candidate_key, category, status, payload_json,
                    first_bundle_sha256, updated_at
                ) VALUES (?, ?, 'candidate', ?, ?, ?)
                ON CONFLICT(candidate_key) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                WHERE improvement_candidates.status NOT IN ('promoted', 'rejected')
                """,
                (
                    candidate["candidate_key"],
                    candidate["category"],
                    json.dumps(candidate, ensure_ascii=False, sort_keys=True),
                    digest,
                    now,
                ),
            )
    return {
        "schema": LEARNING_RECEIPT_SCHEMA,
        "registry_schema": LEARNING_REGISTRY_SCHEMA,
        "registry_path": str(registry_path.resolve()),
        "bundle_sha256": digest,
        "status": "absorbed",
        "absorbed_at": now,
        "finding_count": len(bundle["findings"]),
        "candidate_count": len(bundle["improvement_candidates"]),
    }


def render_learning_markdown(bundle: dict[str, Any]) -> str:
    identity = bundle["run_identity"]
    live = bundle["coordination"]["live"]
    lines = [
        "# Runtime Orchestra 过程学习报告",
        "",
        f"- Phase：`{identity['phase']}`",
        f"- 实例：`{identity['instance_id']}`",
        f"- Run：`{identity['run_id']}`",
        f"- Job：`{identity['job_id']}`",
        f"- 最终状态：`{identity['job_state']}`",
        "",
        "## 协调结果",
        "",
        f"- Live directive：尝试 {live['attempted']}，接受 {live['accepted']}，ACK {live['acknowledged']}，降级 {live['fallback']}。",
        f"- 在 target terminal 前避免的过时工作：{live['stale_work_avoided_count']}。",
        f"- 未避免的过时工作：{live['stale_work_not_avoided_count']}。",
        f"- 最终质量：`{json.dumps(bundle['quality'], ensure_ascii=False, sort_keys=True)}`",
        "",
        "## 关键时间线",
        "",
    ]
    for item in bundle["timeline"]:
        lines.append(
            f"- `{item['timestamp']}` `{item['event_type']}` "
            f"{item.get('node_key') or '-'}：{item['summary']} "
            f"（{item['evidence_ref']}）"
        )
    lines.extend(["", "## 发现与吸收", ""])
    for finding in bundle["findings"]:
        lines.extend(
            [
                f"### {finding['category']}",
                "",
                finding["summary"],
                "",
                f"- 根因：{finding['root_cause']}",
                f"- 处理：`{finding['absorption_disposition']}`",
                f"- 证据：{', '.join(finding['evidence_refs'])}",
                "",
            ]
        )
    lines.extend(["## 改进候选", ""])
    if bundle["improvement_candidates"]:
        for candidate in bundle["improvement_candidates"]:
            lines.append(
                f"- `{candidate['candidate_key']}`：{candidate['proposed_change']} "
                f"状态 `{candidate['status']}`。"
            )
    else:
        lines.append("- 本 run 无需建立新候选。")
    lines.append("")
    return "\n".join(lines)


def finalize_learning_bundle(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    run_root: Path,
    registry_path: Path,
    phase: str,
    instance_id: str,
    run_id: str,
    source_db_ref: str,
    quality: Optional[dict[str, Any]] = None,
    baseline_bundle_ref: Optional[str] = None,
) -> dict[str, Any]:
    reports = run_root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    bundle_path = reports / "orchestration-learning.json"
    markdown_path = reports / "orchestration-learning.md"
    receipt_path = reports / "orchestration-learning-receipt.json"
    bundle = build_learning_bundle(
        conn,
        job_id,
        phase=phase,
        instance_id=instance_id,
        run_id=run_id,
        source_db_ref=source_db_ref,
        quality=quality,
        baseline_bundle_ref=baseline_bundle_ref,
    )
    bundle_path.write_text(
        json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    receipt = absorb_learning_bundle(
        bundle,
        bundle_path=bundle_path,
        registry_path=registry_path,
    )
    bundle["absorption"].update(
        {
            "status": "absorbed",
            "bundle_sha256": receipt["bundle_sha256"],
            "registry_receipt": str(receipt_path.resolve()),
            "absorbed_at": receipt["absorbed_at"],
        }
    )
    validate_learning_bundle(bundle)
    bundle_path.write_text(
        json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_learning_markdown(bundle), encoding="utf-8")
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "bundle": bundle,
        "bundle_path": str(bundle_path),
        "markdown_path": str(markdown_path),
        "receipt_path": str(receipt_path),
        "receipt": receipt,
    }


def record_candidate_evaluation(
    registry_path: Path,
    *,
    candidate_key: str,
    arm: str,
    bundle_sha256_value: str,
    quality_score: float,
    target_metric: float,
) -> None:
    if arm not in {"baseline", "treatment"}:
        raise LearningBundleError("candidate evaluation arm must be baseline or treatment")
    init_learning_registry(registry_path)
    with sqlite3.connect(registry_path) as conn:
        if conn.execute(
            "SELECT 1 FROM improvement_candidates WHERE candidate_key = ?",
            (candidate_key,),
        ).fetchone() is None:
            raise LearningBundleError("unknown improvement candidate")
        conn.execute(
            """
            INSERT INTO candidate_evaluations (
                candidate_key, arm, bundle_sha256, quality_score,
                target_metric, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_key,
                arm,
                bundle_sha256_value,
                float(quality_score),
                float(target_metric),
                int(time.time()),
            ),
        )


def promote_candidate(
    registry_path: Path,
    *,
    candidate_key: str,
    approved_by: str,
) -> dict[str, Any]:
    if not approved_by.strip():
        raise LearningBundleError("candidate promotion requires explicit approval")
    init_learning_registry(registry_path)
    with sqlite3.connect(registry_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM candidate_evaluations
             WHERE candidate_key = ? ORDER BY created_at DESC, id DESC
            """,
            (candidate_key,),
        ).fetchall()
        latest = {}
        for row in rows:
            latest.setdefault(str(row["arm"]), row)
        if set(latest) != {"baseline", "treatment"}:
            raise LearningBundleError("promotion requires baseline and treatment replay")
        baseline = latest["baseline"]
        treatment = latest["treatment"]
        if float(treatment["quality_score"]) < float(baseline["quality_score"]):
            raise LearningBundleError("treatment quality regressed")
        if float(treatment["target_metric"]) >= float(baseline["target_metric"]):
            raise LearningBundleError("targeted orchestration metric did not improve")
        now = int(time.time())
        conn.execute(
            "UPDATE improvement_candidates SET status = 'promoted', updated_at = ? WHERE candidate_key = ?",
            (now, candidate_key),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO candidate_promotions (
                candidate_key, approved_by, baseline_bundle_sha256,
                treatment_bundle_sha256, promoted_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                candidate_key,
                approved_by.strip(),
                baseline["bundle_sha256"],
                treatment["bundle_sha256"],
                now,
            ),
        )
    return {
        "candidate_key": candidate_key,
        "status": "promoted",
        "approved_by": approved_by.strip(),
        "baseline_bundle_sha256": baseline["bundle_sha256"],
        "treatment_bundle_sha256": treatment["bundle_sha256"],
    }
