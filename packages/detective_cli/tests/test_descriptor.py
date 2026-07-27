"""Presentation coverage, pinned against the engine's own type definitions.

The terminal renderer keys off typed enums, so the failure mode worth guarding
is silent drift: the engine gains a report type or defect kind and the CLI
renders it as UNANALYSED or "Foo defect" without anyone noticing. These tests
fail the moment that happens, and they compare against the engine's types
rather than a copied list, so they cannot drift themselves.
"""

from __future__ import annotations

from typing import get_args

import pytest
from blame_engine import Design, External, Localized, ReportType, Unlocalized
from blame_engine.defect import DefectKind

from detective_cli.descriptor import (
    CAVEAT_LABELS,
    CULPRIT_HEADING,
    DEFECT_KIND_META,
    NOT_VERIFIED_VERDICT,
    PASSED_VERDICT,
    UNANALYSED_VERDICT,
    VERDICT_META,
    culprit_heading,
    defect_descriptor,
    origin_phrase,
    origin_tone,
    verdict_descriptor,
)


class TestCoverage:
    def test_every_engine_report_type_has_a_verdict(self):
        assert set(VERDICT_META) == set(get_args(ReportType))

    def test_every_engine_report_type_has_a_culprit_heading(self):
        assert set(CULPRIT_HEADING) == set(get_args(ReportType))

    def test_every_engine_defect_kind_has_a_label(self):
        assert set(DEFECT_KIND_META) == set(get_args(DefectKind))

    def test_every_origin_variant_has_a_tone(self):
        for origin in (Localized, Unlocalized, External, Design):
            assert origin_tone(origin.__name__) in ("ok", "warn", "fail", "unknown")

    def test_every_caveat_field_on_a_defect_has_a_label(self):
        from blame_engine.defect import Defect

        caveat_fields = {
            name
            for name in Defect.__dataclass_fields__
            if name in {"base_assumed", "observability_boundary", "unverified_in_channel", "recovered"}
        }
        assert set(CAVEAT_LABELS) == caveat_fields


class TestVerdicts:
    def test_an_unknown_report_type_falls_back_rather_than_crashing(self):
        assert verdict_descriptor("something_new") is UNANALYSED_VERDICT

    def test_no_report_type_means_unanalysed(self):
        assert verdict_descriptor(None) is UNANALYSED_VERDICT

    @pytest.mark.parametrize(
        "report_type",
        ["cut_point", "multi_culprit", "verification_gap", "loop_detected"],
    )
    def test_localised_failures_read_as_failures(self, report_type):
        assert verdict_descriptor(report_type).label == "FAILED"
        assert verdict_descriptor(report_type).tone == "fail"

    def test_a_recovered_degradation_is_not_sold_as_a_clean_pass(self):
        descriptor = verdict_descriptor("degraded_recovered")
        assert descriptor.tone == "warn"
        assert "warnings" in descriptor.label.lower()

    def test_inconclusive_is_not_coloured_as_a_failure(self):
        assert verdict_descriptor("unclassified").tone == "unknown"

    def test_not_verified_is_distinct_from_both_passed_and_inconclusive(self):
        # The three states answer different questions and must not collapse.
        assert NOT_VERIFIED_VERDICT.label != PASSED_VERDICT.label
        assert NOT_VERIFIED_VERDICT.label != verdict_descriptor("unclassified").label
        assert NOT_VERIFIED_VERDICT.tone != "ok"

    def test_not_verified_says_absence_of_evidence_explicitly(self):
        assert "absence of evidence" in NOT_VERIFIED_VERDICT.template


class TestDefects:
    def test_a_localized_defect_is_toned_as_a_failure(self):
        descriptor = defect_descriptor("contract", {"kind": "Localized", "run_id": "r1"})
        assert descriptor.tone == "fail"
        assert descriptor.label == "Contract breach"

    def test_the_same_defect_unlocalized_is_only_a_warning_at_most(self):
        # We observed it but could not attribute it — not the same claim.
        localized = defect_descriptor("content", {"kind": "Localized", "run_id": "r1"})
        unlocalized = defect_descriptor("content", {"kind": "Unlocalized", "reason": "x"})
        assert localized.tone == "fail"
        assert unlocalized.tone == "unknown"

    def test_an_unknown_kind_still_renders_something_readable(self):
        descriptor = defect_descriptor("brandnew", {"kind": "Localized", "run_id": "r"})
        assert "Brandnew" in descriptor.label

    def test_the_template_carries_an_origin_slot_for_the_caller(self):
        descriptor = defect_descriptor("form", {"kind": "Localized", "run_id": "r"})
        assert "{origin}" in descriptor.template


class TestOriginPhrase:
    def test_a_localized_origin_names_the_node(self):
        phrase = origin_phrase({"kind": "Localized", "run_id": "r1"}, lambda r: "writer")
        assert phrase == "writer"

    def test_a_localized_origin_falls_back_to_the_raw_id(self):
        assert origin_phrase({"kind": "Localized", "run_id": "r1"}) == "r1"

    def test_an_unlocalized_origin_shows_its_reason_code(self):
        phrase = origin_phrase({"kind": "Unlocalized", "reason": "no_scored_predecessor"})
        assert "no_scored_predecessor" in phrase

    def test_an_external_origin_points_outside_the_graph(self):
        assert "external" in origin_phrase({"kind": "External", "run_id": None})

    def test_a_design_origin_without_a_reason_still_reads(self):
        assert "design" in origin_phrase({"kind": "Design", "reason": ""})

    def test_an_unrecognised_origin_does_not_crash_the_report(self):
        assert origin_phrase({"kind": "Martian"}) == "an unrecognised origin"


class TestCulpritHeading:
    def test_headings_pluralise_for_several_culprits(self):
        assert culprit_heading("verification_gap", plural=True) == "Rubber-stamping verifiers"

    def test_singular_is_returned_untouched(self):
        assert culprit_heading("verification_gap") == "Rubber-stamping verifier"

    def test_an_unknown_type_gets_a_neutral_heading(self):
        assert culprit_heading(None) == "Suspected culprit"
