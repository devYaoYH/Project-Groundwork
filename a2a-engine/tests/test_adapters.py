from a2a_engine.adapters import AdapterDescriptor, AdapterRegistry


def test_adapter_registry_is_named_and_kind_scoped():
    registry = AdapterRegistry()
    registry.register(
        AdapterDescriptor(name="local.in_process", kind="communication", capabilities={"broadcast"}),
        lambda config: {"kind": "local", **config},
    )

    assert registry.descriptor("communication", "local.in_process").capabilities == {"broadcast"}
    assert registry.create("communication", "local.in_process", {"turns": 2}) == {
        "kind": "local", "turns": 2,
    }
    assert [item.name for item in registry.list("communication")] == ["local.in_process"]


def test_live_builtin_factories_and_runtime_aware_resolution(monkeypatch):
    import pytest
    from a2a_engine.adapters import adapters, resolve_bindings
    from a2a_engine.comm import CommRouter, Topology

    sentinel = object()
    monkeypatch.setattr("a2a_engine.llm.factory.make_llm_client", lambda config: sentinel)
    assert adapters.create("model", "engine.llm", {"model": "mock"}) is sentinel
    assert adapters.create("model", "remote", {"runtime_context": object(), "client_factory": lambda: sentinel}) is sentinel
    topology = Topology.from_communication(None, seats=[0, 1], phases={"CHEAP_TALK"}, default_send_phases={"CHEAP_TALK"})
    assert isinstance(adapters.create("communication", "local.in_process", {"topology": topology}), CommRouter)
    assert isinstance(adapters.create("communication", "mcp.http", {"topology": topology, "runtime_context": object()}), CommRouter)
    specs = [{"type": "scripted"}, {"type": "llm", "runtime": "external"}]
    assert resolve_bindings({}, specs)["models"] == {"0": "engine.llm", "1": "remote"}
    for config in ({"adapter_bindings": {"communication": "local.in_process"}}, {"adapter_bindings": {"model": "remote"}}):
        with pytest.raises(ValueError, match="incompatible"):
            resolve_bindings(config, specs)
    with pytest.raises(ValueError, match="live runtime"):
        adapters.create("model", "remote", {})
