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

## 当前项目规则与执行能力
请按需读取 AGENTS.md 及相关项目规则，确认真实约束、操作权限和人类明确的运行边界；不要把已结束的股票子目标或代理转写的局部完成条件当作新的长期授权。项目决策不等于人类批准，当前中程方向与任务由你根据整体目标和事实维护。
当前 Orchestra 只能读取和搜索仓库；Codex worker 可修改普通文件，但 .git 只读（workspace-write、approval=never）。不要要求 worker 提交或推送；宿主负责已获授权的 Git 交付和 worker 环境不能执行的机械操作。宿主已有权限内的操作可在任务中明确列出，不仅因 worker 沙箱不可用就要求人类重新批准；尚未授权的操作仍须询问。其他权限与资源以实际运行结果为准。

## 当前项目判断
以下内容来自 state.md，是可修订的项目判断而非人类意图，当前可核实事实能够推翻其中假设。更新时分别表达仍有效的能力概况与当前推进方向；已验证的样本覆盖不等于接口可接收范围，关注主题改变也不使既有能力失效。
{state.strip() or '(尚无当前项目判断)'}

## 当前机械决定
{decision.strip() or '(尚未决定)'}

“开始新任务”表示新 task 尚未建立可恢复 thread；`run-worker` 取得新 thread ID 后会把机械决定切换为“继续当前任务”。若仍要执行尚未启动的新 task，应继续选择“开始新任务”。

## 当前 worker 推进说明
以下任务及其中的资格条件由此前 Orchestra 生成，不是新增人类约束。吸收结果时按当前用途重新判断这些条件是否必要；未满足某个条件，先区分它限制当前使用、长期完整覆盖，还是仅限制尚未承诺的用途。
{task.strip() or '(尚无当前任务)'}

## 最近一次 worker 结果
以下只是线索，不自动视为事实；按旧任务条件得出的结论仍受该用途和条件限制。先重新判断自拟阶段目标、路线与验收是否必要，再决定保留、调整或等待；等待理由与恢复条件只覆盖已有依据支持的范围：
{result.strip() or '(尚无 worker 结果)'}

## Git 机械事实
{_render_git_facts(git_facts)}

请从整体目标判断当前主要缺口，简述本任务为何值得优先推进，并根据结果延续、调整或换线。已有测试与运行结果支持能力判断，不单独证明任务优先级；无需每轮比较全部候选或生成额外评审。只按需读取会改变判断的证据，详细历史不默认进入下一轮。严格按系统提示规定的形状输出。"""
