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
