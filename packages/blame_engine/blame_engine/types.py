"""Data types for the blame engine. Mirrors spec section 3.2 exactly."""

from dataclasses import dataclass, field
from typing import Literal

ReportType = Literal["cut_point", "multi_culprit", "composition_failure",
                     "loop_detected", "root_cause_external", "verification_gap",
                     "degraded_recovered", "shipped_with_latent_defect",
                     "unclassified"]


@dataclass(frozen=True)
class NodeScore:
    run_id: str
    score: float | None                    # None = UNKNOWN, never defaults to 1.0
    components: dict[str, float | None]    # {"schema": .., "judge": .., "heuristics": ..}
    input_flawed: bool | None              # judge verdict: was the node's INPUT already flawed?
    unscored_reason: str | None            # "judge_error" | "payload_missing" | "insufficient_components"
    judge_note: str | None
    # Structured judge/heuristic flags ("missing_required_content",
    # "unverifiable_artifact", ...). They cap the judge component deterministically
    # in the worker and surface in the blame report as per-node evidence.
    flags: tuple[str, ...] = ()
    # Deterministic input-contract violations: a carried-through parameter
    # (file_type, lang, format, ...) this node silently rewrote. Each is
    # ``(key, input_value, output_value)``. This is a SEPARATE evidence stream
    # from the LLM judge_note — stronger (a hard, reproducible check) and with its
    # own provenance — so it must never be glued into the fluent judge prose.
    contract_violations: tuple[tuple[str, object, object], ...] = ()
    # Named deterministic signals raised against this node's own output by
    # reproducible checks (docs/deterministic-signals.md). Each dict:
    # {"name", "severity" ("fail"|"warn"), "detail", "basis",
    #  "provenance": "deterministic"}. The worker fills these (e.g.
    # artifact_integrity_fail from the [artifact_meta] block); the engine only
    # carries them into Evidence — provenance stays with the check, never the LLM.
    deterministic_signals: tuple[dict, ...] = ()


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
    # False when the terminal judge could not actually SEE the deliverable
    # (its content was absent from the payload — e.g. only a file path, or the
    # judged run was an orchestrator wrapper / verifier with no artifact). A
    # not-checkable verdict is NOT trustworthy ground truth: ``bad`` must be
    # ignored by classification, never used to manufacture a culprit. Defaults
    # True so existing callers (and the well-instrumented case) are unchanged.
    checkable: bool = True
    # True when the verdict's DETERMINISTIC basis no longer reproduces on the
    # current payload/rule set (tier1 ran under different registered rules, or
    # the artifact/payload diverged — representation divergence). A stale
    # verdict ships with checkable=False (not ground truth) and this flag so
    # the report explains WHY it was discarded instead of blaming
    # instrumentation.
    stale: bool = False


@dataclass(frozen=True)
class BlameConfig:
    threshold: float = 0.5
    gap_threshold: float = 0.25            # "significant drop" on a single edge
    min_drop: float = 0.10                 # below this = inherited degradation, not origin
    # Cumulative degradation: a chain of 2+ consecutive dropping edges whose total
    # decline reaches cum_drop_threshold is an origin signal even when no single
    # edge crosses gap_threshold (slow erosion instead of one sharp break).
    cum_drop_threshold: float = 0.30
    cum_min_edges: int = 2
    cum_step_min: float = 0.05             # per-edge decline below this = noise, breaks the chain
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
    # Where the failure surfaced — the terminal ARTIFACT/output, not the last
    # node in execution order. A verifier sink did not "manifest" anything; it
    # issued a verdict about work produced upstream, so verifier sinks are mapped
    # back to their nearest non-verifier producer.
    manifestation_run_ids: list[str] = field(default_factory=list)
    # Verifier nodes (qa/eval/review/…) whose PASS was wrong. Each:
    # {run_id, agent_name, basis} where basis is
    #   "verdict_scored_incorrect" — role-aware judge scored the verdict wrong, or
    #   "passed_bad_terminal"     — deduced: terminal verdict is bad, yet this
    #                               verifier let the work through unflagged.
    verification_gaps: list[dict] = field(default_factory=list)
    # Per-node candidacy: why each node was or wasn't the culprit, WITH the
    # numbers behind the decision (score vs threshold, drop vs reference). Makes
    # the verdict auditable instead of a black box.
    candidacy: dict[str, str] = field(default_factory=dict)
    # The tier1 terminal-judge verdict the classification leaned on:
    # {"bad": bool, "score": float|None, "reasoning": str|None}. A report that
    # claims "terminal is bad" without showing this evidence is not trustworthy.
    terminal_verdict: dict | None = None
    # Cumulative degradation chains: monotone declines over 2+ consecutive edges
    # whose total drop crossed cum_drop_threshold even though no single edge
    # crossed gap_threshold. Each: {"path": [run_ids], "scores": [floats],
    # "cumulative_drop": float}.
    degradation_paths: list[dict] = field(default_factory=list)
    # Raw nodes in deterministic topological order — JSONB scrambles dict key
    # order, so the UI needs this to render the score map in pipeline order.
    topo_order: list[str] = field(default_factory=list)
    # Nodes recognised as verifiers/gates (by agent-name hint), so the UI can
    # group them apart from the producing pipeline.
    verifier_run_ids: list[str] = field(default_factory=list)
    # Structured per-node flags from scoring (e.g. "unverifiable_artifact").
    node_flags: dict[str, list[str]] = field(default_factory=dict)
    # Post-hoc score corrections backed by ground truth. A role-aware verifier
    # score CLAIMS to measure verdict correctness; when the terminal verdict
    # proves the verdict wrong, the judged number is refuted evidence and the
    # effective score reflects that — shown with the original, never silently.
    # Each: {run_id, original, effective, reason}.
    score_overrides: list[dict] = field(default_factory=list)
    # Competing ORIGIN hypotheses, for when the independent evidence streams do
    # NOT agree on where the fault started. The score gap / content flag localises
    # one origin, but a divergence signal (representation_divergence: an
    # empty/degenerate terminal beside a non-empty reviewed artifact;
    # evidence_tension: the origin's claims survived in a LATER producer's
    # payload) points at a render/export step later than reported — a hypothesis
    # the engine has not ruled out. Filled by the WORKER after blame, because
    # those signals (fact propagation, the degenerate-output tier1 flag) only
    # exist at tier2. Each: {origin, agent, basis, weight}; the weights sum to 1.0
    # with an explicit {"origin": None, ...} "unresolved" remainder. Empty when
    # the origin is settled — a single confident origin is only honest when
    # nothing contests it, so this list existing IS the "do not trust one number"
    # signal for the UI.
    hypotheses: list[dict] = field(default_factory=list)
    # The single headline confidence conflates two DIFFERENT questions. We split
    # them so a certain finding is never buried by localisation doubt:
    #   observation_confidence — how sure the culprit's OUTPUT is defective
    #     (severity + deterministic contract/flag signals). For a contract
    #     violation this is near-certain regardless of where it originated.
    #   attribution_confidence — how sure THIS node is the ORIGIN vs having
    #     inherited a bad input (the classic gap/pred/unknown-upstream formula).
    # Both None for report types where the split does not apply (the headline
    # confidence carries its own capped semantics there).
    observation_confidence: float | None = None
    attribution_confidence: float | None = None
    # Attribution PER DEFECT: one origin can carry defects with very different
    # evidential strength — a contract breach observes BOTH sides (input intact,
    # output rewritten: origination is observed, not inferred → near-certain),
    # while a content degradation at the observability boundary is capped. A
    # single blended number takes the worse of the two and undersells the
    # stronger claim; ``attribution_confidence`` stays as the conservative
    # blend, this breakdown carries the per-defect truth. Each:
    # {"defect", "attribution", "basis"}.
    attribution_breakdown: list[dict] = field(default_factory=list)
    # Deterministic input-contract violations across the graph, as their own
    # evidence stream (provenance: a hard check, not the LLM judge). Each:
    # {"run_id", "agent", "key", "from", "to"}.
    contract_violations: list[dict] = field(default_factory=list)
    # Named deterministic signals across the graph (docs/deterministic-signals.md):
    # node-level entries assembled here from NodeScore.deterministic_signals
    # (each gains "run_id" and "agent"); graph-level entries (tier1 checks on the
    # deliverable) are appended by the worker post-serialize. Same shape either
    # way: {"name", "run_id", "agent", "severity", "detail", "basis",
    # "provenance": "deterministic"}.
    deterministic_signals: list[dict] = field(default_factory=list)
    # Advisory topology classification (blame_engine.topology.classify_topology):
    # structural attributes + "primary" archetype. Presentational only — it
    # never drives verdicts, confidence, culprits or candidacy.
    topology: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BlameReport:
    report_type: ReportType
    culprit_run_ids: list[str]
    propagation_path: list[str]
    confidence: float
    evidence: Evidence
    downstream_cost_usd: float
    unscored_run_ids: list[str]
