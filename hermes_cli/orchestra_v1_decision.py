"""Orchestra v1 的决定协议与请求组装。"""

from __future__ import annotations

from dataclasses import dataclass
import re

from hermes_cli.orchestra_v1_control import GitFacts


DECISIONS = (
    "继续当前任务",
    "开始新任务",
    "等待",
    "询问人类",
    "停止",
)
RUN_DECISIONS = {"继续当前任务", "开始新任务"}

_DECISION_RE = re.compile(r"^决定\s*[:：]\s*(.+?)\s*$", re.MULTILINE)
_STATE_HEADING_RE = re.compile(r"^#\s+项目状态\s*$", re.MULTILINE)
_TASK_HEADING_RE = re.compile(
    r"^#\s+worker 推进说明\s*$",
    re.MULTILINE | re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedDecision:
    decision: str
    state: str
    task: str


def parse_orchestra_output(output: str) -> ParsedDecision:
    state_match = _STATE_HEADING_RE.search(output)
    task_match = _TASK_HEADING_RE.search(output)
    if state_match is None or task_match is None:
        raise ValueError("Orchestra 输出缺少必要标题")
    if state_match.start() >= task_match.start():
        raise ValueError("Orchestra 输出段落顺序错误")
    decision_match = _DECISION_RE.fullmatch(output[: state_match.start()].strip())
    if decision_match is None:
        raise ValueError("Orchestra 输出开头必须只包含一行决定")

    decision = decision_match.group(1).strip()
    if decision not in DECISIONS:
        raise ValueError(f"未知决定：{decision}")
    state = output[state_match.end() : task_match.start()].strip()
    task = output[task_match.end() :].strip()
    if not state or not task:
        raise ValueError("Orchestra 输出的项目状态或 worker 推进说明为空")
    return ParsedDecision(decision=decision, state=state, task=task)


def _render_git_facts(facts: GitFacts) -> str:
    return f"""仓库根目录：{facts.root}
当前分支：{facts.branch}
当前提交：{facts.commit}

git status --short：
{facts.status_short or '(clean)'}

git diff --stat：
{facts.diff_stat or '(empty)'}

最近提交：
{facts.recent_commits or '(none)'}"""


def build_decision_request(
    *,
    intent: str,
    state: str,
    task: str,
    result: str,
    decision: str,
    git_facts: GitFacts,
) -> str:
    return f"""请完成一次全新的 Orchestra 项目决策轮。

## 人类意图（唯一来源，只读）
以下内容来自 intent.md，只有人类可以修改。不得用项目状态、worker 结果或代理自建目标替代或扩张它。
{intent.strip() or '(intent.md 为空)'}

## 当前项目判断
以下内容来自 state.md，只是上一轮 Orchestra 对当前项目的可重写判断，不是人类意图。
{state.strip() or '(尚无当前项目判断)'}

## 当前机械决定
{decision.strip() or '(尚未决定)'}

“开始新任务”表示新 task 尚未建立可恢复 thread；`run-worker` 取得新 thread ID 后会把机械决定切换为“继续当前任务”。若仍要执行尚未启动的新 task，应继续选择“开始新任务”。

## 当前 worker 推进说明
{task.strip() or '(尚无当前任务)'}

## 最近一次 worker 结果
以下只是线索，不自动视为事实：
{result.strip() or '(尚无 worker 结果)'}

## Git 机械事实
{_render_git_facts(git_facts)}

请按需使用只读文件与搜索工具核实会改变路线的信息，然后严格按系统提示规定的形状输出。不要提及或依赖任何上一轮 Orchestra 原始输出。"""
