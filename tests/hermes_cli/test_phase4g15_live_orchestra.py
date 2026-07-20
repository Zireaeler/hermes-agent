from types import SimpleNamespace

from hermes_cli import phase4g15_live_orchestra as phase4g15


def test_run_turn_forwards_live_lifecycle_callbacks(monkeypatch, tmp_path):
    observed = {}
    completed = []

    def fake_run_app_server_turn(**kwargs):
        observed.update(kwargs)
        kwargs["complete_turn"]("thread-1", "turn-1")
        return SimpleNamespace(
            status="completed",
            error=None,
            final_text=(
                '{"status":"completed","observed_contract":"contract-v2",'
                '"result_value":"contract-v2","consumed_directive_ids":[],'
                '"verification":"passed"}'
            ),
            thread_id="thread-1",
            turn_id="turn-1",
            accepted_delivery_ids=[],
        )

    monkeypatch.setattr(phase4g15, "run_app_server_turn", fake_run_app_server_turn)
    callback = lambda thread, turn: completed.append((thread, turn))
    receipt, transport = phase4g15._run_turn(
        workspace=tmp_path,
        codex_home=tmp_path,
        model="test-model",
        prompt="controlled",
        timeout=5,
        complete_turn=callback,
    )

    assert observed["complete_turn"] is callback
    assert completed == [("thread-1", "turn-1")]
    assert receipt["status"] == "completed"
    assert transport["thread_id"] == "thread-1"
