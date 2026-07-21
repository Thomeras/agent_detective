"""OpenAI-compatible chat/completions judge client (build spec section 4.3).

A deliberately thin client: one ``complete_json`` call posts a single-user-turn
chat completion and returns the model's response parsed as JSON. It works
against the Anthropic OpenAI-compat endpoint, Ollama and the demo mock_llm
because it only relies on the ``/chat/completions`` shape and extracts JSON
robustly (strip Markdown fences, then take the first balanced ``{...}`` object).

The ``JudgeClient`` protocol is the test seam; ``FakeJudge`` in the test harness
returns canned verdicts keyed on prompt content. ``judge_json_with_retries``
adds the spec's "2 retries with backoff, then None" policy on top of any client.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Protocol

logger = logging.getLogger(__name__)


class JudgeError(Exception):
    """Raised when a judge call fails at transport or JSON-parse level."""


class JudgeClient(Protocol):
    """Async judge seam; faked in tests."""

    async def complete_json(self, prompt: str, *, system: str | None = None) -> dict[str, Any]:
        """Run one completion and return the response parsed as a JSON object.

        Raises ``JudgeError`` on transport failure or when no JSON object can
        be extracted from the response.
        """
        ...


def extract_json(text: str) -> dict[str, Any]:
    """Best-effort extraction of the first JSON object from model output.

    Handles bare JSON, ```json fenced blocks and prose wrapping a single
    object. Raises ``JudgeError`` when no balanced object is found or it does
    not parse.
    """
    if text is None:
        raise JudgeError("empty judge response")
    stripped = text.strip()
    # Strip a leading Markdown code fence and its optional language tag.
    if stripped.startswith("```"):
        stripped = stripped.split("```", 2)[1] if stripped.count("```") >= 2 else stripped[3:]
        newline = stripped.find("\n")
        if newline != -1 and stripped[:newline].strip().isalpha():
            stripped = stripped[newline + 1 :]
    # Scan for the first balanced {...}, honouring strings and escapes.
    start = stripped.find("{")
    if start == -1:
        raise JudgeError("no JSON object in judge response")
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(stripped)):
        ch = stripped[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = stripped[start : i + 1]
                try:
                    parsed = json.loads(candidate)
                except ValueError as exc:
                    raise JudgeError(f"malformed JSON in judge response: {exc}") from exc
                if not isinstance(parsed, dict):
                    raise JudgeError("judge response JSON is not an object")
                return parsed
    raise JudgeError("unbalanced JSON object in judge response")


class OpenAIJudgeClient:
    """chat/completions client built from Settings. httpx.AsyncClient is
    constructed lazily on first use so importing this module never connects."""

    def __init__(self, settings: "object") -> None:
        self._settings = settings
        self._client: Any = None

    def _http(self) -> Any:
        if self._client is None:
            import httpx

            # Only send Authorization when a key is configured; an empty key
            # (e.g. the bundled mock LLM) would produce an illegal "Bearer "
            # header value that httpx rejects.
            headers = {}
            api_key = (self._settings.judge_api_key or "").strip()
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            self._client = httpx.AsyncClient(
                base_url=self._settings.judge_base_url,
                timeout=self._settings.judge_timeout_s,
                headers=headers,
            )
        return self._client

    async def complete_json(self, prompt: str, *, system: str | None = None) -> dict[str, Any]:
        import httpx

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body = {
            "model": self._settings.judge_model,
            "messages": messages,
            "max_tokens": self._settings.judge_max_tokens,
            "temperature": 0,
        }
        try:
            response = await self._http().post("/chat/completions", json=body)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise JudgeError(f"judge request failed: {exc}") from exc
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise JudgeError(f"unexpected judge response shape: {exc}") from exc
        return extract_json(content)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()


async def judge_json_with_retries(
    client: JudgeClient,
    prompt: str,
    *,
    system: str | None = None,
    retries: int = 2,
    base_delay: float = 0.5,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, Any] | None:
    """Call ``complete_json`` with exponential backoff; None after exhaustion.

    One initial attempt plus ``retries`` retries (spec: "2 retries with
    backoff, then None"). ``sleep`` is injectable so tests run without delay.
    """
    attempts = retries + 1
    for attempt in range(attempts):
        try:
            return await client.complete_json(prompt, system=system)
        except JudgeError as exc:
            if attempt + 1 >= attempts:
                logger.warning("judge call failed after %d attempts: %s", attempts, exc)
                return None
            await sleep(base_delay * (2**attempt))
    return None
