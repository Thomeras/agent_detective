"""Opt-in control hook (pure stdlib).

Honesty first: **Agent Detective cannot stop anything.** It is an
observability platform — it records breaker decisions in its own database and
exposes them over the API, but it holds no lever inside your agent. This hook
is the integration's OPT-IN: an agent loop that *chooses* to call
:func:`should_halt` before doing work turns a recorded decision into an actual
halt. An agent that never calls it is completely unaffected.

The hook is deliberately tolerant: any network, HTTP, or JSON failure returns
``False``. Observability must never take the agent down — an unreachable
Agent Detective deployment means "no known reason to halt", not an outage.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

# Span/resource attribute carrying the 12-hex tool-schema hash
# (see tool_schema_hash in versioning.py).
TOOL_SCHEMA_HASH_ATTRIBUTE = "agent_detective.tool_schema_hash"


def should_halt(endpoint: str, agent_name: str, *, timeout_s: float = 2.0) -> bool:
    """True iff Agent Detective records an OPEN breaker for ``agent_name``.

    Performs ``GET {endpoint}/control/breakers`` (the API service's endpoint)
    and looks for a breaker row with ``state == 'open'`` scoped to
    ``scope_kind == 'agent_name'`` and ``scope_value == agent_name``.

    TOLERANT BY DESIGN: any error — connection refused, timeout, non-2xx,
    unparseable JSON, unexpected shape — returns ``False``. A halt only ever
    happens on a positively confirmed open breaker.
    """
    url = endpoint.rstrip("/") + "/control/breakers"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            body = resp.read()
        payload = json.loads(body)
    except Exception:  # noqa: BLE001 - tolerance is the contract, see docstring
        return False
    if not isinstance(payload, dict):
        return False
    breakers = payload.get("breakers")
    if not isinstance(breakers, list):
        return False
    for row in breakers:
        if not isinstance(row, dict):
            continue
        if (
            row.get("state") == "open"
            and row.get("scope_kind") == "agent_name"
            and row.get("scope_value") == agent_name
        ):
            return True
    return False
