import asyncio
import json
from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph

from a2a_engine.workflow import environment_workflow, export_program
from a2a_engine.workflow_runtime import AgentStep, GraphProgram, GuardedAgent, require_agent_step
from word_guess.game import WordGuessGame


def test_export_uses_executable_bindings_without_evaluation(monkeypatch):
    import word_guess.workflow as workflow

    def fail(*args, **kwargs):
        raise AssertionError("export executed game code")

    for name in ("next_turn", "can_continue", "select_guesser", "select_host",
                 "guesser_observation", "host_observation", "word_guess_adapter", "record_guess",
                 "judge_reply", "finalize"):
        original = getattr(workflow, name)
        fail.__name__, fail.__qualname__, fail.__module__ = original.__name__, original.__qualname__, original.__module__
        # Unique function objects retain their distinct symbolic identity.
        def double(*args, **kwargs):
            return fail(*args, **kwargs)
        double.__name__, double.__qualname__, double.__module__ = original.__name__, original.__qualname__, original.__module__
        monkeypatch.setattr(workflow, name, double)
    monkeypatch.setattr(WordGuessGame, "__init__", fail)
    monkeypatch.setattr("word_guess.game.make_llm_client", fail)
    monkeypatch.setattr(WordGuessGame, "runtime_capability", fail)
    for target in ("word_guess.game.GuesserAgent.__init__", "word_guess.game.HostAgent.__init__",
                   "word_guess.remote.RemoteTextAgent.__init__", "word_guess.remote.TurnDispatcherV2.__init__",
                   "a2a_engine.remote.server.RuntimeManager.__init__", "word_guess.game.EventLog.from_config",
                   "word_guess.game.EventLog.append"):
        monkeypatch.setattr(target, fail)
    result = environment_workflow("word_guess")
    assert result["status"] == "available"
    root = result["workflow"]["graphs"][0]
    agents = [node for node in root["nodes"] if node["kind"] == "agent_call"]
    assert [node["actor_selector"] for node in agents] == ["word_guess.workflow.select_guesser", "word_guess.workflow.select_host"]
    assert {edge.get("route_key") for edge in root["edges"] if edge["conditional"]} == {"continue", "finished"}
    assert len(root["nodes"]) == 8 and len(root["edges"]) == 8
    assert "initialize" not in {node["id"] for node in root["nodes"]}
    assert result == environment_workflow("word_guess")
    assert len(json.dumps(result)) < 10000
    assert all(not node.get("source_ref", {}).get("path", "").startswith("/") for node in root["nodes"])


class State(TypedDict):
    value: int


def leaf(state):
    return {"value": 1}


def route(state):
    raise AssertionError("route executed during export")


def test_unknown_route_is_partial_not_all_to_all():
    builder = StateGraph(State)
    builder.add_node("leaf", leaf)
    builder.add_edge(START, "leaf")
    builder.add_conditional_edges("leaf", route)
    result = export_program(GraphProgram(builder), "test")
    assert result["status"] == "partial"
    assert result["workflow"]["diagnostics"][0]["code"] == "unbounded_route"
    assert not any(edge["conditional"] for edge in result["workflow"]["graphs"][0]["edges"])


def test_actual_subgraphs_are_exported_once():
    child = StateGraph(State)
    child.add_node("leaf", leaf)
    child.add_edge(START, "leaf")
    child.add_edge("leaf", END)
    parent = StateGraph(State)
    parent.add_node("child", child.compile())
    parent.add_edge(START, "child")
    parent.add_edge("child", END)
    result = export_program(GraphProgram(parent), "test")
    assert len(result["workflow"]["graphs"]) == 2
    node = next(n for n in result["workflow"]["graphs"][0]["nodes"] if n["id"] == "child")
    assert node["subgraph"] == "root/child"


def test_finite_route_aliases_are_not_overwritten():
    builder = StateGraph(State)
    builder.add_node("leaf", leaf)
    builder.add_edge(START, "leaf")
    builder.add_conditional_edges("leaf", route, {"victory": END, "exhausted": END})
    result = export_program(GraphProgram(builder), "test")
    edges = result["workflow"]["graphs"][0]["edges"]
    assert len([edge for edge in edges if edge["conditional"]]) == 2
    assert {edge["route_key"] for edge in edges if edge["conditional"]} == {"victory", "exhausted"}
    assert all(edge["route_symbol"].endswith(".route") for edge in edges if edge["conditional"])


def test_export_enforces_node_and_route_bounds():
    builder = StateGraph(State)
    for index in range(255):
        builder.add_node(str(index), leaf)
        builder.add_edge(START, str(index))
        builder.add_edge(str(index), END)
    with pytest.raises(ValueError, match="bound"):
        export_program(GraphProgram(builder), "test")
    builder = StateGraph(State)
    builder.add_node("leaf", leaf)
    builder.add_edge(START, "leaf")
    builder.add_conditional_edges("leaf", route, {str(index): END for index in range(1025)})
    with pytest.raises(ValueError, match="bound"):
        export_program(GraphProgram(builder), "test")


def test_runtime_gateway_rejects_hidden_invocation():
    with pytest.raises(RuntimeError, match="AgentStep"):
        require_agent_step()


def test_domain_node_cannot_invoke_an_actual_guarded_word_guess_agent():
    from word_guess.workflow import GameContext
    game = WordGuessGame({"secret_word": "dog", "max_turns": 1}, dry_run=True)
    context = GameContext(game)
    async def hidden(state):
        await context.game.guesser.act({"history": [], "turns_remaining": 1}, {})
        return {}
    builder = StateGraph(State)
    builder.add_node("hidden", hidden)
    builder.add_edge(START, "hidden")
    builder.add_edge("hidden", END)
    with pytest.raises(RuntimeError, match="AgentStep"):
        asyncio.run(GraphProgram(builder).ainvoke({}, context=context))


def test_agent_step_cannot_hide_a_second_call():
    class Agent:
        async def act(self, observation, tools):
            return {}
    actor = GuardedAgent(Agent())
    async def doubled(agent, operation, observation, state, context):
        await agent.act(observation, {})
        await agent.act(observation, {})
    step = AgentStep(lambda state, context: actor, "act", lambda state, context: {}, doubled)
    from types import SimpleNamespace
    with pytest.raises(RuntimeError, match="AgentStep"):
        asyncio.run(step({}, SimpleNamespace(context=None)))


@pytest.mark.parametrize("environment_id", ["negotiation", "buyer_seller", "word_guess"])
def test_migrated_games_have_executable_workflows(environment_id):
    assert environment_workflow(environment_id)["status"] == "available"
