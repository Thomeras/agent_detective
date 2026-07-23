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
"""

from .artifacts import artifact_meta, artifact_meta_block, detect_kind
from .control import TOOL_SCHEMA_HASH_ATTRIBUTE, should_halt
from .versioning import content_hash, git_version, tool_schema_hash

__version__ = "0.1.0"

__all__ = [
    "detect_kind",
    "artifact_meta",
    "artifact_meta_block",
    "git_version",
    "content_hash",
    "tool_schema_hash",
    "TOOL_SCHEMA_HASH_ATTRIBUTE",
    "should_halt",
]
