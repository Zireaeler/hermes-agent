"""Standalone native Codex orchestra baseline for Phase 4G9 Arm 1."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
import tomllib
from datetime import datetime
from typing import Any, Optional
import uuid

from agent.redact import redact_sensitive_text
from hermes_cli import kanban_runtime_phase4g8 as p4g8
from hermes_cli import kanban_runtime_phase4g8_run as p4g8_run
from hermes_cli import phase4g8_swe_evo as swe_evo


ARM1_REPORT_SCHEMA = "hermes_phase4g9_native_arm1_v1"
ARM1_EVENT_SCHEMA = "hermes_phase4g9_native_events_v1"
ARM1_PROTOCOL_VERSION = "phase4g9-native-arm1-v1"
FROZEN_INSTANCE_ID = "iterative__dvc_1.0.0a1_1.0.0a2"
FROZEN_BASE_COMMIT = "fc42ca721c25bdd24875c999e37fb4f589ecd63c"
FROZEN_MODEL = "gpt-5.6-sol"
FROZEN_REASONING_EFFORT = "ultra"
FROZEN_CONTEXT_WINDOW = 353_400
FROZEN_AUTO_COMPACT_LIMIT = 230_000
FROZEN_MAX_THREADS = 4
WORKER_UID = 65534
WORKER_GID = 65534


def prepare_native_codex_home(
    source_home: Path,
    target_home: Path,
    *,
    proxy_base_url: str,
    model: str = FROZEN_MODEL,
    worker_uid: int = WORKER_UID,
    worker_gid: int = WORKER_GID,
) -> dict[str, Any]:
    """Create a history-free Codex home with native proactive multi-agent enabled."""

    source_home = source_home.expanduser().resolve()
    source_config = source_home / "config.toml"
    source_auth = source_home / "auth.json"
    if not source_config.is_file() or not source_auth.is_file():
        raise ValueError("source CODEX_HOME requires config.toml and auth.json")
    source_hashes = {
        "config.toml": _sha256_file(source_config),
        "auth.json": _sha256_file(source_auth),
    }
    config = tomllib.loads(source_config.read_text(encoding="utf-8"))
    auth = json.loads(source_auth.read_text(encoding="utf-8"))
    provider_name = str(config.get("model_provider") or "").strip()
    providers = config.get("model_providers")
    provider = providers.get(provider_name) if isinstance(providers, dict) else None
    api_key = str(auth.get("OPENAI_API_KEY") or "").strip()
    if not isinstance(provider, dict) or not str(provider.get("base_url") or "").strip():
        raise ValueError("source Codex provider base_url is missing")
    if not api_key:
        raise ValueError("source Codex OPENAI_API_KEY is missing")

    target_home.mkdir(parents=True, exist_ok=False)
    provider_alias = "phase4g9_proxy"
    transport = p4g8._isolated_provider_transport_settings(provider)
    lines = [
        f"model = {json.dumps(model)}",
        f"model_reasoning_effort = {json.dumps(FROZEN_REASONING_EFFORT)}",
        f"model_context_window = {FROZEN_CONTEXT_WINDOW}",
        f"model_auto_compact_token_limit = {FROZEN_AUTO_COMPACT_LIMIT}",
        f"approval_policy = {json.dumps(p4g8.PHASE4G8_CODEX_APPROVAL_POLICY)}",
        f"approvals_reviewer = {json.dumps(p4g8.PHASE4G8_CODEX_APPROVAL_REVIEWER)}",
        f"model_provider = {json.dumps(provider_alias)}",
        "",
        f"[model_providers.{provider_alias}]",
        'name = "Phase 4G9 native orchestra proxy"',
        f"base_url = {json.dumps(proxy_base_url.rstrip('/'))}",
        f"wire_api = {json.dumps(str(provider.get('wire_api') or 'responses'))}",
        "requires_openai_auth = true",
    ]
    lines.extend(f"{key} = {json.dumps(value)}" for key, value in transport.items())
    lines.extend([
        "",
        "[auto_review]",
        f"policy = {json.dumps(p4g8.PHASE4G8_CODEX_AUTO_REVIEW_POLICY.strip())}",
        "",
        "[features]",
        "guardian_approval = true",
        "multi_agent_v2 = true",
        "",
    ])
    target_config = target_home / "config.toml"
    target_auth = target_home / "auth.json"
    rules = target_home / "rules"
    target_rule = rules / "default.rules"
    target_config.write_text("\n".join(lines), encoding="utf-8")
    target_auth.write_text(json.dumps({"OPENAI_API_KEY": api_key}) + "\n", encoding="utf-8")
    rules.mkdir(mode=0o755)
    target_rule.write_text(p4g8.PHASE4G8_CODEX_EXEC_POLICY.strip() + "\n", encoding="utf-8")
    for path, mode in ((target_home, 0o700), (target_config, 0o600), (target_auth, 0o600)):
        os.chmod(path, mode)
        os.chown(path, int(worker_uid), int(worker_gid))
    for path, mode in ((rules, 0o555), (target_rule, 0o444)):
        os.chmod(path, mode)
        os.chown(path, int(worker_uid), int(worker_gid))

    parsed = tomllib.loads(target_config.read_text(encoding="utf-8"))
    if (
        parsed.get("model_reasoning_effort") != FROZEN_REASONING_EFFORT
        or parsed["features"].get("multi_agent_v2") is not True
    ):
        raise RuntimeError("Phase 4G9 native Codex configuration preflight failed")
    preflight_env = os.environ.copy()
    preflight_env["CODEX_HOME"] = str(target_home)
    preflight = subprocess.run(
        [shutil.which("codex") or "codex", "features", "list"],
        env=preflight_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    if preflight.returncode != 0 or not any(
        line.split()[:1] == ["multi_agent_v2"] and line.split()[-1:] == ["true"]
        for line in preflight.stdout.splitlines()
    ):
        raise RuntimeError("installed Codex rejected the Phase 4G9 native multi-agent config")
    return {
        "protocol_version": ARM1_PROTOCOL_VERSION,
        "source_hashes": source_hashes,
        "isolated_hashes": {
            "config.toml": _sha256_file(target_config),
            "auth.json": _sha256_file(target_auth),
            "rules/default.rules": _sha256_file(target_rule),
        },
        "source_provider": provider_name,
        "isolated_provider": provider_alias,
        "model": model,
        "reasoning_effort": FROZEN_REASONING_EFFORT,
        "wire_reasoning_effort": "max",
        "multi_agent_mode": "proactive",
        "max_threads_including_parent": FROZEN_MAX_THREADS,
        "max_threads_source": "codex-0.144.4-multi-agent-v2-default",
        "provider_transport": transport,
        "copied_session_history": False,
        "copied_memory_or_plugins": False,
    }


def build_parent_prompt(locked_task: dict[str, Any], *, toolchain: Path) -> str:
    srs = str(locked_task.get("srs") or "").strip()
    if not srs:
        raise ValueError("locked task SRS is missing")
    return f"""You own the complete implementation outcome for this software evolution task.

Work continuously from repository inspection through implementation, integration, testing,
debugging, and final verification. You are the parent integrator. Proactively use native Codex
subagents when parallel or independently focused work would materially improve final quality or
speed. Decide their count, roles, scopes, communication, and timing yourself. Keep integration and
the terminal result under your ownership. Do not stop after analysis or after only changing code.

The workspace is a clean detached checkout of the exact benchmark base revision. A dependency
toolchain matching the official image is available at {toolchain}. Use project-visible tests and
write additional tests when useful. Continue until you have the strongest terminal candidate you
can produce or a real blocker prevents further work.

Integrity constraints:
- Do not seek, fetch, infer from, or inspect the target release implementation, gold patch,
  protected tests, evaluator sources, historical benchmark candidates, or prior run transcripts.
- Do not contact external networks from shell tools.
- No official evaluator result will be provided during execution.
- Do not weaken the execution isolation or modify files outside this workspace.
- Your final response must summarize the approach, subagent integration, changed areas, tests run,
  known limitations, and any blocker. Do not claim official benchmark success.

Software Requirements Specification:

{srs}
"""


def summarize_exec_events(lines: list[str]) -> dict[str, Any]:
    """Extract bounded orchestration and usage evidence from `codex exec --json`."""

    events: list[dict[str, Any]] = []
    thread_id: Optional[str] = None
    usage = {key: 0 for key in (
        "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"
    )}
    turns_completed = 0
    collab_calls: list[dict[str, Any]] = []
    commands: list[dict[str, Any]] = []
    final_message = ""
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "")
        if event_type == "thread.started":
            thread_id = str(event.get("thread_id") or "") or thread_id
        elif event_type == "turn.completed":
            turns_completed += 1
            raw_usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            for key in usage:
                usage[key] = max(usage[key], int(raw_usage.get(key) or 0))
        item = event.get("item") if isinstance(event.get("item"), dict) else None
        if item:
            item_type = str(item.get("type") or "")
            if item_type == "collab_tool_call" and event_type in {"item.completed", "item.updated"}:
                collab_calls.append({
                    "event_type": event_type,
                    "tool": item.get("tool"),
                    "sender_thread_id": item.get("sender_thread_id"),
                    "receiver_thread_ids": list(item.get("receiver_thread_ids") or []),
                    "prompt": redact_sensitive_text(str(item.get("prompt") or ""))[:4000],
                    "agents_states": item.get("agents_states") or {},
                    "status": item.get("status"),
                })
            elif item_type == "command_execution" and event_type == "item.completed":
                commands.append({
                    "command": redact_sensitive_text(str(item.get("command") or ""))[:2000],
                    "exit_code": item.get("exit_code"),
                    "status": item.get("status"),
                })
            elif item_type == "agent_message" and event_type == "item.completed":
                final_message = redact_sensitive_text(str(item.get("text") or ""))
        events.append({"type": event_type, "item_type": item.get("type") if item else None})
    child_ids = sorted({
        str(child)
        for call in collab_calls
        for child in call.get("receiver_thread_ids") or []
        if child and str(child) != thread_id
    })
    cache_hit_ratio = (
        round(usage["cached_input_tokens"] / usage["input_tokens"], 6)
        if usage["input_tokens"] > 0 else None
    )
    return {
        "schema": ARM1_EVENT_SCHEMA,
        "parent_thread_id": thread_id,
        "child_thread_ids": child_ids,
        "subagent_count": len(child_ids),
        "turns_completed": turns_completed,
        "usage": usage,
        "cache_hit_ratio": cache_hit_ratio,
        "collaboration_calls": collab_calls,
        "command_count": len(commands),
        "commands": commands,
        "terminal_message": final_message,
        "event_count": len(events),
        "event_type_counts": _counts(event["type"] for event in events),
    }


def summarize_rollout_sessions(codex_home: Path, *, parent_thread_id: Optional[str]) -> dict[str, Any]:
    """Summarize per-thread timing and final token totals without retaining model reasoning."""

    sessions: list[dict[str, Any]] = []
    for path in sorted((codex_home / "sessions").rglob("*.jsonl")):
        thread_id = ""
        source: Any = None
        first_timestamp = ""
        last_timestamp = ""
        usage = {key: 0 for key in (
            "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"
        )}
        event_count = 0
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                event_count += 1
                timestamp = str(event.get("timestamp") or "")
                if timestamp:
                    first_timestamp = first_timestamp or timestamp
                    last_timestamp = timestamp
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                if event.get("type") == "session_meta":
                    thread_id = str(payload.get("id") or payload.get("session_id") or thread_id)
                    source = payload.get("source") or payload.get("thread_source")
                if event.get("type") == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                    total = (
                        info.get("total_token_usage")
                        if isinstance(info.get("total_token_usage"), dict)
                        else {}
                    )
                    for key in usage:
                        usage[key] = max(usage[key], int(total.get(key) or 0))
        if not thread_id:
            continue
        sessions.append({
            "thread_id": thread_id,
            "kind": "parent" if thread_id == parent_thread_id else "subagent",
            "source": source,
            "started_at": first_timestamp or None,
            "finished_at": last_timestamp or None,
            "duration_seconds": _timestamp_delta(first_timestamp, last_timestamp),
            "usage": usage,
            "event_count": event_count,
        })
    totals = {
        key: sum(int(session["usage"].get(key) or 0) for session in sessions)
        for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
    }
    intervals = [
        (_parse_timestamp(session["started_at"]), _parse_timestamp(session["finished_at"]))
        for session in sessions
        if session.get("started_at") and session.get("finished_at")
    ]
    cache_hit_ratio = (
        round(totals["cached_input_tokens"] / totals["input_tokens"], 6)
        if totals["input_tokens"] > 0 else None
    )
    return {
        "session_count": len(sessions),
        "sessions": sessions,
        "aggregate_usage": totals,
        "aggregate_cache_hit_ratio": cache_hit_ratio,
        "peak_observed_concurrency": _peak_concurrency(intervals),
    }


def run_native_arm1(
    *,
    qualification_spec_path: Path,
    run_root: Path,
    source_codex_home: Path,
    execute_real: bool,
    max_wall_seconds: float = 21_600,
) -> dict[str, Any]:
    """Run one native Codex parent and evaluate its frozen terminal candidate once."""

    if not execute_real:
        raise ValueError("Phase 4G9 Arm 1 requires execute_real=True")
    spec_path = qualification_spec_path.expanduser().resolve()
    spec = p4g8.load_qualification_spec(spec_path)
    p4g8_run._require_qualified(spec, spec_path)
    if spec.get("instance_id") != FROZEN_INSTANCE_ID or spec.get("base_commit") != FROZEN_BASE_COMMIT:
        raise ValueError("Phase 4G9 Arm 1 requires the frozen DVC Large instance")
    locked_path = spec_path.parent.parent / "worker" / "locked-task.json"
    if not locked_path.is_file():
        raise ValueError("Phase 4G9 Arm 1 requires the locked worker task manifest")
    locked_task = json.loads(locked_path.read_text(encoding="utf-8"))

    run_root = run_root.expanduser().resolve()
    if run_root.exists():
        raise ValueError("Phase 4G9 Arm 1 run root must not already exist")
    paths = _prepare_layout(run_root, spec)
    prompt = build_parent_prompt(locked_task, toolchain=paths["worker_toolchain"])
    protocol_path = Path(__file__).resolve().parent.parent / "docs" / "kanban-runtime-kernel-phase4g9.md"
    started_epoch = int(time.time())
    started = time.monotonic()
    evaluator_invocations = 0
    process_timed_out = False
    return_code: Optional[int] = None
    model_transport: dict[str, Any] = {}
    config_audit: dict[str, Any] = {}
    raw_lines: list[str] = []
    run_id = f"phase4g9-arm1-{uuid.uuid4().hex[:12]}"

    model_source = p4g8.load_codex_model_source(source_codex_home, model=FROZEN_MODEL)
    with p4g8.Phase4G8NetworkNamespace(run_id, model_source["explicit_base_url"]) as network:
        config_audit = prepare_native_codex_home(
            source_codex_home,
            paths["codex_home"],
            proxy_base_url=str(network.proxy_base_url),
        )
        argv = network.wrap_argv([
            shutil.which("codex") or "codex",
            "--strict-config",
            "--sandbox",
            "danger-full-access",
            "--ask-for-approval",
            "on-request",
            "--cd",
            str(paths["workspace"]),
            "exec",
            "--json",
            "-",
        ])
        env = os.environ.copy()
        env.update({
            "HOME": str(paths["home"]),
            "CODEX_HOME": str(paths["codex_home"]),
            "PATH": str(paths["worker_toolchain"] / "bin") + os.pathsep + env.get("PATH", ""),
            "PYTHONPATH": str(paths["workspace"]),
            p4g8.PROCESS_OWNER_ENV: run_id,
        })
        process = subprocess.Popen(
            argv,
            cwd=paths["workspace"],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(prompt, timeout=max_wall_seconds)
        except subprocess.TimeoutExpired:
            process_timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate(timeout=30)
        return_code = process.returncode
        raw_lines = stdout.splitlines()
        _write_text(paths["worker_events"] / "codex-exec.jsonl", _redact_jsonl(raw_lines))
        _write_text(paths["worker_events"] / "codex-stderr.log", redact_sensitive_text(stderr))
        model_transport = network.transport_audit()

    event_summary = summarize_exec_events(raw_lines)
    if not event_summary.get("parent_thread_id"):
        raise RuntimeError(
            "native Codex did not start a thread; the run is infrastructure-invalid and was not evaluated"
        )
    _reclaim_workspace(paths["workspace"])
    rollout_summary = summarize_rollout_sessions(
        paths["codex_home"],
        parent_thread_id=event_summary.get("parent_thread_id"),
    )
    candidate_patch = swe_evo.collect_candidate_patch(paths["workspace"], FROZEN_BASE_COMMIT)
    candidate_sha = hashlib.sha256(candidate_patch.encode("utf-8")).hexdigest()
    _write_text(paths["reports"] / "candidate.patch", candidate_patch)
    candidate = {
        "base_commit": FROZEN_BASE_COMMIT,
        "patch_sha256": candidate_sha,
        "patch_bytes": len(candidate_patch.encode("utf-8")),
        "changed_files": _changed_files(paths["workspace"]),
        "frozen_at": int(time.time()),
    }
    _write_json(paths["reports"] / "candidate.json", candidate)

    evaluator_invocations += 1
    evaluator = p4g8._run_evaluator(spec, paths["workspace"])
    if evaluator_invocations != 1:
        raise RuntimeError("Phase 4G9 Arm 1 evaluator invocation invariant failed")
    source_unchanged = p4g8.verify_codex_source_unchanged(
        source_codex_home.expanduser().resolve(), config_audit["source_hashes"]
    )
    report = {
        "schema": ARM1_REPORT_SCHEMA,
        "protocol_version": ARM1_PROTOCOL_VERSION,
        "run_id": run_id,
        "instance_id": FROZEN_INSTANCE_ID,
        "dataset_revision": spec["dataset_revision"],
        "started_at": started_epoch,
        "finished_at": int(time.time()),
        "wall_time_seconds": round(time.monotonic() - started, 3),
        "worker": {
            "kind": "standalone_native_codex_parent",
            "codex_cli_version": _codex_version(),
            "return_code": return_code,
            "timed_out": process_timed_out,
            "runtime_kernel_used": False,
            "decision_provider_used": False,
            "evaluator_feedback_turns": 0,
            **event_summary,
            "rollouts": rollout_summary,
        },
        "config": config_audit,
        "model_transport": model_transport,
        "candidate": candidate,
        "evaluator_invocation_count": evaluator_invocations,
        "evaluator": evaluator,
        "integrity": {
            "protocol_sha256": _sha256_file(protocol_path),
            "source_codex_home_unchanged": source_unchanged,
            "gold_or_protected_tests_exposed_to_worker": False,
            "historical_candidate_exposed_to_worker": False,
            "evaluator_before_terminal_candidate": 0,
            "evaluator_after_terminal_candidate": evaluator_invocations,
        },
    }
    _write_json(paths["reports"] / "run-report.json", report)
    _write_text(paths["reports"] / "execution-summary.md", render_execution_summary(report))
    return report


def render_execution_summary(report: dict[str, Any]) -> str:
    worker = report["worker"]
    evaluator = report["evaluator"]
    fail = evaluator.get("fail_to_pass") or {}
    p2p = evaluator.get("pass_to_pass") or {}
    collab = worker.get("collaboration_calls") or []
    spawn_calls = [call for call in collab if call.get("tool") == "spawn_agent"]
    lines = [
        "# Phase 4G9 Arm 1: Native Codex Orchestra",
        "",
        "## Result",
        "",
        f"- Official resolved: `{bool(evaluator.get('resolved'))}`",
        f"- FAIL_TO_PASS: `{fail.get('passed', 0)}/{fail.get('total', 0)}`",
        f"- PASS_TO_PASS: `{p2p.get('passed', 0)}/{p2p.get('total', 0)}`",
        f"- Wall time: `{report.get('wall_time_seconds')}s`",
        f"- Parent thread: `{worker.get('parent_thread_id') or 'unavailable'}`",
        f"- Native subagents observed: `{worker.get('subagent_count', 0)}`",
        f"- Candidate patch: `{report['candidate']['patch_bytes']} bytes`, "
        f"`{len(report['candidate']['changed_files'])}` changed files",
        "",
        "## Frozen Protocol",
        "",
        "One standalone Codex parent used `gpt-5.6-sol` with `ultra` client semantics "
        "(`max` model reasoning plus proactive native multi-agent delegation). Hermes Runtime, "
        "Decision Provider, and evaluator feedback were absent during execution. The official "
        "evaluator ran once after the candidate patch was frozen.",
        "",
        "## Native Allocation",
        "",
    ]
    if spawn_calls:
        for index, call in enumerate(spawn_calls, start=1):
            receivers = ", ".join(call.get("receiver_thread_ids") or []) or "unknown"
            prompt = str(call.get("prompt") or "").replace("\n", " ").strip()
            lines.append(f"{index}. `{receivers}`: {prompt[:500]}")
    else:
        lines.append("The parent created no observable native subagents.")
    lines.extend([
        "",
        "## Parent Terminal Summary",
        "",
        str(worker.get("terminal_message") or "No terminal parent message was captured."),
        "",
        "## Measurement Boundary",
        "",
        "This is a single-run architecture baseline, not a model leaderboard result. Hidden tests, "
        "gold content, prior candidates, and evaluator diagnostics were not available to the native "
        "orchestra before termination.",
        "",
    ])
    return "\n".join(lines)


def _prepare_layout(root: Path, spec: dict[str, Any]) -> dict[str, Path]:
    mirror = Path(spec["source"]["local_mirror"]).resolve()
    p4g8_run._protect_source_mirror(mirror)
    root.mkdir(parents=True, exist_ok=False)
    os.chmod(root, 0o711)
    paths = {
        "root": root,
        "home": root / "home",
        "codex_home": root / "codex-home",
        "workspace": root / "workspace",
        "protected": root / "protected",
        "worker_events": root / "worker-events",
        "reports": root / "reports",
    }
    for key in ("home", "protected", "worker_events", "reports"):
        paths[key].mkdir()
    os.chmod(paths["protected"], 0o700)
    os.chmod(paths["worker_events"], 0o700)
    os.chmod(paths["reports"], 0o700)
    subprocess.run(["git", "init", "--quiet", str(paths["workspace"])], check=True)
    subprocess.run(["git", "remote", "add", "source", mirror.as_uri()], cwd=paths["workspace"], check=True)
    subprocess.run([
        "git", "-c", "protocol.file.allow=always", "fetch", "--quiet", "--depth=1", "--no-tags",
        "source", FROZEN_BASE_COMMIT,
    ], cwd=paths["workspace"], check=True)
    subprocess.run(["git", "checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=paths["workspace"], check=True)
    subprocess.run(["git", "remote", "remove", "source"], cwd=paths["workspace"], check=True)
    setup = p4g8_run._render_worker_environment_setup(spec)
    toolchain = p4g8_run._prepare_worker_toolchain(
        str(spec["benchmark"]["official_image"]),
        environment_setup=setup,
        setup_env=(spec.get("worker_environment") or {}).get("env") or {},
    )
    paths["worker_toolchain"] = toolchain
    p4g8.prepare_worker_workspace(paths["workspace"], worker_uid=WORKER_UID, worker_gid=WORKER_GID)
    for key in ("home",):
        os.chmod(paths[key], 0o700)
        os.chown(paths[key], WORKER_UID, WORKER_GID)
    return paths


def _changed_files(workspace: Path) -> list[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=workspace,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return sorted({line[3:] for line in result.stdout.splitlines() if len(line) >= 4})


def _reclaim_workspace(workspace: Path) -> None:
    for root, directories, files in os.walk(workspace):
        os.chown(root, 0, 0)
        for name in directories:
            os.chown(Path(root) / name, 0, 0)
        for name in files:
            os.chown(Path(root) / name, 0, 0)


def _codex_version() -> str:
    result = subprocess.run(
        [shutil.which("codex") or "codex", "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    return (result.stdout or result.stderr).strip()


def _redact_jsonl(lines: list[str]) -> str:
    return "\n".join(redact_sensitive_text(line) for line in lines) + ("\n" if lines else "")


def _counts(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _parse_timestamp(value: Any) -> Optional[float]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _timestamp_delta(start: Any, end: Any) -> Optional[float]:
    started = _parse_timestamp(start)
    finished = _parse_timestamp(end)
    if started is None or finished is None:
        return None
    return round(max(0.0, finished - started), 3)


def _peak_concurrency(intervals: list[tuple[Optional[float], Optional[float]]]) -> int:
    points: list[tuple[float, int]] = []
    for started, finished in intervals:
        if started is None or finished is None:
            continue
        points.append((started, 1))
        points.append((finished, -1))
    current = 0
    peak = 0
    for _timestamp, delta in sorted(points, key=lambda point: (point[0], -point[1])):
        current += delta
        peak = max(peak, current)
    return peak


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    os.chmod(path, 0o600)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    _write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m hermes_cli.phase4g9_native_arm")
    parser.add_argument("--spec", required=True)
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--source-codex-home", default="~/.codex")
    parser.add_argument("--max-wall-seconds", type=float, default=21_600)
    parser.add_argument("--execute-real", action="store_true", required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        report = run_native_arm1(
            qualification_spec_path=Path(args.spec),
            run_root=Path(args.run_root),
            source_codex_home=Path(args.source_codex_home),
            execute_real=bool(args.execute_real),
            max_wall_seconds=float(args.max_wall_seconds),
        )
    except Exception as exc:
        print(json.dumps({
            "status": "failed",
            "error": type(exc).__name__,
            "message": redact_sensitive_text(str(exc))[:2000],
        }, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
