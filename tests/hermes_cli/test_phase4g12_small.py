from __future__ import annotations

import json
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import phase4g12_small as small
from hermes_cli.worker_lanes import clear_worker_lanes, get_worker_lane


def test_phase4g12_fixture_omits_dynamic_responsibility(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    small._write_small_repository(workspace)
    config = small.SmallRunConfig(root=tmp_path / "run")

    small._register_lane(config)
    try:
        kb.init_db()
        with kb.connect() as conn:
            job_id, _, initial_node_keys = small._create_job(
                conn,
                config,
                workspace,
            )
            assert initial_node_keys == [
                "parser-contract",
                "pipeline-integration",
                "renderer-contract",
            ]
            assert conn.execute(
                """
                SELECT 1 FROM execution_nodes
                 WHERE job_id = ? AND node_key = 'legacy-token-adapter'
                """,
                (job_id,),
            ).fetchone() is None
            parser = conn.execute(
                """
                SELECT description FROM execution_nodes
                 WHERE job_id = ? AND node_key = 'parser-contract'
                """,
                (job_id,),
            ).fetchone()
            assert "responsibility candidate" in parser["description"]
            patch = json.loads(
                conn.execute(
                    """
                    SELECT patch_json FROM graph_patches
                     WHERE job_id = ? AND status = 'applied'
                     ORDER BY created_at, id LIMIT 1
                    """,
                    (job_id,),
                ).fetchone()[0]
            )
            assert not any(
                op.get("source_responsibility_ref")
                for op in patch.get("ops") or []
            )
    finally:
        clear_worker_lanes()


def test_phase4g12_lane_has_capacity_for_dynamic_child():
    clear_worker_lanes()
    try:
        config = small.SmallRunConfig(root=Path("/tmp/phase4g12-test"))
        small._register_lane(config)
        lane = get_worker_lane(config.lane_name)

        assert lane is not None
        assert lane.kind == "codex_cli"
        assert lane.max_concurrency == 3
        assert lane.config["model"] == "gpt-5.6-sol"
        assert lane.config["sandbox"] == "workspace-write"
        assert lane.config["approval"] == "never"
    finally:
        clear_worker_lanes()
