"""Data types for the blame engine. Mirrors spec section 3.2 exactly."""

from dataclasses import dataclass
from typing import Literal

ReportType = Literal["cut_point", "multi_culprit", "composition_failure",
                     "loop_detected", "root_cause_external", "unclassified"]


@dataclass(frozen=True)
class NodeScore:
    run_id: str
    score: float | None                    # None = UNKNOWN, never defaults to 1.0
    components: dict[str, float | None]    # {"schema": .., "judge": .., "heuristics": ..}
    input_flawed: bool | None              # judge verdict: was the node's INPUT already flawed?
    unscored_reason: str | None            # "judge_error" | "payload_missing" | "insufficient_components"
    judge_note: str | None


@dataclass(frozen=True)
class LoopBaseline:
    mean_iterations: float
    std_iterations: float
    sample_count: int


@dataclass(frozen=True)
class TerminalVerdict:                     # Tier 1 result
    bad: bool
    score: float | None
    reasoning: str | None


@dataclass(frozen=True)
class BlameConfig:
    threshold: float = 0.5
    gap_threshold: float = 0.25            # "significant drop"
    min_drop: float = 0.10                 # below this = inherited degradation, not origin
    max_loop_iterations: int = 10
    loop_zscore: float = 3.0
    loop_min_history: int = 5
    unknown_confidence_cap: float = 0.6
    scc_confidence_penalty: float = 0.8
    multi_culprit_penalty: float = 0.8


@dataclass(frozen=True)
class BlameInput:
    nodes: list[str]                        # run_ids
    edges: list[tuple[str, str]]            # (from, to); cycles ALLOWED
    scores: dict[str, NodeScore]
    node_costs: dict[str, float]
    node_end_times: dict[str, float]        # epoch s; SCC exit-node + tie-breaks
    agent_names: dict[str, str]
    error_span_ids: dict[str, list[str]]
    terminal_verdict: TerminalVerdict | None
    loop_baselines: dict[str, LoopBaseline]  # key: agent_name
    config: BlameConfig = BlameConfig()


@dataclass(frozen=True)
class LoopAnomaly:
    member_run_ids: list[str]
    agent_names: list[str]
    iterations: int
    limit_kind: Literal["max_iterations", "statistical"]
    baseline: LoopBaseline | None


@dataclass(frozen=True)
class Evidence:                             # worker serializes to JSONB
    score_map: dict[str, float | None]
    drops: dict[str, float]                 # candidate -> drop size
    judge_notes: dict[str, str]
    error_span_ids: dict[str, list[str]]
    loop_anomalies: list[LoopAnomaly]
    unknown_ancestors: list[str]
    fact_propagation: list[dict] | None     # filled by WORKER after blame; always None here
    notes: list[str]                        # human-readable classification rationale


@dataclass(frozen=True)
class BlameReport:
    report_type: ReportType
    culprit_run_ids: list[str]
    propagation_path: list[str]
    confidence: float
    evidence: Evidence
    downstream_cost_usd: float
    unscored_run_ids: list[str]
