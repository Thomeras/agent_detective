"""Blame analysis engine for multi-agent execution graphs.

Pure Python (networkx only), no I/O. Public API lands in milestone M1:
find_blame() plus condense, detect_loop_anomalies, select_candidates,
compute_confidence and downstream_cost for tests.
"""

__version__ = "0.1.0"
