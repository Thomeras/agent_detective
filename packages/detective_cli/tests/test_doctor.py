"""``detective doctor``: the pre-flight check, and the silent failures it names.

Each test here pins a failure that already cost real time. The status-record
case is the anchor — an app shipped ``{"ok": true, "step": "collect"}`` where
node outputs belonged, the analysis stayed confident, and a whole debugging
session went into discovering it. A doctor that reports "3 nodes, 2 edges, all
good" on that trace is worse than no doctor at all.
"""

from __future__ import annotations

import json

from detective_cli.cli import EXIT_CLEAN, EXIT_ERROR, main
from detective_cli.doctor import (
    check_quiescence,
    classify_payload,
    diagnose,
    fetch_ingest_config,
    payload_judgeable,
    render_doctor_json,
    render_doctor_terminal,
    status_record_keys,
)

from conftest import export, linear_pipeline, span


def run_cli(argv, capsys):
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def check(diagnosis, check_id, graph=0):
    """One check by id, from the trace level or from a graph."""
    pool = diagnosis.checks if graph is None else diagnosis.graphs[graph].checks
    matches = [c for c in pool if c.id == check_id]
    assert matches, f"no check {check_id!r} in {[c.id for c in pool]}"
    return matches[0]


def claim(diagnosis, name, graph=0):
    matches = [c for c in diagnosis.graphs[graph].claims if c.name == name]
    assert matches, f"no claim {name!r}"
    return matches[0]


class TestStatusRecords:
    """The failure that cost a session: pings instead of work."""

    def test_the_canonical_status_ping_is_recognised(self):
        assert status_record_keys('{"ok": true, "step": "collect"}') == ["ok", "step"]

    def test_counts_ride_along_with_state_words(self):
        keys = status_record_keys('{"ok": true, "step": "write", "documents": 3}')
        assert keys == ["ok", "step", "documents"]

    def test_real_work_is_not_a_status_record(self):
        # A false positive here would tell a correctly instrumented team to
        # rewrite working instrumentation.
        text = json.dumps({"brief": "Acme reported revenue of $412M. " * 8})
        assert status_record_keys(text) is None

    def test_short_data_without_state_words_is_not_a_status_record(self):
        assert status_record_keys('{"company": "Acme Corp", "ticker": "ACME"}') is None

    def test_prose_output_is_not_a_status_record(self):
        assert status_record_keys("the writer produced a complete draft") is None

    def test_a_status_field_beside_real_content_is_not_a_ping(self):
        # {"status": "ok", "report": "<800 chars>"} is work with a status field,
        # not a status record — the judge has something to read.
        text = json.dumps({"status": "ok", "report": "finding. " * 40})
        assert status_record_keys(text) is None

    def test_a_status_record_is_not_judgeable(self):
        assert payload_judgeable('{"ok": true, "step": "collect"}') is False
        assert payload_judgeable("a paragraph of actual output") is True
        assert payload_judgeable("") is False

    def test_a_bare_string_status_output_is_a_ping(self):
        """A ping that is not JSON was invisible: the detector required a `{`.

        A three-node chain outputting "done"/"ok"/"success" earned `ok payload
        content — no output looks like a status ping`, and a two-character
        deliverable reading "ok" earned an affirmative terminal check.
        """
        for text in ("done", "ok", "success", "COMPLETED.", "failed"):
            assert classify_payload(text) == "ping", text
        assert payload_judgeable("ok") is False

    def test_a_terse_router_payload_is_unclear_not_a_ping(self):
        """The keyword list called correct instrumentation broken.

        `result`, `action`, `message`, `code`, `event` were all in the ping
        vocabulary, so a router/classifier/QA chain was told `2 of 2 outputs are
        STATUS RECORDS` and `no localization`. Those payloads may be the real
        work; the doctor cannot tell, and must say exactly that.
        """
        for text in (
            '{"action": "escalate_to_legal"}',
            '{"result": "fraud", "code": "F-21"}',
            '{"event": "contract_breach_detected", "count": 3}',
            "fraud",
        ):
            assert classify_payload(text) == "unclear", text
            assert status_record_keys(text) is None, text

    def test_a_terse_sentence_under_an_ambiguous_key_reads_as_work(self):
        """`{"message": "Yes, the contract permits early termination."}` was
        flagged a status record. A sentence's worth of words is content whatever
        key it sits under."""
        assert classify_payload('{"message": "Yes, the contract permits it."}') == "work"
        assert (
            classify_payload('{"result": "Acme Corp reported $412M revenue in Q3."}')
            == "work"
        )

    def test_a_lifecycle_key_stays_a_ping_however_wordy_its_value(self):
        # "step": "collect the files from s3" is still a step, not a work
        # product — the words-are-content rule must not open that hole.
        assert classify_payload('{"ok": true, "step": "collect the files from s3"}') == "ping"

    def test_an_empty_json_literal_is_empty_not_work(self):
        # `{}` is the absence of an output written in JSON. Reporting it as
        # content would be the cardinal sin in two characters.
        for text in ("{}", "[]", "null", ""):
            assert classify_payload(text) == "empty", text

    def test_the_report_states_the_consequence_not_just_the_fact(self, trace_file):
        pings = linear_pipeline(
            {
                "planner": '{"ok": true, "step": "plan"}',
                "writer": '{"ok": true, "step": "write", "chars": 1180}',
                "reviewer": '{"status": "done", "issues": 0}',
            }
        )
        diagnosis = diagnose([pings], "pings.json")
        content = check(diagnosis, "status_records")
        assert content.level == "warn"
        assert "STATUS RECORDS" in content.detail
        assert "per-node quality cannot be judged from this" in content.consequence
        assert content.fix
        assert claim(diagnosis, "localization").supported is False

    def test_a_real_deliverable_among_pings_keeps_the_terminal_check(self, trace_file):
        # The archintel shape: intermediate phases report {"ok": true, ...} but
        # the render phase attaches the document. The terminal check survives;
        # localization does not, and saying otherwise would be the whole bug.
        mixed = linear_pipeline(
            {
                "planner": '{"ok": true, "step": "plan"}',
                "writer": "Acme Corp reported revenue of $412M in Q3. " * 6,
                "reviewer": '{"ok": true, "issues": 0}',
            }
        )
        diagnosis = diagnose([mixed], "mixed.json")
        assert check(diagnosis, "status_records").level == "warn"
        assert check(diagnosis, "deliverable").level == "ok"
        assert claim(diagnosis, "terminal check").supported is True
        localization = claim(diagnosis, "localization")
        assert localization.supported is False
        assert "observability boundary" in localization.reason

    def test_a_ping_at_the_head_of_the_chain_is_still_reported(self, trace_file):
        """The flagship check failed at the one position that mattered.

        The wrapper-root exclusion was purely topological, so the FIRST node of
        any linear chain was dropped from the status-record population. A chain
        whose planner shipped the canonical `{"ok": true, "step": "plan"}` — the
        payload named in doctor.py's opening docstring — reported `ok payload
        content — no output looks like a status ping` and `yes localization`.
        """
        head = linear_pipeline(
            {
                "planner": '{"ok": true, "step": "plan"}',
                "writer": "Acme Corp reported revenue of $412M in Q3. " * 6,
                "reviewer": "the draft is accurate and complete, approved for release",
            }
        )
        diagnosis = diagnose([head], "head.json")
        content = check(diagnosis, "status_records")
        assert content.level == "warn"
        assert "1 of 3 outputs are STATUS RECORDS" in content.detail
        assert "`planner`" in content.detail
        assert claim(diagnosis, "localization").supported is False

    def test_a_classifier_chain_is_reported_as_unclear_not_as_broken(self):
        """A correctly instrumented router was told its instrumentation was broken.

        Every one of these payloads may be the step's real product. The doctor
        cannot tell a one-word decision from a one-word ping, so it says so:
        no STATUS RECORDS verdict, and the claims come back "cannot tell"
        rather than an affirmative `no`.
        """
        router = linear_pipeline(
            {
                "planner": '{"action": "route_to_analyst"}',
                "writer": '{"result": "fraud", "code": "F-21"}',
                "reviewer": '{"message": "Confirmed: the transaction is fraudulent."}',
            }
        )
        diagnosis = diagnose([router], "router.json")
        content = check(diagnosis, "status_records")
        assert content.level == "warn"
        assert "STATUS RECORD" not in content.detail
        assert "too short or too generic" in content.detail
        assert claim(diagnosis, "localization").supported is None
        assert claim(diagnosis, "terminal check").supported is None

    def test_a_two_character_deliverable_earns_no_terminal_claim(self):
        """`ok` as the deliverable used to report `ok deliverable text — writer
        carries 2 chars of artifact text` and `yes terminal check`."""
        bare = linear_pipeline({"planner": "done", "writer": "ok", "reviewer": "success"})
        diagnosis = diagnose([bare], "bare.json")
        assert check(diagnosis, "status_records").level == "warn"
        assert "3 of 3 outputs are STATUS RECORDS" in check(diagnosis, "status_records").detail
        deliverable = check(diagnosis, "deliverable")
        assert deliverable.level == "warn"
        assert "status record" in deliverable.detail
        assert claim(diagnosis, "terminal check").supported is False


class TestAgentSpans:
    """A span without openinference.span.kind=AGENT never becomes a node."""

    def test_a_chain_only_trace_is_reported_as_a_gap_not_an_error(self, trace_file):
        # The LangChain-instrumentor shape: every node marked CHAIN. `analyze`
        # exits 2 here; the doctor's job is to say WHY and how to fix it.
        chain = span(name="collect", span_id="0" * 16, agent_name="collector")
        chain["attributes"][0]["value"]["stringValue"] = "CHAIN"
        diagnosis = diagnose([export([chain])], "chain.json")
        agent_spans = check(diagnosis, "agent_spans", graph=None)
        assert agent_spans.level == "gap"
        assert "0 of 1 spans" in agent_spans.detail
        assert "CHAIN 1" in agent_spans.detail
        assert "no span becomes a node" in agent_spans.consequence
        assert "openinference.span.kind=AGENT" in agent_spans.fix
        assert diagnosis.graphs == []

    def test_a_healthy_trace_reports_the_kind_census(self, trace_file):
        diagnosis = diagnose([linear_pipeline()], "trace.json")
        agent_spans = check(diagnosis, "agent_spans", graph=None)
        assert agent_spans.level == "ok"
        assert "3 of 3" in agent_spans.detail


class TestTopologyAndNames:
    def test_unnamed_runs_cost_role_detection(self):
        anonymous = [
            span(name="step.one", span_id="1" * 16, agent_name=None),
            span(name="step.two", span_id="2" * 16, parent_span_id="1" * 16, agent_name=None),
        ]
        diagnosis = diagnose([export(anonymous)], "anon.json")
        names = check(diagnosis, "agent_names")
        assert names.level == "gap"
        assert "role detection never engages" in names.consequence

    def test_a_graph_without_edges_cannot_localize(self):
        # Sibling spans under no common parent: nodes, no handoffs. This is the
        # CrewAI-corpus shape — blame has no path to walk.
        siblings = [
            span(name="a.run", span_id="1" * 16, agent_name="alpha"),
            span(name="b.run", span_id="2" * 16, agent_name="beta"),
        ]
        diagnosis = diagnose([export(siblings)], "flat.json")
        edges = check(diagnosis, "edges")
        assert edges.level == "gap"
        assert "0 edges" in edges.detail
        assert "cut point" in edges.consequence
        assert claim(diagnosis, "localization").supported is False

    def test_the_deliverable_is_the_producer_behind_a_verifier_sink(self):
        # planner -> writer -> reviewer. The sink is `reviewer`, but a reviewer
        # emits a PASS/FAIL verdict, not the artifact — the doctor must name the
        # run the terminal check would actually grade, or its "you can check the
        # deliverable" line points at the wrong payload.
        diagnosis = diagnose([linear_pipeline()], "trace.json")
        assert check(diagnosis, "edges").level == "ok"
        topology = check(diagnosis, "topology")
        assert topology.level == "ok"
        assert "one sink (`reviewer`)" in topology.detail
        assert "deliverable resolves to `writer`" in topology.detail


    def test_a_single_node_graph_does_not_claim_localization(self):
        """The doctor contradicted itself on a degenerate graph.

        One node with real output reported `yes localization — 1/1 nodes
        judgeable across 0 edge(s)`, while the doctor's own edges consequence
        says 0 edges means blame has no path to walk. There is no handoff, so
        there is no drop between neighbours to locate.
        """
        solo = span(
            name="solo.run",
            span_id="1" * 16,
            agent_name="solo",
            output="a complete answer to the question that was asked",
        )
        diagnosis = diagnose([export([solo])], "solo.json")
        localization = claim(diagnosis, "localization")
        assert localization.supported is False
        assert "single-node graph" in localization.reason
        assert "0 edge(s)" not in localization.reason

    def test_two_sinks_of_the_same_agent_are_told_apart(self):
        # "scraper, scraper" reads as a rendering bug rather than as the two
        # distinct sinks it is (a fan-out or a retried step).
        spans = [
            span(name="root.run", span_id="1" * 16, agent_name="root"),
            span(name="s.run", span_id="2" * 16, parent_span_id="1" * 16, agent_name="scraper"),
            span(name="s.run", span_id="3" * 16, parent_span_id="1" * 16, agent_name="scraper"),
        ]
        detail = check(diagnose([export(spans)], "fanout.json"), "topology").detail
        assert detail.count("scraper#") == 2


class TestPayloads:
    def test_missing_outputs_are_a_gap_with_the_unscored_consequence(self):
        empty = linear_pipeline({"planner": "", "writer": "", "reviewer": ""})
        diagnosis = diagnose([empty], "empty.json")
        payloads = check(diagnosis, "payloads")
        assert payloads.level == "gap"
        assert "unscored" in payloads.consequence

    def test_a_wrapper_root_without_output_is_named_not_silently_dropped(self):
        """Rewritten: the old version asserted only that the word "wrapper root"
        appeared, which the buggy topological exclusion also satisfied.

        A root span with an empty output really is the one run whose missing
        payload is not a finding, so it stays out of the payload denominator —
        but it must be NAMED, with what the analysis will still say about it. A
        denominator that shrinks without saying whose number left is how `ok`
        got printed over runs the doctor had never looked at.
        """
        root = span(name="run", span_id="1" * 16, agent_name="orchestrator", output="")
        child = span(
            name="write.run", span_id="2" * 16, parent_span_id="1" * 16, agent_name="writer"
        )
        diagnosis = diagnose([export([root, child])], "wrapped.json")
        payloads = check(diagnosis, "payloads")
        assert payloads.level == "ok"
        assert "orchestrator" in payloads.detail
        assert "no output of its own" in payloads.detail
        assert "reports it unscored" in payloads.detail
        # Cost and model keep the whole population: a span that spent money spent
        # it whether or not it emitted an output.
        assert "0/2" in check(diagnosis, "cost").detail
        assert "0/2" in check(diagnosis, "model").detail

    def test_a_root_that_carries_real_output_is_never_excluded(self):
        """The exclusion was topological and never consulted the output.

        `planner` here is a real producing agent with real output but NO
        gen_ai.usage.cost and NO gen_ai.request.model. Being the first node of a
        linear chain was enough to drop it from every denominator, so the doctor
        reported `ok cost / tokens 2/2`, `ok model 2/2` and `yes cost` while a
        real LLM-backed node contributed no usage data at all.
        """
        spans = []
        for index, agent in enumerate(("planner", "writer", "reviewer")):
            spans.append(
                span(
                    name=f"{agent}.run",
                    span_id=f"{index + 1:016x}",
                    parent_span_id="" if index == 0 else f"{index:016x}",
                    agent_name=agent,
                    output=f"{agent} produced a complete result with plenty of detail",
                    extra_attributes={}
                    if index == 0
                    else {
                        "gen_ai.usage.cost": "0.01",
                        "gen_ai.usage.input_tokens": "100",
                        "gen_ai.usage.output_tokens": "50",
                        "gen_ai.request.model": "gpt-4o",
                    },
                )
            )
        diagnosis = diagnose([export(spans)], "uncosted.json")
        assert check(diagnosis, "payloads").detail.startswith("3/3")
        cost = check(diagnosis, "cost")
        assert cost.level == "warn"
        assert "2/3" in cost.detail
        assert "planner" in cost.detail
        assert claim(diagnosis, "cost").supported is False
        model = check(diagnosis, "model")
        assert model.level == "warn"
        assert "2/3" in model.detail
        assert "planner" in model.detail

    def test_the_doctors_denominator_is_the_analysis_population(self, demo_traces):
        """The doctor's stated contract, checked against the repo's own trace.

        `testdata/demo_pipeline_happy.json` has six runs and the analysis prices
        all six. The topological exclusion dropped `orchestrator` — 162 chars of
        output, cost_usd 0.012, the largest single cost in the graph, 32% of the
        bundle total — and the doctor then certified `ok cost 5/5` and `yes cost`
        over a population the analysis never used.
        """
        from detective_cli.bundle import bundles_from_exports, load_trace

        exports = load_trace(demo_traces["happy"])
        bundle = bundles_from_exports(exports)[0]
        costed = [r for r in bundle.runs if r.cost_usd is not None]
        assert len(bundle.runs) == 6 and len(costed) == 6

        diagnosis = diagnose(exports, str(demo_traces["happy"]))
        assert f"gen_ai.usage.cost {len(costed)}/{len(bundle.runs)}" in check(
            diagnosis, "cost"
        ).detail
        assert check(diagnosis, "payloads").detail.startswith(
            f"{len(bundle.runs)}/{len(bundle.runs)}"
        )
        assert "not graded here" not in check(diagnosis, "payloads").detail
        assert claim(diagnosis, "cost").reason.endswith(
            f"{len(bundle.runs)}/{len(bundle.runs)} runs"
        )


class TestCostAndDeliverable:
    def test_no_usage_attributes_means_cost_stays_unknown(self):
        diagnosis = diagnose([linear_pipeline()], "trace.json")
        cost = check(diagnosis, "cost")
        assert cost.level == "warn"
        assert "cost stays unknown" in cost.consequence
        assert claim(diagnosis, "cost").supported is False

    def test_full_cost_coverage_supports_the_cost_claim(self):
        spans = []
        for index, agent in enumerate(("planner", "writer", "reviewer")):
            spans.append(
                span(
                    name=f"{agent}.run",
                    span_id=f"{index + 1:016x}",
                    parent_span_id="" if index == 0 else f"{index:016x}",
                    agent_name=agent,
                    extra_attributes={
                        "gen_ai.usage.cost": "0.01",
                        "gen_ai.usage.input_tokens": "100",
                        "gen_ai.usage.output_tokens": "50",
                        "gen_ai.request.model": "gpt-4o",
                    },
                )
            )
        diagnosis = diagnose([export(spans)], "costed.json")
        assert check(diagnosis, "cost").level == "ok"
        assert check(diagnosis, "model").level == "ok"
        assert claim(diagnosis, "cost").supported is True

    def test_a_file_reference_without_artifact_text_is_not_checkable(self):
        # worker.scoring.opaque_artifact_refs: the deliverable names a .pdf whose
        # content never travelled, so the terminal judge grades a claim about an
        # unopened file.
        # On `writer`, not `reviewer`: the reviewer is a verifier, so the
        # deliverable the terminal check grades is the writer's output.
        refs = linear_pipeline({"writer": "Wrote the proposal to out/final.pdf (12 pages)."})
        diagnosis = diagnose([refs], "opaque.json")
        deliverable = check(diagnosis, "deliverable")
        assert deliverable.level == "warn"
        assert "out/final.pdf" in deliverable.detail
        assert "not_checkable" in deliverable.consequence
        assert claim(diagnosis, "terminal check").supported is False

    def test_embedded_artifact_text_keeps_the_terminal_check(self):
        embedded = linear_pipeline(
            {"writer": "Wrote out/final.pdf\n[artifact_text out/final.pdf]:\nthe body"}
        )
        diagnosis = diagnose([embedded], "embedded.json")
        assert check(diagnosis, "deliverable").level == "ok"
        assert claim(diagnosis, "terminal check").supported is True


class TestCommand:
    def test_doctor_never_gates_even_on_a_trace_it_cannot_use(self, trace_file, capsys):
        # A diagnostic that fails builds gets deleted from CI.
        chain = span(name="collect", span_id="0" * 16, agent_name="collector")
        chain["attributes"][0]["value"]["stringValue"] = "CHAIN"
        path = trace_file(export([chain]))
        code, out, _ = run_cli(["doctor", str(path), "--color", "never"], capsys)
        assert code == EXIT_CLEAN
        assert "always exits 0" in out

    def test_a_missing_path_still_exits_two(self, tmp_path, capsys):
        # "no such file" is a fact about the command line, not about
        # instrumentation: a typo must not read as a clean bill of health.
        code, _, err = run_cli(["doctor", str(tmp_path / "nope.json")], capsys)
        assert code == EXIT_ERROR
        assert "cannot read" in err

    def test_a_binary_export_is_diagnosed_not_crashed(self, tmp_path, capsys):
        # Python's stock OTLPSpanExporter writes protobuf; load_trace guards only
        # OSError, so the bytes reach the UTF-8 decoder. Doctor reports it.
        path = tmp_path / "spans.pb"
        path.write_bytes(b"\x0a\x08protobuf\xff")
        code, out, _ = run_cli(["doctor", str(path), "--color", "never"], capsys)
        assert code == EXIT_CLEAN
        assert "protobuf" in out

    def test_a_file_that_is_not_otlp_json_is_a_finding(self, tmp_path, capsys):
        path = tmp_path / "trace.json"
        path.write_text('{"resourceSpans": [', encoding="utf-8")
        code, out, _ = run_cli(["doctor", str(path), "--color", "never"], capsys)
        assert code == EXIT_CLEAN
        assert "trace format" in out

    def test_json_mode_is_pipeable(self, trace_file, capsys):
        path = trace_file(linear_pipeline({"writer": '{"ok": true, "step": "write"}'}))
        code, out, _ = run_cli(["doctor", str(path), "--json"], capsys)
        assert code == EXIT_CLEAN
        payload = json.loads(out)
        assert payload["worst_level"] == "warn"
        ids = [c["id"] for c in payload["graphs"][0]["checks"]]
        assert "status_records" in ids
        assert {c["name"] for c in payload["graphs"][0]["claims"]} == {
            "localization",
            "cost",
            "terminal check",
        }

    def test_colour_is_honoured_both_ways(self, trace_file, capsys):
        path = trace_file(linear_pipeline())
        _, plain, _ = run_cli(["doctor", str(path), "--color", "never"], capsys)
        _, painted, _ = run_cli(["doctor", str(path), "--color", "always"], capsys)
        assert "\033[" not in plain
        assert "\033[" in painted

    def test_no_verdict_language_appears_anywhere(self, trace_file, capsys):
        # The doctor reports capture, never quality. A "PASSED"/"FAILED" here
        # would be a verdict formed without a judge or a blame report.
        path = trace_file(linear_pipeline())
        _, out, _ = run_cli(["doctor", str(path), "--color", "never"], capsys)
        for word in ("PASSED", "FAILED", "incident", "culprit", "confidence"):
            assert word not in out


class TestQuiescence:
    """The effective window is read from the ingest, never guessed."""

    def test_a_known_window_is_reported_with_what_it_means(self):
        result = check_quiescence(120.0)

        assert result.level == "ok"
        assert "120" in result.detail
        assert "finalized" in result.detail

    def test_an_unknown_window_is_admitted_not_defaulted(self):
        # "30s is the default" is a fact about config.py, not about the
        # deployment in front of the reader — an unknown stays unknown.
        result = check_quiescence(None)

        assert result.level == "warn"
        assert "unknown" in result.detail

    def test_an_unreachable_ingest_yields_no_config(self):
        assert fetch_ingest_config("http://localhost:1", timeout=0.5) is None

    def test_doctor_reports_the_ingests_effective_quiescence(
        self, trace_file, capsys, monkeypatch
    ):
        monkeypatch.setenv("INGEST_URL", "http://ingest.test")
        monkeypatch.setattr(
            "detective_cli.cli.fetch_ingest_config",
            lambda url: {"graph_quiescence_seconds": 120.0},
        )
        path = trace_file(linear_pipeline())
        _, out, _ = run_cli(["doctor", str(path), "--color", "never"], capsys)
        assert "graph_quiescence_seconds=120" in out

    def test_an_empty_ingest_url_skips_the_check(self, trace_file, capsys, monkeypatch):
        # Same opt-out convention as demo/run.sh: empty means no ingest.
        monkeypatch.setenv("INGEST_URL", "")
        path = trace_file(linear_pipeline())
        _, out, _ = run_cli(["doctor", str(path), "--color", "never"], capsys)
        assert "quiescence" not in out


class TestRendering:
    def test_every_non_ok_check_carries_a_consequence_and_a_fix(self):
        # A finding without a consequence is trivia the reader cannot act on.
        for payload in (
            linear_pipeline(),
            linear_pipeline({"planner": "", "writer": "", "reviewer": ""}),
            linear_pipeline({"writer": '{"ok": true, "step": "write"}'}),
        ):
            diagnosis = diagnose([payload], "trace.json")
            checks = list(diagnosis.checks) + [
                c for g in diagnosis.graphs for c in g.checks
            ]
            for c in checks:
                if c.level == "ok":
                    continue
                assert c.consequence, f"{c.id} has no consequence"
                assert c.fix, f"{c.id} has no fix"

    def test_terminal_and_json_report_the_same_levels(self):
        """Rewritten: the old version asserted `payload["worst_level"] ==
        diagnosis.worst_level`, comparing a value to the field it had just been
        copied from, and then restated the Level Literal. It could not fail.

        What matters is that the two renderers cannot disagree, and that
        worst_level is derived rather than asserted — so it is recomputed here
        from the JSON's own check list, and every check is looked for in the
        terminal text with the level the JSON gave it.
        """
        diagnosis = diagnose(
            [linear_pipeline({"planner": "", "writer": '{"ok": true, "step": "w"}'})],
            "trace.json",
        )
        payload = render_doctor_json(diagnosis)
        text = render_doctor_terminal(diagnosis, color=False)

        emitted = [
            *payload["checks"],
            *[c for g in payload["graphs"] for c in g["checks"]],
        ]
        assert len(emitted) == len(diagnosis.checks) + sum(
            len(g.checks) for g in diagnosis.graphs
        ), "--json dropped a check the terminal report shows"
        levels = {c["level"] for c in emitted}
        assert levels & {"warn", "gap"}, "fixture must exercise a non-ok level"
        rank = {"ok": 0, "warn": 1, "gap": 2}
        assert payload["worst_level"] == max(levels, key=lambda lv: rank[lv])

        flat = " ".join(text.split())
        lines = [line.strip() for line in text.splitlines()]
        for entry in emitted:
            assert any(
                line.startswith(entry["level"]) and entry["title"] in line
                for line in lines
            ), f"{entry['title']} is not rendered at level {entry['level']}"
            assert " ".join(entry["detail"].split()) in flat, entry["title"]

        assert "What you can claim" in text
        for claim_json in payload["graphs"][0]["claims"]:
            assert claim_json["name"] in text
            assert claim_json["supported"] in (True, False, None)

    def test_an_unsettleable_claim_renders_as_a_question_not_as_a_no(self):
        """"Cannot tell" is a first-class answer, in the terminal and in --json.

        Collapsing it into `no` is what told a working router that no node
        carried a judgeable payload; collapsing it into `yes` is the failure the
        whole command exists to prevent.
        """
        router = linear_pipeline({agent: '{"action": "route"}' for agent in
                                  ("planner", "writer", "reviewer")})
        diagnosis = diagnose([router], "router.json")
        payload = render_doctor_json(diagnosis)
        supported = {c["name"]: c["supported"] for c in payload["graphs"][0]["claims"]}
        assert supported["localization"] is None
        assert '"supported": null' in json.dumps(payload)  # the wire format, not a 0
        text = render_doctor_terminal(diagnosis, color=False)
        assert "?    localization" in text
        painted = render_doctor_terminal(diagnosis, color=True)
        assert "\033[36m?  \033[0m" in painted  # the "unknown" tone, not ok and not fail

    def test_the_report_wraps_to_a_readable_width(self):
        # Consequences are full sentences; unwrapped they become a wall.
        diagnosis = diagnose(
            [linear_pipeline({"planner": "", "writer": "", "reviewer": ""})], "t.json"
        )
        text = render_doctor_terminal(diagnosis, color=False)
        assert max(len(line) for line in text.splitlines()) <= 100


class TestOneExtraKeyCannotDefeatTheCheck:
    """A ping with a correlation id on it is still a ping.

    `_classify_object` returned "work" the moment it met any key outside its
    vocabularies holding a string — so `{"ok": true, "step": "plan",
    "run_id": "abc-123"}` earned an affirmative "no output looks like a status
    ping". The false clean bill landed at a chain's FIRST node, which is exactly
    the position the command exists to inspect, and `ok` there is not "I did not
    look" — it is a positive statement.
    """

    def test_a_ping_carrying_metadata_is_not_certified_as_work(self):
        assert classify_payload('{"ok": true, "step": "plan", "run_id": "abc-123"}') != "work"
        assert classify_payload('{"ok": true, "step": "c", "docs": 42, "trace": "t-1"}') != "work"

    def test_a_bare_ping_is_still_caught(self):
        assert classify_payload('{"ok": true, "step": "collect"}') == "ping"

    def test_short_domain_data_is_still_work(self):
        # No lifecycle vocabulary anywhere: nothing suggests a status report.
        assert classify_payload('{"company": "Acme"}') == "work"

    def test_a_router_decision_stays_unclear_not_a_false_positive(self):
        # The check must not tell a correctly instrumented router it is broken.
        assert classify_payload('{"action": "route_to_analyst"}') == "unclear"

    def test_substantive_text_under_an_unknown_key_is_work(self):
        assert classify_payload(
            '{"finding": "Acme reported $412M revenue in Q3 2024, up 12% YoY."}'
        ) == "work"
