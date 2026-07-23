"""Judge client: robust JSON extraction, the retries-then-None policy and the
chat/completions request-body contract (temperature=0, optional JUDGE_SEED)."""

import asyncio

import pytest

from worker.judge_client import JudgeError, OpenAIJudgeClient, extract_json, judge_json_with_retries

from conftest import FakeJudge, make_settings


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


# --- chat/completions request-body construction (determinism knob) ---


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self._content = content

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self._content}}]}


class _FakeHttp:
    """Stands in for httpx.AsyncClient behind OpenAIJudgeClient._http()."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []

    async def post(self, url: str, json: dict) -> _FakeResponse:
        self.requests.append((url, json))
        return _FakeResponse('{"ok": true}')


def _request_body(settings) -> dict:
    client = OpenAIJudgeClient(settings)
    http = _FakeHttp()
    client._client = http  # bypass the lazy httpx construction
    result = asyncio.run(client.complete_json("judge this", system="be strict"))
    assert result == {"ok": True}
    (url, body), = http.requests
    assert url == "/chat/completions"
    return body


def test_request_body_default_has_no_seed():
    body = _request_body(make_settings())
    assert body["temperature"] == 0
    assert "seed" not in body
    assert body["messages"] == [
        {"role": "system", "content": "be strict"},
        {"role": "user", "content": "judge this"},
    ]


def test_request_body_carries_configured_seed():
    body = _request_body(make_settings(judge_seed=1234))
    assert body["seed"] == 1234
    assert body["temperature"] == 0  # seed must not change temperature


def test_request_body_seed_zero_is_sent():
    # 0 is a valid seed and must not be dropped by falsiness checks.
    body = _request_body(make_settings(judge_seed=0))
    assert body["seed"] == 0


def test_judge_seed_empty_env_string_is_none():
    # Compose passes JUDGE_SEED through as "" when unset; that means "no seed".
    assert make_settings(judge_seed="").judge_seed is None
