"""活动执行期间，其他入口不能改写同一项目。"""

from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys

import pytest

from hermes_cli.orchestra_v1 import decide
from hermes_cli.orchestra_v1_control import initialize_project
from hermes_cli.orchestra_v1_lock import project_lock
from hermes_cli.orchestra_v1_worker import run_worker


def test_project_lock_is_reentrant_but_rejects_other_thread(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "hermes"
    with project_lock(project, home):
        initialize_project(project, "目标", hermes_home=home)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run_worker, project, hermes_home=home)
            with pytest.raises(RuntimeError, match="已有活动"):
                future.result()
    with project_lock(project, home):
        pass


def test_other_process_blocks_decision_worker_and_forced_init(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "hermes"
    control = initialize_project(project, "目标", hermes_home=home)
    script = """
import sys
from hermes_cli.orchestra_v1_lock import project_lock
with project_lock(sys.argv[1], sys.argv[2]):
    print('ready', flush=True)
    sys.stdin.readline()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(project), str(home)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        for operation in (
            lambda: decide(project, hermes_home=home),
            lambda: run_worker(project, hermes_home=home),
            lambda: initialize_project(
                project, "替换", hermes_home=home, overwrite=True
            ),
        ):
            with pytest.raises(RuntimeError, match="已有活动"):
                operation()
        assert (control / "intent.md").read_text() == "目标\n"
    finally:
        child.communicate("exit\n", timeout=5)
    initialize_project(project, "新目标", hermes_home=home, overwrite=True)
