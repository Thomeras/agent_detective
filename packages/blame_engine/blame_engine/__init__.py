"""Blame analysis engine for multi-agent execution graphs.

Pure Python (networkx only), no I/O. Public API: find_blame(); the helpers
condense, detect_loop_anomalies, select_candidates, compute_confidence and
downstream_cost are exported for tests.
"""

from .blame import find_blame
from .condense import Condensation, SuperNode, condense
from .confidence import compute_confidence
from .cost import downstream_cost
from .cutpoint import Candidate, select_candidates
from .loops import detect_loop_anomalies
from .types import (
    BlameConfig,
    BlameInput,
    BlameReport,
    Evidence,
    LoopAnomaly,
    LoopBaseline,
    NodeScore,
    ReportType,
    TerminalVerdict,
)

__version__ = "0.1.0"

__all__ = [
    "find_blame",
    "condense",
    "select_candidates",
    "detect_loop_anomalies",
    "compute_confidence",
    "downstream_cost",
    "Candidate",
    "Condensation",
    "SuperNode",
    "BlameConfig",
    "BlameInput",
    "BlameReport",
    "Evidence",
    "LoopAnomaly",
    "LoopBaseline",
    "NodeScore",
    "ReportType",
    "TerminalVerdict",
]
