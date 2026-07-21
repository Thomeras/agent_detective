"""Thin OpenAI-compatible chat-completions client for the demo agents.

The demo owns its structured outputs, so an agent's LLM reply is used only for
realism and token accounting. The client therefore never fails the pipeline:
if the mock LLM is unreachable it returns a canned offline completion so the
pipeline (and its OTLP payload) can still be produced without a live server.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class LLMClient:
    def __init__(self, base_url: str, model: str, timeout_s: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_s = timeout_s

    def complete(self, prompt: str) -> tuple[str, int, int]:
        """Return (content, prompt_tokens, completion_tokens).

        Falls back to a canned response (with estimated tokens) when the LLM
        endpoint cannot be reached.
        """
        url = f"{self._base_url}/chat/completions"
        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }
        try:
            resp = httpx.post(url, json=payload, timeout=self._timeout_s)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage") or {}
            return (
                str(content),
                int(usage.get("prompt_tokens", max(1, len(prompt) // 4))),
                int(usage.get("completion_tokens", max(1, len(str(content)) // 4))),
            )
        except Exception:
            logger.warning("LLM call failed (%s); using offline fallback", url, exc_info=False)
            content = "[offline] step completed"
            return content, max(1, len(prompt) // 4), max(1, len(content) // 4)
