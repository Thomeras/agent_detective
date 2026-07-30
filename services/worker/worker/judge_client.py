"""OpenAI-compatible chat/completions judge client (build spec section 4.3).

A deliberately thin client: one ``complete_json`` call posts a single-user-turn
chat completion and returns the model's response parsed as JSON. It works
against the Anthropic OpenAI-compat endpoint, Ollama and the demo mock_llm
because it only relies on the ``/chat/completions`` shape and extracts JSON
robustly (strip Markdown fences, then take the first balanced ``{...}`` object).

The ``JudgeClient`` protocol is the test seam; ``FakeJudge`` in the test harness
returns canned verdicts keyed on prompt content. ``judge_json_with_retries``
adds the spec's "2 retries with backoff, then None" policy on top of any client.

Judge calls cost money, so every call is also accounted here (``JudgeSpend``):
tokens always, dollars only when the endpoint prices the call itself or
JUDGE_PRICE_* is configured — an unknown cost stays null, never $0. With
JUDGE_MAX_SPEND_USD set, a reached cap turns further calls into
``PermanentJudgeError``, which the retry helper already converts to "no
verdict": the affected nodes come back unjudged instead of the analysis dying.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterator, Protocol

logger = logging.getLogger(__name__)

# How often unscoped spend reaches the log; a scope logs once, on exit.
_UNSCOPED_SPEND_LOG_EVERY = 25


class JudgeError(Exception):
    """Raised when a judge call fails at transport or JSON-parse level."""


class PermanentJudgeError(JudgeError):
    """A judge failure that retrying cannot fix.

    The retry loop treats it as immediately exhausted: there is no judge to
    reach (none configured for this run), so backing off and asking again only
    buys latency. Callers see the same ``None`` an exhausted retry produces —
    "no judged verdict" is one outcome regardless of why — so nothing
    downstream needs to distinguish the two.
    """


class JudgeSpendExhausted(PermanentJudgeError):
    """The configured JUDGE_MAX_SPEND_USD is used up; no further calls are made.

    A subclass of ``PermanentJudgeError`` on purpose: retrying cannot buy back
    budget, and the existing "no verdict" handling (node left unscored) is
    exactly the right outcome — a partial analysis that says so beats a crash.
    """


@dataclass
class JudgeSpend:
    """Running total of what the judge cost.

    ``cost_usd`` is None until at least one call could actually be priced;
    unknown is null, never $0. ``unpriced_calls`` says how much of the total is
    missing, so a dollar figure is never mistaken for the whole bill.
    """

    calls: int = 0
    priced_calls: int = 0
    unpriced_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float | None = None
    # Which model(s) ran up this bill — a cost without its model says nothing.
    models: set[str] = field(default_factory=set)
    # One-shot log guards; state, not configuration.
    _cap_logged: bool = field(default=False, repr=False)
    _unpriced_logged: bool = field(default=False, repr=False)

    def record(
        self,
        *,
        cost_usd: float | None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        model: str | None = None,
    ) -> None:
        if model:
            self.models.add(model)
        self.calls += 1
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        if cost_usd is None:
            self.unpriced_calls += 1
            return
        self.priced_calls += 1
        self.cost_usd = cost_usd if self.cost_usd is None else self.cost_usd + cost_usd

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "priced_calls": self.priced_calls,
            "unpriced_calls": self.unpriced_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": self.cost_usd,
            "models": sorted(self.models),
        }

    def describe(self) -> str:
        cost = (
            "unknown (no usage cost and no JUDGE_PRICE_* configured)"
            if self.cost_usd is None
            else f"${self.cost_usd:.6f}"
            + (f" + {self.unpriced_calls} unpriced call(s)" if self.unpriced_calls else "")
        )
        return (
            f"model={','.join(sorted(self.models)) or 'unknown'} calls={self.calls} "
            f"prompt_tokens={self.prompt_tokens} completion_tokens={self.completion_tokens} "
            f"cost_usd={cost}"
        )


# Process-lifetime fallback: used whenever no per-analysis scope is active.
PROCESS_JUDGE_SPEND = JudgeSpend()

_spend_scope: ContextVar[JudgeSpend | None] = ContextVar("judge_spend_scope", default=None)


def active_judge_spend() -> JudgeSpend:
    """The ledger a call counts against: the open scope, else the process total."""
    return _spend_scope.get() or PROCESS_JUDGE_SPEND


@contextmanager
def judge_spend_scope(label: str) -> Iterator[JudgeSpend]:
    """Account (and log) judge spend for one analysis.

    Tasks spawned inside the scope inherit the same ledger object, so a
    fan-out over nodes still totals into one line. With JUDGE_MAX_SPEND_USD set
    the cap applies to this scope; unscoped calls fall back to a
    process-lifetime cap instead.
    """
    ledger = JudgeSpend()
    token = _spend_scope.set(ledger)
    try:
        yield ledger
    finally:
        _spend_scope.reset(token)
        logger.info("judge spend for %s: %s", label, ledger.describe())


def _as_int(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def usage_tokens(usage: Any) -> tuple[int, int]:
    """(prompt, completion) tokens from an OpenAI-compatible usage block."""
    if not isinstance(usage, dict):
        return 0, 0
    prompt = _as_int(usage.get("prompt_tokens")) or _as_int(usage.get("input_tokens"))
    completion = _as_int(usage.get("completion_tokens")) or _as_int(usage.get("output_tokens"))
    return prompt, completion


def usage_cost_usd(usage: Any, settings: Any) -> float | None:
    """Dollar cost of one call, or None when it genuinely cannot be known.

    Order matters: a cost the endpoint reports itself (OpenRouter's
    ``usage.cost``) is the real bill and beats any local price table.
    """
    if isinstance(usage, dict):
        for key in ("cost", "total_cost"):
            reported = _as_float(usage.get(key))
            if reported is not None:
                return reported
    prompt_price = getattr(settings, "judge_price_prompt_usd_per_1k", None)
    completion_price = getattr(settings, "judge_price_completion_usd_per_1k", None)
    if prompt_price is None or completion_price is None:
        return None
    prompt, completion = usage_tokens(usage)
    if prompt == 0 and completion == 0:
        # Priced model, but the response told us nothing to price.
        return None
    return (prompt / 1000.0) * float(prompt_price) + (completion / 1000.0) * float(completion_price)


class JudgeClient(Protocol):
    """Async judge seam; faked in tests."""

    @property
    def model(self) -> str | None:
        """The model string this client actually calls, for the record.

        None means "no model answered" (a disabled/null judge). Read it with
        ``getattr(judge, "model", None)``: third-party and older fakes predate
        this member and a missing model must not crash an analysis.
        """
        ...

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


class NullJudge:
    """A judge that never answers, so `score_node` runs its deterministic half
    and nothing else. Used by the CLI's --no-judge and by the tier2 gate."""

    # No model answered, so there is no model to record against a verdict.
    model: str | None = None

    async def complete_json(self, prompt: str, *, system: str | None = None) -> dict:
        raise PermanentJudgeError("judge disabled")

    async def close(self) -> None:
        return None


class OpenAIJudgeClient:
    """chat/completions client built from Settings. httpx.AsyncClient is
    constructed lazily on first use so importing this module never connects."""

    def __init__(self, settings: "object") -> None:
        self._settings = settings
        self._client: Any = None
        # Which model judges (and whether it is the mock) decides what a verdict
        # is worth; say it at construction so it is in the log, not in `env`.
        describe = getattr(settings, "describe_judge", None)
        if callable(describe):
            logger.info("%s", describe())

    @property
    def model(self) -> str | None:
        """The model string verdicts should be recorded against (Settings.judge_model)."""
        return getattr(self._settings, "judge_model", None)

    @property
    def spend(self) -> JudgeSpend:
        """The ledger this client's next call counts against."""
        return active_judge_spend()

    def _spend_cap_reached(self) -> bool:
        cap = getattr(self._settings, "judge_max_spend_usd", None)
        if cap is None:
            return False
        spend = active_judge_spend()
        if spend.cost_usd is not None and spend.cost_usd >= float(cap):
            if not spend._cap_logged:
                spend._cap_logged = True
                logger.warning(
                    "judge spend cap reached ($%.6f of $%g after %d call(s)); "
                    "remaining nodes stay unjudged",
                    spend.cost_usd,
                    float(cap),
                    spend.calls,
                )
            return True
        if spend.cost_usd is None and spend.unpriced_calls and not spend._unpriced_logged:
            # A cap that cannot bind is worse than no cap, because it looks like one.
            spend._unpriced_logged = True
            logger.warning(
                "JUDGE_MAX_SPEND_USD=%g cannot be enforced: %d judge call(s) reported no "
                "cost and no JUDGE_PRICE_PROMPT_USD_PER_1K / "
                "JUDGE_PRICE_COMPLETION_USD_PER_1K is set",
                float(cap),
                spend.unpriced_calls,
            )
        return False

    def _record_spend(self, usage: Any) -> None:
        prompt_tokens, completion_tokens = usage_tokens(usage)
        cost = usage_cost_usd(usage, self._settings)
        spend = active_judge_spend()
        spend.record(
            cost_usd=cost,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=self.model,
        )
        logger.debug("judge call: %s", spend.describe())
        if _spend_scope.get() is None and spend.calls % _UNSCOPED_SPEND_LOG_EVERY == 0:
            # With no per-analysis scope open, this is the only place spend surfaces.
            logger.info("judge spend (process total): %s", spend.describe())

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

        if self._spend_cap_reached():
            raise JudgeSpendExhausted(
                f"judge spend cap (JUDGE_MAX_SPEND_USD="
                f"{getattr(self._settings, 'judge_max_spend_usd', None)}) reached"
            )
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
        # Determinism knob (JUDGE_SEED): forwarded verbatim when configured.
        # Backends that ignore "seed" still accept the request; None sends
        # nothing so the default body is byte-identical to before.
        if self._settings.judge_seed is not None:
            body["seed"] = self._settings.judge_seed
        try:
            response = await self._http().post("/chat/completions", json=body)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise JudgeError(f"judge request failed: {exc}") from exc
        # Bill first: a completion that arrived was paid for even if its content
        # turns out to be unparseable.
        self._record_spend(data.get("usage") if isinstance(data, dict) else None)
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
    on_unavailable: Callable[[PermanentJudgeError], None] | None = None,
) -> dict[str, Any] | None:
    """Call ``complete_json`` with exponential backoff; None after exhaustion.

    One initial attempt plus ``retries`` retries (spec: "2 retries with
    backoff, then None"). ``sleep`` is injectable so tests run without delay.
    """
    attempts = retries + 1
    for attempt in range(attempts):
        try:
            return await client.complete_json(prompt, system=system)
        except PermanentJudgeError as exc:
            # Nothing to retry against, and nothing alarming either — a run
            # with no judge configured is a supported mode, not a fault. The
            # callback lets the caller tell WHY it got no verdict: an exhausted
            # budget is an operator's decision, an unreachable judge is a fault,
            # and a report that renders them identically hides which one it was.
            if on_unavailable is not None:
                on_unavailable(exc)
            logger.debug("judge unavailable: %s", exc)
            return None
        except JudgeError as exc:
            if attempt + 1 >= attempts:
                logger.warning("judge call failed after %d attempts: %s", attempts, exc)
                return None
            await sleep(base_delay * (2**attempt))
    return None
