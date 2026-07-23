#!/usr/bin/env python3
"""Determinism probe: same trace in -> same verdict out?

Re-analyzes ONE fixed, already-ingested graph N times through the live stack
(POST /graphs/{id}/analyze -> tier2 -> new versioned blame report) and measures
how much the verdict moves between runs: report_type + culprit set stability,
per-node quality-score spread, confidence spread, and nodes sitting close
enough to the 0.50 blame threshold to flip an edge between runs.

Tier0/deterministic checks are stable by construction; the LLM judge is not
guaranteed to be, even at temperature=0 (and even with JUDGE_SEED set — most
backends do not promise bitwise determinism). This probe turns that risk into
a measured number instead of an assumption. See docs/deterministic-signals.md,
section "Is the detective deterministic?".

Usage:
    scripts/determinism_probe.py --graph-id <uuid> [--rounds 10] [--json]
    scripts/determinism_probe.py            # no graph id: list recent graphs

Exit codes: 0 = verdicts 100% stable, 1 = verdicts varied, 2 = inconclusive
(no blame report ever landed / usage error).

Stdlib-only on purpose, mirroring tests/e2e/test_acceptance.py and
benchmarks/whoandwhen/adapter/client.py. The API base URL defaults to
$E2E_API_URL (same env var as the e2e suite), then http://localhost:8000.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

DEFAULT_API_URL = os.environ.get("E2E_API_URL", "http://localhost:8000")

BLAME_THRESHOLD = 0.50  # worker Settings.blame_threshold default
FLIP_BAND = 0.10  # |mean - threshold| <= band => flip risk

NO_REPORT = "no_report"  # round sentinel: analysis triggered, no report landed


class ProbeError(RuntimeError):
    pass


# --- HTTP (stdlib, same shape as benchmarks/whoandwhen/adapter/client.py) ---


def _request(method: str, url: str, body: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raise ProbeError(f"{method} {url} -> {exc.code}: {exc.read().decode('utf-8', 'replace')}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ProbeError(f"{method} {url} -> {exc}") from exc


# --- API access ---


def get_graph(api: str, graph_id: str) -> dict[str, Any]:
    return _request("GET", f"{api}/graphs/{graph_id}")


def trigger_analyze(api: str, graph_id: str) -> dict[str, Any]:
    return _request("POST", f"{api}/graphs/{graph_id}/analyze", {})


def graph_incident_ids(api: str, graph_id: str) -> list[int]:
    listing = _request("GET", f"{api}/incidents?limit=200")
    return [
        int(inc["id"])
        for inc in listing.get("incidents", [])
        if str(inc.get("graph_id")) == str(graph_id)
    ]


def latest_reports(api: str, graph_id: str) -> list[dict[str, Any]]:
    """Latest blame report of every incident on this graph (detail shape,
    including evidence)."""
    reports = []
    for incident_id in graph_incident_ids(api, graph_id):
        detail = _request("GET", f"{api}/incidents/{incident_id}")
        report = detail.get("latest_report")
        if report is not None:
            reports.append(report)
    return reports


def _created_at(report: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(report["created_at"])


def newest_report(reports: list[dict[str, Any]]) -> dict[str, Any] | None:
    return max(reports, key=_created_at) if reports else None


def wait_for_new_report(
    api: str,
    graph_id: str,
    after: datetime | None,
    timeout: float,
    interval: float = 2.0,
) -> dict[str, Any] | None:
    """The latest completed report is authoritative: poll until a report newer
    than `after` lands, or the timeout expires (-> None)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        report = newest_report(latest_reports(api, graph_id))
        if report is not None and (after is None or _created_at(report) > after):
            return report
        time.sleep(interval)
    return None


# --- Round extraction ---


def node_labels(graph: dict[str, Any]) -> dict[str, str]:
    """run_id -> human label (agent_name, disambiguated with a run-id prefix
    when one agent name covers several runs)."""
    names: dict[str, str] = {
        node["data"]["id"]: node["data"].get("agent_name") or "unknown"
        for node in graph.get("nodes", [])
    }
    counts: dict[str, int] = {}
    for name in names.values():
        counts[name] = counts.get(name, 0) + 1
    return {
        run_id: name if counts[name] == 1 else f"{name}:{run_id[:8]}"
        for run_id, name in names.items()
    }


def extract_round(report: dict[str, Any] | None, labels: dict[str, str]) -> dict[str, Any]:
    """One round's observation, as plain JSON-friendly data."""
    if report is None:
        return {
            "report_type": NO_REPORT,
            "culprits": [],
            "confidence": None,
            "attribution_confidence": None,
            "scores": {},
        }
    evidence = report.get("evidence") or {}
    score_map = evidence.get("score_map") or {}
    return {
        "report_type": report.get("report_type"),
        "culprits": sorted(labels.get(rid, rid) for rid in report.get("culprit_run_ids") or []),
        "confidence": report.get("confidence"),
        "attribution_confidence": evidence.get("attribution_confidence"),
        "scores": {labels.get(rid, rid): score for rid, score in score_map.items()},
        "report_version": report.get("version"),
        "report_id": report.get("id"),
    }


# --- Summary (pure; unit-tested without the stack) ---


def _stats(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "n": len(values),
        "mean": statistics.mean(values),
        "stddev": statistics.pstdev(values),
        "min": min(values),
        "max": max(values),
    }


def summarize_rounds(rounds: list[dict]) -> dict:
    """Aggregate per-round observations into the determinism summary.

    Pure function over the round dicts produced by ``extract_round`` (only the
    keys report_type/culprits/confidence/attribution_confidence/scores are
    read), so it is unit-testable without a live stack.
    """
    n = len(rounds)
    verdict_keys = [
        f"{r.get('report_type')}|culprits={','.join(r.get('culprits') or []) or '-'}"
        for r in rounds
    ]
    distribution: dict[str, int] = {}
    for key in verdict_keys:
        distribution[key] = distribution.get(key, 0) + 1

    no_report_rounds = sum(1 for r in rounds if r.get("report_type") == NO_REPORT)
    inconclusive = n == 0 or no_report_rounds == n
    stable = not inconclusive and len(distribution) == 1

    # Culprit stability: how often the modal culprit set shows up.
    culprit_sets: dict[tuple[str, ...], int] = {}
    for r in rounds:
        key = tuple(r.get("culprits") or [])
        culprit_sets[key] = culprit_sets.get(key, 0) + 1
    if culprit_sets:
        modal_culprits, modal_count = max(culprit_sets.items(), key=lambda kv: kv[1])
        culprit_stability = {
            "modal_culprits": list(modal_culprits),
            "modal_fraction": modal_count / n,
            "distinct_sets": len(culprit_sets),
        }
    else:
        culprit_stability = {"modal_culprits": [], "modal_fraction": 0.0, "distinct_sets": 0}

    # Per-node score spread + threshold proximity.
    per_node: dict[str, list[float]] = {}
    for r in rounds:
        for label, score in (r.get("scores") or {}).items():
            if score is not None:
                per_node.setdefault(label, []).append(float(score))
    node_scores = {}
    flip_risks = []
    for label in sorted(per_node):
        stats = _stats(per_node[label])
        assert stats is not None
        # Flip risk: the node's mean sits inside the band around the blame
        # threshold, so ordinary judge jitter can move it across 0.50 and
        # rewire which edge "drops" (verdict flips like degraded_recovered
        # <-> shipped). crossed_threshold = it ACTUALLY landed on both sides.
        stats["flip_risk"] = abs(stats["mean"] - BLAME_THRESHOLD) <= FLIP_BAND
        stats["crossed_threshold"] = stats["min"] < BLAME_THRESHOLD <= stats["max"]
        node_scores[label] = stats
        if stats["flip_risk"] or stats["crossed_threshold"]:
            flip_risks.append(label)

    return {
        "rounds": n,
        "stable": stable,
        "inconclusive": inconclusive,
        "no_report_rounds": no_report_rounds,
        "verdict_distribution": distribution,
        "culprit_stability": culprit_stability,
        "confidence": _stats([r["confidence"] for r in rounds if r.get("confidence") is not None]),
        "attribution_confidence": _stats(
            [
                r["attribution_confidence"]
                for r in rounds
                if r.get("attribution_confidence") is not None
            ]
        ),
        "node_scores": node_scores,
        "flip_risks": flip_risks,
        "blame_threshold": BLAME_THRESHOLD,
        "flip_band": FLIP_BAND,
    }


# --- Output ---


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def print_summary(summary: dict) -> None:
    n = summary["rounds"]
    print(f"\n=== Determinism probe: {n} rounds ===")
    print("Verdict distribution:")
    for key, count in sorted(summary["verdict_distribution"].items(), key=lambda kv: -kv[1]):
        print(f"  {count}/{n}  {key}")
    cs = summary["culprit_stability"]
    print(
        f"Culprit stability: modal set {cs['modal_culprits'] or ['(none)']} "
        f"in {cs['modal_fraction']:.0%} of rounds ({cs['distinct_sets']} distinct set(s))"
    )
    for name in ("confidence", "attribution_confidence"):
        stats = summary[name]
        if stats:
            print(
                f"{name}: mean={_fmt(stats['mean'])} stddev={_fmt(stats['stddev'])} "
                f"min={_fmt(stats['min'])} max={_fmt(stats['max'])} (n={stats['n']})"
            )
        else:
            print(f"{name}: no data")
    if summary["node_scores"]:
        print(f"Per-node quality scores (blame threshold {BLAME_THRESHOLD}, flip band ±{FLIP_BAND}):")
        for label, stats in summary["node_scores"].items():
            marks = []
            if stats["flip_risk"]:
                marks.append("FLIP RISK")
            if stats["crossed_threshold"]:
                marks.append("CROSSED 0.50")
            print(
                f"  {label:32s} mean={_fmt(stats['mean'])} stddev={_fmt(stats['stddev'])} "
                f"min={_fmt(stats['min'])} max={_fmt(stats['max'])} n={stats['n']}"
                + (f"   << {', '.join(marks)}" if marks else "")
            )
    if summary["no_report_rounds"]:
        print(f"WARNING: {summary['no_report_rounds']}/{n} round(s) produced no blame report before the timeout.")
    if summary["inconclusive"]:
        print("RESULT: INCONCLUSIVE — no round produced a blame report. Wrong graph id, "
              "or the worker is not processing (check `docker compose logs worker`).")
    elif summary["stable"]:
        print(f"RESULT: STABLE — {n}/{n} rounds produced an identical verdict.")
    else:
        print("RESULT: UNSTABLE — the verdict moved between rounds on a fixed trace.")


def list_recent_graphs(api: str) -> None:
    listing = _request("GET", f"{api}/graphs?limit=20")
    print("Recent graphs (pass one as --graph-id):")
    for g in listing.get("graphs", []):
        print(f"  {g.get('graph_id')}  status={g.get('status')}  runs={g.get('run_count')}  {g.get('name')}")


# --- Main ---


def main() -> int:
    ap = argparse.ArgumentParser(description="Re-analyze one graph N times; measure verdict variance.")
    ap.add_argument("--graph-id", default=None, help="graph UUID to probe (omit to list recent graphs)")
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--api", default=DEFAULT_API_URL, help=f"API base URL (default {DEFAULT_API_URL})")
    ap.add_argument("--round-timeout", type=float, default=180.0, help="seconds to wait for each round's report")
    ap.add_argument("--interval", type=float, default=2.0, help="poll interval in seconds")
    ap.add_argument("--json", action="store_true", help="machine-readable output on stdout")
    args = ap.parse_args()
    api = args.api.rstrip("/")

    if not args.graph_id:
        list_recent_graphs(api)
        return 2

    graph = get_graph(api, args.graph_id)  # 404 -> ProbeError before any round
    labels = node_labels(graph)
    if not args.json:
        print(f"Probing graph {args.graph_id} ({graph.get('name')}), {len(labels)} node(s), {args.rounds} round(s).")

    rounds: list[dict] = []
    for i in range(1, args.rounds + 1):
        # Baseline BEFORE triggering: the newest report currently on the graph.
        # tier2 always inserts a new version (is_latest flipped), so "newer
        # created_at than baseline" means THIS round's analysis completed.
        baseline = newest_report(latest_reports(api, args.graph_id))
        trigger_analyze(api, args.graph_id)
        report = wait_for_new_report(
            api,
            args.graph_id,
            after=_created_at(baseline) if baseline else None,
            timeout=args.round_timeout,
            interval=args.interval,
        )
        observed = {"round": i} | extract_round(report, labels)
        rounds.append(observed)
        if not args.json:
            print(
                f"[{i}/{args.rounds}] type={observed['report_type']} "
                f"culprits={observed['culprits'] or '-'} "
                f"confidence={_fmt(observed['confidence'])} "
                f"attribution={_fmt(observed['attribution_confidence'])}"
            )

    summary = summarize_rounds(rounds)
    if args.json:
        print(json.dumps({"graph_id": args.graph_id, "rounds": rounds, "summary": summary}, indent=2))
    else:
        print_summary(summary)

    if summary["inconclusive"]:
        return 2
    return 0 if summary["stable"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ProbeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
