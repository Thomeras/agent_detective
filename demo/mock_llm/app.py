"""Content-aware, dependency-light mock LLM (build spec section 6.5).

An OpenAI-compatible ``POST /v1/chat/completions`` server with canned,
deterministic responses driven purely by prompt content. It plays two roles at
once, with no API keys:

- **The pipeline's LLM.** ``demo/synthetic_pipeline`` calls it for each agent
  step. The demo owns its structured outputs, so agent-generation replies are
  short, plausible completions and their exact text does not affect the graph.
- **The worker's judge.** The two-tier worker (M4) sends per-node judge prompts
  (expecting JSON ``{task_score, input_flawed, reasoning}``) and a terminal
  judge prompt (expecting ``{ok|bad, score, reasoning}``). This server detects
  which is which from the prompt and answers with valid JSON.

The flagship demo scenario is a *silent* scraper hallucination: the scraper
invents prices that the source pages never listed, and every downstream agent
transforms that fabricated data faithfully. Detection is therefore
content-driven: a concrete price is a fabrication (the source has none), so

- a per-node judge scores the *scraper* low when its output carries a price,
- downstream nodes still score high but are marked ``input_flawed`` when their
  input already carried a fabricated price, and
- the terminal judge fails the run when the final output carries a price.

That makes the worker flag the graph and blame the scraper, without the mock
ever needing to see the source pages. In a clean run no node emits a price, so
every verdict is healthy and no incident is raised.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="agent-detective-mock-llm")

# A concrete monetary amount. The demo's source pages list no prices, so any
# amount that appears is fabricated. Matches "$24.99", "24,99 zl", "42.00 PLN",
# "19.99 EUR", "15,00 zloty".
_PRICE_RE = re.compile(
    r"(?:[$€£]\s?\d[\d.,]*\d)"
    r"|(?:\d[\d.,]*\d\s?(?:z[lł]|zloty|pln|usd|eur))",
    re.IGNORECASE,
)

# Stage markers the demo stamps into each agent's output so a judge can tell
# which node it is scoring from content alone (see synthetic_pipeline).
_STAGES = ("scrape", "translate", "compliance", "publish", "orchestrate")


def _contains_price(text: str) -> bool:
    return bool(_PRICE_RE.search(text))


def _detect_stage(text: str) -> str | None:
    """Identify the node under judgement from its embedded ``stage`` marker."""
    for stage in _STAGES:
        if f'"stage": "{stage}"' in text or f'"stage":"{stage}"' in text:
            return stage
    # Fall back to agent-name mentions in the prompt.
    lowered = text.lower()
    for name, stage in (
        ("scraper", "scrape"),
        ("translator", "translate"),
        ("compliance", "compliance"),
        ("publisher", "publish"),
        ("orchestrator", "orchestrate"),
    ):
        if name in lowered:
            return stage
    return None


def _messages_text(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            # OpenAI "parts" content form.
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
    return "\n".join(parts)


def _classify(text: str) -> str:
    """Route a prompt to 'per_node_judge', 'terminal_judge', 'claims' or 'agent'."""
    lowered = text.lower()
    if "task_score" in lowered or "input_flawed" in lowered:
        return "per_node_judge"
    verdict_words = ("terminal", "final output", "overall goal", "verdict", "ok/bad")
    if any(w in lowered for w in verdict_words) and (
        '"score"' in lowered or "score" in lowered
    ):
        return "terminal_judge"
    if '"claims"' in lowered or ("claims" in lowered and "suspect output" in lowered):
        return "claims"
    return "agent"


def _claims_response(text: str) -> dict[str, Any]:
    """Extract the concrete claims (fabricated prices) from the suspect output.

    Returned claims are searched verbatim against downstream outputs by the
    worker, so returning the price strings is exactly what surfaces the
    hallucination propagating from the scraper to the published product.
    """
    claims: list[str] = []
    seen: set[str] = set()
    for match in _PRICE_RE.findall(text):
        value = match.strip()
        if value and value not in seen:
            seen.add(value)
            claims.append(value)
    if not claims:
        claims = ["no concrete fabricated claims detected"]
    return {"claims": claims[:5]}


def _per_node_verdict(text: str) -> dict[str, Any]:
    stage = _detect_stage(text)
    price = _contains_price(text)
    if stage == "scrape":
        if price:
            return {
                "task_score": 0.15,
                "input_flawed": False,
                "reasoning": (
                    "The scraper emitted concrete prices, but the source pages "
                    "list no prices. These values are fabricated (hallucinated); "
                    "the scrape task was to report only what the pages contain."
                ),
            }
        return {
            "task_score": 0.9,
            "input_flawed": False,
            "reasoning": "Prices correctly reported as unavailable; output matches source.",
        }
    # Downstream nodes (translator, compliance, publisher, orchestrator):
    # they transform their input faithfully. If the input already carries a
    # fabricated price, the fault is inherited, not introduced here.
    return {
        "task_score": 0.85,
        "input_flawed": bool(price),
        "reasoning": (
            "Output is a faithful transformation of the provided input"
            + (
                "; however the input already carried fabricated prices, so the "
                "flaw is inherited from upstream."
                if price
                else "."
            )
        ),
    }


def _terminal_verdict(text: str) -> dict[str, Any]:
    if _contains_price(text):
        return {
            "verdict": "bad",
            "ok": False,
            "bad": True,
            "score": 0.2,
            "reasoning": (
                "The published products carry concrete prices, but the source "
                "pages list none. The final output contains fabricated prices."
            ),
        }
    return {
        "verdict": "ok",
        "ok": True,
        "bad": False,
        "score": 0.92,
        "reasoning": "Published products are consistent with the source; no fabricated data.",
    }


def _agent_completion(text: str) -> str:
    stage = _detect_stage(text)
    label = stage if stage else "requested"
    return f"Completed the {label} step as instructed."


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> JSONResponse:
    try:
        body: Any = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "invalid JSON body"}}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": {"message": "body must be an object"}}, status_code=400)

    messages = body.get("messages") or []
    model = body.get("model") or "mock-llm"
    text = _messages_text(messages if isinstance(messages, list) else [])
    kind = _classify(text)

    if kind == "per_node_judge":
        content = json.dumps(_per_node_verdict(text))
    elif kind == "terminal_judge":
        content = json.dumps(_terminal_verdict(text))
    elif kind == "claims":
        content = json.dumps(_claims_response(text))
    else:
        content = _agent_completion(text)

    prompt_tokens = max(1, len(text) // 4)
    completion_tokens = max(1, len(content) // 4)
    return JSONResponse(
        {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
    )


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})
