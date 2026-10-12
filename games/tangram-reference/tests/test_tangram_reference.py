import asyncio
import json
from pathlib import Path
import pytest
from tangram_reference.environment import TangramReferenceEnvironment
from tangram_reference.agents import TangramReferenceAgent
from tangram_reference.declaration import BANK_PATH, DECLARATION
from tangram_reference.stimuli import polygons, STANDARD
from a2a_engine.content import provider_messages
from a2a_engine.items import ItemBank


def game(**kw):
    return TangramReferenceEnvironment({"num_rounds": 2, **kw}, dry_run=True)


def setup():
    return {
        "round": 1,
        "exchange": 1,
        "history": [],
        "board": [None] * 4,
        "validation_error": None,
    }


def test_smoke_same_dyad_two_round_success():
    g = game()
    before = (id(g.director), id(g.matcher))
    t = g.run()
    assert t.metrics["round_success"] == [1, 1]
    assert (id(g.director.agent), id(g.matcher.agent)) == before
    assert len(g.director.observations) == len(g.matcher.observations) == 2
    assert any(
        m["speaker"] == "environment" for m in g.director.observations[1]["messages"]
    )
    assert t.final_state["rounds_completed"] == 2


def test_explicit_private_projection_and_trial_handles():
    g = game()
    s = setup()
    director = g.observation(s, "director")
    matcher = g.observation(s, "matcher")
    assert director["labels"] == [1, 2, 3, 4]
    assert matcher["labels"] == ["h1a", "h1b", "h1c", "h1d"]
    assert not any(
        k in director or k in matcher
        for k in ["geometry", "target", "gallery", "oracle_result"]
    )
    assert director["content"][-1] != matcher["content"][-1]
    assert "h1a" not in json.dumps(director)
    assert g.observation({**s, "round": 2}, "matcher")["labels"] != matcher["labels"]
    # Private placement edits never reach director prompt.
    assert (
        g.observation({**s, "board": ["h1d", "h1c", "h1b", "h1a"]}, "director")
        == director
    )


def test_native_payload_and_retained_private_model_context():
    class Client:
        def __init__(self):
            self.calls = []

        def oneshot(self, messages):
            payload = provider_messages(
                messages, "openai", str(BANK_PATH.parent / "assets")
            )
            self.calls.append(payload)
            return '{"text":"descriptions"}'

    g = game()
    client = Client()
    a = TangramReferenceAgent(client, "director")
    asyncio.run(a.act(g.observation(setup(), "director"), {}))
    asyncio.run(a.act(g.observation({**setup(), "round": 2}, "director"), {}))
    assert [len(c) for c in client.calls] == [2, 4]
    assert (
        sum(
            p["type"] == "image_url"
            for m in client.calls[1]
            if isinstance(m["content"], list)
            for p in m["content"]
        )
        == 2
    )
    assert all(
        p["image_url"]["url"].startswith("data:image/png;base64,")
        for m in client.calls[1]
        if isinstance(m["content"], list)
        for p in m["content"]
        if p["type"] == "image_url"
    )
    assert a.history[0]["content"][-1]["type"] == "image"
    assert all(
        "data" not in p
        for m in a.history
        if isinstance(m["content"], list)
        for p in m["content"]
    )


def test_invalid_actions_bounded_and_unscored_until_end():
    class Wrong:
        name = "matcher"

        async def act(self, observation, tools):
            return {"text": "got it", "arrangement": ["h1a"] * 4, "submit": True}

    g = game(max_exchanges=2)
    g.matcher = Wrong()
    t = g.run()
    assert t.metrics["round_success"] == [0, 0]
    assert all(
        o["reason"] == "deadline" and o["exchanges"] == 2
        for o in t.final_state["outcomes"]
    )
    assert sum(e.type == "invalid_action" for e in t.events) == 4
    assert all(o["accuracy"] == 0 for o in t.final_state["outcomes"])
    rounds = [e for e in t.events if e.type == "round_end"]
    assert len(rounds) == 2


def test_wrong_complete_submission_scores_truthfully():
    class Wrong:
        name = "matcher"

        async def act(self, observation, tools):
            return {
                "text": "I think this is right",
                "arrangement": list(observation["labels"]),
                "submit": True,
            }

    g = game()
    g.matcher = Wrong()
    t = g.run()
    assert t.metrics["round_success"] != [1, 1]
    assert all(o["submitted"] for o in t.final_state["outcomes"])


def test_trace_images_are_private_metadata_only():
    t = game().run()
    obs = [e for e in t.events if e.type == "observation"]
    assert len(obs) == 4
    assert all(e.data["visibility"] == "private" for e in obs)
    assert all(e.data["content"][-1]["asset"].startswith("asset://") for e in obs)
    assert "base64" not in t.model_dump_json()
    assert not any("content" in e.data for e in t.events if e.type == "message")


def test_packaged_bank_verifies_bytes_and_drift(tmp_path):
    b = ItemBank.load(BANK_PATH)
    copy = b.package(tmp_path)
    assert (
        copy.item_bank_sha256
        == b.item_bank_sha256
        == DECLARATION.item_policy.item_bank_sha256
    )
    digest = copy.assets[0].sha256
    (tmp_path / "assets" / digest).write_bytes(b"bad")
    with pytest.raises(ValueError, match="digest"):
        ItemBank.load(copy.path)


def area(poly):
    return (
        abs(sum(x * b - y * a for (x, y), (a, b) in zip(poly, poly[1:] + poly[:1]))) / 2
    )


def clipped(subject, clip):
    # Convex polygon intersection, including boundary contacts of zero area.
    orientation = sum(
        x * b - y * a for (x, y), (a, b) in zip(clip, clip[1:] + clip[:1])
    )
    direction = 1 if orientation > 0 else -1
    result = list(subject)
    for a, b in zip(clip, clip[1:] + clip[:1]):
        old = result
        result = []
        if not old:
            break

        def signed(p):
            return direction * (
                (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
            )

        for prev, cur in zip(old[-1:] + old[:-1], old):
            sp, sc = signed(prev), signed(cur)
            if (sp >= 0) != (sc >= 0):
                f = sp / (sp - sc)
                result.append(
                    (prev[0] + f * (cur[0] - prev[0]), prev[1] + f * (cur[1] - prev[1]))
                )
            if sc >= 0:
                result.append(cur)
    return result


@pytest.mark.parametrize("index", range(4))
def test_exact_seven_tangram_pieces_no_interior_overlap(index):
    pieces = polygons(index)
    assert [area(p) for p in pieces] == [4, 4, 2, 1, 1, 2, 2]
    assert len(pieces) == 7
    for i, p in enumerate(pieces):
        for q in pieces[i + 1 :]:
            assert area(clipped(p, q)) == pytest.approx(0)


def test_executable_workflow_export_is_compact_and_pure(monkeypatch):
    from a2a_engine.workflow import export_program

    monkeypatch.setattr(
        TangramReferenceEnvironment,
        "__init__",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no game construction")),
    )
    doc = export_program(
        TangramReferenceEnvironment.build_program(), "tangram_reference"
    )["workflow"]
    assert not doc["diagnostics"]
    assert len(doc["graphs"]) == 2
    assert len(doc["graphs"][0]["nodes"]) == 4
    assert len(doc["graphs"][1]["nodes"]) == 8
    assert (
        sum(n["kind"] == "agent_call" for g in doc["graphs"] for n in g["nodes"]) == 2
    )


def test_seeded_gallery_layouts_reproducible_and_private():
    a = game(seed=17)
    b = game(seed=17)
    c = game(seed=18)
    assert a.config.gallery_layout_indices == b.config.gallery_layout_indices
    assert a.config.gallery_layout_indices != c.config.gallery_layout_indices
    assert a.observation(setup(), "director") == c.observation(setup(), "director")
    assert (
        a.observation(setup(), "matcher")["content"][-1]
        != c.observation(setup(), "matcher")["content"][-1]
    )
    assert len(a.item.raw["rounds"][0]["gallery_variants"]) == 24
    for g in (a, b, c):
        g.episode.close()


def test_six_round_smoke_retains_prior_permitted_messages():
    g = game(num_rounds=6, seed=17)
    t = g.run()
    assert t.metrics["round_success"] == [1] * 6
    assert len(g.director.observations) == 6
    assert len(g.matcher.observations) == 6
    assert len(g.director.observations[-1]["messages"]) == 15


def test_cleanup_on_agent_failure(monkeypatch):
    class Failing:
        name = "director"

        async def act(self, observation, tools):
            raise RuntimeError("test failure")

    g = game()
    g.director = Failing()
    closed = []
    original = g.episode.close

    def close():
        closed.append(True)
        original()

    monkeypatch.setattr(g.episode, "close", close)
    with pytest.raises(RuntimeError, match="test failure"):
        g.run()
    assert closed
    assert not any(e.type == "game_end" for e in g.events.all())


def test_text_only_model_rejected_before_provider_io():
    with pytest.raises(ValueError, match="modalit|capabilit"):
        TangramReferenceEnvironment(
            {
                "num_rounds": 2,
                "agents": [
                    {
                        "model": "deepseek/deepseek-v4.1-flash",
                        "capabilities": {"input": ["text"]},
                    }
                ],
            }
        )


@pytest.mark.parametrize("bad", ["not-json", "[]", '{"text":"hello"}'])
def test_parser_malformed_actions_never_guess_oracle(bad):
    agent = TangramReferenceAgent(None, "matcher")
    action = agent.parse_response(bad, {}, {})
    assert "arrangement" not in action
    assert action.get("submit") is not True


def test_remote_scripted_strategy_uses_only_inline_private_pixels():
    from types import SimpleNamespace
    from a2a_engine.content import inline_media
    from tangram_reference.runtime_strategy import TangramReferenceStrategy

    g = game(seed=17)
    strategy = TangramReferenceStrategy()
    asyncio.run(
        strategy.calls(
            SimpleNamespace(kind="register", observation={"role": "director"})
        )
    )
    private = inline_media(g.observation(setup(), "director"), g.config.asset_root)
    calls = asyncio.run(
        strategy.calls(SimpleNamespace(kind="turn", observation=private))
    )
    assert calls[0][:2] == ("env", "submit_tangram")
    assert calls[0][2]["text"] == "Visual signatures by slot: 0,2,1,3"
    assert strategy.agent.asset_root is None
    g.episode.close()


def test_shared_declaration_and_bank_contracts():
    from a2a_engine.testing import (
        assert_declaration_matches_config,
        assert_item_parameters_match_bank,
    )
    from tangram_reference.environment import TangramReferenceConfig

    assert_declaration_matches_config(DECLARATION, TangramReferenceConfig)
    assert_item_parameters_match_bank(DECLARATION, ItemBank.load(BANK_PATH))


def test_typed_release_matches_installed_declaration():
    from a2a_engine.environment import load_release_declaration

    release = load_release_declaration(
        Path(__file__).parents[1] / "environments/tangram_reference_tiny_v1.yaml"
    )
    assert release.parameters == DECLARATION.parameters
    assert release.roles == DECLARATION.roles
    assert release.required_modalities == DECLARATION.required_modalities
    assert (
        release.item_policy.item_bank_sha256 == DECLARATION.item_policy.item_bank_sha256
    )
