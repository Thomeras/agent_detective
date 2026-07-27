"""Choosing a judge for a local run — and running honestly without one.

Agent Detective reads two independent evidence channels. The **deterministic**
channel (contract breaches, named signals, loop anomalies, artifact integrity)
is rules over payloads: it needs nothing but the trace. The **judged** channel
needs a model.

So the default here is no judge. `pip install agent-detective` then
`detective analyze trace.json` performs the full deterministic analysis
offline, with no API key and no network — and says so, rather than presenting a
one-channel verdict as if both had spoken. Point ``JUDGE_BASE_URL`` /
``JUDGE_MODEL`` at any OpenAI-compatible endpoint (a hosted model, Ollama, the
bundled mock) to turn the second channel on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from worker.config import Settings
from worker.judge_client import JudgeClient, OpenAIJudgeClient, PermanentJudgeError

# The worker's Settings default — a placeholder pointing at a local port
# nothing is listening on. Treating it as "configured" would make every run
# wait on connection refusals.
_PLACEHOLDER_BASE_URL = "http://localhost:8080/v1"


class NullJudge:
    """A ``JudgeClient`` that never judges.

    Every call fails permanently, so ``judge_json_with_retries`` returns None
    without backoff and scoring records the node as unscored with
    ``insufficient_components`` — the same state a genuinely unreachable judge
    produces. That is the honest representation: no verdict was obtained. The
    alternative (defaulting nodes to a passing score) would manufacture the one
    thing this project exists to prevent — confidence with nothing behind it.
    """

    async def complete_json(self, prompt: str, *, system: str | None = None) -> dict[str, Any]:
        raise PermanentJudgeError("no judge configured (judged channel is off)")

    async def close(self) -> None:
        pass


@dataclass(frozen=True)
class JudgeChoice:
    """The judge a run will use, and the one-line reason it was chosen."""

    client: JudgeClient
    enabled: bool
    description: str

    async def close(self) -> None:
        close = getattr(self.client, "close", None)
        if close is not None:
            await close()


def judge_configured(settings: Settings) -> bool:
    """True when the environment actually points at a reachable-looking judge.

    "Configured" means the base URL was set to something other than the
    placeholder default — an unset environment must not be read as "there is a
    judge at localhost:8080".
    """
    base_url = (settings.judge_base_url or "").strip()
    return bool(base_url) and base_url != _PLACEHOLDER_BASE_URL


def select_judge(settings: Settings, *, force_off: bool = False) -> JudgeChoice:
    """Pick the judge for this run: configured endpoint, or none at all."""
    if force_off:
        return JudgeChoice(
            client=NullJudge(),
            enabled=False,
            description="disabled with --no-judge",
        )
    if not judge_configured(settings):
        return JudgeChoice(
            client=NullJudge(),
            enabled=False,
            description="not configured (set JUDGE_BASE_URL and JUDGE_MODEL)",
        )
    return JudgeChoice(
        client=OpenAIJudgeClient(settings),
        enabled=True,
        description=f"{settings.judge_model} at {settings.judge_base_url}",
    )
