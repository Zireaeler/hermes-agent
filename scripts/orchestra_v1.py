#!/usr/bin/env python3
"""Foreground CLI for the minimal Hermes Orchestra v1 loop."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli.orchestra_v1 import (  # noqa: E402
    RUN_DECISIONS,
    decide,
    format_status,
    initialize_project,
    project_control_dir,
    project_status,
    run_worker,
)


def _worker_event(note: dict[str, Any]) -> None:
    method = str(note.get("method") or "")
    params = note.get("params")
    if not isinstance(params, dict):
        params = {}
    item = params.get("item")
    detail = ""
    if isinstance(item, dict):
        detail = str(item.get("type") or item.get("id") or "")
    print(f"[codex] {method}{f' {detail}' if detail else ''}", flush=True)


def _show_decision(project: str) -> str:
    control = project_control_dir(project)
    decision = (control / "decision.txt").read_text(encoding="utf-8").strip()
    task = (control / "task.md").read_text(encoding="utf-8").strip()
    print(f"决定：{decision}\n")
    print("# worker 推进说明")
    print(task)
    return decision


def _cmd_init(args: argparse.Namespace) -> int:
    goal = Path(args.goal_file).expanduser().read_text(encoding="utf-8")
    control = initialize_project(
        args.project,
        goal,
        overwrite=bool(args.force),
    )
    print(f"已初始化：{control}")
    return 0


def _cmd_decide(args: argparse.Namespace) -> int:
    decide(args.project, human=args.human)
    _show_decision(args.project)
    return 0


def _cmd_run_worker(args: argparse.Namespace) -> int:
    result = run_worker(args.project, on_notification=_worker_event)
    if result is None:
        decision = _show_decision(args.project)
        print(f"决定为“{decision}”，未启动 worker。")
        return 0
    print(f"worker 状态：{result.status}")
    if result.thread_id:
        print(f"worker thread：{result.thread_id}")
    if result.error:
        print(f"worker 错误：{result.error}", file=sys.stderr)
    return 0 if result.status == "completed" else 130 if result.status == "interrupted" else 1


def _cmd_step(args: argparse.Namespace) -> int:
    decide(args.project, human=args.human)
    decision = _show_decision(args.project)
    if decision not in RUN_DECISIONS:
        print("当前决定不启动 worker。")
        return 0
    answer = input("启动这个 worker？[y/N] ").strip().lower()
    if answer not in {"y", "yes"}:
        print("未启动 worker。")
        return 0
    return _cmd_run_worker(args)


def _cmd_status(args: argparse.Namespace) -> int:
    print(format_status(project_status(args.project)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python scripts/orchestra_v1.py")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="初始化项目控制材料")
    init_parser.add_argument("--project", required=True)
    init_parser.add_argument("--goal-file", required=True)
    init_parser.add_argument("--force", action="store_true", help="显式覆盖已有控制材料")
    init_parser.set_defaults(handler=_cmd_init)

    decide_parser = subparsers.add_parser("decide", help="运行一次全新的 Orchestra 决策轮")
    decide_parser.add_argument("--project", required=True)
    decide_parser.add_argument("--human")
    decide_parser.set_defaults(handler=_cmd_decide)

    worker_parser = subparsers.add_parser("run-worker", help="按当前决定启动或恢复 Codex worker")
    worker_parser.add_argument("--project", required=True)
    worker_parser.set_defaults(handler=_cmd_run_worker)

    step_parser = subparsers.add_parser("step", help="交互式执行 decide 后按确认运行 worker")
    step_parser.add_argument("--project", required=True)
    step_parser.add_argument("--human")
    step_parser.set_defaults(handler=_cmd_step)

    status_parser = subparsers.add_parser("status", help="显示机械运行状态")
    status_parser.add_argument("--project", required=True)
    status_parser.set_defaults(handler=_cmd_status)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
