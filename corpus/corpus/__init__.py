"""Foreign-trace corpus: agent_topo_db topologies, instrumented from outside.

The grid in ``packages/blame_engine/tests`` exercises the engine on traces this
project shaped itself. This package exercises the layer below it — ``otel_mapper``
— on spans produced by the stock OpenTelemetry SDK over code that knows nothing
about Agent Detective, with faults injected from outside so every entry carries
ground truth.
"""

from .inject import FAULTS, Fault, ResponseInjector
from .otel_bridge import JsonFileSpanExporter, build_tracer_provider
from .topolab_adapter import TopolabTracer

__all__ = [
    "FAULTS",
    "Fault",
    "ResponseInjector",
    "JsonFileSpanExporter",
    "build_tracer_provider",
    "TopolabTracer",
]
