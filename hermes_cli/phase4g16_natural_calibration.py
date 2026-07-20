"""Phase 4G16 natural orchestration paired calibration campaign."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time
import traceback
from typing import Any, Optional

from hermes_cli import codex_worker
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_runtime_kernel as rk
from hermes_cli import kanban_runtime_learning as learning
from hermes_cli import kanban_runtime_phase4g8 as p4g8
from hermes_cli import validation_artifacts
from hermes_cli.codex_app_server_worker import run_app_server_turn
from hermes_cli.kanban_runtime_worker_smoke import run_real_worker_lane_smoke
from hermes_cli.worker_lanes import clear_worker_lanes, register_worker_lane


CAMPAIGN_SCHEMA = "hermes_phase4g16_natural_calibration_v1"
CASE_REPORT_SCHEMA = "hermes_phase4g16_case_report_v1"
PHASE = "phase4g16"
LANE_NAME = "phase4g16-codex"


@dataclass(frozen=True)
class CalibrationConfig:
    root: Path
    artifact_root: Path
    source_codex_home: Path
    model: Optional[str] = None
    worker_timeout_seconds: int = 600
    decision_timeout_seconds: int = 180
    cleanup_source: bool = True


@dataclass(frozen=True)
class CalibrationCase:
    key: str
    title: str
    kind: str
    objective: str
    goal_item_key: str
    goal_description: str
    files: dict[str, str]


def _archive_instance_id(config: CalibrationConfig, case: CalibrationCase) -> str:
    return f"{case.key}-{config.root.expanduser().resolve().name}"


def _cases() -> tuple[CalibrationCase, ...]:
    return (
        CalibrationCase(
            key="coherent-negative",
            title="Coherent negative control",
            kind="coherent_negative_control",
            objective=(
                "为现有同步操作执行器增加可验证的重试策略：支持固定最大尝试次数、"
                "可注入的等待函数、异常过滤和最终异常传播。保持 API 小而清晰，补齐测试，"
                "并运行 python3 -m unittest discover -s tests -v。"
            ),
            goal_item_key="retry-policy",
            goal_description="重试策略行为完整且全部仓库测试通过",
            files={
                "src/__init__.py": "",
                "src/retry.py": (
                    "def run(operation):\n"
                    "    return operation()\n"
                ),
                "tests/test_retry.py": '''import unittest

from src.retry import RetryPolicy, run_with_retry


class RetryTest(unittest.TestCase):
    def test_retries_then_returns_value(self):
        calls = []
        waits = []

        def operation():
            calls.append(len(calls))
            if len(calls) < 3:
                raise ValueError("temporary")
            return "ok"

        result = run_with_retry(
            operation,
            RetryPolicy(max_attempts=3, delay_seconds=0.25),
            sleep=waits.append,
        )
        self.assertEqual(result, "ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual(waits, [0.25, 0.25])

    def test_unmatched_exception_is_not_retried(self):
        calls = []

        def operation():
            calls.append(1)
            raise TypeError("programming error")

        with self.assertRaises(TypeError):
            run_with_retry(
                operation,
                RetryPolicy(max_attempts=4, retry_exceptions=(ValueError,)),
                sleep=lambda _: None,
            )
        self.assertEqual(len(calls), 1)

    def test_policy_rejects_invalid_attempt_count(self):
        with self.assertRaises(ValueError):
            RetryPolicy(max_attempts=0)


if __name__ == "__main__":
    unittest.main()
''',
            },
        ),
        CalibrationCase(
            key="shared-contract-medium",
            title="Shared contract Medium",
            kind="shared_contract_medium",
            objective=(
                "升级记录处理流水线：所有输入规范化为包含 id、source 和稳定 tags 的共享记录契约；"
                "格式化输出和内存存储必须消费同一契约，存储往返后保持语义一致。兼容缺少 tags 的旧输入，"
                "补齐测试并运行 python3 -m unittest discover -s tests -v。"
            ),
            goal_item_key="record-contract",
            goal_description="规范化、格式化和存储共享一个稳定记录契约",
            files={
                "src/__init__.py": "",
                "src/normalize.py": '''def normalize_record(raw):
    return {"id": str(raw["id"]), "name": str(raw.get("name", ""))}
''',
                "src/formatting.py": '''def format_record(record):
    return f"{record['id']}:{record['name']}"
''',
                "src/store.py": '''class RecordStore:
    def __init__(self):
        self._items = {}

    def put(self, record):
        self._items[record["id"]] = dict(record)

    def get(self, record_id):
        value = self._items.get(str(record_id))
        return dict(value) if value is not None else None
''',
                "tests/test_records.py": '''import unittest

from src.formatting import format_record
from src.normalize import normalize_record
from src.store import RecordStore


class RecordPipelineTest(unittest.TestCase):
    def test_shared_contract_is_normalized_and_formatted(self):
        record = normalize_record({
            "id": 7,
            "source": " Import ",
            "tags": ["Blue", "blue", " fast "],
        })
        self.assertEqual(record, {
            "id": "7",
            "source": "import",
            "tags": ("blue", "fast"),
        })
        self.assertEqual(format_record(record), "7@import [blue,fast]")

    def test_legacy_input_and_store_round_trip(self):
        record = normalize_record({"id": "old", "source": "legacy"})
        self.assertEqual(record["tags"], ())
        store = RecordStore()
        store.put(record)
        recovered = store.get("old")
        self.assertEqual(recovered, record)
        recovered["source"] = "changed"
        self.assertEqual(store.get("old")["source"], "legacy")

    def test_invalid_source_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize_record({"id": "x", "source": "   "})


if __name__ == "__main__":
    unittest.main()
''',
            },
        ),
        CalibrationCase(
            key="durable-boundary-medium",
            title="Durable boundary Medium",
            kind="durable_boundary_medium",
            objective=(
                "将事件处理平台升级到 schema v2：核心摄取与导出必须生成稳定、隔离的事件契约；"
                "同时保持已安装的 v1 transform 插件和 audit JSONL 批处理消费者继续工作。"
                "两类扩展都必须独立、可测试，不能把兼容字段或敏感字段处理逻辑带入核心 schema，"
                "最终完成端到端集成测试并运行 "
                "python3 -m unittest discover -s tests -v。"
            ),
            goal_item_key="event-schema-v2",
            goal_description="schema v2 核心、legacy plugin 与 audit consumer 全部集成通过",
            files={
                "src/__init__.py": "",
                "src/core/__init__.py": "",
                "src/core/event.py": '''def ingest(kind, payload):
    return {"kind": str(kind), "payload": dict(payload)}
''',
                "src/core/schema.py": '''def validate_event(event):
    return event
''',
                "src/core/batch.py": '''from .event import ingest


def ingest_batch(records):
    return [ingest(item["kind"], item["payload"]) for item in records]
''',
                "src/core/export.py": '''import json


def export_event(event):
    return json.dumps(event, sort_keys=True)
''',
                "src/extensions/__init__.py": "",
                "src/extensions/loader.py": '''import importlib


def load_plugin(module_name):
    return importlib.import_module(module_name)
''',
                "src/extensions/legacy_v1.py": '''from .loader import load_plugin


def run_v1_plugin(module_name, event):
    return load_plugin(module_name).transform(event)
''',
                "src/extensions/audit_jsonl.py": '''import json


def export_audit_batch(policy_module, events):
    return "\\n".join(json.dumps(event) for event in events)
''',
                "fixtures/__init__.py": "",
                "fixtures/v1_enricher.py": '''def transform(event):
    return {
        "type": event["type"],
        "data": {**event["data"], "legacy_plugin": "v1"},
    }
''',
                "fixtures/bad_v1_plugin.py": '''def transform(event):
    return [event]
''',
                "fixtures/audit_policy.py": '''SENSITIVE_FIELDS = {"password", "secret", "token"}


def include(event):
    return event["kind"] != "heartbeat"
''',
                "tests/test_core_pipeline.py": '''import json
import math
import unittest

from src.core.event import ingest
from src.core.export import export_event


class CorePipelineTest(unittest.TestCase):
    def test_schema_v2_round_trip(self):
        event = ingest("created", {"item": 3})
        self.assertEqual(event, {
            "version": 2,
            "kind": "created",
            "payload": {"item": 3},
        })
        self.assertEqual(json.loads(export_event(event)), event)

    def test_payload_is_deep_copied(self):
        payload = {"item": {"value": 4}}
        event = ingest("updated", payload)
        payload["item"]["value"] = 99
        self.assertEqual(event["payload"], {"item": {"value": 4}})

    def test_export_is_compact_and_stable(self):
        event = ingest("created", {"z": 1, "a": {"b": 2, "a": 1}})
        self.assertEqual(
            export_event(event),
            '{"kind":"created","payload":{"a":{"a":1,"b":2},"z":1},"version":2}',
        )

    def test_non_standard_numbers_are_rejected(self):
        with self.assertRaises(ValueError):
            export_event(ingest("bad", {"value": math.nan}))

    def test_kind_is_normalized_and_must_not_be_empty(self):
        self.assertEqual(ingest(" Created ", {})["kind"], "created")
        with self.assertRaises(ValueError):
            ingest("   ", {})

    def test_payload_must_be_a_mapping(self):
        with self.assertRaises(TypeError):
            ingest("created", [("item", 3)])


if __name__ == "__main__":
    unittest.main()
''',
                "tests/test_core_batch.py": '''import copy
import unittest

from src.core.batch import ingest_batch


class CoreBatchTest(unittest.TestCase):
    def test_batch_uses_the_same_v2_contract(self):
        records = [
            {"kind": "created", "payload": {"item": 1}},
            {"kind": "updated", "payload": {"item": 2}},
        ]
        self.assertEqual(ingest_batch(records), [
            {"version": 2, "kind": "created", "payload": {"item": 1}},
            {"version": 2, "kind": "updated", "payload": {"item": 2}},
        ])

    def test_batch_does_not_mutate_source_records(self):
        records = [{"kind": "created", "payload": {"nested": {"value": 1}}}]
        original = copy.deepcopy(records)
        result = ingest_batch(records)
        result[0]["payload"]["nested"]["value"] = 9
        self.assertEqual(records, original)

    def test_batch_reports_the_failing_record_index(self):
        records = [
            {"kind": "created", "payload": {}},
            {"kind": "   ", "payload": {}},
        ]
        with self.assertRaisesRegex(ValueError, "record 1"):
            ingest_batch(records)


if __name__ == "__main__":
    unittest.main()
''',
                "tests/test_schema_validation.py": '''import math
import unittest

from src.core.schema import validate_event


class SchemaValidationTest(unittest.TestCase):
    def test_valid_v2_event_is_returned_unchanged(self):
        event = {"version": 2, "kind": "created", "payload": {"item": 3}}
        self.assertIs(validate_event(event), event)

    def test_contract_has_exact_top_level_fields(self):
        with self.assertRaises(ValueError):
            validate_event({
                "version": 2,
                "kind": "created",
                "payload": {},
                "legacy": True,
            })

    def test_nested_payload_keys_must_be_strings(self):
        with self.assertRaises(TypeError):
            validate_event({
                "version": 2,
                "kind": "created",
                "payload": {"nested": {1: "bad"}},
            })

    def test_payload_must_be_json_safe(self):
        with self.assertRaises(ValueError):
            validate_event({
                "version": 2,
                "kind": "created",
                "payload": {"value": math.inf},
            })


if __name__ == "__main__":
    unittest.main()
''',
                "tests/test_legacy_plugin.py": '''import copy
import unittest

from src.core.event import ingest
from src.extensions.legacy_v1 import run_v1_plugin


class LegacyPluginTest(unittest.TestCase):
    def test_installed_plugin_round_trip(self):
        event = ingest("legacy", {"value": 8})
        transformed = run_v1_plugin("fixtures.v1_enricher", event)
        self.assertEqual(transformed, {
            "version": 2,
            "kind": "legacy",
            "payload": {"value": 8, "legacy_plugin": "v1"},
        })

    def test_adapter_does_not_mutate_input(self):
        event = ingest("legacy", {"nested": {"value": 3}})
        original = copy.deepcopy(event)
        run_v1_plugin("fixtures.v1_enricher", event)
        self.assertEqual(event, original)

    def test_legacy_fields_never_escape_adapter(self):
        transformed = run_v1_plugin(
            "fixtures.v1_enricher",
            ingest("created", {"value": 1}),
        )
        self.assertEqual(set(transformed), {"version", "kind", "payload"})
        self.assertNotIn("type", transformed)
        self.assertNotIn("data", transformed)

    def test_malformed_plugin_is_rejected(self):
        with self.assertRaises(ValueError):
            run_v1_plugin("fixtures.bad_v1_plugin", ingest("bad", {}))

    def test_plugin_exception_keeps_its_original_type(self):
        with self.assertRaisesRegex(RuntimeError, "plugin failed"):
            run_v1_plugin(
                "fixtures.v1_failure",
                ingest("created", {"value": 1}),
            )

    def test_module_without_transform_is_rejected(self):
        with self.assertRaises(ValueError):
            run_v1_plugin("fixtures.audit_policy", ingest("created", {}))


if __name__ == "__main__":
    unittest.main()
''',
                "fixtures/v1_failure.py": '''def transform(event):
    raise RuntimeError("plugin failed")
''',
                "tests/test_audit_jsonl.py": '''import copy
import json
import unittest

from src.core.event import ingest
from src.extensions.audit_jsonl import export_audit_batch


class AuditJsonlTest(unittest.TestCase):
    def test_batch_is_stable_jsonl_with_sensitive_fields_removed(self):
        events = [
            ingest("created", {"item": 3, "token": "hidden"}),
            ingest("updated", {"secret": "hidden", "item": 4}),
        ]
        output = export_audit_batch("fixtures.audit_policy", events)
        rows = [json.loads(line) for line in output.splitlines()]
        self.assertEqual(rows, [
            {"kind": "created", "payload": {"item": 3}, "version": 2},
            {"kind": "updated", "payload": {"item": 4}, "version": 2},
        ])

    def test_policy_can_filter_events(self):
        events = [
            ingest("heartbeat", {"sequence": 1}),
            ingest("created", {"item": 2}),
        ]
        output = export_audit_batch("fixtures.audit_policy", events)
        self.assertEqual(len(output.splitlines()), 1)
        self.assertIn('"created"', output)

    def test_export_does_not_mutate_events(self):
        events = [ingest("created", {"password": "hidden", "item": 3})]
        original = copy.deepcopy(events)
        export_audit_batch("fixtures.audit_policy", events)
        self.assertEqual(events, original)

    def test_non_v2_event_is_rejected(self):
        with self.assertRaises(ValueError):
            export_audit_batch(
                "fixtures.audit_policy",
                [{"kind": "legacy", "payload": {}}],
            )

    def test_nested_sensitive_fields_are_removed(self):
        events = [ingest("created", {
            "item": {"token": "hidden", "name": "kept"},
            "rows": [{"password": "hidden", "value": 2}],
        })]
        row = json.loads(
            export_audit_batch("fixtures.audit_policy", events)
        )
        self.assertEqual(row["payload"], {
            "item": {"name": "kept"},
            "rows": [{"value": 2}],
        })

    def test_empty_filtered_batch_has_no_trailing_newline(self):
        output = export_audit_batch(
            "fixtures.audit_policy",
            [ingest("heartbeat", {"sequence": 1})],
        )
        self.assertEqual(output, "")

    def test_policy_module_contract_is_checked(self):
        with self.assertRaises(ValueError):
            export_audit_batch(
                "fixtures.v1_enricher",
                [ingest("created", {})],
            )


if __name__ == "__main__":
    unittest.main()
''',
            },
        ),
    )


_BASELINE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["completed", "blocked"]},
        "summary": {"type": "string"},
        "tests_run": {"type": "array", "items": {"type": "string"}},
        "known_limitations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "summary", "tests_run", "known_limitations"],
    "additionalProperties": False,
}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _git(workspace: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=workspace,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return completed.stdout.strip()


def _write_repository(workspace: Path, case: CalibrationCase) -> str:
    workspace.mkdir(parents=True)
    for relative, content in case.files.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (workspace / ".gitignore").write_text(
        "__pycache__/\n*.py[cod]\n.hermes-*\n", encoding="utf-8"
    )
    (workspace / "README.md").write_text(
        f"# {case.title}\n\n{case.objective}\n", encoding="utf-8"
    )
    _git(workspace, "init", "--quiet")
    _git(workspace, "config", "user.email", "runtime@example.invalid")
    _git(workspace, "config", "user.name", "Runtime Calibration")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "--quiet", "-m", f"freeze {case.key}")
    return _git(workspace, "rev-parse", "HEAD")


def _clone_repository(source: Path, target: Path) -> None:
    subprocess.run(
        ["git", "clone", "--quiet", str(source), str(target)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _git(target, "config", "user.email", "runtime@example.invalid")
    _git(target, "config", "user.name", "Runtime Calibration")


def _oracle(workspace: Path) -> dict[str, Any]:
    started = time.monotonic()
    completed = subprocess.run(
        ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=workspace,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=120,
    )
    match = re.search(r"Ran (\d+) tests?", completed.stdout)
    return {
        "passed": completed.returncode == 0,
        "returncode": completed.returncode,
        "test_count": int(match.group(1)) if match else None,
        "wall_time_seconds": round(time.monotonic() - started, 3),
        "output": completed.stdout[-12000:],
    }


def _baseline_prompt(case: CalibrationCase) -> str:
    return f"""你是这个仓库唯一的 coherent implementation worker。完整承担结果责任：理解现有行为、
选择方案、修改实现、补充必要测试、运行测试并调试到最强终态。不要委派 subagent，不要只完成一个步骤，
也不要在修改后未经验证就结束。不得读取 workspace 之外的 Hermes 源码、其他 arm 或历史运行产物。

目标：
{case.objective}

最终只返回约束 JSON；status 只能在完整工作和验证结束后设为 completed，真实阻塞时设为 blocked。
"""


def _prepare_home(
    source_home: Path,
    target_home: Path,
    *,
    model: str,
    base_url: str,
) -> dict[str, Any]:
    return p4g8.prepare_isolated_codex_home(
        source_home,
        target_home,
        proxy_base_url=base_url,
        model=model,
        worker_uid=os.getuid(),
        worker_gid=os.getgid(),
        reasoning_effort_override="max",
        multi_agent_enabled=False,
    )


def _isolated_config_summary(audit: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": audit.get("model"),
        "reasoning_effort": audit.get("reasoning_effort"),
        "multi_agent_enabled": audit.get("multi_agent_enabled"),
        "context_window_tokens": audit.get("context_window_tokens"),
        "auto_compact_token_limit": audit.get("auto_compact_token_limit"),
        "provider_transport": audit.get("provider_transport"),
        "approval": audit.get("approval"),
        "copied_session_history": audit.get("copied_session_history"),
    }


def _run_baseline(
    case: CalibrationCase,
    workspace: Path,
    codex_home: Path,
    *,
    model: str,
    timeout_seconds: int,
    reports: Path,
) -> dict[str, Any]:
    notifications: list[dict[str, Any]] = []
    started = time.monotonic()
    result = run_app_server_turn(
        prompt=_baseline_prompt(case),
        workspace=str(workspace),
        model=model,
        sandbox="workspace-write",
        approval="never",
        output_schema=_BASELINE_OUTPUT_SCHEMA,
        resume_thread_id=None,
        codex_bin=shutil.which("codex") or "codex",
        codex_home=str(codex_home),
        env={"CODEX_HOME": str(codex_home), "HOME": str(codex_home.parent)},
        timeout_seconds=float(timeout_seconds),
        poll_interval=0.2,
        on_notification=notifications.append,
    )
    try:
        receipt = json.loads(result.final_text or "{}")
    except json.JSONDecodeError:
        receipt = {"status": "invalid_output", "raw_tail": (result.final_text or "")[-2000:]}
    oracle = _oracle(workspace)
    _write_json(reports / "baseline-notifications.json", notifications)
    return {
        "transport_status": result.status,
        "transport_error": result.error,
        "thread_id": result.thread_id,
        "turn_id": result.turn_id,
        "wall_time_seconds": round(time.monotonic() - started, 3),
        "receipt": receipt,
        "oracle": oracle,
        "changed_files": _changed_files(workspace),
    }


def _changed_files(workspace: Path) -> list[str]:
    output = _git(workspace, "status", "--short", "--untracked-files=all")
    return sorted(line[3:].strip() for line in output.splitlines() if len(line) >= 4)


def _create_runtime_job(
    case: CalibrationCase,
    workspace: Path,
    case_root: Path,
) -> str:
    with kb.connect() as conn:
        root_task = kb.create_task(
            conn,
            title=f"Phase 4G16 {case.title}",
            initial_status="running",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        return rk.create_runtime_job(
            conn,
            root_task,
            case.objective,
            workspace_path=str(workspace),
            goal_items=[
                {
                    "item_key": case.goal_item_key,
                    "description": case.goal_description,
                    "required": True,
                    "verifier_required": False,
                    "acceptance_criteria": [
                        case.goal_description,
                        "python3 -m unittest discover -s tests -v passes",
                    ],
                }
            ],
            initial_assignee=LANE_NAME,
            initialization_mode="provider_first",
            orchestration_policy={
                "schema": rk.RUNTIME_ORCHESTRATION_POLICY_SCHEMA,
                "mode": "closed_loop_coordination",
                "worker_lane": LANE_NAME,
                "max_child_nodes": 3,
                "artifact_root": str(case_root / "runtime-contributions"),
                "retention": "retain",
            },
        )


def _runtime_evidence(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    orchestration = rk.summarize_runtime_orchestration(conn, job_id)
    nodes = [
        {
            "node_key": row["node_key"],
            "node_type": row["node_type"],
            "state": row["state"],
            "assignee": row["assignee"],
        }
        for row in conn.execute(
            "SELECT * FROM execution_nodes WHERE job_id = ? ORDER BY created_at, node_key",
            (job_id,),
        ).fetchall()
    ]
    actions = (orchestration.get("coordination") or {}).get("actions") or []
    candidate_count = int(
        (orchestration.get("coordination") or {}).get("cost", {}).get(
            "terminal_candidate_count", 0
        )
        or 0
    ) + sum(len(item.get("candidate_refs") or []) for item in actions)
    return {
        "status": rk.status_runtime_job(conn, job_id),
        "consistency": rk.check_runtime_consistency(conn, job_id, write_events=False),
        "orchestration": orchestration,
        "nodes": nodes,
        "candidate_count": candidate_count,
    }


def _run_treatment(
    case: CalibrationCase,
    workspace: Path,
    case_root: Path,
    *,
    provider_source: dict[str, Any],
    worker_timeout_seconds: int,
    decision_timeout_seconds: int,
) -> tuple[str, dict[str, Any]]:
    job_id = _create_runtime_job(case, workspace, case_root)
    conn = kb.connect()
    try:
        smoke = run_real_worker_lane_smoke(
            conn,
            job_id,
            provider_source=provider_source,
            lane_name=LANE_NAME,
            max_decision_ticks=5,
            max_steps=180,
            # A structure assessment is a complete model turn. The reducer can
            # only consume its checkpoint after the worker has persisted the
            # terminal receipt, so use the configured attempt budget here.
            worker_wait_seconds=float(worker_timeout_seconds),
            poll_interval_seconds=0.2,
            timeout_seconds=float(decision_timeout_seconds),
            max_retries=1,
        )
    finally:
        conn.close()
    # The real worker is a process boundary and may replace WAL sidecars.
    # Evidence collection must not reuse the pre-worker connection.
    with kb.connect() as conn:
        evidence = _runtime_evidence(conn, job_id)
    return job_id, {
        "smoke": smoke,
        "oracle": _oracle(workspace),
        "changed_files": _changed_files(workspace),
        **evidence,
    }


def _coordination_observations(
    case: CalibrationCase,
    baseline: dict[str, Any],
    treatment: dict[str, Any],
) -> dict[str, Any]:
    orchestration = treatment.get("orchestration") or {}
    coordination = orchestration.get("coordination") or {}
    actions = coordination.get("actions") or []
    missed: list[str] = []
    calibration_gaps: list[str] = []
    overhead: list[str] = []
    if case.kind == "durable_boundary_medium" and int(treatment.get("candidate_count") or 0) == 0:
        structure_checkpoint = orchestration.get("structure_checkpoint") or {}
        event_id = structure_checkpoint.get("event_id")
        if event_id is not None:
            calibration_gaps.append(f"execution_event:{event_id}")
        calibration_gaps.append(f"report:{case.key}:candidate-not-observed")
    if (
        case.kind == "coherent_negative_control"
        and actions
        and baseline["oracle"]["passed"]
        and treatment["oracle"]["passed"]
    ):
        overhead.append(f"report:{case.key}:unnecessary-action-cost")
    return {
        "missed_coordination_evidence_refs": missed,
        "calibration_fixture_gap_evidence_refs": calibration_gaps,
        "coordination_overhead_evidence_refs": overhead,
    }


def _acceptance(
    case: CalibrationCase,
    baseline: dict[str, Any],
    treatment: dict[str, Any],
) -> dict[str, bool]:
    orchestration = treatment.get("orchestration") or {}
    coordination = orchestration.get("coordination") or {}
    actions = coordination.get("actions") or []
    checkpoint_count = len(coordination.get("checkpoints") or [])
    candidate_count = int(treatment.get("candidate_count") or 0)
    consistency = treatment.get("consistency") or {}
    base = {
        "baseline_quality_passed": baseline["oracle"]["passed"] is True,
        "treatment_quality_passed": treatment["oracle"]["passed"] is True,
        "quality_non_regression": (
            int(treatment["oracle"]["passed"]) >= int(baseline["oracle"]["passed"])
        ),
        "runtime_consistency_passed": consistency.get("status") == "passed",
        "natural_prompt_integrity": True,
    }
    if case.kind == "coherent_negative_control":
        base["no_false_coordination"] = not actions
    elif case.kind == "shared_contract_medium":
        base["checkpoint_or_coherent_route"] = checkpoint_count > 0 or not actions
    elif case.kind == "durable_boundary_medium":
        terminal_candidates = coordination.get(
            "terminal_responsibility_candidates"
        ) or {}
        base["natural_candidate_observed"] = candidate_count > 0
        base["candidate_consumed_by_provider"] = any(
            item.get("route") == "provider_required"
            and item.get("status") == "applied"
            for item in actions
        ) or int(terminal_candidates.get("expanded_count") or 0) + int(
            terminal_candidates.get("resolved_without_expansion_count") or 0
        ) > 0
    return base


def _render_trace(report: dict[str, Any]) -> str:
    case = report["case"]
    baseline = report["baseline"]
    treatment = report["treatment"]
    orchestration = treatment.get("orchestration") or {}
    coordination = orchestration.get("coordination") or {}
    lines = [
        f"# Phase 4G16 {case['title']} 过程报告",
        "",
        "## 任务",
        "",
        case["objective"],
        "",
        "## 对照结果",
        "",
        f"- coherent baseline：{'通过' if baseline['oracle']['passed'] else '失败'}；"
        f"耗时 {baseline['wall_time_seconds']} 秒；修改 {len(baseline['changed_files'])} 个文件。",
        f"- Runtime treatment：{'通过' if treatment['oracle']['passed'] else '失败'}；"
        f"job 状态 `{treatment['status']['job']['state']}`；"
        f"materialization {treatment['smoke']['materialization_attempt_count']} 次。",
        f"- Runtime node：{', '.join(item['node_key'] for item in treatment['nodes']) or '无'}。",
        "",
        "## Orchestra 过程",
        "",
        f"- structure assessment：{(coordination.get('cost') or {}).get('structure_assessment_count', 0)} 次。",
        f"- coordination checkpoint：{len(coordination.get('checkpoints') or [])} 次。",
        f"- coordination action：{len(coordination.get('actions') or [])} 次。",
        f"- natural candidate：{treatment.get('candidate_count', 0)} 个。",
        f"- Decision Provider：{treatment['smoke']['decision_tick_count']} 次；"
        f"accepted {treatment['smoke']['accepted_patch_count']}，"
        f"rejected {treatment['smoke']['rejected_patch_count']}。",
        "",
    ]
    for action in coordination.get("actions") or []:
        lines.append(
            f"- action `{action['id']}`：`{action['classification']}` -> "
            f"`{action['route']}` -> `{action['status']}`；targets="
            f"{', '.join(action.get('affected_node_keys') or []) or '-'}。"
        )
    lines.extend(
        [
            "",
            "## 验收",
            "",
            *[
                f"- {'通过' if passed else '失败'}：`{key}`"
                for key, passed in report["acceptance"].items()
            ],
            "",
            "## 结论",
            "",
            report["conclusion"],
            "",
        ]
    )
    return "\n".join(lines)


def _case_conclusion(
    *,
    passed: bool,
    acceptance: dict[str, bool],
    finding_categories: list[str],
) -> str:
    if passed:
        return "该 paired case 满足冻结验收，Runtime 质量未低于 coherent baseline。"
    quality_keys = (
        "baseline_quality_passed",
        "treatment_quality_passed",
        "quality_non_regression",
        "runtime_consistency_passed",
    )
    if (
        "calibration_fixture_gap" in finding_categories
        and all(acceptance.get(key) is True for key in quality_keys)
    ):
        return (
            "该 paired case 的 baseline、Runtime treatment、质量非回退和 Runtime consistency "
            "均通过；唯一未满足项是仓库没有自然暴露 durable responsibility candidate。"
            "该结果归类为校准夹具不足，不是 Runtime correctness 或任务质量失败。"
        )
    return "该 paired case 暴露了未满足的自然协调或质量条件，结论已进入 learning bundle。"


def _cleanup_archived_case_source(
    case_root: Path,
    *,
    manifest_path: Path,
) -> dict[str, Any]:
    rebuildable = validation_artifacts.cleanup_rebuildable_entries(
        case_root,
        manifest_path=manifest_path,
        entries=("workspace", "home", "codex-home-seed"),
        orchestration_learning_required=True,
    )
    archived = validation_artifacts.verify_artifact_manifest(manifest_path)
    raw_entries = sorted(
        set(str(value) for value in archived.get("selected_entries") or [])
        - {"reports"}
    )
    raw = validation_artifacts.cleanup_archived_source_entries(
        case_root,
        manifest_path=manifest_path,
        entries=raw_entries,
        orchestration_learning_required=True,
    )
    return {
        "status": "cleaned_after_verified_archive",
        "manifest_path": str(manifest_path.expanduser().resolve()),
        "rebuildable": rebuildable,
        "archived_source": raw,
        "retained_entries": ["reports"],
        "bytes_removed": int(rebuildable["bytes_removed"])
        + int(raw["bytes_removed"]),
    }


def run_case(config: CalibrationConfig, case: CalibrationCase) -> dict[str, Any]:
    case_root = (config.root / case.key).resolve()
    if case_root.exists() and any(case_root.iterdir()):
        raise ValueError(f"case root must be empty: {case_root}")
    case_root.mkdir(parents=True, exist_ok=True)
    validation_artifacts.declare_managed_orchestration_validation(
        case_root, phase=PHASE, instance_id=case.key
    )
    reports = case_root / "reports"
    reports.mkdir()
    workspace_root = case_root / "workspace"
    base_workspace = workspace_root / "base"
    baseline_workspace = workspace_root / "baseline"
    treatment_workspace = workspace_root / "treatment"
    base_revision = _write_repository(base_workspace, case)
    _clone_repository(base_workspace, baseline_workspace)
    _clone_repository(base_workspace, treatment_workspace)
    base_oracle = _oracle(base_workspace)
    if base_oracle["passed"]:
        raise RuntimeError(f"frozen case must fail before implementation: {case.key}")

    source = p4g8.load_codex_model_source(
        config.source_codex_home, model=config.model
    )
    model = str(source["model"])
    baseline_home = case_root / "codex-home"
    treatment_seed = case_root / "codex-home-seed"
    baseline_config = _prepare_home(
        config.source_codex_home,
        baseline_home,
        model=model,
        base_url=str(source["explicit_base_url"]),
    )
    treatment_config = _prepare_home(
        config.source_codex_home,
        treatment_seed,
        model=model,
        base_url=str(source["explicit_base_url"]),
    )
    (case_root / "codex-homes").mkdir()
    baseline = _run_baseline(
        case,
        baseline_workspace,
        baseline_home,
        model=model,
        timeout_seconds=config.worker_timeout_seconds,
        reports=reports,
    )

    hermes_home = case_root / "hermes-home"
    home = case_root / "home"
    home.mkdir()
    prior_env = {
        key: os.environ.get(key)
        for key in (
            "HERMES_HOME",
            "HOME",
            "HERMES_RUNTIME_CONTRIBUTION_ROOT",
        )
    }
    os.environ["HERMES_HOME"] = str(hermes_home)
    os.environ["HOME"] = str(home)
    os.environ["HERMES_RUNTIME_CONTRIBUTION_ROOT"] = str(
        case_root / "runtime-contributions"
    )
    clear_worker_lanes()
    register_worker_lane(
        codex_worker.make_codex_worker_lane(
            {
                "name": LANE_NAME,
                "transport": "codex_app_server",
                "model": model,
                "sandbox": "workspace-write",
                "approval": "never",
                "max_concurrency": 3,
                "success_policy": "auto_complete",
                "timeout_seconds": config.worker_timeout_seconds,
                "json_events": True,
                "isolated_codex_home_seed": str(treatment_seed),
                "isolated_codex_home_root": str(case_root / "codex-homes"),
            },
            source="phase4g16",
        )
    )
    try:
        kb.init_db()
        started = time.monotonic()
        job_id, treatment = _run_treatment(
            case,
            treatment_workspace,
            case_root,
            provider_source=source,
            worker_timeout_seconds=config.worker_timeout_seconds,
            decision_timeout_seconds=config.decision_timeout_seconds,
        )
        treatment["wall_time_seconds"] = round(time.monotonic() - started, 3)
        acceptance = _acceptance(case, baseline, treatment)
        observations = _coordination_observations(case, baseline, treatment)
        quality = {
            "status": "passed" if treatment["oracle"]["passed"] else "failed",
            "case_kind": case.kind,
            "baseline_final_passed": baseline["oracle"]["passed"],
            "treatment_final_passed": treatment["oracle"]["passed"],
            "quality_non_regression": acceptance["quality_non_regression"],
            "coordination_observations": observations,
        }
        with kb.connect() as conn:
            learning_result = learning.finalize_learning_bundle(
                conn,
                job_id,
                run_root=case_root,
                registry_path=(
                    config.artifact_root
                    / "orchestration-learning"
                    / "registry.sqlite3"
                ),
                phase=PHASE,
                instance_id=case.key,
                run_id=config.root.expanduser().resolve().name,
                source_db_ref="hermes-home/kanban.db",
                quality=quality,
                baseline_bundle_ref=f"reports/{case.key}-baseline",
            )
        passed = all(acceptance.values())
        finding_categories = [
            item["category"] for item in learning_result["bundle"]["findings"]
        ]
        report = {
            "schema": CASE_REPORT_SCHEMA,
            "case": {
                "key": case.key,
                "title": case.title,
                "kind": case.kind,
                "objective": case.objective,
                "base_revision": base_revision,
            },
            "model": model,
            "source_summary": source["summary"],
            "isolated_config": {
                "baseline": _isolated_config_summary(baseline_config),
                "treatment": _isolated_config_summary(treatment_config),
            },
            "base_oracle": base_oracle,
            "baseline": baseline,
            "treatment": treatment,
            "acceptance": acceptance,
            "learning": {
                "status": learning_result["receipt"]["status"],
                "bundle_sha256": learning_result["receipt"]["bundle_sha256"],
                "finding_categories": finding_categories,
            },
            "status": "passed" if passed else "failed",
            "conclusion": _case_conclusion(
                passed=passed,
                acceptance=acceptance,
                finding_categories=finding_categories,
            ),
            "generated_at": int(time.time()),
        }
        _write_json(reports / "case-report.json", report)
        (reports / "capability-trace.md").write_text(
            _render_trace(report), encoding="utf-8"
        )
        manifest = validation_artifacts.archive_validation_run(
            case_root,
            artifact_root=config.artifact_root,
            phase=PHASE,
            instance_id=_archive_instance_id(config, case),
            redactions=validation_artifacts.model_source_redactions(
                config.source_codex_home
            ),
            expected_entries=(
                "codex-home",
                "codex-homes",
                "hermes-home",
                "reports",
                "runtime-state",
            ),
            orchestration_learning_required=True,
        )
        report["artifact_archive"] = {
            "status": manifest["status"],
            "artifact_path": manifest["artifact_path"],
            "manifest_path": str(Path(manifest["artifact_path"]) / "manifest.json"),
        }
        if config.cleanup_source:
            report["cleanup"] = _cleanup_archived_case_source(
                case_root,
                manifest_path=Path(manifest["artifact_path"]) / "manifest.json",
            )
        _write_json(reports / "case-report.json", report)
        return report
    finally:
        clear_worker_lanes()
        for key, value in prior_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _archive_infrastructure_invalid_case(
    config: CalibrationConfig,
    case: CalibrationCase,
    exc: BaseException,
) -> dict[str, Any]:
    case_root = (config.root / case.key).resolve()
    reports = case_root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    db_path = case_root / "hermes-home" / "kanban.db"
    error = rk.redact_sensitive_text(str(exc)).strip()[:2000]
    stack = rk.redact_sensitive_text(traceback.format_exc()).strip()[-12000:]
    job_id = None
    learning_result = None
    learning_error = None
    if db_path.is_file():
        try:
            with kb.connect(db_path=db_path) as conn:
                row = conn.execute(
                    "SELECT id FROM runtime_jobs ORDER BY created_at DESC, rowid DESC LIMIT 1"
                ).fetchone()
                if row is not None:
                    job_id = str(row["id"])
                    learning_result = learning.finalize_learning_bundle(
                        conn,
                        job_id,
                        run_root=case_root,
                        registry_path=(
                            config.artifact_root
                            / "orchestration-learning"
                            / "registry.sqlite3"
                        ),
                        phase=PHASE,
                        instance_id=case.key,
                        run_id=config.root.name,
                        source_db_ref="hermes-home/kanban.db",
                        quality={
                            "status": "infrastructure_invalid",
                            "case_kind": case.kind,
                            "runtime_error": error,
                            "coordination_observations": {
                                "missed_coordination_evidence_refs": [],
                                "coordination_overhead_evidence_refs": [],
                            },
                        },
                        baseline_bundle_ref=f"reports/{case.key}-baseline",
                    )
        except sqlite3.DatabaseError as db_exc:
            learning_error = rk.redact_sensitive_text(str(db_exc)).strip()[:2000]
    report = {
        "schema": CASE_REPORT_SCHEMA,
        "case": {
            "key": case.key,
            "title": case.title,
            "kind": case.kind,
            "objective": case.objective,
        },
        "status": "infrastructure_invalid",
        "runtime_job_id": job_id,
        "exception_type": type(exc).__name__,
        "error": error,
        "traceback": stack,
        "learning": (
            {
                "status": learning_result["receipt"]["status"],
                "bundle_sha256": learning_result["receipt"]["bundle_sha256"],
            }
            if learning_result is not None
            else {
                "status": (
                    "absorption_blocked_db_corruption"
                    if learning_error
                    else "not_available_before_runtime_job"
                ),
                **({"error": learning_error} if learning_error else {}),
            }
        ),
        "conclusion": (
            "该 case 因校准基础设施失效而中止，不计入 Runtime 能力结论；"
            "已保留此前产生的 worker、Decision Provider 与 DB 事实。"
        ),
        "generated_at": int(time.time()),
    }
    _write_json(reports / "infrastructure-invalid.json", report)
    (reports / "capability-trace.md").write_text(
        "\n".join(
            [
                f"# Phase 4G16 {case.title} 基础设施失效",
                "",
                f"- 状态：`infrastructure_invalid`",
                f"- Runtime job：`{job_id or '未创建'}`",
                f"- 异常：`{type(exc).__name__}`：{error}",
                (
                    "- learning absorption：DB 损坏阻断；source 已保留，禁止清理。"
                    if learning_error
                    else "- 本 run 不计入能力结论，已保留并吸收此前产生的权威事实。"
                ),
                "",
            ]
        ),
        encoding="utf-8",
    )
    if learning_result is None:
        return report
    archive_instance = (
        f"{case.key}-infrastructure-invalid-{config.root.name}"
    )
    expected_entries = tuple(
        name
        for name in (
            "codex-home",
            "codex-homes",
            "hermes-home",
            "reports",
            "runtime-state",
        )
        if (case_root / name).exists()
    )
    manifest = validation_artifacts.archive_validation_run(
        case_root,
        artifact_root=config.artifact_root,
        phase=PHASE,
        instance_id=archive_instance,
        redactions=validation_artifacts.model_source_redactions(
            config.source_codex_home
        ),
        expected_entries=expected_entries,
        orchestration_learning_required=True,
    )
    report["artifact_archive"] = {
        "status": manifest["status"],
        "artifact_path": manifest["artifact_path"],
        "manifest_path": str(Path(manifest["artifact_path"]) / "manifest.json"),
    }
    if config.cleanup_source:
        report["cleanup"] = _cleanup_archived_case_source(
            case_root,
            manifest_path=Path(manifest["artifact_path"]) / "manifest.json",
        )
    _write_json(reports / "infrastructure-invalid.json", report)
    return report


def run_campaign(
    config: CalibrationConfig,
    *,
    case_key: Optional[str] = None,
) -> dict[str, Any]:
    root = config.root.expanduser().resolve()
    available_cases = _cases()
    selected_cases = tuple(
        case for case in available_cases if case_key is None or case.key == case_key
    )
    if not selected_cases:
        raise ValueError(f"unknown calibration case: {case_key}")
    if root.exists() and any(root.iterdir()):
        raise ValueError(f"campaign root must be empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    case_reports = []
    for case in selected_cases:
        try:
            case_reports.append(run_case(config, case))
        except Exception as exc:
            case_reports.append(
                _archive_infrastructure_invalid_case(config, case, exc)
            )
            break
    report = {
        "schema": CAMPAIGN_SCHEMA,
        "status": (
            "passed"
            if len(case_reports) == len(selected_cases)
            and all(item["status"] == "passed" for item in case_reports)
            else "failed"
        ),
        "selected_cases": [case.key for case in selected_cases],
        "cases": [
            {
                "key": item["case"]["key"],
                "status": item["status"],
                "acceptance": item.get("acceptance"),
                "artifact_archive": item.get("artifact_archive"),
                "learning": item.get("learning"),
                "cleanup": item.get("cleanup"),
            }
            for item in case_reports
        ],
        "generated_at": int(time.time()),
    }
    _write_json(root / "campaign-report.json", report)
    return report


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="运行 Phase 4G16 自然编排校准")
    parser.add_argument("--root", required=True)
    parser.add_argument(
        "--artifact-root",
        default=str(validation_artifacts.default_artifact_root()),
    )
    parser.add_argument(
        "--source-codex-home",
        default=str(Path.home() / ".codex"),
    )
    parser.add_argument("--model")
    parser.add_argument("--worker-timeout-seconds", type=int, default=600)
    parser.add_argument("--decision-timeout-seconds", type=int, default=180)
    parser.add_argument(
        "--case",
        choices=[case.key for case in _cases()],
        help="只运行一个冻结 case；缺省时按顺序运行全部 case",
    )
    parser.add_argument("--keep-source", action="store_true")
    args = parser.parse_args(argv)
    report = run_campaign(
        CalibrationConfig(
            root=Path(args.root),
            artifact_root=Path(args.artifact_root),
            source_codex_home=Path(args.source_codex_home),
            model=args.model,
            worker_timeout_seconds=args.worker_timeout_seconds,
            decision_timeout_seconds=args.decision_timeout_seconds,
            cleanup_source=not args.keep_source,
        ),
        case_key=args.case,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
