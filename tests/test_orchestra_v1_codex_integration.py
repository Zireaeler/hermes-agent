from __future__ import annotations

import subprocess

import pytest

from hermes_cli.orchestra_v1_codex import run_codex_turn


@pytest.mark.integration
@pytest.mark.timeout(600)
def test_real_codex_starts_compacts_and_resumes_in_new_process(tmp_path):
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)

    first = run_codex_turn(
        prompt="Create first.txt containing exactly first followed by a newline, then report completion.",
        workspace=tmp_path,
        timeout_seconds=180,
    )
    assert first.status == "completed", first.error
    assert first.thread_id
    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "first\n"

    second = run_codex_turn(
        prompt="Continue the same task by creating second.txt containing exactly second followed by a newline, then report completion.",
        workspace=tmp_path,
        resume_thread_id=first.thread_id,
        compact_before_turn=True,
        timeout_seconds=180,
    )
    assert second.status == "completed", second.error
    assert second.thread_id == first.thread_id
    assert (tmp_path / "second.txt").read_text(encoding="utf-8") == "second\n"
    assert second.final_text
