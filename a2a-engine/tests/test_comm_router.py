import pytest

from a2a_engine.comm import CommRouter, RoutingContext, Topology


CONTEXT = RoutingContext((0, 1, 2, 3), 7, (2, 0, 3, 1))


def router(config=None, **kwargs):
    return CommRouter(Topology(config, seats=list(CONTEXT.seat_ids), phases={"CHEAP_TALK", "DECISION"},
                               default_send_phases={"CHEAP_TALK"}, **kwargs))


def authorize(policy, channel="dm", to=1, usage=0, phase="CHEAP_TALK", sender=0, context=CONTEXT):
    return policy.authorize(sender, channel, to, phase=phase, round=0, context=context, attempted_this_round=usage)


@pytest.mark.parametrize("graph, extra, expected", [
    ("complete", {}, (2, 3, 1)), ("ring", {}, (3, 1)),
    ("star", {"hub": 2}, (2,)), ("edges", {"edges": [[0, 2], [0, 1]], "directed": True}, (2, 1)),
])
def test_participant_chat_intersects_neighbors_in_meeting_order(graph, extra, expected):
    policy = router({"default": {"graph": graph, **extra, "channels": {"participant_groupchat": {"enabled": True}}}})
    decision = authorize(policy, "participant_groupchat")
    assert decision.allowed and decision.charge_attempt
    assert decision.recipients == expected


def test_all_chat_uses_seat_order_and_dm_uses_one_neighbor():
    policy = router({"default": {"graph": "ring", "channels": {"dm": {}, "all_groupchat": {}}}})
    assert authorize(policy, "all_groupchat").recipients == (1, 3)
    assert authorize(policy, to="3").recipients == (3,)
    assert not authorize(policy, to=2).allowed
    assert not authorize(policy, to=0).allowed


@pytest.mark.parametrize("tool, target, expected", [
    ("dm", 0, (0,)), ("dm", "2", (2,)), ("group_chat", None, (1, 2, 3)),
    ("meeting_chat", None, (2, 3, 1)),
])
def test_legacy_aliases_self_dm_coercion_and_recipient_order(tool, target, expected):
    decision = authorize(router(communication_protocol="all"), tool, target)
    assert decision.allowed and decision.charge_attempt
    assert decision.recipients == expected


@pytest.mark.parametrize("legacy", [True, False])
def test_empty_group_delivery_is_allowed_and_charged(legacy):
    policy = router(None if legacy else {"default": {"channels": {"participant_groupchat": {}}}}, communication_protocol="all")
    decision = authorize(policy, "participant_groupchat", context=RoutingContext(CONTEXT.seat_ids, 7, (0,)))
    assert decision.allowed and decision.recipients == () and decision.charge_attempt


@pytest.mark.parametrize("channel, target, reason", [
    ("all_groupchat", None, "all_groupchat tool is disabled by communication_protocol"),
    ("dm", None, "dm tool missing integer 'to'"),
    ("dm", "bad", "dm tool missing integer 'to'"),
    ("dm", 99, "dm recipient is out of range"),
])
def test_legacy_invalid_attempts_charge_until_capped(channel, target, reason):
    policy = router(dm_cap=1)
    decision = authorize(policy, channel, target)
    assert not decision.allowed and decision.charge_attempt and decision.reason == reason
    capped = authorize(policy, channel, target, usage=1)
    assert not capped.allowed and not capped.charge_attempt
    assert capped.reason == "per-agent cheap-talk messaging-tool budget exhausted (dm_cap=1)"


def test_explicit_policy_checks_topology_before_budget_and_never_charges_denials():
    policy = router({"default": {"graph": "ring", "channels": {"dm": {}}, "budget": {"per_agent_per_round": 1}}})
    for channel, target in [("dm", 0), ("dm", 2), ("dm", None), ("all_groupchat", None)]:
        decision = authorize(policy, channel, target, usage=1)
        assert not decision.allowed and not decision.charge_attempt
        assert "budget" not in decision.reason
    assert authorize(policy).allowed
    capped = authorize(policy, usage=1)
    assert not capped.allowed and not capped.charge_attempt and "budget exhausted" in capped.reason
    assert authorize(policy).allowed  # Router owns no usage counter.


def test_budget_is_supplied_by_game_and_shared_across_phases():
    policy = router({"default": {"budget": {"per_agent_per_round": 1}}, "phases": {"DECISION": {}}})
    assert authorize(policy).charge_attempt
    assert not authorize(policy, phase="DECISION", usage=1).allowed
    assert authorize(policy, phase="DECISION", sender=1, to=0).allowed


@pytest.mark.parametrize("cap", [-10, -1])
def test_negative_legacy_budgets_remain_unlimited(cap):
    assert authorize(router(dm_cap=cap), usage=100).allowed


def test_unknown_tools_are_silent_and_do_not_charge_on_legacy_path():
    decision = authorize(router(dm_cap=0), "noop")
    assert not decision.allowed and decision.reason is None and not decision.charge_attempt
