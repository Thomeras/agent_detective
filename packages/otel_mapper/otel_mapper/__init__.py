"""OTEL span to AgentRun/Edge candidate mapping.

Pure mapping, no I/O. Implements build spec section 6.1 (milestone M2).
License: Apache-2.0. See ``otel_mapper.mapper``'s module docstring for the
accepted input shapes, run/edge keying, and the correlation-header
limitation.
"""

from .ids import graph_id_from_str, run_id_from_key
from .mapper import flatten_export_request, map_spans
from .types import (
    AgentRunCandidate,
    EdgeCandidate,
    EdgeType,
    MappingResult,
    UnresolvedDelegation,
)

__version__ = "0.3.0"

__all__ = [
    "AgentRunCandidate",
    "EdgeCandidate",
    "EdgeType",
    "MappingResult",
    "UnresolvedDelegation",
    "flatten_export_request",
    "graph_id_from_str",
    "map_spans",
    "run_id_from_key",
    "__version__",
]
