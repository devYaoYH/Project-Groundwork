from a2a_engine.provenance import configured_roles


def test_explicit_roles_follow_configured_identity_not_roster_sort_order():
    assert configured_roles({"agents": [{"name": "b"}, {"name": "a"}], "seat_roles": ["director", "matcher"]}) == {"b": "director", "a": "matcher"}
    assert configured_roles({"agents": [{}, {}], "seat_roles": ["giver", "follower"]}) == {"participant_0": "giver", "participant_1": "follower"}


def test_missing_malformed_or_ambiguous_assignments_are_not_invented():
    assert configured_roles({"agents": [{}, {}]}) == {}
    assert configured_roles({"agents": [{}, {}], "seat_roles": ["giver"]}) == {}
    assert configured_roles({"agents": [{"name": "same"}, {"name": "same"}], "seat_roles": ["a", "b"]}) == {}
    assert configured_roles({"agents": [{}, {}], "seat_roles": [None, {}]}) == {}
