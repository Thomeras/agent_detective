"""Judge client: robust JSON extraction and the retries-then-None policy."""

import asyncio

import pytest

from worker.judge_client import JudgeError, extract_json, judge_json_with_retries

from conftest import FakeJudge


def test_extract_plain_json():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_strips_code_fences():
    text = "```json\n{\"task_score\": 0.5, \"input_flawed\": false}\n```"
    assert extract_json(text) == {"task_score": 0.5, "input_flawed": False}


def test_extract_first_object_from_prose():
    text = 'Sure! Here is my verdict: {"ok": true, "nested": {"x": 1}} Hope that helps.'
    assert extract_json(text) == {"ok": True, "nested": {"x": 1}}


def test_extract_handles_braces_in_strings():
    text = '{"reasoning": "use {curly} braces", "score": 1}'
    assert extract_json(text) == {"reasoning": "use {curly} braces", "score": 1}


def test_extract_raises_without_object():
    with pytest.raises(JudgeError):
        extract_json("no json here")


def test_retries_return_none_after_exhaustion():
    judge = FakeJudge(fail=True)
    calls = {"n": 0}

    async def fake_sleep(_: float) -> None:
        calls["n"] += 1

    result = asyncio.run(
        judge_json_with_retries(judge, "prompt", retries=2, sleep=fake_sleep)
    )
    assert result is None
    assert calls["n"] == 2  # 2 backoff sleeps between 3 attempts


def test_retries_return_first_success():
    judge = FakeJudge(terminal={"verdict": "bad", "score": 0.1})
    result = asyncio.run(judge_json_with_retries(judge, "final quality gate prompt"))
    assert result == {"verdict": "bad", "score": 0.1}
