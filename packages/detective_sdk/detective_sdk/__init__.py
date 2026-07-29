"""Universal instrumentation helpers for Agent Detective conventions.

Pure stdlib, zero runtime dependencies. Any instrumentation — a LangChain
exporter, an OpenAI-SDK wrapper, a custom loop — can adopt these helpers to
emit the platform's deterministic-signal and versioning conventions:

- artifact_meta / artifact_meta_block: the ``[artifact_meta <path>]:`` payload
  attached beside ``artifact_text``.
- git_version / content_hash / tool_schema_hash: the ``gen_ai.agent.version``,
  ``agent_detective.prompt_hash`` and ``agent_detective.tool_schema_hash``
  attribute values.
- should_halt: the OPT-IN control hook. Agent Detective cannot stop anything
  by itself — this helper only turns a recorded breaker decision into a halt
  when the integration chooses to call it (see ``control.py``).

For code that has no instrumentation yet, ``run``/``step``/``span`` (see
``tracing.py``) ARE the instrumentation — a context manager per agent step and
an OTLP export when the run ends::

    from detective_sdk import run

    with run("intel", task=user_request) as r:
        with r.step("resolve") as s:
            s.output = company            # the work, not {"ok": true}
        with r.step("collect") as s:
            s.output = documents
            s.cost(usd=0.004, tokens_in=1200, tokens_out=340, model="gpt-4o")

``branch``/``join`` and ``retry`` cover the two shapes span nesting cannot
express — a fan-IN (one parent id per span, so a joiner has no edge from the
work it merged) and a loop (attempts of one agent get no edge between them).

Off unless ``AGENT_DETECTIVE_ENDPOINT`` or ``AGENT_DETECTIVE_TRACE_FILE`` is set.
"""

from .artifacts import artifact_meta, artifact_meta_block, detect_kind
from .control import TOOL_SCHEMA_HASH_ATTRIBUTE, should_halt
from .tracing import ATTEMPT_SEPARATOR, MAX_PAYLOAD_CHARS, Retry, Run, Span, run
from .versioning import content_hash, git_version, tool_schema_hash

__version__ = "0.2.0"

__all__ = [
    "detect_kind",
    "artifact_meta",
    "artifact_meta_block",
    "git_version",
    "content_hash",
    "tool_schema_hash",
    "TOOL_SCHEMA_HASH_ATTRIBUTE",
    "should_halt",
    "run",
    "Run",
    "Span",
    "Retry",
    "MAX_PAYLOAD_CHARS",
    "ATTEMPT_SEPARATOR",
]
