"""Offline contracts for private image input, provider mapping and replay."""
import asyncio
import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import yaml

from a2a_engine.agent import LLMAgent
from a2a_engine.content import AssetStore, ImagePart, inline_media, media_metadata, observation_content, provider_messages
from a2a_engine.items import ItemBank
from a2a_engine.llm import api
from a2a_engine.modalities import require_modalities, validate_config
from a2a_engine.schemas import EpisodeConfigBase, EpisodeTrace
from a2a_engine.tracing import EventLog, read_episode, write_episode

CAPS = {"input": ["text", "image"], "evidence": "offline fixture model deployment contract"}
# Two deterministic 1x1 PNGs, no study stimulus bank.
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII=")


@pytest.fixture
def bank(tmp_path):
    store = AssetStore(tmp_path / "assets")
    image = store.put(PNG, "image/png")
    path = tmp_path / "items.jsonl"
    path.write_text(json.dumps({"item_id": "canonical-private-id", "params": {"content": [image.model_dump(exclude_none=True)]},
                                "oracle_result": "private-evaluator-answer"}) + "\n")
    return ItemBank.load(path)


def message(bank):
    return [{"role": "user", "content": [{"type": "text", "text": "Inspect your shape"},
                                           bank.assets[0].model_dump(exclude_none=True)]}]


def test_text_compatibility_and_private_content_projection(bank):
    agent = LLMAgent(api.LLMClient(model="fixture"))
    assert agent.build_messages({"legacy": "text"}, {}) == [{"role": "user", "content": "{'legacy': 'text'}"}]
    observation = {"content": message(bank)[0]["content"], "item_id": "canonical-private-id", "oracle_result": "hidden"}
    rendered = agent.build_messages(observation, {})
    assert rendered[0]["content"] == observation["content"]
    assert "canonical-private-id" not in json.dumps(rendered)
    assert "hidden" not in json.dumps(rendered)


def test_asset_packaging_integrity_and_provenance(bank, tmp_path):
    packaged = bank.package(tmp_path / "container" / "release")
    assert packaged.item_bank_sha256 == bank.item_bank_sha256
    assert packaged.asset_store.read(packaged.assets[0]) == PNG
    assert bank.item_bank_sha256 != hashlib.sha256(bank.path.read_bytes()).hexdigest()
    (packaged.asset_store.root / packaged.assets[0].sha256).write_bytes(PNG + b"changed")
    with pytest.raises(ValueError, match="digest mismatch"):
        ItemBank.load(packaged.path, expected_sha256=bank.item_bank_sha256)
    with pytest.raises(ValueError, match="digest mismatch"):
        validate_config({"asset_root": str(packaged.asset_store.root), "image_assets": [packaged.assets[0].model_dump()]})


@pytest.mark.parametrize("mime", ["image/jpeg", "image/webp"])
def test_mime_and_digest_are_verified(bank, mime):
    wrong = bank.assets[0].model_copy(update={"mime_type": mime})
    with pytest.raises(ValueError, match="MIME"):
        bank.asset_store.read(wrong)
    with pytest.raises(ValueError, match="disagree"):
        ImagePart(asset="asset://" + "0" * 64, sha256=bank.assets[0].sha256, mime_type="image/png")


@pytest.mark.parametrize("format", ["openai", "anthropic", "vertexai", "vertexai_anthropic", "vertexai_openai"])
def test_provider_conversion_and_local_wire_parity(bank, format):
    original = message(bank)
    local = provider_messages(original, format, str(bank.asset_store.root))
    remote = provider_messages(inline_media(original, str(bank.asset_store.root)), format)
    assert local == remote
    assert "data" not in original[0]["content"][1]
    block = local[0]["content"][1]
    if format in {"anthropic", "vertexai_anthropic"}:
        assert base64.b64decode(block["source"]["data"]) == PNG
    elif format == "vertexai":
        assert base64.b64decode(block["data"]) == PNG
    else:
        assert base64.b64decode(block["image_url"]["url"].split(",", 1)[1]) == PNG
    assert "canonical-private-id" not in json.dumps(local)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("format", ["openai", "anthropic", "vertexai_anthropic", "vertexai_openai", "vertexai"])
def test_paid_entrypoints_admit_then_convert_before_network(bank, monkeypatch, stream, format):
    calls = []
    function = ("_stream_" if stream else "_oneshot_") + format
    if format == "vertexai_openai":
        function = ("_stream_" if stream else "_oneshot_") + "openai"
        monkeypatch.setattr(api, "_vertexai_openai_base_url", lambda **kw: "http://fixture")
        monkeypatch.setattr(api, "_get_vertexai_access_token", lambda *a: "fixture")
    def fake(*args, **kwargs):
        converted = args[3] if format in {"openai", "anthropic", "vertexai_openai"} else args[1]
        calls.append(converted)
        if stream:
            return {"text": "ok"}
        return ("ok", None) if format.startswith("vertexai") and format != "vertexai_openai" else "ok"
    monkeypatch.setattr(api, function, fake)
    invoke = api.call_llm_streaming if stream else api.call_llm_oneshot
    kwargs = dict(api_format=format, api_base="http://fixture", api_key="", model="fixture", messages=message(bank),
                  max_tokens=20, temperature=0, asset_root=str(bank.asset_store.root))
    with pytest.raises(ValueError, match="explicit verified"):
        invoke(**kwargs)
    assert calls == []
    invoke(**kwargs, capabilities=CAPS)
    assert calls == [provider_messages(message(bank), format, str(bank.asset_store.root))]


def test_anthropic_stream_cache_keeps_image_block(bank, monkeypatch):
    captured = []
    class Response:
        def __iter__(self):
            return iter([])
        def close(self): pass
    def send(request, **kwargs):
        captured.append(json.loads(request.data))
        return Response()
    monkeypatch.setattr(api.urllib.request, "urlopen", send)
    before = message(bank)
    api.call_llm_streaming("anthropic", "http://fixture", "", "fixture", before, 20, 0,
                           capabilities=CAPS, asset_root=str(bank.asset_store.root))
    content = captured[0]["messages"][0]["content"]
    assert content[1]["type"] == "image" and content[1]["cache_control"] == {"type": "ephemeral"}
    assert content[0]["text"] == "Inspect your shape"
    assert "data" not in before[0]["content"][1]


def test_vertex_sdk_mapping_uses_binary_part(bank, monkeypatch):
    class Part:
        def __init__(self, **kw): self.values = kw
        @classmethod
        def from_bytes(cls, **kw): return cls(**kw)
    monkeypatch.setattr(api, "_google_genai", SimpleNamespace(types=SimpleNamespace(Part=Part, Content=lambda **kw: kw)))
    system, contents = api._messages_to_vertexai(provider_messages(message(bank), "vertexai", str(bank.asset_store.root)))
    assert system is None
    assert contents[0]["parts"][1].values == {"data": PNG, "mime_type": "image/png"}


@pytest.mark.parametrize("caps,adapter,runtime", [({}, "openai", "in_process"),
    ({"input": ["text", "image"]}, "openai", "in_process"), (CAPS, "unsupported", "in_process"), (CAPS, "openai", "unsupported")])
def test_unknown_capabilities_adapters_and_transports_reject(caps, adapter, runtime):
    with pytest.raises(ValueError):
        require_modalities(["image"], caps, api_format=adapter, runtime=runtime)


def test_persisted_replay_and_telemetry_do_not_dump_bytes(bank, tmp_path):
    wire = inline_media(message(bank), str(bank.asset_store.root))
    log = EventLog()
    log.append("observation", {"seat": 0, "messages": wire})
    trace = EpisodeTrace(episode_uid="media", config=EpisodeConfigBase(environment_id="fixture", num_agents=2), events=log.all())
    path = write_episode(trace, tmp_path / "results")
    persisted = path.read_text()
    assert base64.b64encode(PNG).decode() not in persisted
    assert base64.b64encode(PNG).decode() not in api._messages_to_genai(wire)
    replay = read_episode(path).events[0].data["messages"]
    assert provider_messages(replay, "openai", str(bank.asset_store.root)) == provider_messages(wire, "openai")
    assert bank.assets[0].sha256 in persisted


def test_provider_content_capture_remains_optional(bank, monkeypatch):
    attrs = {}
    monkeypatch.setattr(api, "get_tracer", lambda: SimpleNamespace(start_span=lambda *a, **k: SimpleNamespace(set_attribute=lambda k, v: attrs.update({k: v}))))
    monkeypatch.setenv("A2A_CAPTURE_CONTENT", "false")
    api._start_chat_span("fixture", "", message(bank), 0, 20, False)
    assert "gen_ai.input.messages" not in attrs


def test_typed_release_and_role_compilation_reject_incompatible_seat(bank, tmp_path):
    from a2a_engine.compiler import compile, validate
    from a2a_engine.design import Design
    from a2a_engine.environment import load_release_declaration
    raw = {"id": "fixture", "environment_id": "fixture", "required_modalities": ["text"],
           "roles": [{"id": "viewer", "required_modalities": ["image"]}],
           "item_policy": {"mode": "enumerate", "bank_path": bank.path.name, "item_bank_sha256": bank.item_bank_sha256}}
    release_path = tmp_path / "release.yaml"
    release_path.write_text(yaml.safe_dump(raw))
    release = load_release_declaration(release_path)
    design = Design(release="fixture", units={"episodes_per_cell": 1}, seed={"root": 1},
                    roster=[{"id": "viewer", "role": "viewer", "binding": "arbitrary-vision-name", "api_format": "openai"}])
    assert any(issue.path.endswith("capabilities") for issue in validate(design, release, bank))
    design.roster[0].capabilities = CAPS
    config = compile(design, release, bank, experiment_id="fixture", experiment_name="fixture").preview_episode_config
    assert config["agents"][0]["required_modalities"] == ["image", "text"]
    assert config["provenance"]["image_assets"][0]["sha256"] == bank.assets[0].sha256
    config["agents"][0]["capabilities"] = {}
    with pytest.raises(ValueError, match=r"agents\[0\]"):
        validate_config(config)


def test_researcher_asset_inspection_is_verified_and_restricted(bank):
    from local_stack.control_plane import ControlPlane
    cp = ControlPlane.__new__(ControlPlane)
    cp._item_bank = lambda environment_id: bank
    assert cp.environment_asset("fixture", bank.assets[0].sha256) == (PNG, "image/png")
    with pytest.raises(KeyError):
        cp.environment_asset("fixture", "0" * 64)


@pytest.mark.parametrize("protocol", [1, 2])
def test_private_seat_images_survive_real_remote_transport(bank, remote_harness, remote_harness_v2, protocol):
    from a2a_engine.remote.contract import TurnInvocation, TurnInvocationV2
    from a2a_engine.remote.dispatch import TurnDispatcher, TurnDispatcherV2
    harness = (remote_harness if protocol == 1 else remote_harness_v2)(seats=(0, 1), join=False)
    class Policy:
        capabilities = CAPS
        supported_input_modalities = {"text", "image"}
        def __init__(self): self.seen = []
        async def calls(self, invocation):
            self.seen.append(invocation)
            return []
    policies = [Policy(), Policy()]
    second_image = bank.asset_store.put(PNG + b"second deterministic image fixture", "image/png")
    images = [bank.assets[0], second_image]
    for seat_id in (0, 1):
        seat = harness.episode.seats[seat_id]
        seat.required_modalities = {"image", "text"}
        seat.asset_root = str(bank.asset_store.root)
        url = f"{harness.io.base_url}/episodes/{harness.episode.episode_id}/join"
        if protocol == 1:
            runtime = harness.runtimes[seat_id]
            runtime.policy = policies[seat_id]
            harness.callbacks[seat_id].run(runtime.join(url, harness.callbacks[seat_id].base_url))
            dispatcher = TurnDispatcher(harness.io)
            mcp = runtime.admission.mcp
        else:
            admission = harness.callback.run(harness.runtime.join(url, harness.callback.base_url, seat.ticket, policy=policies[seat_id]))
            dispatcher = TurnDispatcherV2(harness.io, harness.environment.registry.lifecycle)
            mcp = admission.mcp
        for kind in ("register", "round_start"):
            extra = {"environment_id": "calendar", "attempt_id": harness.episode.attempt_id,
                     "operation": "register" if kind == "register" else "observe"} if protocol == 2 else {}
            invocation = (TurnInvocationV2 if protocol == 2 else TurnInvocation)(
                episode_id=harness.episode.episode_id, seat=seat_id, turn_id=f"{seat_id}-{kind}", kind=kind,
                mcp=mcp, deadline=datetime.now(timezone.utc) + timedelta(seconds=10),
                observation={} if kind == "register" else {"content": [images[seat_id].model_dump(exclude_none=True)]}, **extra)
            dispatcher.dispatch(seat, invocation)
        assert policies[seat_id].seen[0].observation == {}
        received = policies[seat_id].seen[1].observation
        local = [{"role": "user", "content": [images[seat_id].model_dump(exclude_none=True)]}]
        remote = [{"role": "user", "content": observation_content(received)}]
        assert provider_messages(local, "openai", str(bank.asset_store.root)) == provider_messages(remote, "openai")
        assert images[1-seat_id].sha256 not in json.dumps(received)
        assert "canonical-private-id" not in json.dumps(received)
        assert "private-evaluator-answer" not in json.dumps(received)


def test_remote_admission_rejects_unknown_image_capability_without_consuming_ticket(remote_harness):
    from a2a_engine.remote.contract import JoinRequest, PROTOCOL_VERSION
    from a2a_engine.remote.admission import join
    harness = remote_harness(join=False)
    seat = harness.episode.seats[0]
    seat.required_modalities = {"text", "image"}
    request = JoinRequest(join_ticket=seat.ticket, callback_url=harness.callbacks[0].base_url, protocol_versions=[PROTOCOL_VERSION])
    with pytest.raises(ValueError, match="input modalities"):
        asyncio.run(join(harness.episode, request, harness.io.base_url))
    assert not seat.consumed


def test_manifest_and_sqlite_preserve_asset_identity_without_bytes(bank, tmp_path):
    from a2a_engine.manifest import EpisodeManifest, config_hash
    from a2a_engine.storage.sqlite import SQLiteEpisodeStore
    config = {"environment_id": "fixture", "num_agents": 1, "episode_id": "media", "image_assets": [bank.assets[0].model_dump()],
              "asset_root": str(bank.asset_store.root), "required_modalities": ["text", "image"],
              "agents": [{"type": "llm", "model": "fixture", "capabilities": CAPS, "required_modalities": ["text", "image"]}]}
    manifest = EpisodeManifest.from_run(config=config, experiment_name="fixture", cell_id="cell", episode_idx=0, episode_uid="media")
    assert manifest.image_assets[0]["sha256"] == bank.assets[0].sha256
    assert set(manifest.agents[0].capabilities["input"]) == {"text", "image"}
    assert config_hash(config) == config_hash({**config, "asset_root": "/another/container/assets"})
    wire = inline_media(message(bank), str(bank.asset_store.root))
    trace = EpisodeTrace(episode_uid="media", config=config, events=[{"type": "observation", "data": {"seat": 0, "messages": wire}}])
    store = SQLiteEpisodeStore(path=tmp_path / "traces.db")
    store.put_episode(trace, manifest)
    replay = store.get_episode("media")
    assert replay.events[0].data["messages"] == message(bank)
    assert base64.b64encode(PNG).decode() not in replay.model_dump_json()


def test_image_body_budget_does_not_relax_text_metadata_or_completions(bank):
    from a2a_engine.remote.contract import MAX_BODY_BYTES, valid_body_size
    image = bank.asset_store.put(PNG + b"fixture-padding" * 25000, "image/png")
    invocation = {"kind": "round_start", "observation": {"content": [inline_media(image.model_dump(), str(bank.asset_store.root))]}}
    body = json.dumps(invocation).encode()
    assert len(body) > MAX_BODY_BYTES and valid_body_size(body)
    invocation["observation"]["unbounded-text"] = "x" * MAX_BODY_BYTES
    assert not valid_body_size(json.dumps(invocation).encode())
    assert not valid_body_size(json.dumps({"reflection": {"data": "x" * MAX_BODY_BYTES}}).encode())
    invocation["kind"] = "register"
    assert not valid_body_size(json.dumps(invocation).encode())


def test_legacy_prompt_only_policies_do_not_advertise_image_support():
    from a2a_engine.modalities import policy_modalities
    policy = SimpleNamespace(model=api.LLMClient(model="fixture", capabilities=CAPS))
    assert policy_modalities(policy) == {"text"}
    policy.supported_input_modalities = {"text", "image"}
    assert policy_modalities(policy) == {"text", "image"}


def test_default_agent_rejects_override_that_drops_image_before_call(bank):
    class TextOnly(LLMAgent):
        def build_messages(self, observation, tools):
            return [{"role": "user", "content": str(observation)}]
    agent = TextOnly(api.LLMClient(model="fixture", capabilities=CAPS))
    with pytest.raises(ValueError, match="discarded"):
        asyncio.run(agent.act({"content": message(bank)[0]["content"]}, {}))


def test_unstructured_images_and_image_registration_fail_before_transport(bank):
    from a2a_engine.remote.dispatch import prepare_invocation
    from a2a_engine.remote.contract import TurnInvocation
    with pytest.raises(ValueError, match="observation.content"):
        observation_content({"private_image": bank.assets[0].model_dump()})
    seat = SimpleNamespace(input_modalities={"text", "image"}, asset_root=str(bank.asset_store.root))
    invocation = TurnInvocation(episode_id="fixture", seat=0, turn_id="register", kind="register",
                                deadline=datetime.now(timezone.utc) + timedelta(seconds=10),
                                observation={"content": [bank.assets[0].model_dump()]})
    with pytest.raises(ValueError, match="registration"):
        prepare_invocation(seat, invocation)


def test_local_process_and_compose_bindings_keep_capabilities(bank, monkeypatch):
    from a2a_engine.remote.seats import LocalProcessLauncher
    from a2a_agent.service import descriptor_policy
    from a2a_engine.remote.network import NetworkPolicy
    captured = {}
    def spawn(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace()
    monkeypatch.setattr("a2a_engine.remote.seats.subprocess.Popen", spawn)
    spec = {"type": "llm", "model": "fixture", "api_format": "openai", "capabilities": CAPS,
            "required_modalities": ["text", "image"], "asset_root": "/private/environment/assets"}
    LocalProcessLauncher("http://127.0.0.1:1/join", "fixture-ticket", spec)
    model_config = json.loads(captured["env"]["A2A_MODEL_CONFIG"])
    assert model_config["capabilities"] == CAPS
    assert "asset_root" not in model_config
    strategy = descriptor_policy(
        {"environment_id": "calendar", "policy": {k: spec[k] for k in ("type", "model", "api_format", "capabilities")}},
        bank.path.parent, NetworkPolicy())
    assert strategy.model.capabilities.input == {"text", "image"}


def test_dataset_retains_image_message_parts_without_promoting_private_observations(bank):
    from a2a_engine.dataset import EpisodeRecord
    trace = EpisodeTrace(episode_uid="media", config=EpisodeConfigBase(environment_id="fixture", num_agents=2), events=[
        {"type": "observation", "data": {"seat": 0, "content": message(bank)[0]["content"]}},
        {"type": "message", "data": {"speaker": "sender", "content": message(bank)[0]["content"]}},
    ])
    record = EpisodeRecord(trace=trace)
    assert len(record.messages) == 1
    assert record.messages[0].text == "Inspect your shape"
    assert record.messages[0].content[1].sha256 == bank.assets[0].sha256


def test_registered_environment_requirements_cannot_be_downgraded_by_loose_config(monkeypatch):
    from a2a_engine.environment import ReleaseDeclaration, RoleConfig
    declaration = ReleaseDeclaration(environment_id="image_fixture", roles=[RoleConfig(id="viewer", required_modalities={"text", "image"})])
    monkeypatch.setattr("a2a_engine.registry.get_environment_spec", lambda name: SimpleNamespace(declaration=declaration))
    with pytest.raises(ValueError, match="explicit verified"):
        validate_config({"environment_id": "image_fixture", "required_modalities": ["text"], "agents": [{"model": "gpt-name"}]})


def test_release_modality_serialization_is_stable_across_orderings():
    from a2a_engine.environment import ReleaseDeclaration, RoleConfig
    first = ReleaseDeclaration(environment_id="fixture", required_modalities={"text", "image"},
                               roles=[RoleConfig(id="viewer", required_modalities={"image", "text"})])
    second = ReleaseDeclaration(environment_id="fixture", required_modalities={"image", "text"},
                                roles=[RoleConfig(id="viewer", required_modalities={"text", "image"})])
    assert first.content_sha256() == second.content_sha256()
    assert first.model_dump(mode="json")["required_modalities"] == ["image", "text"]


def test_typed_yaml_pool_images_resolve_relative_to_release_and_relocate(bank, tmp_path):
    from a2a_engine.experiment import load_experiment
    release = {"id": "fixture", "environment_id": "fixture", "engine": {"environment_id": "fixture", "defaults": {"num_agents": 1}},
               "roles": [{"id": "viewer", "required_modalities": ["text", "image"]}],
               "item_policy": {"mode": "enumerate", "bank_path": "items.jsonl", "item_bank_sha256": bank.item_bank_sha256}}
    (tmp_path / "release.yaml").write_text(yaml.safe_dump(release))
    experiment = {"name": "fixture", "release": "release.yaml", "participants": ["visual"], "episodes": [{"label": "one"}]}
    (tmp_path / "experiment.yaml").write_text(yaml.safe_dump(experiment))
    (tmp_path / "experiments").mkdir()
    (tmp_path / "experiments" / "agents.yaml").write_text(yaml.safe_dump({"agents": {"visual": {"model": "fixture", "api_format": "openai", "capabilities": CAPS}}}))
    plan = load_experiment(tmp_path / "experiment.yaml")
    assert set(plan.defaults["agents"][0]["required_modalities"]) == {"text", "image"}
    assert provider_messages(message(bank), "openai", plan.defaults["asset_root"])[0]["content"][1]["type"] == "image_url"
    relocated = tmp_path / "container"
    bank.package(relocated)
    for filename in ("release.yaml", "experiment.yaml"):
        (relocated / filename).write_bytes((tmp_path / filename).read_bytes())
    (relocated / "experiments").mkdir()
    (relocated / "experiments" / "agents.yaml").write_bytes((tmp_path / "experiments" / "agents.yaml").read_bytes())
    moved = load_experiment(relocated / "experiment.yaml")
    from a2a_engine.manifest import config_hash
    assert config_hash(moved.defaults) == config_hash(plan.defaults)
    assert moved.defaults["image_assets"] == plan.defaults["image_assets"]


def test_invalid_wire_image_diagnostics_do_not_echo_inline_bytes(bank):
    from a2a_engine.remote.contract import TurnInvocation
    raw = inline_media(bank.assets[0].model_dump(), str(bank.asset_store.root))
    raw["sha256"] = "0" * 64
    with pytest.raises(ValueError) as error:
        TurnInvocation(episode_id="fixture", seat=0, turn_id="turn", kind="round_start",
                       deadline=datetime.now(timezone.utc) + timedelta(seconds=10), observation={"content": [raw]})
    assert raw["data"] not in str(error.value)


def test_explicit_text_requirement_cannot_override_release_role_and_mixed_roles_are_supported(monkeypatch):
    from a2a_engine.environment import ReleaseDeclaration, RoleConfig
    declaration = ReleaseDeclaration(environment_id="fixture", roles=[
        RoleConfig(id="viewer", required_modalities={"text", "image"}), RoleConfig(id="listener")])
    monkeypatch.setattr("a2a_engine.registry.get_environment_spec", lambda name: SimpleNamespace(declaration=declaration))
    viewer = {"model": "fixture", "api_format": "openai", "required_modalities": ["text"]}
    with pytest.raises(ValueError, match="explicit verified"):
        validate_config({"environment_id": "fixture", "agents": [viewer]})
    listener = {"model": "fixture", "api_format": "openai"}
    validate_config({"environment_id": "fixture", "seat_roles": ["viewer", "listener"],
                     "agents": [{**viewer, "capabilities": CAPS}, listener]})


def test_image_role_with_implicit_roster_is_not_assumed_capable(monkeypatch):
    from a2a_engine.environment import ReleaseDeclaration, RoleConfig
    declaration = ReleaseDeclaration(environment_id="fixture", roles=[RoleConfig(id="viewer", required_modalities={"image"})])
    monkeypatch.setattr("a2a_engine.registry.get_environment_spec", lambda name: SimpleNamespace(declaration=declaration))
    with pytest.raises(ValueError, match="explicit capable seat roster"):
        validate_config({"environment_id": "fixture"})


def test_direct_manifest_construction_preserves_metadata_without_inline_bytes(bank):
    from a2a_engine.manifest import EpisodeManifest
    manifest = EpisodeManifest(experiment_name="fixture", episode_id="fixture", cell_id="fixture", episode_idx=0,
                               environment_id="fixture", episode_uid="fixture",
                               image_assets=[inline_media(bank.assets[0].model_dump(), str(bank.asset_store.root))])
    assert "data" not in manifest.image_assets[0]
    assert base64.b64encode(PNG).decode() not in manifest.model_dump_json()


def test_image_inboxes_are_rejected_instead_of_leaking_into_remote_registration(bank):
    from a2a_engine.remote.contract import TurnInvocation, TurnInvocationV2
    for model, extra in [(TurnInvocation, {}), (TurnInvocationV2, {"environment_id": "fixture", "attempt_id": "attempt", "operation": "register", "mcp": {}})]:
        with pytest.raises(ValueError, match="image inboxes"):
            model(episode_id="fixture", seat=0, turn_id="register", kind="register",
                  deadline=datetime.now(timezone.utc) + timedelta(seconds=10),
                  inbox=[{"content": [bank.assets[0].model_dump()]}], **extra)


def test_text_only_tool_messages_keep_nullable_content_unchanged():
    from a2a_engine.content import content_modalities
    messages = [{"role": "assistant", "content": None, "tool_calls": [{"id": "fixture", "type": "function", "function": {"name": "fixture", "arguments": "{}"}}]},
                {"role": "tool", "content": "text result", "tool_call_id": "fixture"}]
    assert content_modalities(messages) == {"text"}
    assert provider_messages(messages, "openai") == messages


@pytest.mark.parametrize("stream", [False, True])
def test_image_provider_exception_spans_omit_echoed_bytes(bank, monkeypatch, stream):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(api, "get_tracer", lambda: provider.get_tracer("image-errors"))
    encoded = base64.b64encode(PNG).decode()
    def fail(*args, **kwargs):
        raise RuntimeError(f"provider echoed submitted image: {encoded}")
    monkeypatch.setattr(api, "_stream_openai" if stream else "_oneshot_openai", fail)
    invoke = api.call_llm_streaming if stream else api.call_llm_oneshot
    with pytest.raises(RuntimeError):
        invoke(api_format="openai", api_base="http://fixture", api_key="", model="fixture",
               messages=message(bank), capabilities=CAPS, asset_root=str(bank.asset_store.root), max_tokens=20, temperature=0)
    span = exporter.get_finished_spans()[0]
    assert span.attributes["error.type"] == "RuntimeError"
    assert span.events[0].attributes["exception.type"] == "RuntimeError"
    assert all(encoded not in json.dumps(dict(event.attributes)) for event in span.events)
    assert encoded not in (span.status.description or "")
    assert encoded not in json.dumps(dict(span.attributes))
    provider.shutdown()


def test_image_provider_retry_logs_and_exhaustion_preserve_policy_without_bytes(bank, monkeypatch, caplog):
    from a2a_engine.llm.retry import RetryPolicy, RetryExhausted, retry_after_seconds
    import urllib.error
    encoded = base64.b64encode(PNG).decode()
    calls = []
    def fail(*args, **kwargs):
        calls.append(True)
        raise urllib.error.HTTPError("http://fixture", 429, f"provider echoed {encoded}", {"Retry-After": "0"}, None)
    monkeypatch.setattr(api, "_oneshot_openai", fail)
    client = api.LLMClient(model="fixture", capabilities=CAPS, asset_root=str(bank.asset_store.root),
                           retry=RetryPolicy(max_attempts=2, backoff_base=0, jitter_ratio=0, retry_after_jitter_ratio=0))
    with pytest.raises(RetryExhausted) as error:
        client.oneshot(message(bank))
    assert len(calls) == 2
    assert error.value.last_exception.status_code == 429
    assert retry_after_seconds(error.value.last_exception) == 0
    assert encoded not in str(error.value)
    assert encoded not in error.value.failure.error_message
    assert encoded not in caplog.text


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("api_format", ["openai", "anthropic"])
@pytest.mark.parametrize("image_input", [False, True])
def test_http_error_body_reaches_logs_and_image_failures_without_request_echo(
    bank, monkeypatch, caplog, stream, api_format, image_input,
):
    from io import BytesIO
    from urllib.error import HTTPError
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from a2a_engine.llm.retry import ImageProviderError, is_retryable

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(api, "get_tracer", lambda: provider.get_tracer("provider-errors"))
    encoded = base64.b64encode(PNG).decode()
    key = "fixture-secret-key"
    requests = []

    def fail(request, **kwargs):
        requests.append(request)
        payload = {"error": {"message": "API key expired.",
                              "metadata": {"request": encoded, "key": key}}}
        raise HTTPError(request.full_url, 401, "Unauthorized", {}, BytesIO(json.dumps(payload).encode()))

    monkeypatch.setattr(api.urllib.request, "urlopen", fail)
    invoke = api.call_llm_streaming if stream else api.call_llm_oneshot
    try:
        with pytest.raises(ImageProviderError if image_input else HTTPError) as failure:
            invoke(api_format=api_format, api_base="https://fixture.invalid", api_key=key, model="fixture",
                   messages=message(bank) if image_input else [{"role": "user", "content": "hello"}],
                   capabilities=CAPS, asset_root=str(bank.asset_store.root), max_tokens=20, temperature=0)
        assert len(requests) == 1
        assert not is_retryable(failure.value)
        assert "Provider request failed (HTTP 401): API key expired." in caplog.text
        span = exporter.get_finished_spans()[0]
        output = caplog.text + str(failure.value) + str(span.events)
        if image_input:
            assert "API key expired." in str(failure.value)
            assert "API key expired." in span.events[0].attributes["exception.message"]
            assert failure.value.status_code == 401
        assert encoded not in output and key not in output
    finally:
        provider.shutdown()


def test_image_rate_limit_body_keeps_retry_after_and_failure_detail(bank, monkeypatch, caplog):
    from io import BytesIO
    from urllib.error import HTTPError
    from a2a_engine.llm.retry import RetryPolicy, RetryExhausted, retry_after_seconds
    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        raise HTTPError("https://fixture.invalid", 429, "Too Many Requests", {"Retry-After": "0"},
                        BytesIO(b'{"error":{"message":"Rate limit exceeded; try again later."}}'))

    monkeypatch.setattr(api.urllib.request, "urlopen", fail)
    client = api.LLMClient(model="fixture", api_base="https://fixture.invalid", capabilities=CAPS, asset_root=str(bank.asset_store.root),
                           retry=RetryPolicy(max_attempts=2, backoff_base=0, jitter_ratio=0, retry_after_jitter_ratio=0))
    with pytest.raises(RetryExhausted) as failure:
        client.oneshot(message(bank))
    assert len(calls) == 2
    assert retry_after_seconds(failure.value.last_exception) == 0
    assert "Rate limit exceeded; try again later." in failure.value.failure.error_message
    assert "Rate limit exceeded; try again later." in caplog.text
