"""Tests for worker/checks_content.py — deterministic content checks."""

from __future__ import annotations

import json
from datetime import datetime

from worker.checks_content import (
    language_mismatch_signals,
    required_section_signals,
    sum_invariant_signals,
    temporal_invariant_signals,
    unit_inconsistency_signals,
)

# ---------------------------------------------------------------------------
# required_section_signals
# ---------------------------------------------------------------------------


def _rule(name="Summary", match="substring", pattern="## Summary", **kw):
    return {"name": name, "match": match, "pattern": pattern, **kw}


class TestRequiredSection:
    def test_present_substring_no_signal(self):
        assert required_section_signals("intro\n## Summary\nbody", [_rule()]) == []

    def test_missing_substring_fails(self):
        signals = required_section_signals("no such heading here", [_rule()])
        assert len(signals) == 1
        sig = signals[0]
        assert sig["name"] == "missing_required_section"
        assert sig["severity"] == "fail"
        assert sig["detail"] == "required section 'Summary' not found"
        assert sig["basis"] == "substring '## Summary' not present in the deliverable text"

    def test_substring_is_case_insensitive_by_default(self):
        assert required_section_signals("## SUMMARY", [_rule()]) == []

    def test_substring_case_sensitive_when_requested(self):
        rule = _rule(case_sensitive=True)
        assert required_section_signals("## SUMMARY", [rule]) != []
        assert required_section_signals("## Summary", [rule]) == []

    def test_substring_unicode_nfc_casefold(self):
        # NFD-decomposed haystack vs NFC pattern: must still match.
        import unicodedata

        haystack = unicodedata.normalize("NFD", "## Závěr")
        rule = _rule(name="Zaver", pattern="## závěr")
        assert required_section_signals(haystack, [rule]) == []

    def test_regex_match(self):
        rule = _rule(match="regex", pattern=r"^#+\s*Summary", name="Summary")
        assert required_section_signals("prefix\ntext # Summary", [rule]) != []
        assert required_section_signals("## Summary", [rule]) == []

    def test_regex_case_insensitive_by_default(self):
        rule = _rule(match="regex", pattern=r"summary")
        assert required_section_signals("SUMMARY", [rule]) == []

    def test_invalid_regex_skipped(self):
        rule = _rule(match="regex", pattern=r"([unclosed")
        assert required_section_signals("anything", [rule]) == []

    def test_none_text_counts_as_missing(self):
        signals = required_section_signals(None, [_rule()])
        assert len(signals) == 1
        assert signals[0]["name"] == "missing_required_section"

    def test_malformed_rules_no_signal_no_raise(self):
        bad_rules = [
            None,
            "not a dict",
            {},  # no pattern
            {"pattern": "x"},  # no match kind
            {"pattern": "x", "match": "fuzzy"},  # unknown kind
            {"pattern": 42, "match": "substring"},  # non-str pattern
        ]
        assert required_section_signals("text", bad_rules) == []
        assert required_section_signals("text", "not a list") == []

    def test_rule_without_name_falls_back_to_pattern(self):
        rule = {"match": "substring", "pattern": "## Detail"}
        signals = required_section_signals("nope", [rule])
        assert signals[0]["detail"] == "required section '## Detail' not found"

    def test_multiple_rules_multiple_signals(self):
        rules = [_rule(name="A", pattern="AAA"), _rule(name="B", pattern="BBB")]
        signals = required_section_signals("only AAA here", rules)
        assert len(signals) == 1
        assert "B" in signals[0]["detail"]


# ---------------------------------------------------------------------------
# sum_invariant_signals
# ---------------------------------------------------------------------------


def _sum_rule(**kw):
    rule = {
        "name": "invoice total",
        "items_path": "items[].price",
        "total_path": "total",
    }
    rule.update(kw)
    return rule


class TestSumInvariant:
    def test_matching_sum_no_signal(self):
        out = json.dumps({"items": [{"price": 10}, {"price": 20}], "total": 30})
        assert sum_invariant_signals(out, [_sum_rule()]) == []

    def test_breach_fails_with_exact_shape(self):
        out = json.dumps({"items": [{"price": 10}, {"price": 20}], "total": 25})
        signals = sum_invariant_signals(out, [_sum_rule()])
        assert len(signals) == 1
        sig = signals[0]
        assert sig["name"] == "numeric_invariant_breach"
        assert sig["severity"] == "fail"
        assert sig["detail"] == "sum(items[].price)=30 != total=25"
        assert sig["basis"] == "registered invariant 'invoice total', tolerance 0.01"

    def test_within_tolerance_no_signal(self):
        out = json.dumps({"items": [{"price": 10.004}, {"price": 20}], "total": 30})
        assert sum_invariant_signals(out, [_sum_rule()]) == []

    def test_custom_tolerance(self):
        out = json.dumps({"items": [{"price": 10}, {"price": 20}], "total": 29})
        assert sum_invariant_signals(out, [_sum_rule(tolerance=2.0)]) == []
        assert sum_invariant_signals(out, [_sum_rule(tolerance=0.5)]) != []

    def test_nested_total_path(self):
        out = json.dumps(
            {"items": [{"price": 5}], "summary": {"grand_total": 9}}
        )
        rule = _sum_rule(total_path="summary.grand_total")
        signals = sum_invariant_signals(out, [rule])
        assert len(signals) == 1
        assert "summary.grand_total=9" in signals[0]["detail"]

    def test_missing_items_key_rule_does_not_apply(self):
        out = json.dumps({"total": 30})
        assert sum_invariant_signals(out, [_sum_rule()]) == []

    def test_missing_total_key_rule_does_not_apply(self):
        out = json.dumps({"items": [{"price": 10}]})
        assert sum_invariant_signals(out, [_sum_rule()]) == []

    def test_field_never_present_rule_does_not_apply(self):
        out = json.dumps({"items": [{"qty": 1}, {"qty": 2}], "total": 30})
        assert sum_invariant_signals(out, [_sum_rule()]) == []

    def test_non_numeric_total_no_signal(self):
        out = json.dumps({"items": [{"price": 10}], "total": "thirty"})
        assert sum_invariant_signals(out, [_sum_rule()]) == []

    def test_non_json_output_no_signal(self):
        assert sum_invariant_signals("plain prose, no json", [_sum_rule()]) == []
        assert sum_invariant_signals(None, [_sum_rule()]) == []
        assert sum_invariant_signals("", [_sum_rule()]) == []

    def test_json_embedded_in_prose_is_parsed(self):
        out = "Here you go: " + json.dumps(
            {"items": [{"price": 1}, {"price": 2}], "total": 99}
        )
        assert len(sum_invariant_signals(out, [_sum_rule()])) == 1

    def test_malformed_rules_skipped(self):
        out = json.dumps({"items": [{"price": 10}], "total": 99})
        bad = [
            None,
            {"items_path": "items[].price"},  # no total_path
            {"items_path": "noseparator", "total_path": "total"},
            {"items_path": "a[].b[].c", "total_path": "total"},
            {"items_path": 3, "total_path": "total"},
        ]
        assert sum_invariant_signals(out, bad) == []

    def test_booleans_do_not_count_as_numbers(self):
        out = json.dumps({"items": [{"price": True}], "total": 1})
        assert sum_invariant_signals(out, [_sum_rule()]) == []


# ---------------------------------------------------------------------------
# unit_inconsistency_signals
# ---------------------------------------------------------------------------


class TestUnitInconsistency:
    def test_family_switch_warns(self):
        signals = unit_inconsistency_signals(
            "Rozpočet je 500 000 CZK na kvartál.",
            "The budget is EUR 20,000 per quarter.",
        )
        assert len(signals) == 1
        sig = signals[0]
        assert sig["name"] == "unit_inconsistency"
        assert sig["severity"] == "warn"
        assert "CZK" in sig["basis"] and "EUR" in sig["basis"]

    def test_symbol_tokens_detected(self):
        signals = unit_inconsistency_signals("price: 100 Kč", "price: $4.20")
        assert len(signals) == 1
        assert "CZK" in signals[0]["basis"] and "USD" in signals[0]["basis"]

    def test_same_family_no_signal(self):
        assert unit_inconsistency_signals("100 CZK", "2 300 Kč") == []

    def test_output_mentions_both_no_signal(self):
        # Conversion output legitimately names both families.
        assert (
            unit_inconsistency_signals("100 CZK", "100 CZK is about 4 EUR") == []
        )

    def test_input_with_two_families_no_signal(self):
        assert unit_inconsistency_signals("100 CZK or 4 EUR", "5 USD") == []

    def test_input_without_currency_no_signal(self):
        assert unit_inconsistency_signals("no money here", "price 100 EUR") == []

    def test_output_without_currency_no_signal(self):
        assert unit_inconsistency_signals("100 CZK", "no currency mentioned") == []

    def test_word_boundary_no_false_positive(self):
        # 'eureka' must not read as EUR; 'usda' must not read as USD.
        assert unit_inconsistency_signals("100 CZK", "eureka usda report") == []

    def test_none_inputs_no_signal(self):
        assert unit_inconsistency_signals(None, "100 EUR") == []
        assert unit_inconsistency_signals("100 CZK", None) == []


# ---------------------------------------------------------------------------
# temporal_invariant_signals
# ---------------------------------------------------------------------------

_RUN_STARTED = datetime(2026, 7, 23, 12, 0, 0)


class TestTemporalInvariant:
    def test_ordered_dates_no_signal(self):
        out = json.dumps({"start_date": "2026-08-01", "end_date": "2026-08-15"})
        assert temporal_invariant_signals(out, run_started_at=_RUN_STARTED) == []

    def test_end_before_start_warns(self):
        out = json.dumps({"start_date": "2026-08-15", "end_date": "2026-08-01"})
        signals = temporal_invariant_signals(out, run_started_at=_RUN_STARTED)
        assert len(signals) == 1
        sig = signals[0]
        assert sig["name"] == "temporal_invariant_breach"
        assert sig["severity"] == "warn"
        assert "'end_date'" in sig["detail"] and "'start_date'" in sig["detail"]

    def test_deadline_before_start_warns(self):
        out = json.dumps({"start": "2026-09-01", "deadline": "2026-08-01"})
        signals = temporal_invariant_signals(out, run_started_at=None)
        assert len(signals) == 1
        assert signals[0]["name"] == "temporal_invariant_breach"

    def test_datetime_values_compared(self):
        out = json.dumps(
            {"startTime": "2026-08-01T10:00:00", "endTime": "2026-08-01T09:00"}
        )
        signals = temporal_invariant_signals(out, run_started_at=_RUN_STARTED)
        assert len(signals) == 1

    def test_deadline_in_the_past_warns(self):
        out = json.dumps({"deadline": "2026-07-01"})
        signals = temporal_invariant_signals(out, run_started_at=_RUN_STARTED)
        assert len(signals) == 1
        assert signals[0]["detail"] == "'deadline'=2026-07-01 deadline in the past"
        assert signals[0]["basis"] == "run started 2026-07-23"

    def test_due_date_in_the_past_warns(self):
        out = json.dumps({"due_date": "2025-12-31"})
        signals = temporal_invariant_signals(out, run_started_at=_RUN_STARTED)
        assert len(signals) == 1

    def test_deadline_today_or_future_no_signal(self):
        out = json.dumps({"deadline": "2026-07-23", "due_date": "2026-09-30"})
        assert temporal_invariant_signals(out, run_started_at=_RUN_STARTED) == []

    def test_no_run_started_at_skips_past_check(self):
        out = json.dumps({"deadline": "2020-01-01"})
        assert temporal_invariant_signals(out, run_started_at=None) == []

    def test_nested_and_list_structures_walked(self):
        out = json.dumps(
            {
                "phases": [
                    {"start": "2026-08-01", "end": "2026-07-01"},
                    {"start": "2026-08-01", "end": "2026-09-01"},
                ]
            }
        )
        signals = temporal_invariant_signals(out, run_started_at=_RUN_STARTED)
        assert len(signals) == 1

    def test_non_sibling_dates_not_paired(self):
        out = json.dumps(
            {"a": {"start": "2026-08-15"}, "b": {"end": "2026-08-01"}}
        )
        assert temporal_invariant_signals(out, run_started_at=_RUN_STARTED) == []

    def test_non_date_values_ignored(self):
        out = json.dumps({"start": "soon", "end": "later", "deadline": 42})
        assert temporal_invariant_signals(out, run_started_at=_RUN_STARTED) == []

    def test_invalid_calendar_date_tolerated(self):
        out = json.dumps({"start": "2026-13-45", "end": "2026-01-01"})
        assert temporal_invariant_signals(out, run_started_at=_RUN_STARTED) == []

    def test_non_json_no_signal(self):
        assert temporal_invariant_signals("plain text", run_started_at=_RUN_STARTED) == []
        assert temporal_invariant_signals(None, run_started_at=_RUN_STARTED) == []


# ---------------------------------------------------------------------------
# language_mismatch_signals
# ---------------------------------------------------------------------------

_CZECH = (
    "Dobrý den, toto je závěrečná zpráva o projektu. Analýza ukázala, že "
    "výsledky jsou velmi dobré a doporučujeme pokračovat v dalším vývoji "
    "produktu. Děkujeme za spolupráci a těšíme se na další setkání."
)
_ENGLISH = (
    "Hello, this is the final project report. The analysis shows that the "
    "results are very good and we recommend continuing further development "
    "of the product. Thank you very much for the cooperation."
)


class TestLanguageMismatch:
    def test_czech_output_expected_english_fails(self):
        signals = language_mismatch_signals("en", _CZECH)
        assert len(signals) == 1
        sig = signals[0]
        assert sig["name"] == "language_mismatch"
        assert sig["severity"] == "fail"
        assert "expected 'en' but detected 'cs'" in sig["detail"]
        assert "langdetect over" in sig["basis"]

    def test_english_output_expected_czech_fails(self):
        signals = language_mismatch_signals("cs", _ENGLISH)
        assert len(signals) == 1
        assert "expected 'cs' but detected 'en'" in signals[0]["detail"]

    def test_matching_language_no_signal(self):
        assert language_mismatch_signals("cs", _CZECH) == []
        assert language_mismatch_signals("en", _ENGLISH) == []

    def test_expected_none_no_signal(self):
        assert language_mismatch_signals(None, _CZECH) == []
        assert language_mismatch_signals("", _CZECH) == []

    def test_short_text_skipped(self):
        assert language_mismatch_signals("en", "Krátký text.") == []
        assert language_mismatch_signals("en", None) == []

    def test_min_chars_override(self):
        short_czech = "Toto je krátká česká věta o počasí a přírodě."
        assert language_mismatch_signals("en", short_czech) == []
        assert language_mismatch_signals("en", short_czech, min_chars=10) != []

    def test_json_output_uses_string_leaves_only(self):
        payload = json.dumps(
            {"status": "ok", "count": 42, "body": _CZECH, "note": "krátké"}
        )
        signals = language_mismatch_signals("en", payload)
        assert len(signals) == 1
        assert "detected 'cs'" in signals[0]["detail"]

    def test_json_with_only_short_leaves_skipped(self):
        payload = json.dumps({"a": "hi", "b": "ok", "c": 1})
        assert language_mismatch_signals("en", payload) == []

    def test_deterministic_across_calls(self):
        first = language_mismatch_signals("en", _CZECH)
        for _ in range(3):
            assert language_mismatch_signals("en", _CZECH) == first

    def test_locale_style_expected_code_normalized(self):
        assert language_mismatch_signals("EN-us", _ENGLISH) == []


# --- round-6 fixes: word_prefix + honest basis subject ----------------------


def test_word_prefix_matches_czech_inflections():
    """'rozpočt' must match 'rozpočtová tabulka' — a full-word substring would
    false-fail a document that genuinely contains the section inflected."""
    from worker.checks_content import required_section_signals, section_present

    # NB the correct Czech stem is 'rozpoč' — 'rozpočet' has an epenthetic 'e'
    # ('rozpoč-e-t' vs 'rozpoč-t-ová'), so 'rozpočt' would MISS the nominative.
    rule = {"name": "budget", "match": "word_prefix", "pattern": "rozpoč"}
    assert section_present("Rozpočtová tabulka: 12 000 Kč", rule) is True
    assert section_present("Rozpočet projektu", rule) is True
    assert section_present("dokument bez té sekce", rule) is False
    assert required_section_signals("Rozpočtová tabulka", [rule]) == []
    sigs = required_section_signals("nic tu není", [rule])
    assert len(sigs) == 1 and "word_prefix" in sigs[0]["basis"]


def test_basis_names_the_checked_subject():
    """The basis must state WHAT text was checked — a per-node check claiming
    'the deliverable text' would be a false statement in the evidence."""
    from worker.checks_content import required_section_signals

    rule = {"name": "budget", "match": "substring", "pattern": "rozpočet"}
    default = required_section_signals("x", [rule])[0]
    node = required_section_signals("x", [rule], subject="this node's own output")[0]
    assert "the deliverable text" in default["basis"]
    assert "this node's own output" in node["basis"]
