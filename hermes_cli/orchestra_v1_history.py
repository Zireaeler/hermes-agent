"""Orchestra 调用的观察档案及有范围的只读查询。"""

from datetime import datetime, timezone
import json
from pathlib import Path
import time
import uuid

from hermes_cli.orchestra_v1_control import CONTROL_FILES, atomic_write


class CallRecord:
    def __init__(self, control: Path, kind: str, context: str = ""):
        self.path = (
            control
            / "history"
            / (
                datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
                + f"-{kind}-{uuid.uuid4().hex[:8]}"
            )
        )
        self.path.mkdir(parents=True)
        self.started = time.monotonic()
        self.metadata = {
            "kind": kind,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "context": context,
        }
        for name in CONTROL_FILES:
            source = control / name
            if source.is_file():
                atomic_write(
                    self.path / "input" / name, source.read_text(encoding="utf-8")
                )
        self.write(
            "metadata.json", json.dumps(self.metadata, ensure_ascii=False, indent=2)
        )

    def write(self, name: str, content: str):
        atomic_write(self.path / name, content)

    def notification(self, note: dict):
        with (self.path / "notifications.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"at": datetime.now(timezone.utc).isoformat(), "note": note},
                    ensure_ascii=False,
                )
                + "\n"
            )

    def finish(self, status: str):
        self.metadata.update(
            status=status,
            ended_at=datetime.now(timezone.utc).isoformat(),
            elapsed_seconds=round(time.monotonic() - self.started, 3),
        )
        self.write(
            "metadata.json", json.dumps(self.metadata, ensure_ascii=False, indent=2)
        )


def history_index(control: Path, limit: int = 8) -> str:
    history = control / "history"
    paths = sorted(history.iterdir(), reverse=True) if history.is_dir() else []
    rows = []
    worker_seconds = 0.0
    worker_calls = 0
    decision_seconds = 0.0
    for path in paths:
        if path.is_dir():
            try:
                metadata = json.loads(
                    (path / "metadata.json").read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                if len(rows) < limit:
                    rows.append(f"{path.name}: 档案元信息未完成，可按需检查")
                continue
            if metadata.get("kind") == "worker":
                worker_calls += 1
                worker_seconds += metadata.get("elapsed_seconds", 0)
            elif metadata.get("kind") in {"decide", "observe"}:
                decision_seconds += metadata.get("elapsed_seconds", 0)
            if len(rows) < limit:
                rows.append(
                    f"{path.name}: {metadata.get('status', 'running')}，用时 {metadata.get('elapsed_seconds', '?')} 秒；{metadata.get('context', '')[:160]}"
                )
    if not rows:
        return "(尚无调用档案)"
    return (
        f"档案内累计 worker 调用 {worker_calls} 次，已结束调用用时 {worker_seconds:.3f} 秒；"
        f"项目决策及观察用时 {decision_seconds:.3f} 秒。并行观察时间可能重叠，不相加为项目墙钟时间；未记录更早投入及完整 token 用量。\n"
        + "\n".join(rows)
    )


def read_history(control: Path, args: dict) -> str:
    """只查询本项目观察档案，拒绝绝对路径及符号链接逃逸。"""
    root = (control / "history").resolve()
    value = Path(str(args.get("path", ".")))
    target = (root / value).resolve()
    if value.is_absolute() or not target.is_relative_to(root):
        return json.dumps(
            {"error": "只能读取当前项目 history 内的相对路径"}, ensure_ascii=False
        )
    offset = max(0, int(args.get("offset", 0)))
    limit = max(1, min(200, int(args.get("limit", 100))))
    char_offset = max(0, int(args.get("char_offset", 0)))
    try:
        if target.is_dir():
            entries = sorted(
                p.name + ("/" if p.is_dir() else "") for p in target.iterdir()
            )
            return json.dumps(
                {"entries": entries[offset : offset + limit], "total": len(entries)},
                ensure_ascii=False,
            )
        lines = []
        more = False
        size = 0
        truncated = []
        with target.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle):
                if number < offset:
                    continue
                if len(lines) >= limit or size >= 20000:
                    more = True
                    break
                content = line.rstrip("\n")
                start = char_offset if number == offset else 0
                part = content[start : start + min(8000, 20000 - size)]
                if start + len(part) < len(content):
                    truncated.append({
                        "line": number,
                        "next_char_offset": start + len(part),
                    })
                lines.append(part)
                size += len(part)
        return json.dumps(
            {"lines": lines, "offset": offset, "more": more, "truncated": truncated},
            ensure_ascii=False,
        )
    except OSError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
