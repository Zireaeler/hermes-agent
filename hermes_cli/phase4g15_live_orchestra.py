"""Phase 4G15 lightweight real app-server live-orchestra validation."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_kernel as rk
from hermes_cli import kanban_runtime_learning as learning
from hermes_cli import validation_artifacts
from hermes_cli.codex_app_server_worker import run_app_server_turn


INSTANCE_ID = "controlled-live-directive"


@dataclass(frozen=True)
class LiveRunConfig:
    root: Path
    artifact_root: Path
    model: str = "gpt-5.6-sol"
    turn_timeout_seconds: int = 300
    cleanup_source: bool = True


_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["completed", "blocked"]},
        "observed_contract": {"type": "string"},
        "result_value": {"type": "string"},
        "consumed_directive_ids": {
            "type": "array",
            "items": {"type": "string"},
        },
        "verification": {"type": "string"},
    },
    "required": [
        "status",
        "observed_contract",
        "result_value",
        "consumed_directive_ids",
        "verification",
    ],
    "additionalProperties": False,
}


def _copy_codex_home(source: Path, target: Path) -> None:
    target.mkdir(parents=True)
    for name in ("config.toml", "auth.json"):
        source_file = source / name
        if not source_file.is_file():
            raise RuntimeError(f"Codex source home lacks {name}: {source}")
        shutil.copy2(source_file, target / name)
        (target / name).chmod(0o600)
    rules = source / "rules"
    if rules.is_dir():
        shutil.copytree(rules, target / "rules")


def _write_workspace(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "contract.json").write_text(
        json.dumps({"version": "legacy-v1"}) + "\n",
        encoding="utf-8",
    )
    prepare_source = """import json
import time
from pathlib import Path

root = Path(__file__).parent
contract = json.loads((root / "contract.json").read_text(encoding="utf-8"))
(root / "captured-contract.txt").write_text(contract["version"] + "\\n", encoding="utf-8")
(root / "consumer-inspected.marker").write_text("ready\\n", encoding="utf-8")
time.sleep(6)
"""
    verify_source = """from pathlib import Path

value = (Path(__file__).parent / "result.txt").read_text(encoding="utf-8").strip()
if value != "contract-v2":
    raise SystemExit(f"expected contract-v2, got {value!r}")
print("verified contract-v2")
"""
    compile(prepare_source, "prepare_consumer.py", "exec")
    compile(verify_source, "verify_result.py", "exec")
    (path / "prepare_consumer.py").write_text(prepare_source, encoding="utf-8")
    (path / "verify_result.py").write_text(verify_source, encoding="utf-8")


def _producer_process(workspace: Path) -> subprocess.Popen[str]:
    code = (
        "import json,time; from pathlib import Path; "
        f"p=Path({str(workspace)!r}); m=p/'consumer-inspected.marker'; "
        "deadline=time.time()+90; "
        "\nwhile not m.exists() and time.time()<deadline: time.sleep(0.05)"
        "\nif not m.exists(): raise SystemExit('consumer marker timeout')"
        "\n(p/'contract.json').write_text(json.dumps({'version':'contract-v2'})+'\\n', encoding='utf-8')"
        "\n(p/'producer.done').write_text('contract-v2\\n', encoding='utf-8')"
    )
    return subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=workspace,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _parse_result(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"worker did not return constrained JSON: {text[-1000:]}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("worker result is not an object")
    return value


def _run_turn(
    *,
    workspace: Path,
    codex_home: Path,
    model: str,
    prompt: str,
    timeout: int,
    resume_thread_id: Optional[str] = None,
    register_turn=None,
    poll_live_directives=None,
    record_live_delivery=None,
    complete_turn=None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    events: list[dict[str, Any]] = []
    result = run_app_server_turn(
        prompt=prompt,
        workspace=str(workspace),
        model=model,
        sandbox="workspace-write",
        approval="never",
        output_schema=_OUTPUT_SCHEMA,
        resume_thread_id=resume_thread_id,
        codex_bin=shutil.which("codex") or "codex",
        codex_home=str(codex_home),
        env={"CODEX_HOME": str(codex_home)},
        timeout_seconds=float(timeout),
        poll_interval=0.1,
        on_notification=events.append,
        register_turn=register_turn,
        poll_live_directives=poll_live_directives,
        record_live_delivery=record_live_delivery,
        complete_turn=complete_turn,
    )
    if result.status != "completed" or result.error:
        raise RuntimeError(f"Codex app-server turn failed: {result.error or result.status}")
    receipt = _parse_result(result.final_text)
    return receipt, {
        "thread_id": result.thread_id,
        "turn_id": result.turn_id,
        "notification_count": len(events),
        "accepted_delivery_ids": result.accepted_delivery_ids,
    }


def _primary_prompt() -> str:
    return """Complete this controlled responsibility using the terminal tools.

1. Run `python3 prepare_consumer.py` exactly once. It captures the contract that
   existed when this responsibility started and creates a marker for another
   process. Do not rerun this command and do not reread contract.json afterward.
2. After that command exits, write the captured value from
   captured-contract.txt into result.txt unless a Hermes Runtime live directive
   changes the responsibility. A live directive takes precedence over the
   captured plan.
3. Verify result.txt only for readability; do not run verify_result.py because
   the external quality check is intentionally independent.
4. Return only the constrained JSON object. Include a live directive ID in
   consumed_directive_ids only after applying it.
"""


def _correction_prompt() -> str:
    return """The independent quality check rejected the previous candidate:
result.txt must contain the current contract value `contract-v2`. Continue the
same responsibility, inspect the current contract if needed, correct result.txt,
run `python3 verify_result.py`, and return only the constrained JSON object.
There is no Runtime directive in this correction turn, so keep
consumed_directive_ids empty.
"""


def _verify(workspace: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "verify_result.py"],
        cwd=workspace,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    return {
        "passed": completed.returncode == 0,
        "returncode": completed.returncode,
        "output": completed.stdout.strip(),
        "result_value": (
            (workspace / "result.txt").read_text(encoding="utf-8").strip()
            if (workspace / "result.txt").is_file()
            else None
        ),
    }


def _run_baseline(config: LiveRunConfig, codex_home: Path) -> dict[str, Any]:
    workspace = config.root / "workspace" / "baseline"
    _write_workspace(workspace)
    producer = _producer_process(workspace)
    first_receipt, first_transport = _run_turn(
        workspace=workspace,
        codex_home=codex_home,
        model=config.model,
        prompt=_primary_prompt(),
        timeout=config.turn_timeout_seconds,
    )
    if first_receipt.get("status") != "completed":
        producer.terminate()
        producer.communicate(timeout=5)
        raise RuntimeError(f"baseline worker blocked: {first_receipt}")
    producer_output = producer.communicate(timeout=10)[0]
    first_quality = _verify(workspace)
    recovery_turns = 0
    correction_receipt = None
    correction_transport = None
    if not first_quality["passed"]:
        recovery_turns = 1
        correction_receipt, correction_transport = _run_turn(
            workspace=workspace,
            codex_home=codex_home,
            model=config.model,
            prompt=_correction_prompt(),
            timeout=config.turn_timeout_seconds,
            resume_thread_id=first_transport["thread_id"],
        )
    final_quality = _verify(workspace)
    return {
        "first_receipt": first_receipt,
        "first_transport": first_transport,
        "first_quality": first_quality,
        "recovery_turns": recovery_turns,
        "correction_receipt": correction_receipt,
        "correction_transport": correction_transport,
        "final_quality": final_quality,
        "producer_output": producer_output,
    }


def _create_treatment_job(conn, workspace: Path) -> tuple[str, dict[str, Any]]:
    root_task = kb.create_task(
        conn,
        title="Phase 4G15 treatment",
        initial_status="running",
        workspace_kind="dir",
        workspace_path=str(workspace),
    )
    job_id = rk.create_runtime_job(
        conn,
        root_task,
        "Consume the latest contract without completing obsolete v1 work.",
        workspace_path=str(workspace),
        goal_items=[
            {
                "item_key": "current-contract-result",
                "description": "result uses contract-v2",
                "required": True,
                "verifier_required": False,
            }
        ],
        initialization_mode="fixture",
    )
    consumer = dict(
        conn.execute(
            "SELECT * FROM execution_nodes WHERE job_id = ? ORDER BY created_at LIMIT 1",
            (job_id,),
        ).fetchone()
    )
    task_id = kb.create_task(
        conn,
        title="Phase 4G15 active consumer",
        body="controlled app-server consumer",
        assignee="phase4g15-app-server",
        initial_status="running",
        workspace_kind="dir",
        workspace_path=str(workspace),
        tenant=f"runtime:{job_id}",
    )
    claimed = kb.claim_task(
        conn,
        task_id,
        claimer="phase4g15-app-server",
    )
    if claimed is None:
        raise RuntimeError("controlled consumer task could not be claimed")
    run = kb.latest_run(conn, task_id)
    if run is None:
        raise RuntimeError("controlled consumer claim did not create a task run")
    materialization_id = rk._id("mat")
    now = int(time.time())
    conn.execute(
        """
        UPDATE execution_nodes
           SET node_key = 'consumer', state = 'running', assignee = ?,
               latest_task_id = ?, latest_run_id = ?, started_at = ?, updated_at = ?
         WHERE id = ?
        """,
        ("phase4g15-app-server", task_id, run.id, now, now, consumer["id"]),
    )
    conn.execute(
        """
        INSERT INTO node_materializations (
            id, job_id, node_id, attempt, task_id, run_id, worker_lane,
            status, created_at, started_at, metadata_json
        ) VALUES (?, ?, ?, 1, ?, ?, ?, 'running', ?, ?, ?)
        """,
        (
            materialization_id,
            job_id,
            consumer["id"],
            task_id,
            run.id,
            "phase4g15-app-server",
            now,
            now,
            json.dumps({"worker_transport": "codex_app_server"}),
        ),
    )
    source_node_id = rk._id("rnode")
    conn.execute(
        """
        INSERT INTO execution_nodes (
            id, job_id, node_key, node_type, state, title, description,
            metadata_json, created_at, updated_at, completed_at
        ) VALUES (?, ?, 'producer', 'implementation', 'running', ?, ?, '{}', ?, ?, NULL)
        """,
        (
            source_node_id,
            job_id,
            "Publish contract-v2",
            "Independent producer process changes the shared contract.",
            now,
            now,
        ),
    )
    conn.execute(
        "UPDATE runtime_jobs SET state = 'waiting_worker', updated_at = ? WHERE id = ?",
        (now, job_id),
    )
    return job_id, {
        "consumer_node_id": consumer["id"],
        "producer_node_id": source_node_id,
        "materialization_id": materialization_id,
        "task_id": task_id,
        "run_id": run.id,
    }


def _run_treatment(
    config: LiveRunConfig,
    codex_home: Path,
    job_id: str,
    ids: dict[str, Any],
) -> dict[str, Any]:
    workspace = config.root / "workspace" / "treatment"
    producer = _producer_process(workspace)
    directive_ref: dict[str, str] = {}
    producer_finished = threading.Event()

    def _publish_evidence() -> None:
        output = producer.communicate(timeout=90)[0]
        if producer.returncode != 0:
            directive_ref["error"] = output or f"producer exited {producer.returncode}"
            producer_finished.set()
            return
        with kb.connect() as writer:
            now = int(time.time())
            writer.execute(
                "UPDATE execution_nodes SET state = 'succeeded', completed_at = ?, updated_at = ? WHERE id = ?",
                (now, now, ids["producer_node_id"]),
            )
            event_id = rk._event(
                writer,
                job_id,
                "worker_coordination_checkpointed",
                {
                    "checkpoint": {
                        "kind": "shared_contract_changed",
                        "summary": "Producer published contract-v2 while consumer was active.",
                        "findings": [
                            {
                                "finding_key": "contract-v2",
                                "type": "shared_contract_changed",
                                "summary": "legacy-v1 is obsolete",
                                "affected_node_keys": ["consumer"],
                                "evidence_refs": ["workspace:contract.json"],
                            }
                        ],
                    }
                },
                node_id=ids["producer_node_id"],
            )
            directive_id = rk._id("rdir")
            payload = {
                "schema": rk.RUNTIME_NODE_DIRECTIVE_SCHEMA,
                "directive_id": directive_id,
                "target_node_key": "consumer",
                "source_checkpoint_event_id": event_id,
                "action": "stop_obsolete_work",
                "summary": "legacy-v1 is obsolete; complete only contract-v2 output.",
                "instructions": [
                    "Do not write captured legacy-v1 into result.txt.",
                    "Write contract-v2 and verify it before terminal receipt.",
                ],
                "evidence_refs": [f"event:{event_id}", "workspace:contract.json"],
                "expected_contract_revision": 1,
                "applied_contract_revision": 1,
                "contract": None,
            }
            writer.execute(
                """
                INSERT INTO runtime_node_directives (
                    id, job_id, target_node_id, source_checkpoint_event_id,
                    action, status, expected_contract_revision,
                    applied_contract_revision, directive_json, created_at
                ) VALUES (?, ?, ?, ?, 'stop_obsolete_work', 'queued', 1, 1, ?, ?)
                """,
                (
                    directive_id,
                    job_id,
                    ids["consumer_node_id"],
                    event_id,
                    json.dumps(payload),
                    now,
                ),
            )
            delivery_id = rk._queue_runtime_live_delivery(writer, directive_id)
            if delivery_id is None:
                directive_ref["error"] = "Runtime did not queue a live delivery"
            else:
                directive_ref.update(
                    {"directive_id": directive_id, "delivery_id": delivery_id}
                )
        producer_finished.set()

    publisher = threading.Thread(target=_publish_evidence, daemon=True)
    publisher.start()

    def _register(thread_id: str, turn_id: str) -> list[dict[str, Any]]:
        with kb.connect() as reader:
            registered = rk.register_runtime_live_turn(
                reader,
                task_id=ids["task_id"],
                run_id=ids["run_id"],
                thread_id=thread_id,
                turn_id=turn_id,
            )
        return list(registered.get("deliveries") or [])

    def _poll(thread_id: str, turn_id: str) -> list[dict[str, Any]]:
        with kb.connect() as reader:
            return rk.pending_runtime_live_directives(
                reader,
                task_id=ids["task_id"],
                run_id=ids["run_id"],
                thread_id=thread_id,
                turn_id=turn_id,
            )

    def _record(
        delivery: dict[str, Any],
        accepted: bool,
        error_code: Optional[str],
        error_message: Optional[str],
    ) -> None:
        with kb.connect() as writer:
            rk.record_runtime_live_delivery(
                writer,
                str(delivery["id"]),
                accepted=accepted,
                thread_id=str(delivery["thread_id"]),
                turn_id=str(delivery["turn_id"]),
                request_ref=f"turn/steer:{delivery['id']}",
                response_ref="turn/steer:accepted" if accepted else None,
                error_code=error_code,
                error_message=error_message,
            )

    def _complete(thread_id: str, turn_id: str) -> None:
        with kb.connect() as writer:
            rk.close_runtime_live_turn(
                writer,
                task_id=ids["task_id"],
                run_id=ids["run_id"],
                thread_id=thread_id,
                turn_id=turn_id,
            )

    receipt, transport = _run_turn(
        workspace=workspace,
        codex_home=codex_home,
        model=config.model,
        prompt=_primary_prompt(),
        timeout=config.turn_timeout_seconds,
        register_turn=_register,
        poll_live_directives=_poll,
        record_live_delivery=_record,
        complete_turn=_complete,
    )
    producer_finished.wait(timeout=10)
    publisher.join(timeout=1)
    if directive_ref.get("error"):
        raise RuntimeError(directive_ref["error"])
    quality = _verify(workspace)
    with kb.connect() as writer:
        materialization = dict(
            writer.execute(
                "SELECT * FROM node_materializations WHERE id = ?",
                (ids["materialization_id"],),
            ).fetchone()
        )
        consumer = dict(
            writer.execute(
                "SELECT * FROM execution_nodes WHERE id = ?",
                (ids["consumer_node_id"],),
            ).fetchone()
        )
        consumed = [str(value) for value in receipt.get("consumed_directive_ids") or []]
        acknowledged = rk._acknowledge_node_directives(
            writer,
            consumer,
            materialization,
            consumed,
        )
        now = int(time.time())
        writer.execute(
            "UPDATE node_materializations SET status = 'succeeded', completed_at = ? WHERE id = ?",
            (now, ids["materialization_id"]),
        )
        writer.execute(
            "UPDATE execution_nodes SET state = 'succeeded', completed_at = ?, updated_at = ? WHERE id = ?",
            (now, now, ids["consumer_node_id"]),
        )
        writer.execute(
            "UPDATE runtime_jobs SET state = ?, updated_at = ? WHERE id = ?",
            ("done" if quality["passed"] else "failed", now, job_id),
        )
        rk._event(
            writer,
            job_id,
            "node_completed" if quality["passed"] else "node_failed",
            {
                "node_key": "consumer",
                "quality": quality,
                "consumed_directive_ids": acknowledged,
            },
            node_id=ids["consumer_node_id"],
            task_id=ids["task_id"],
            run_id=ids["run_id"],
        )
    return {
        "receipt": receipt,
        "transport": transport,
        "quality": quality,
        "directive": directive_ref,
        "acknowledged_directive_ids": acknowledged,
    }


def _write_report(root: Path, report: dict[str, Any]) -> None:
    reports = root / "reports"
    reports.mkdir(exist_ok=True)
    (reports / "run-report.json").write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    baseline = report["baseline"]
    treatment = report["treatment"]
    markdown = f"""# Phase 4G15 受控真实验证

## 对照设计

- Baseline：同一 coherent Codex thread 首轮按旧契约完成，独立检查失败后第二轮修正。
- Treatment：同一任务在首轮 active turn 中收到 Runtime `turn/steer`，并在 terminal receipt ACK。
- 两边模型、能力、仓库任务和最终质量检查一致。

## 结果

- Baseline 首轮通过：`{baseline['first_quality']['passed']}`
- Baseline 恢复轮次：`{baseline['recovery_turns']}`
- Baseline 最终通过：`{baseline['final_quality']['passed']}`
- Treatment 首轮通过：`{treatment['quality']['passed']}`
- Treatment directive：`{treatment['directive'].get('directive_id')}`
- Treatment ACK：`{treatment['acknowledged_directive_ids']}`
- Treatment stale work avoided：`{report['learning']['stale_work_avoided_count']}`

## 结论

Runtime 在 producer 发布真实新契约后、consumer terminal 前改变了仍在执行的责任。Treatment 保持与
coherent single worker 相同的最终质量，同时少了一次外部质量检查后的恢复 turn。该结论只覆盖
live directive transport 与责任更新，不外推为 hard benchmark 的整体质量优势。
"""
    (reports / "capability-trace.md").write_text(markdown, encoding="utf-8")


def _prepare_worker_events(root: Path, codex_home: Path) -> None:
    target = root / "worker-events"
    sessions_target = target / "sessions"
    sessions_target.mkdir(parents=True)
    session_root = codex_home / "sessions"
    files: list[dict[str, Any]] = []
    for source in sorted(session_root.rglob("*.jsonl")):
        relative = source.relative_to(session_root)
        destination = sessions_target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        content = destination.read_bytes()
        files.append(
            {
                "path": str(Path("sessions") / relative),
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    (target / "index.json").write_text(
        json.dumps(
            {
                "schema": "hermes_phase4g15_worker_event_index_v1",
                "session_count": len(files),
                "files": files,
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def run_phase4g15(config: LiveRunConfig) -> dict[str, Any]:
    root = config.root.resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError(f"Phase 4G15 run root must be empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    validation_artifacts.declare_managed_orchestration_validation(
        root,
        phase="phase4g15",
        instance_id=INSTANCE_ID,
    )
    source_codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    codex_home = root / "cache" / "codex-home"
    _copy_codex_home(source_codex_home, codex_home)
    hermes_home = root / "hermes-home"
    prior_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(hermes_home)
    try:
        baseline = _run_baseline(config, codex_home)
        treatment_workspace = root / "workspace" / "treatment"
        _write_workspace(treatment_workspace)
        kb.init_db()
        with kb.connect() as conn:
            job_id, ids = _create_treatment_job(conn, treatment_workspace)
        treatment = _run_treatment(
            config,
            codex_home,
            job_id,
            ids,
        )
        with kb.connect() as conn:
            learning_result = learning.finalize_learning_bundle(
                conn,
                job_id,
                run_root=root,
                registry_path=(
                    config.artifact_root
                    / "orchestration-learning"
                    / "registry.sqlite3"
                ),
                phase="phase4g15",
                instance_id=INSTANCE_ID,
                run_id=root.name,
                source_db_ref="hermes-home/kanban.db",
                quality={
                    "status": "passed" if treatment["quality"]["passed"] else "failed",
                    "baseline_final_passed": baseline["final_quality"]["passed"],
                    "treatment_final_passed": treatment["quality"]["passed"],
                    "quality_non_regression": bool(
                        treatment["quality"]["passed"]
                        >= baseline["final_quality"]["passed"]
                    ),
                },
            )
        live = learning_result["bundle"]["coordination"]["live"]
        report = {
            "schema": "hermes_phase4g15_live_orchestra_report_v1",
            "instance_id": INSTANCE_ID,
            "job_id": job_id,
            "model": config.model,
            "real_model_worker_used": True,
            "official_evaluator_used": False,
            "baseline": baseline,
            "treatment": treatment,
            "learning": {
                "bundle_sha256": learning_result["receipt"]["bundle_sha256"],
                "absorption_status": learning_result["receipt"]["status"],
                "stale_work_avoided_count": live["stale_work_avoided_count"],
                "stale_work_not_avoided_count": live["stale_work_not_avoided_count"],
            },
            "acceptance": {
                "baseline_final_quality": baseline["final_quality"]["passed"],
                "treatment_final_quality": treatment["quality"]["passed"],
                "quality_non_regression": bool(
                    treatment["quality"]["passed"]
                    >= baseline["final_quality"]["passed"]
                ),
                "live_directive_acked_before_terminal": bool(
                    treatment["acknowledged_directive_ids"]
                    and live["stale_work_avoided_count"] >= 1
                ),
                "recovery_turn_reduced": baseline["recovery_turns"] >= 1,
            },
            "generated_at": int(time.time()),
        }
        passed = all(report["acceptance"].values())
        report["status"] = "passed" if passed else "failed"
        _write_report(root, report)
        _prepare_worker_events(root, codex_home)
        manifest = validation_artifacts.archive_validation_run(
            root,
            artifact_root=config.artifact_root,
            phase="phase4g15",
            instance_id=INSTANCE_ID,
            redactions=validation_artifacts.model_source_redactions(codex_home),
            expected_entries=("worker-events", "hermes-home", "reports"),
            orchestration_learning_required=True,
        )
        report["artifact_archive"] = {
            "artifact_path": manifest["artifact_path"],
            "manifest_path": str(Path(manifest["artifact_path"]) / "manifest.json"),
            "status": manifest["status"],
        }
        if config.cleanup_source:
            validation_artifacts.cleanup_rebuildable_entries(
                root,
                manifest_path=Path(manifest["artifact_path"]) / "manifest.json",
                entries=("workspace", "cache"),
                orchestration_learning_required=True,
            )
        return report
    finally:
        if prior_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = prior_home


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="运行 Phase 4G15 轻量真实协调验证")
    parser.add_argument("--root", required=True)
    parser.add_argument(
        "--artifact-root",
        default=str(validation_artifacts.default_artifact_root()),
    )
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--turn-timeout-seconds", type=int, default=300)
    parser.add_argument("--keep-source", action="store_true")
    args = parser.parse_args(argv)
    report = run_phase4g15(
        LiveRunConfig(
            root=Path(args.root),
            artifact_root=Path(args.artifact_root),
            model=args.model,
            turn_timeout_seconds=args.turn_timeout_seconds,
            cleanup_source=not args.keep_source,
        )
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
