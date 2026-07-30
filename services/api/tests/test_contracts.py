"""Contract inference: what the stored payloads support, and what they do not.

The dangerous failure here is not a missing contract — it is a permissive one.
A schema that passes everything manufactures a 0.35-weight scoring channel out
of payloads that never agreed on anything, and the composite then presents as
three-channel evidence. Every test below is about the engine refusing to do
that.
"""

import json

from api.contracts import infer_schema

MIN = 5


def _payloads(*objs: dict) -> list[str | None]:
    return [json.dumps(o) for o in objs]


def test_refuses_below_the_minimum_sample_count():
    result = infer_schema(_payloads({"a": 1}, {"a": 2}), MIN)
    assert result.schema is None
    assert result.reason
    assert result.usable_samples == 2


def test_requires_a_key_to_appear_in_every_sample():
    # `b` is in four of five: present often enough to look required, and a
    # required key the fifth run does not carry scores that run 0.0.
    samples = _payloads(
        {"a": 1, "b": "x"},
        {"a": 2, "b": "y"},
        {"a": 3, "b": "z"},
        {"a": 4, "b": "w"},
        {"a": 5},
    )
    result = infer_schema(samples, MIN)
    assert result.schema is not None
    assert result.schema["required"] == ["a"]
    assert "b" not in result.schema.get("properties", {})


def test_reports_the_optional_key_it_left_out():
    samples = _payloads(
        {"a": 1, "b": "x"}, {"a": 2}, {"a": 3}, {"a": 4}, {"a": 5}
    )
    result = infer_schema(samples, MIN)
    keys = {k["key"]: k for k in result.keys}
    assert keys["b"]["required"] is False
    assert keys["b"]["included"] is False


def test_excludes_a_key_whose_type_the_samples_disagree_on():
    # A type read off some of the samples is wrong for the rest, and a wrong
    # property type scores a perfectly good output 0.0.
    samples = _payloads(
        {"a": 1}, {"a": "one"}, {"a": 3}, {"a": "four"}, {"a": 5}
    )
    result = infer_schema(samples, MIN)
    assert result.schema is None or "a" not in result.schema.get("properties", {})


def test_refuses_when_no_key_survives_rather_than_passing_everything():
    samples = _payloads(
        {"a": 1}, {"b": 2}, {"c": 3}, {"d": 4}, {"e": 5}
    )
    result = infer_schema(samples, MIN)
    assert result.schema is None
    assert result.reason


def test_unparseable_payloads_are_unusable_samples_not_errors():
    outputs = [*_payloads({"a": 1}, {"a": 2}, {"a": 3}), "not json", None]
    result = infer_schema(outputs, MIN)
    assert result.runs_examined == 5
    assert result.usable_samples == 3
    assert result.schema is None  # three usable is below the floor of five


def test_accepts_when_every_sample_agrees():
    samples = _payloads(*[{"id": i, "ok": True} for i in range(5)])
    result = infer_schema(samples, MIN)
    assert result.schema is not None
    assert result.schema["type"] == "object"
    assert sorted(result.schema["required"]) == ["id", "ok"]
