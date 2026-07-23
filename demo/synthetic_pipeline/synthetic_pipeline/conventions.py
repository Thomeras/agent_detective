"""OpenInference / OpenTelemetry GenAI attribute keys used by the demo.

These are the exact keys ``packages/otel_mapper`` reads to reconstruct runs and
edges (build spec section 6.1). The OpenInference span-kind and input/output
keys come from the ``openinference-semantic-conventions`` package; the GenAI
``gen_ai.*`` keys are OpenTelemetry semantic conventions used as plain strings.
"""

from __future__ import annotations

from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes

# OpenInference.
SPAN_KIND = SpanAttributes.OPENINFERENCE_SPAN_KIND  # "openinference.span.kind"
INPUT_VALUE = SpanAttributes.INPUT_VALUE  # "input.value"
OUTPUT_VALUE = SpanAttributes.OUTPUT_VALUE  # "output.value"

KIND_AGENT = OpenInferenceSpanKindValues.AGENT.value  # "AGENT"
KIND_TOOL = OpenInferenceSpanKindValues.TOOL.value  # "TOOL"
KIND_LLM = OpenInferenceSpanKindValues.LLM.value  # "LLM"

# OpenTelemetry GenAI semantic conventions.
AGENT_NAME = "gen_ai.agent.name"
AGENT_VERSION = "gen_ai.agent.version"
USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
USAGE_COST = "gen_ai.usage.cost"
TOOL_NAME = "gen_ai.tool.name"
TOOL_TARGET_AGENT = "gen_ai.tool.target_agent"

# A2A (agent-to-agent) message correlation. ``otel_mapper`` derives an
# A2A_MESSAGE edge from any span carrying ``a2a.task_id`` whose
# ``a2a.peer_agent`` resolves to a known run (gated by A2A_DETECTION).
A2A_TASK_ID = "a2a.task_id"
A2A_PEER_AGENT = "a2a.peer_agent"

# Correlation attribute that joins all runs into one execution graph
# (build spec 6.1 fallback rule).
GRAPH_ID = "x-execution-graph-id"
