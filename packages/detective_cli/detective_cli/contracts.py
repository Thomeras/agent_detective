"""``detective contracts`` — the output-contract channel, without the UI.

Unlike ``analyze``/``capture`` there is no local mode here: contracts live in
the database the deployed worker reads, so every command talks to the API
(``DETECTIVE_API_URL``, default ``http://localhost:8000``).

Why this exists at all: an empty ``output_contracts`` table is the default
state of a fresh install, and an empty table means the schema component is null
on every node — the composite then renormalizes the judge's weight upward and
"three independent channels" quietly becomes two. Registering one contract is
what closes that, and until now it took hand-written SQL.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

from .render import Painter

DEFAULT_API_URL = "http://localhost:8000"


class ApiError(RuntimeError):
    """The API could not be reached, or answered with an error."""


def api_base_url() -> str:
    return os.environ.get("DETECTIVE_API_URL", DEFAULT_API_URL).rstrip("/")


def request(
    method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 15.0
) -> Any:
    url = f"{api_base_url()}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise ApiError(f"{url} — HTTP {exc.code}: {_error_detail(exc)}") from exc
    except (OSError, ValueError) as exc:
        raise ApiError(f"{url} — {exc}") from exc


def _error_detail(exc: urllib.error.HTTPError) -> str:
    """FastAPI's `detail`, or the raw body when it is not a FastAPI error."""
    try:
        payload = json.loads(exc.read())
    except (OSError, ValueError):
        return exc.reason or "no response body"
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return detail if isinstance(detail, str) else json.dumps(detail or payload)


def contract_body(raw: str) -> dict[str, Any]:
    """A registerable body from either a bare contract or a suggest envelope.

    ``detective contracts suggest --json > c.json`` then ``register --file
    c.json`` has to work unchanged — a review step that requires hand-editing
    the file is a review step people skip.
    """
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    if "json_schema" not in data and "contract" in data:
        if data["contract"] is None:
            reason = data.get("reason") or "no schema was proposed"
            raise ValueError(f"this suggestion proposed no contract: {reason}")
        data = data["contract"]
    if not isinstance(data.get("json_schema"), dict):
        raise ValueError("no json_schema object found")
    if not str(data.get("agent_name") or "").strip():
        raise ValueError("no agent_name found")
    return {
        "agent_name": data["agent_name"],
        "agent_version_pattern": data.get("agent_version_pattern"),
        "json_schema": data["json_schema"],
    }


def read_source(source: str) -> str:
    """A file path, or '-' for stdin."""
    if source == "-":
        return sys.stdin.read()
    with open(source, encoding="utf-8") as handle:
        return handle.read()


# --- Terminal rendering ---------------------------------------------------


def _schema_summary(schema: dict[str, Any]) -> str:
    required = schema.get("required") or []
    types = schema.get("type")
    head = ", ".join(types) if isinstance(types, list) else str(types or "any")
    if not required:
        return f"{head} · no required keys"
    return f"{head} · requires {', '.join(str(k) for k in required)}"


def render_list(payload: dict[str, Any], *, color: bool = True) -> str:
    paint = Painter(color)
    contracts = payload.get("contracts") or []
    out = [paint(f"Output contracts — {api_base_url()}", "bold")]
    if not contracts:
        out.append(
            paint(
                "   none registered — the schema channel scores nothing on every "
                "node, and the judge's weight renormalizes to cover it",
                "warn",
            )
        )
        out.append(paint("   detective contracts suggest --agent-name <name>", "dim"))
        return "\n".join(out)
    width = max(len(str(row.get("agent_name"))) for row in contracts)
    for row in contracts:
        name = str(row.get("agent_name")).ljust(width)
        pattern = str(row.get("agent_version_pattern") or "*")
        out.append(f"   {name}  {paint(pattern, 'dim')}")
        out.append(paint(f"   {' ' * width}  {_schema_summary(row.get('json_schema') or {})}", "dim"))
    return "\n".join(out)


def render_registered(payload: dict[str, Any], *, color: bool = True) -> str:
    paint = Painter(color)
    verb = "replaced" if payload.get("replaced") else "registered"
    out = [
        paint(
            f"{verb} contract for {payload.get('agent_name')} "
            f"@ {payload.get('agent_version_pattern')}",
            "bold",
            "ok",
        ),
        paint(f"   {_schema_summary(payload.get('json_schema') or {})}", "dim"),
    ]
    ignored = payload.get("ignored_keywords") or []
    if ignored:
        # Stored, but the scoring engine implements a subset — a contract read
        # as stricter than it is scores nothing extra.
        out.append(
            paint(
                f"   not enforced by scoring (ignored): {', '.join(str(k) for k in ignored)}",
                "warn",
            )
        )
    return "\n".join(out)


def render_suggestion(payload: dict[str, Any], *, color: bool = True) -> str:
    paint = Painter(color)
    samples = payload.get("samples") or {}
    out = [paint(f"Suggested contract — {payload.get('agent_name')}", "bold")]
    out.append(
        paint(
            f"   {samples.get('runs_examined', 0)} run(s) examined · "
            f"{samples.get('runs_with_output', 0)} with output · "
            f"{samples.get('usable_samples', 0)} usable JSON object(s) · "
            f"{samples.get('failed_runs', 0)} failed run(s) · "
            f"minimum {payload.get('min_samples')}",
            "dim",
        )
    )

    keys = payload.get("keys") or []
    if keys:
        out.append("")
        out.append(paint("   Observed keys", "bold"))
        width = max(len(str(k.get("key"))) for k in keys)
        for entry in keys:
            mark = "●" if entry.get("included") else "○"
            tone = "ok" if entry.get("included") else "dim"
            presence = f"{entry.get('present_in')}/{samples.get('usable_samples', 0)}"
            out.append(
                f"     {paint(mark, tone)} {str(entry.get('key')).ljust(width)}  "
                f"{presence.rjust(6)}  {paint(str(entry.get('note')), 'dim')}"
            )

    contract = payload.get("contract")
    out.append("")
    if contract is None:
        out.append(paint("   no contract proposed", "bold", "warn"))
        out.append(paint(f"   {payload.get('reason')}", "dim"))
        return "\n".join(out)

    out.append(paint("   Proposed schema", "bold"))
    for line in json.dumps(contract["json_schema"], indent=2, ensure_ascii=False).splitlines():
        out.append(f"     {line}")
    out.append("")
    out.append(paint("   Review it, then register it:", "dim"))
    out.append(
        paint(
            f"     detective contracts suggest --agent-name {payload.get('agent_name')} "
            "--json > contract.json",
            "dim",
        )
    )
    out.append(paint("     detective contracts register --file contract.json", "dim"))
    return "\n".join(out)
