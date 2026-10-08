import pytest

from a2a_engine.llm.structured_output import parse_actions, parse_reflection_deltas
from calendar_game.clients.llm import _parse_response


@pytest.mark.parametrize("text", [
    '', 'not json', '{}', '{"actions": null}', '[1, {"type":"schedule","slot":0}]',
    '{"thinking":"plan","actions":[{"type":"dm","to":1,"content":"hello"}]}',
    '```json\n{"actions": [{"type":"schedule","slot":0}]}\n```',
    "{'thinking': 'plan', 'actions': [{'type': 'schedule', 'slot': 0,}]}",
    'prefix {"actions":[{"type":"reschedule","item_id":2}]} suffix',
])
def test_calendar_wrapper_preserves_parser(text):
    assert parse_actions(text) == _parse_response(text)


def test_fence_and_filter_without_optional_repair():
    assert parse_actions('```json\n[{"type":"schedule"}, 4]\n```', repair=None) == ([{"type": "schedule"}], None)
    assert parse_actions('prefix [{"type":"schedule"}] suffix', repair=None) == ([], None)


def test_regex_recovery_after_full_repair_fails():
    candidates = []

    def repair(candidate, **kwargs):
        candidates.append(candidate)
        if candidate.startswith('prefix'):
            raise ValueError()
        return {"actions": [{"type": "schedule"}], "thinking": "recovered"}

    assert parse_actions('prefix {broken} suffix', repair=repair) == ([{"type": "schedule"}], "recovered")
    assert candidates == ['prefix {broken} suffix', '{broken}']


def test_calendar_optional_repair_hook_is_preserved(monkeypatch):
    monkeypatch.setattr('calendar_game.clients.llm._repair_json', None)
    assert _parse_response("{'actions':[{'type':'schedule'}]}") == ([], None)


@pytest.mark.parametrize("text", ['{"deltas":[-3, 0, 3]}', '{"states":[-3,0,3]}', '[-3,0,3]', '-3 0 3'])
def test_reflection_delta_compatibility(text):
    assert parse_reflection_deltas(text, 3) == [-3, 0, 3]
