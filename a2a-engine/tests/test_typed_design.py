"""Typed participant bindings cannot cross agent, model, and strategy namespaces."""

import json

import pytest
import yaml

from a2a_engine.agent_pool import AgentPool, AgentPoolEntry
from a2a_engine.compiler import compile, validate
from a2a_engine.design import DesignV2, DesignValidationError, parse_design_text
from a2a_engine.environment import EngineConfig, ItemPolicy, ReleaseDeclaration, RoleConfig
from a2a_engine.items import ItemBank


def document(binding=None, *, kind="llm", **seat):
    return {"schema_version": 2, "release": "fixture@v1", "units": {"episodes_per_cell": 1},
            "roster": [{"id": "player", "role": "player", "kind": kind, "binding": binding, **seat}],
            "seed": {"root": 42}}


@pytest.fixture
def release_and_bank(tmp_path):
    path = tmp_path / "items.jsonl"
    path.write_text(json.dumps({"item_id": "one", "params": {}}) + "\n")
    bank = ItemBank.load(path)
    release = ReleaseDeclaration(
        id="fixture", environment_id="fixture", version="v1",
        engine=EngineConfig(environment_id="fixture"),
        roles=[RoleConfig(id="player", accepts=["llm", "scripted", "human"])],
        item_policy=ItemPolicy(mode="enumerate", bank_path="items.jsonl", item_bank_sha256=bank.item_bank_sha256),
        oracle_version="fixture-v1",
    )
    return release, bank


def compiled(payload, release_and_bank, pool=None):
    return compile(parse_design_text(yaml.safe_dump(payload)), *release_and_bank,
                   experiment_id="e", experiment_name="fixture", agent_pool=pool).preview_episode_config


@pytest.mark.parametrize("payload", [
    document("baseline"),
    document({"kind": "strategy", "name": "baseline"}),
    document({"kind": "agent", "name": "gpt-mini"}, kind="scripted"),
    document({"kind": "model", "model": "gpt-mini", "credential": "OPENAI_API_KEY"}, kind="scripted"),
    document({"kind": "strategy", "name": "baseline"}, kind="human"),
    document({"kind": "agent", "name": " "}),
    document({"kind": "model", "model": "gpt-4o-mini"}),
    document({"kind": "model", "model": "gpt-4o-mini", "credential": "OPENAI_API_KEY", "api_key": "forbidden"}),
    document({"kind": "model", "model": "gpt-4o-mini", "credential": "OPENAI_API_KEY", "api_format": "anthropic"}),
    document({"kind": "agent", "name": "gpt-mini"}, harness="scripted"),
    document({"kind": "strategy", "name": "baseline"}, kind="scripted", harness="structured_output"),
])
def test_mismatched_bindings_and_harnesses_fail_at_parse_time(payload):
    with pytest.raises(DesignValidationError):
        parse_design_text(yaml.safe_dump(payload))


def test_v2_requires_an_explicit_participant_kind():
    payload = document({"kind": "agent", "name": "gpt-mini"})
    del payload["roster"][0]["kind"]
    with pytest.raises(DesignValidationError):
        parse_design_text(yaml.safe_dump(payload))


def test_typed_errors_use_document_paths_without_union_tags():
    with pytest.raises(DesignValidationError) as exc:
        parse_design_text(yaml.safe_dump(document({"kind": "agent", "name": ""})))
    assert exc.value.errors[0].path == "roster[0].binding.name"


@pytest.mark.parametrize("name", ["baseline", "gpt-4o-mini", "vendor/model"])
@pytest.mark.parametrize("pool", [None, AgentPool(agents={})])
def test_unknown_references_never_fall_back_to_model_names(name, pool, release_and_bank):
    with pytest.raises(DesignValidationError, match="unknown agent reference") as exc:
        compiled(document({"kind": "agent", "name": name}), release_and_bank, pool)
    assert exc.value.errors[0].path == "roster[0].binding"


@pytest.mark.parametrize("entry", [AgentPoolEntry(type="scripted"), AgentPoolEntry(type="llm")])
def test_llm_reference_requires_a_model_backed_pool_entry(entry, release_and_bank):
    pool = AgentPool(agents={"baseline": entry})
    with pytest.raises(DesignValidationError, match="must configure an LLM model"):
        compiled(document({"kind": "agent", "name": "baseline"}), release_and_bank, pool)


def test_pool_harness_must_match_the_typed_llm_seat(release_and_bank):
    pool = AgentPool(agents={"bad": AgentPoolEntry(model="gpt-4o-mini", harness="scripted")})
    with pytest.raises(DesignValidationError, match="structured_output harness"):
        compiled(document({"kind": "agent", "name": "bad"}), release_and_bank, pool)


def test_reference_is_resolved_before_provenance_and_is_not_a_reserved_word(release_and_bank):
    pool = AgentPool(agents={"baseline": AgentPoolEntry(model="arbitrary", credential="OPENAI_API_KEY")})
    config = compiled(document({"kind": "agent", "name": "baseline"}), release_and_bank, pool)
    assert config["agents"][0] == {"id": "player", "type": "llm", "model": "arbitrary", "credential": "OPENAI_API_KEY"}
    assert config["provenance"]["participants"][0]["binding"] == "arbitrary"


def test_inline_model_has_explicit_routing_even_when_its_name_is_also_an_alias(release_and_bank):
    binding = {"kind": "model", "model": "baseline", "credential": "OPENAI_API_KEY", "temperature": 0.2}
    pool = AgentPool(agents={"baseline": AgentPoolEntry(type="scripted")})
    config = compiled(document(binding), release_and_bank, pool)
    assert config["agents"][0] == {"id": "player", "type": "llm", "model": "baseline", "credential": "OPENAI_API_KEY", "temperature": 0.2}


def test_scripted_strategy_uses_the_releases_declared_runtime_type(release_and_bank):
    release, bank = release_and_bank
    release.roles[0].scripted_bindings = {"baseline": "heuristic"}
    config = compiled(document({"kind": "strategy", "name": "baseline"}, kind="scripted"), (release, bank))
    assert config["agents"][0] == {"id": "player", "type": "heuristic", "binding": "baseline"}


def test_unbound_draft_is_parseable_but_not_executable(release_and_bank):
    payload = document()
    design = parse_design_text(yaml.safe_dump(payload))
    assert validate(design, *release_and_bank, allow_unbound=True) == []
    assert any(issue.path == "roster[0].binding" for issue in validate(design, *release_and_bank))
    with pytest.raises(DesignValidationError, match="needs a binding"):
        compiled(payload, release_and_bank)


def test_image_reference_inherits_verified_capabilities_and_unbound_draft_needs_no_guess(release_and_bank):
    release, bank = release_and_bank
    release.roles[0].required_modalities = {"text", "image"}
    draft = parse_design_text(yaml.safe_dump(document()))
    assert validate(draft, release, bank, allow_unbound=True) == []
    pool = AgentPool(agents={"visual": AgentPoolEntry(
        model="arbitrary", credential="OPENAI_API_KEY",
        capabilities={"input": ["text", "image"], "evidence": "fixture:visual"},
    )})
    payload = document({"kind": "agent", "name": "visual"})
    config = compiled(payload, (release, bank), pool)
    assert config["agents"][0]["capabilities"]["input"] == ["text", "image"]
    pool.agents["visual"] = AgentPoolEntry(model="arbitrary", credential="OPENAI_API_KEY")
    with pytest.raises(DesignValidationError, match="capabilities"):
        compiled(payload, (release, bank), pool)


def test_unbound_seat_does_not_skip_validation_of_other_bound_seats(release_and_bank):
    release, bank = release_and_bank
    release.roles[0].count = 2
    release.roles[0].required_modalities = {"text", "image"}
    payload = document({"kind": "strategy", "name": "baseline"}, kind="scripted")
    payload["roster"].append({"id": "other", "role": "player", "kind": "llm", "binding": None})
    design = parse_design_text(yaml.safe_dump(payload))
    errors = validate(design, release, bank, allow_unbound=True)
    assert [issue.path for issue in errors] == ["roster[0].capabilities"]


@pytest.mark.parametrize("kind", ["llm", "scripted"])
def test_external_seats_keep_implementation_under_external_ownership(kind, release_and_bank):
    release, bank = release_and_bank
    release.environment_id = "calendar"
    payload = document(kind=kind, runtime="external")
    payload["release"] = "calendar@v1"
    config = compiled(payload, (release, bank))
    assert config["agents"][0] == {"id": "player", "type": kind, "runtime": "external"}


def test_version_1_hash_and_typed_model_routing_remain_stable(release_and_bank):
    legacy = document("gpt-4o-mini")
    legacy["schema_version"] = 1
    parsed = parse_design_text(yaml.safe_dump(legacy))
    assert parsed.content_sha256() == "d354c4cf7110440e7a4b62efc56d94ee15decdf40f31b7506cf709c02649bcd7"
    pool = AgentPool(agents={"gpt-4o-mini": AgentPoolEntry(model="gpt-4o-mini", credential="OPENAI_API_KEY")})
    v2 = document({"kind": "agent", "name": "gpt-4o-mini"})
    old_config = compiled(legacy, release_and_bank, pool)
    new_config = compiled(v2, release_and_bank, pool)
    # Authoring syntax changes its own digest, not the scientific runtime config.
    old_config["provenance"].pop("design_sha256")
    new_config["provenance"].pop("design_sha256")
    assert old_config == new_config


def test_v2_round_trip_preserves_its_digest_and_publishes_discriminated_schema():
    design = parse_design_text(yaml.safe_dump(document({"kind": "agent", "name": "gpt-mini"})))
    assert isinstance(design, DesignV2)
    round_trip = parse_design_text(yaml.safe_dump(design.model_dump(mode="json")))
    assert round_trip.content_sha256() == design.content_sha256()
    schema = DesignV2.model_json_schema()
    assert schema["properties"]["roster"]["items"]["discriminator"]["propertyName"] == "kind"
