#!/usr/bin/env python3
"""Hermes Orchestra v1 最小闭环的前台命令行入口。"""

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
from hermes_cli.orchestra_v1_runtime import run_project  # noqa: E402
from hermes_cli.orchestra_v1_team import run_team_project  # noqa: E402


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
    decide(args.project)
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
    decide(args.project)
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


def _runtime_event(event: dict[str, Any]) -> None:
    kind = event["kind"]
    if kind in {"decision", "observation"}:
        print(f"[orchestra {kind}] {event['decision']}\n{event['task']}", flush=True)
    else:
        print(f"[orchestra] {event}", flush=True)


def _cmd_run(args: argparse.Namespace) -> int:
    result = run_project(
        args.project,
        max_worker_turns=args.max_worker_turns,
        max_seconds=args.max_seconds,
        check_interval=args.check_interval,
        worker_timeout=args.worker_timeout,
        on_notification=_worker_event,
        on_event=_runtime_event,
    )
    print(f"本次运行结束：{result.reason}；worker turn：{result.worker_turns}")
    print(f"运行档案：{result.history_dir}")
    if result.reason == "interrupted":
        return 130
    return 1 if result.reason in {"worker_unconfirmed", "observer_failed", "worker_failure_limit"} else 0


def _cmd_run_team(args: argparse.Namespace) -> int:
    result = run_team_project(
        args.project, output_dir=args.output_dir,
        max_workers=args.max_workers, max_subagents=args.max_subagents,
        max_seconds=args.max_seconds, check_interval=args.check_interval,
        max_iterations=args.max_iterations, codex_bin=args.codex_bin,
        on_event=_runtime_event,
    )
    print(f"团队运行结束：{result.reason}；worker任务数：{result.worker_turns}")
    print(f"运行档案：{result.history_dir}")
    return 130 if result.reason == "interrupted" else 1 if result.reason in {"worker_unconfirmed", "failed", "no_active_work"} else 0


def _cmd_stop_team(args: argparse.Namespace) -> int:
    control = project_control_dir(args.project)
    if not (control / "team-status.json").is_file():
        raise FileNotFoundError("项目尚无团队运行现场")
    (control / "STOP").write_text("人类通过stop-team请求停止\n", encoding="utf-8")
    print("已请求团队交回控制；请等待真实终态，不能把请求当作已经停止。")
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
    decide_parser.set_defaults(handler=_cmd_decide)

    worker_parser = subparsers.add_parser("run-worker", help="按当前决定启动或恢复 Codex worker")
    worker_parser.add_argument("--project", required=True)
    worker_parser.set_defaults(handler=_cmd_run_worker)

    step_parser = subparsers.add_parser("step", help="交互式执行 decide 后按确认运行 worker")
    step_parser.add_argument("--project", required=True)
    step_parser.set_defaults(handler=_cmd_step)

    run_parser = subparsers.add_parser("run", help="在本次预算内自动决策、执行及周期观察")
    run_parser.add_argument("--project", required=True)
    run_parser.add_argument("--max-worker-turns", type=int, default=10, help="本次 worker turn 上限，默认 10")
    run_parser.add_argument("--max-seconds", type=float, default=3600, help="本次运行秒数上限，默认 3600")
    run_parser.add_argument("--check-interval", type=float, default=300, help="执行中项目检查间隔秒数，0 关闭，默认 300")
    run_parser.add_argument("--worker-timeout", type=float, default=3600, help="单次 worker 秒数上限，默认 3600")
    run_parser.set_defaults(handler=_cmd_run)

    team_parser = subparsers.add_parser("run-team", help="显式运行最多4个worker的整合研究团队")
    team_parser.add_argument("--project", required=True)
    team_parser.add_argument("--output-dir", required=True, help="项目内新研究目录，各worker在各自目录写入")
    team_parser.add_argument("--max-workers", type=int, default=4)
    team_parser.add_argument("--max-subagents", type=int, default=3)
    team_parser.add_argument("--max-seconds", type=float, default=86400, help="整次运行秒数，默认一天；单worker无固定时限")
    team_parser.add_argument("--check-interval", type=float, default=900, help="项目观察间隔秒数，默认15分钟")
    team_parser.add_argument("--max-iterations", type=int, default=60, help="每次Orchestra只读决策的工具轮数预算")
    team_parser.add_argument("--codex-bin", default="codex", help="Codex可执行文件或本次隔离包装入口")
    team_parser.set_defaults(handler=_cmd_run_team)

    stop_parser = subparsers.add_parser("stop-team", help="请求团队保存现场并停止，不重启旧任务")
    stop_parser.add_argument("--project", required=True)
    stop_parser.set_defaults(handler=_cmd_stop_team)

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
