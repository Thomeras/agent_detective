# Diamond-topology eval (05_diamond in the agent_topo_db catalog):
#
#   spec_extractor --> tech_writer ------+
#                 \--> marketing_writer -+--> press_editor
#
#   python examples/diamond_eval.py             # healthy run -> exit 0
#   python examples/diamond_eval.py --inject    # marketing rewrites the price -> exit 1
#   AGENT_DETECTIVE_ENDPOINT=http://localhost:8001 python examples/diamond_eval.py --inject
#
# The agents are offline stubs — swap their bodies for real model calls,
# the instrumentation stays the same.

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from detective_sdk import run

TASK = "Prepare the launch press release for the new product."

PRODUCT_DOC = """\
Name: Atlas Sync 2.0
Changes: two-way sync (previously one-way), 200 ms latency (previously 4 s),
         field-level conflict resolution, SSO via SAML, 90-day audit log.
Pricing: Team $12/user/month, Enterprise on request.
Availability: September 1, 2026, EU regions.
"""


def extract_facts(doc: str) -> dict:
    return {
        "product": "Atlas Sync 2.0",
        "price": "$12/user/month",
        "availability": "2026-09-01",
        "latency": "200 ms",
        "sso": "SAML",
    }


def write_tech(facts: dict) -> dict:
    return {
        "section": (
            f"{facts['product']} introduces two-way sync with {facts['latency']} "
            f"latency, field-level conflict resolution and SSO via {facts['sso']}. "
            f"Available {facts['availability']} in EU regions."
        ),
        "price": facts["price"],
        "availability": facts["availability"],
    }


def write_marketing(facts: dict, inject: bool) -> dict:
    price = "from $5/user/month" if inject else facts["price"]  # the injected fault
    return {
        "section": (
            f"Say goodbye to sync conflicts: {facts['product']} keeps every team "
            f"in step, now {price} for Team plans."
        ),
        "price": price,
        "availability": facts["availability"],
    }


def edit_release(tech: dict, marketing: dict) -> dict:
    return {
        "title": "Atlas Sync 2.0: two-way sync, 20x faster",
        "release": f"{marketing['section']}\n\n{tech['section']}",
        "price": marketing["price"],  # the editor trusts marketing — the breach ships
        "availability": tech["availability"],
    }


def run_pipeline(inject: bool, trace_file: str | None) -> None:
    with run("press-diamond", task=TASK, trace_file=trace_file) as r:  # root carries the original ask
        with r.step("spec_extractor", input=PRODUCT_DOC) as s:  # pipeline node
            facts = extract_facts(PRODUCT_DOC)
            s.output = facts  # the work, not {"ok": true}
            s.cost(usd=0.004, tokens_in=1_200, tokens_out=150, model="gpt-4o-mini")

        arms = []
        with r.branch("tech_writer", input=facts) as s:  # fan-out arm
            tech = write_tech(facts)
            s.contract(price=facts["price"], availability=facts["availability"])  # a silent rewrite = deterministic breach
            s.output = tech
            s.cost(usd=0.006, tokens_in=900, tokens_out=280, model="gpt-4o-mini")
            arms.append(s)

        with r.branch("marketing_writer", input=facts) as s:  # fan-out arm
            marketing = write_marketing(facts, inject)
            s.contract(price=facts["price"], availability=facts["availability"])
            s.output = marketing
            s.cost(usd=0.006, tokens_in=900, tokens_out=310, model="gpt-4o-mini")
            arms.append(s)

        with r.join("press_editor", arms) as s:  # fan-in reading both arms
            s.output = edit_release(tech, marketing)
            s.cost(usd=0.03, tokens_in=2_400, tokens_out=600, model="gpt-4o")


def evaluate(trace_path: Path) -> int:
    from detective_cli import analyze, bundles_from_exports, load_trace  # same pipeline the CLI runs

    result = analyze(bundles_from_exports(load_trace(trace_path)))
    for graph in result.graphs:
        report = graph.blame_report or {}
        culprits = [
            graph.agent_names.get(str(rid), str(rid))
            for rid in report.get("culprit_run_ids", [])
        ]
        line = "clean" if graph.clean else f"{report.get('report_type')} — culprit: {', '.join(culprits)}"
        print(f"graph {str(graph.graph_id)[:8]}: {line}")
    return 0 if result.clean else 1  # exit 1 on incident = a CI gate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inject", action="store_true", help="marketing rewrites the price")
    args = parser.parse_args()

    endpoint = os.environ.get("AGENT_DETECTIVE_ENDPOINT")
    trace_file = os.environ.get("AGENT_DETECTIVE_TRACE_FILE")
    if not endpoint and not trace_file:
        trace_file = "diamond_run.json"

    run_pipeline(args.inject, trace_file)

    if endpoint:
        print(f"trace sent to {endpoint} — open the web UI to see the graph")
        return 0
    print(f"trace written to {trace_file}")
    return evaluate(Path(trace_file))


if __name__ == "__main__":
    sys.exit(main())
