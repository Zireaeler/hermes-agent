"""通过真实 stdio 子进程验证自动循环及恢复链路，无需模型认证。"""

import json
import pytest

from hermes_cli.orchestra_v1_codex import _run_codex_turn
from hermes_cli.orchestra_v1_control import initialize_project
from hermes_cli.orchestra_v1_runtime import run_project
from tests.test_orchestra_v1 import FakeAgent, init_git_repo, orchestra_output


@pytest.mark.parametrize("observe", [False, True])
def test_auto_loop_with_real_jsonl_transport_and_resumed_compaction(tmp_path, observe):
    server = tmp_path / "fake-codex"
    log = tmp_path / "requests.jsonl"
    server.write_text(
        """#!/usr/bin/env python3
import json
import sys

def send(value):
    print(json.dumps(value), flush=True)

for line in sys.stdin:
    request = json.loads(line)
    with open(sys.argv[-1], 'a') as output:
        output.write(json.dumps(request) + '\\n')
    if 'id' not in request:
        continue
    method = request['method']
    params = request.get('params', {})
    result = {}
    if method.startswith('thread/') and method != 'thread/compact/start':
        result = {'thread': {'id': params.get('threadId', 'thread-stdio')}}
    if method == 'turn/start':
        result = {'turn': {'id': 'turn-business'}}
    send({'id': request['id'], 'result': result})
    if method == 'thread/compact/start':
        send({'method': 'turn/started', 'params': {'threadId': 'thread-stdio', 'turn': {'id': 'compact'}}})
        for event in ('item/started', 'item/completed'):
            send({'method': event, 'params': {'threadId': 'thread-stdio', 'turnId': 'compact', 'item': {'id': 'compact-item', 'type': 'contextCompaction'}}})
        send({'method': 'turn/completed', 'params': {'turn': {'id': 'compact', 'status': 'completed'}}})
    if method == 'turn/start' and sys.argv[-2] == 'normal':
        send({'method': 'item/completed', 'params': {'item': {'type': 'agentMessage', 'text': 'stdio 已完成'}}})
        send({'method': 'turn/completed', 'params': {'turn': {'id': 'turn-business', 'status': 'completed'}}})
    if method == 'turn/interrupt':
        send({'method': 'turn/completed', 'params': {'turn': {'id': 'turn-business', 'status': 'interrupted'}}})
""",
        encoding="utf-8",
    )
    server.chmod(0o755)
    project = tmp_path / "project"
    init_git_repo(project)
    home = tmp_path / "hermes"
    initialize_project(project, "目标", hermes_home=home)
    decisions = iter([
        "开始新任务",
        "开始新任务" if observe else "继续当前任务",
        "停止",
    ])
    requests = []

    def worker(**kwargs):
        return _run_codex_turn(
            **kwargs,
            codex_bin=str(server),
            app_server_extra_args=["observe" if observe else "normal", str(log)],
        )

    result = run_project(
        project,
        hermes_home=home,
        worker_runner=worker,
        agent_factory=lambda **kw: FakeAgent(
            orchestra_output(next(decisions)), requests, **kw
        ),
        check_interval=0.01 if observe else 300,
    )
    assert result.reason == "停止"
    assert result.worker_turns == (1 if observe else 2)
    methods = [json.loads(line)["method"] for line in log.read_text().splitlines()]
    assert methods.count("thread/start") == 1
    if observe:
        assert methods.count("turn/interrupt") == 1
        assert "状态：interrupted" in requests[-1]
    else:
        assert "stdio 已完成" in requests[-1]
        assert methods.count("thread/resume") == 1
        assert methods.index("thread/compact/start") < len(methods) - 1
        assert methods.count("turn/start") == 2
