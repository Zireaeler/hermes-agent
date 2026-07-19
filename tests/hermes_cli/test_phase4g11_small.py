from __future__ import annotations

import json
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import phase4g11_small as small
from hermes_cli.worker_lanes import clear_worker_lanes, get_worker_lane


def test_phase4g11_small_fixture_has_isolated_contribution_topology(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    small._write_small_repository(workspace)
    assert (workspace / ".gitignore").read_text(encoding="utf-8") == (
        "__pycache__/\n*.py[cod]\n"
    )
    config = small.SmallRunConfig(root=tmp_path / "run")

    small._register_lane(config)
    try:
        kb.init_db()
        with kb.connect() as conn:
            job_id, structure_event_id = small._create_job(conn, config, workspace)
            rows = conn.execute(
                "SELECT * FROM execution_nodes WHERE job_id = ? ORDER BY node_key",
                (job_id,),
            ).fetchall()
            nodes = {row["node_key"]: row for row in rows}

            assert set(nodes) == {
                "parser-contract",
                "pipeline-integration",
                "renderer-contract",
            }
            assert nodes["pipeline-integration"]["state"] == "waiting_dependency"
            for key, expected_scope in (
                ("parser-contract", "src/token_parser.py"),
                ("renderer-contract", "src/token_renderer.py"),
            ):
                node = nodes[key]
                contract = json.loads(node["constraints_json"])["contract"]
                metadata = json.loads(node["metadata_json"])
                assert node["state"] == "ready"
                assert contract["workspace_mode"] == "isolated_worktree"
                assert contract["declared_write_scope"] == [expected_scope]
                assert metadata["non_authoritative_contribution"] is True
                assert metadata["contribution_to_node_key"] == "pipeline-integration"

            patch = json.loads(
                conn.execute(
                    "SELECT patch_json FROM graph_patches WHERE job_id = ?",
                    (job_id,),
                ).fetchone()[0]
            )
            serialized_patch = json.dumps(patch, sort_keys=True)
            assert patch["decomposition"]["justifications"][0]["evidence_refs"] == [
                f"event:{structure_event_id}"
            ]
            assert "api_key" not in serialized_patch
            assert "base_url" not in serialized_patch
            assert not any(op["op"] == "issue_directive" for op in patch["ops"])
    finally:
        clear_worker_lanes()


def test_phase4g11_small_lane_uses_bounded_real_codex_workers():
    clear_worker_lanes()
    try:
        config = small.SmallRunConfig(root=Path("/tmp/phase4g11-test"))
        small._register_lane(config)
        lane = get_worker_lane(config.lane_name)

        assert lane is not None
        assert lane.kind == "codex_cli"
        assert lane.max_concurrency == 2
        assert lane.config == {
            "type": "codex_cli",
            "model": "gpt-5.6-sol",
            "sandbox": "workspace-write",
            "approval": "never",
            "timeout_seconds": 900,
            "json_events": True,
        }
    finally:
        clear_worker_lanes()
