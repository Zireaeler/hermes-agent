"""Filesystem-backed runtime memory lifecycle for the Kanban runtime kernel.

Runtime memory is deliberately non-authoritative.  It can provide selected
decision-provider hints, but DB facts, checkpoints, validators, and capability
policy remain the runtime truth.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Optional


MEMORY_NOTICE = (
    "These are non-authoritative memory hints. DB state, goal contract, "
    "validator rules, capability policy, and current checkpoint override them."
)
MEMORY_STATUS = {"candidate", "accepted", "deprecated"}
MEMORY_SCOPE_TYPES = {"global", "workspace", "domain", "job"}
DEFAULT_MEMORY_BUDGET = {
    "max_hints": 5,
    "max_tokens_per_hint": 240,
    "max_total_memory_tokens": 1200,
    "max_provider_input_ratio": 0.10,
    "max_index_tokens": 1500,
}
HIGH_VALUE_CANDIDATE_KINDS = {
    "validator_rejection",
    "validator_repeated_rejection",
    "anti_stuck_recovery",
    "successful_recovery",
    "human_decision",
    "milestone_completed",
    "verifier_recovery",
    "worker_recovery_success",
    "compaction_fallback",
    "complex_job_done",
}
LOW_VALUE_CANDIDATE_KINDS = {
    "node_success",
    "ordinary_code_change",
    "single_test_failure",
    "ordinary_bug_fix",
}
FIELD_NAMES = {
    "Status",
    "Scope",
    "Applies when",
    "Lesson",
    "Evidence",
    "Use as",
    "Source",
    "Confidence",
    "Usage",
    "Created",
    "Last validated",
}
SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|credential|password)\s*[:=]\s*['\"]?[^'\"\s]+"),
    re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]{12,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
]


@dataclass(frozen=True)
class MemoryPaths:
    global_root: Path
    workspace_roots: tuple[Path, ...]
    domain_roots: tuple[Path, ...]
    candidate_root: Path


@dataclass(frozen=True)
class MemoryEntry:
    entry_id: str
    topic: str
    path: str
    status: str
    scope_type: str
    scope_ref: Optional[str]
    applies_when: str
    lesson: str
    evidence: str
    use_as: str
    keywords: tuple[str, ...]
    estimated_tokens: int
    text: str

    def to_hint(self, *, selected_reason: str) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "topic": self.topic,
            "path": self.path,
            "status": self.status,
            "scope_type": self.scope_type,
            "scope_ref": self.scope_ref,
            "applies_when": self.applies_when,
            "lesson": self.lesson,
            "evidence": self.evidence,
            "use_as": self.use_as,
            "keywords": list(self.keywords),
            "estimated_tokens": self.estimated_tokens,
            "selected_reason": selected_reason,
            "non_authoritative": True,
        }


def _now() -> int:
    return int(time.time())


def _estimate_tokens(payload: Any) -> int:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return max(1, (len(text) + 3) // 4)


def _loads(raw: Any, default: Any = None) -> Any:
    if raw is None or raw == "":
        return {} if default is None else default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {} if default is None else default


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()


def _job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM runtime_jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown runtime job {job_id}")
    return dict(row)


def runtime_memory_paths(conn: sqlite3.Connection, job_id: str, workspace_path: Optional[str] = None) -> MemoryPaths:
    job = _job(conn, job_id)
    workspace = Path(workspace_path or job.get("workspace_path") or "").expanduser() if (workspace_path or job.get("workspace_path")) else None
    global_root = _hermes_home() / "runtime-memory"
    workspace_roots: list[Path] = []
    if workspace:
        workspace_roots.extend([workspace / ".hermes" / "runtime-memory", workspace / "docs" / "runtime-memory"])
    return MemoryPaths(
        global_root=global_root,
        workspace_roots=tuple(workspace_roots),
        domain_roots=(global_root / "domains", *[root / "domains" for root in workspace_roots]),
        candidate_root=(workspace_roots[0] if workspace_roots else global_root) / "candidates",
    )


def _read_text(path: Path, *, max_tokens: Optional[int] = None) -> str:
    if not path.exists() or not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8")
    if max_tokens is not None and _estimate_tokens(text) > max_tokens:
        limit = max(0, int(max_tokens) * 4)
        return text[:limit]
    return text


def load_runtime_guidance(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    paths = runtime_memory_paths(conn, job_id)
    candidates = [paths.global_root.parent / "runtime-guidance.md"]
    for root in paths.workspace_roots:
        candidates.append(root.parent / "runtime-guidance.md")
        candidates.append(root.parent.parent / "docs" / "runtime-guidance.md")
    chunks: list[dict[str, str]] = []
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        content = _read_text(path, max_tokens=DEFAULT_MEMORY_BUDGET["max_index_tokens"])
        if content.strip():
            chunks.append({"path": str(path), "content": content.strip()})
    return {
        "loaded": bool(chunks),
        "sources": [chunk["path"] for chunk in chunks],
        "content": "\n\n".join(chunk["content"] for chunk in chunks),
    }


def _parse_index_file(path: Path, scope_hint: str) -> list[dict[str, Any]]:
    content = _read_text(path, max_tokens=DEFAULT_MEMORY_BUDGET["max_index_tokens"])
    if not content.strip():
        return []
    entries: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.startswith("- ") and ".md" in line:
            topic = line[2:].split()[0].strip()
            current = {"topic": topic, "keywords": [], "scope": scope_hint, "index_path": str(path)}
            entries.append(current)
            continue
        if current is None:
            continue
        lowered = line.lower()
        if lowered.startswith("- keywords:") or lowered.startswith("keywords:"):
            value = line.split(":", 1)[1]
            current["keywords"] = [item.strip().lower() for item in value.split(",") if item.strip()]
        elif lowered.startswith("- scope:") or lowered.startswith("scope:"):
            current["scope"] = line.split(":", 1)[1].strip() or scope_hint
    return entries


def load_runtime_memory_index(conn: sqlite3.Connection, job_id: str, workspace_path: Optional[str] = None) -> dict[str, Any]:
    paths = runtime_memory_paths(conn, job_id, workspace_path)
    roots = [(paths.global_root, "global")]
    roots.extend((root, "workspace") for root in paths.workspace_roots)
    roots.extend((root, "domain") for root in paths.domain_roots)
    topics: list[dict[str, Any]] = []
    sources: list[str] = []
    for root, scope_hint in roots:
        index_path = root / "MEMORY.md"
        entries = _parse_index_file(index_path, scope_hint)
        if entries:
            sources.append(str(index_path))
            for entry in entries:
                entry["root"] = str(root)
                entry["topic_path"] = str((root / entry["topic"]).resolve())
            topics.extend(entries)
    return {"sources": sources, "topics": topics, "topic_count": len(topics)}


def _field_blocks(section: str) -> dict[str, str]:
    blocks: dict[str, list[str]] = {}
    current: Optional[str] = None
    for raw_line in section.splitlines():
        stripped = raw_line.strip()
        name = stripped[:-1] if stripped.endswith(":") else None
        if name in FIELD_NAMES:
            current = name
            blocks.setdefault(current, [])
            continue
        if current:
            blocks[current].append(raw_line.rstrip())
    return {key: "\n".join(value).strip() for key, value in blocks.items()}


def _clean_block_value(value: str) -> str:
    lines = []
    for line in value.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            stripped = stripped[2:].strip()
        lines.append(stripped)
    return "\n".join(line for line in lines if line).strip()


def _parse_scope(value: str) -> tuple[str, Optional[str], tuple[str, ...]]:
    cleaned = _clean_block_value(value)
    scope_type = ""
    scope_ref: Optional[str] = None
    keywords: list[str] = []
    if ":" in cleaned and "\n" not in cleaned and not cleaned.lower().startswith("scope_type"):
        left, right = cleaned.split(":", 1)
        scope_type = left.strip().lower()
        scope_ref = right.strip() or None
    for line in cleaned.splitlines():
        lowered = line.lower()
        if lowered.startswith("scope_type:"):
            scope_type = line.split(":", 1)[1].strip().lower()
        elif lowered.startswith("scope_ref:"):
            scope_ref = line.split(":", 1)[1].strip() or None
        elif "keywords:" in lowered:
            keywords.extend(re.findall(r"[A-Za-z0-9_.-]+", line.split(":", 1)[1].lower()))
    if not scope_type and cleaned:
        scope_type = cleaned.splitlines()[0].strip().lower()
    return scope_type, scope_ref, tuple(dict.fromkeys(keywords))


def parse_runtime_memory_entries(content: str, *, topic: str, path: str) -> list[MemoryEntry]:
    entries: list[MemoryEntry] = []
    parts = re.split(r"(?m)^##\s+", content)
    for part in parts:
        if not part.strip():
            continue
        first_line, _, rest = part.partition("\n")
        entry_id = first_line.strip()
        if not entry_id:
            continue
        fields = _field_blocks(rest)
        required = {"Status", "Scope", "Applies when", "Lesson", "Evidence", "Use as"}
        if not required.issubset(fields):
            continue
        status = _clean_block_value(fields["Status"]).splitlines()[0].strip().lower()
        if status not in MEMORY_STATUS:
            continue
        scope_type, scope_ref, scope_keywords = _parse_scope(fields["Scope"])
        if scope_type not in MEMORY_SCOPE_TYPES:
            continue
        use_as = _clean_block_value(fields["Use as"])
        if "non-authoritative" not in use_as.lower() or "hint" not in use_as.lower():
            continue
        applies = _clean_block_value(fields["Applies when"])
        lesson = _clean_block_value(fields["Lesson"])
        evidence = _clean_block_value(fields["Evidence"])
        if not applies or not lesson or not evidence:
            continue
        keywords = tuple(
            dict.fromkeys(
                [
                    *scope_keywords,
                    *re.findall(r"[A-Za-z0-9_.-]+", applies.lower()),
                    *re.findall(r"[A-Za-z0-9_.-]+", lesson.lower()),
                ]
            )
        )
        text = f"## {entry_id}\n\nApplies when:\n{applies}\n\nLesson:\n{lesson}\n\nEvidence:\n{evidence}"
        entries.append(
            MemoryEntry(
                entry_id=entry_id,
                topic=topic,
                path=path,
                status=status,
                scope_type=scope_type,
                scope_ref=scope_ref,
                applies_when=applies,
                lesson=lesson,
                evidence=evidence,
                use_as=use_as,
                keywords=keywords,
                estimated_tokens=_estimate_tokens(text),
                text=text,
            )
        )
    return entries


def _corpus_for_job(conn: sqlite3.Connection, job_id: str, delta: Optional[dict[str, Any]]) -> str:
    job = _job(conn, job_id)
    chunks = [str(job.get("objective") or ""), json.dumps(delta or {}, ensure_ascii=False)]
    for row in conn.execute("SELECT gap_key, gap_type, summary FROM goal_gaps WHERE job_id = ? AND state = 'open'", (job_id,)).fetchall():
        chunks.extend(str(row[key] or "") for key in ("gap_key", "gap_type", "summary"))
    for row in conn.execute("SELECT node_key, node_type, title, description FROM execution_nodes WHERE job_id = ?", (job_id,)).fetchall():
        chunks.extend(str(row[key] or "") for key in ("node_key", "node_type", "title", "description"))
    return "\n".join(chunks).lower()


def _scope_matches(entry: MemoryEntry, *, workspace_path: Optional[str], corpus: str) -> bool:
    if entry.scope_type == "global":
        return True
    if entry.scope_type == "workspace":
        if not workspace_path:
            return False
        if not entry.scope_ref:
            return True
        try:
            return Path(entry.scope_ref).expanduser().resolve() == Path(workspace_path).expanduser().resolve()
        except OSError:
            return str(entry.scope_ref) == str(workspace_path)
    if entry.scope_type == "domain":
        if entry.scope_ref and str(entry.scope_ref).lower() in corpus:
            return True
        return any(keyword in corpus for keyword in entry.keywords[:12])
    if entry.scope_type == "job":
        return bool(entry.scope_ref and entry.scope_ref in corpus)
    return False


def _topic_matches(index_entry: dict[str, Any], corpus: str) -> bool:
    keywords = [str(item).lower() for item in index_entry.get("keywords") or [] if str(item).strip()]
    if not keywords:
        return False
    return any(keyword in corpus for keyword in keywords)


def select_runtime_memory_hints(
    conn: sqlite3.Connection,
    job_id: str,
    delta: Optional[dict[str, Any]],
    *,
    max_hints: int = 5,
    budget_tokens: int = 1200,
) -> dict[str, Any]:
    job = _job(conn, job_id)
    guidance = load_runtime_guidance(conn, job_id)
    index = load_runtime_memory_index(conn, job_id)
    corpus = _corpus_for_job(conn, job_id, delta)
    workspace_path = job.get("workspace_path")
    selected: list[dict[str, Any]] = []
    topic_reads: list[str] = []
    total_tokens = 0
    max_count = max(0, min(int(max_hints), int(DEFAULT_MEMORY_BUDGET["max_hints"])))
    budget = max(0, min(int(budget_tokens), int(DEFAULT_MEMORY_BUDGET["max_total_memory_tokens"])))
    scope_rank = {"workspace": 0, "domain": 1, "global": 2, "job": 3}
    candidates: list[tuple[int, int, MemoryEntry, str]] = []
    for topic in index["topics"]:
        if not _topic_matches(topic, corpus):
            continue
        topic_path = Path(str(topic["topic_path"]))
        content = _read_text(topic_path)
        if not content:
            continue
        topic_reads.append(str(topic_path))
        for entry in parse_runtime_memory_entries(content, topic=str(topic["topic"]), path=str(topic_path)):
            if entry.status != "accepted":
                continue
            if not _scope_matches(entry, workspace_path=workspace_path, corpus=corpus):
                continue
            overlap = sum(1 for keyword in entry.keywords if keyword and keyword in corpus)
            if overlap <= 0 and entry.scope_type != "workspace":
                continue
            candidates.append((scope_rank.get(entry.scope_type, 9), -overlap, entry, f"scope={entry.scope_type}; keyword_overlap={overlap}"))
    for _, _, entry, reason in sorted(candidates, key=lambda item: (item[0], item[1], item[2].entry_id)):
        if len(selected) >= max_count:
            break
        entry_tokens = min(entry.estimated_tokens, int(DEFAULT_MEMORY_BUDGET["max_tokens_per_hint"]))
        if total_tokens + entry_tokens > budget:
            continue
        hint = entry.to_hint(selected_reason=reason)
        hint["injected_tokens"] = entry_tokens
        selected.append(hint)
        total_tokens += entry_tokens
    return {
        "non_authoritative_notice": MEMORY_NOTICE,
        "guidance": guidance,
        "index": {"sources": index["sources"], "topic_count": index["topic_count"]},
        "topic_reads": topic_reads,
        "selected_hints": selected,
        "budget": {
            **DEFAULT_MEMORY_BUDGET,
            "max_hints": max_count,
            "max_total_memory_tokens": budget,
            "used_tokens": total_tokens,
        },
    }


def record_memory_hint_usage(
    conn: sqlite3.Connection,
    job_id: str,
    decision_id: Optional[str],
    memory: dict[str, Any],
    *,
    outcome: Optional[dict[str, Any]] = None,
    provider_request_ref: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    hints = memory.get("selected_hints") or memory.get("hints") or []
    if not hints:
        return None
    from hermes_cli import kanban_runtime_kernel as rk

    payload = {
        "decision_id": decision_id,
        "hints": hints,
        "hint_count": len(hints),
        "provider_request_ref": provider_request_ref,
        "outcome": outcome,
        "non_authoritative": True,
    }
    return rk.append_decision_segment_entry(
        conn,
        job_id,
        "memory_hint_used" if outcome is None else "memory_hint_outcome_recorded",
        payload,
        decision_id=decision_id,
        ref_type="runtime_memory",
    )


def summarize_runtime_memory(conn: sqlite3.Connection, job_id: str, *, limit: int = 20) -> dict[str, Any]:
    paths = runtime_memory_paths(conn, job_id)
    candidate_count = 0
    latest_candidate: Optional[dict[str, Any]] = None
    if paths.candidate_root.exists():
        files = sorted(paths.candidate_root.glob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True)
        candidate_count = len(files)
        if files:
            latest_candidate = {"path": str(files[0]), "updated_at": int(files[0].stat().st_mtime)}
    rows = conn.execute(
        """
        SELECT id, decision_id, entry_type, payload_json, created_at
          FROM decision_segment_entries
         WHERE job_id = ?
           AND entry_type IN ('memory_hint_used', 'memory_hint_outcome_recorded')
         ORDER BY id DESC
         LIMIT ?
        """,
        (job_id, max(1, int(limit))),
    ).fetchall()
    recent_usage = [
        {
            "id": int(row["id"]),
            "decision_id": row["decision_id"],
            "entry_type": row["entry_type"],
            "payload": _loads(row["payload_json"]),
            "created_at": int(row["created_at"]),
        }
        for row in rows
    ]
    latest = recent_usage[0]["payload"] if recent_usage else {}
    return {
        "guidance_loaded": load_runtime_guidance(conn, job_id)["loaded"],
        "selected_hints": latest.get("hints") or [],
        "candidate_count": candidate_count,
        "latest_candidate": latest_candidate,
        "recent_usage": recent_usage,
    }


def redact_memory_text(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        def repl(match: re.Match[str]) -> str:
            value = match.group(0)
            if "=" in value:
                return value.split("=", 1)[0] + "= [REDACTED]"
            if ":" in value:
                return value.split(":", 1)[0] + ": [REDACTED]"
            return "[REDACTED]"

        redacted = pattern.sub(repl, redacted)
    if len(redacted) > 4000:
        redacted = redacted[:4000] + "\n[TRUNCATED]"
    return redacted


def write_runtime_memory_candidate(
    conn: sqlite3.Connection,
    job_id: str,
    trigger_event_id: int,
    kind: str,
    *,
    lesson: Optional[str] = None,
    applies_when: Optional[str] = None,
) -> Path:
    if kind in LOW_VALUE_CANDIDATE_KINDS or kind not in HIGH_VALUE_CANDIDATE_KINDS:
        raise ValueError(f"memory candidate kind {kind!r} is not high value")
    job = _job(conn, job_id)
    event = conn.execute("SELECT * FROM execution_events WHERE id = ? AND job_id = ?", (trigger_event_id, job_id)).fetchone()
    if event is None:
        raise ValueError("memory candidate requires a valid source event")
    paths = runtime_memory_paths(conn, job_id)
    paths.candidate_root.mkdir(parents=True, exist_ok=True)
    event_payload = _loads(event["payload_json"])
    entry_id = f"{kind}-{job_id}-{trigger_event_id}".replace("_", "-")
    scope_ref = job.get("workspace_path") or job_id
    scope_type = "workspace" if job.get("workspace_path") else "job"
    inferred_applies = applies_when or f"objective resembles: {job.get('objective')}"
    inferred_lesson = lesson or f"Review runtime event pattern `{kind}` before repeating the same strategy."
    evidence = redact_memory_text(json.dumps(event_payload, ensure_ascii=False, sort_keys=True))
    content = f"""## {entry_id}

Status:
- candidate

Scope:
- scope_type: {scope_type}
- scope_ref: {scope_ref}

Applies when:
- {redact_memory_text(inferred_applies)}

Lesson:
- {redact_memory_text(inferred_lesson)}

Evidence:
- source_job: {job_id}
- source_event: {trigger_event_id}
- source_event_type: {event['event_type']}
- source_summary: {evidence}

Use as:
- non-authoritative decision hint

Source:
- generated_from: runtime_trace

Created:
- {_now()}
"""
    path = paths.candidate_root / f"{entry_id}.md"
    path.write_text(content, encoding="utf-8")
    from hermes_cli import kanban_runtime_kernel as rk

    rk._event(
        conn,
        job_id,
        "memory_candidate_created",
        {"path": str(path), "kind": kind, "trigger_event_id": trigger_event_id, "entry_id": entry_id},
        source_event_id=trigger_event_id,
    )
    return path


def validate_memory_candidate(path: Path) -> dict[str, Any]:
    content = path.read_text(encoding="utf-8")
    entries = parse_runtime_memory_entries(content, topic=path.name, path=str(path))
    if not entries:
        return {"status": "rejected", "reason": "candidate has no valid memory entry"}
    entry = entries[0]
    if entry.status != "candidate":
        return {"status": "rejected", "reason": "candidate status must be candidate"}
    if "source_event" not in entry.evidence and "source_job" not in entry.evidence:
        return {"status": "rejected", "reason": "candidate lacks source refs"}
    if "[REDACTED]" not in content:
        # Redaction does not have to replace anything, but this keeps the result
        # explicit for operators reading validation output.
        pass
    return {"status": "accepted", "entry_id": entry.entry_id, "path": str(path)}


def promote_runtime_memory_candidate(candidate_path: Path, topic_path: Path) -> dict[str, Any]:
    validation = validate_memory_candidate(candidate_path)
    if validation["status"] != "accepted":
        return validation
    content = candidate_path.read_text(encoding="utf-8")
    promoted = re.sub(r"(?m)^-\s*candidate\s*$", "- accepted", content, count=1)
    topic_path.parent.mkdir(parents=True, exist_ok=True)
    existing = topic_path.read_text(encoding="utf-8") if topic_path.exists() else ""
    topic_path.write_text((existing.rstrip() + "\n\n" + promoted.strip() + "\n").lstrip(), encoding="utf-8")
    return {"status": "promoted", "candidate_path": str(candidate_path), "topic_path": str(topic_path)}
