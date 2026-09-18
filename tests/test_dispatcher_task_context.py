from types import SimpleNamespace

from super_agents.agent_store import Store

from openbase_coder_cli.livekit_agent.dispatcher_task_context import (
    dispatcher_disallowed_tools,
    registered_dispatcher_task_context,
)


def test_fresh_dispatcher_uses_registered_owner_directory_and_does_not_affect_worker(
    tmp_path,
):
    store = Store(tmp_path / "state.sqlite3")
    # No dispatcher conversation is needed to recover an existing owner's location.
    session = store.create_session(
        name="cedar", cwd=str(tmp_path / "current-cedar"), command=[]
    )
    store.update_session(session.id, agent_name="Aurora", status="waiting")
    backend = SimpleNamespace(store=store)
    context = registered_dispatcher_task_context(backend, "dispatcher")
    assert str(tmp_path / "current-cedar") in context
    assert session.id in context
    assert "Aurora" in context
    assert registered_dispatcher_task_context(backend, "cedar") is None
    assert dispatcher_disallowed_tools(SimpleNamespace(name="dispatcher")) == (
        "Agent",
        "Task",
    )
    assert dispatcher_disallowed_tools(SimpleNamespace(name="cedar")) == ()
