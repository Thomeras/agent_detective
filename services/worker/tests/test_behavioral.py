"""Tests for worker/behavioral.py — behavioral trace signals."""

from __future__ import annotations

import json
from types import SimpleNamespace

from worker.behavioral import (
    cost_zscore_signals,
    duplicate_side_effect_signals,
    loop_fingerprint_signals,
    parse_tool_calls,
    retry_storm_signals,
    tool_args_signals,
)
from worker.types import AgentStat


def call(name: str, sha: str = "sha-1", status: str = "ok", **extra) -> dict:
    return {"name": name, "args_sha": sha, "status": status, **extra}


# ---------------------------------------------------------------------------
# parse_tool_calls
# ---------------------------------------------------------------------------


class TestParseToolCalls:
    def test_parses_array(self):
        text = json.dumps(
            [
                {"name": "search", "args_sha": "abc", "status": "ok"},
                {"name": "fetch", "args_sha": "def", "status": "error"},
            ]
        )
        parsed = parse_tool_calls(text)
        assert parsed == [
            {"name": "search", "args_sha": "abc", "status": "ok"},
            {"name": "fetch", "args_sha": "def", "status": "error"},
        ]

    def test_single_dict_wrapped(self):
        parsed = parse_tool_calls(json.dumps({"name": "x", "args_sha": "s"}))
        assert len(parsed) == 1
        assert parsed[0]["name"] == "x"
        assert parsed[0]["status"] is None

    def test_missing_fields_defaulted_not_dropped(self):
        parsed = parse_tool_calls(json.dumps([{"status": "ok"}]))
        assert parsed == [{"name": "?", "args_sha": "", "status": "ok"}]

    def test_args_passed_through(self):
        parsed = parse_tool_calls(
            json.dumps([{"name": "x", "args_sha": "s", "args": {"to": "a@b.cz"}}])
        )
        assert parsed[0]["args"] == {"to": "a@b.cz"}

    def test_malformed_inputs_yield_empty(self):
        assert parse_tool_calls(None) == []
        assert parse_tool_calls("") == []
        assert parse_tool_calls("not json {") == []
        assert parse_tool_calls(json.dumps("a string")) == []
        assert parse_tool_calls(json.dumps(42)) == []

    def test_non_dict_entries_skipped(self):
        parsed = parse_tool_calls(json.dumps([{"name": "x", "args_sha": "s"}, 7, "y"]))
        assert len(parsed) == 1


# ---------------------------------------------------------------------------
# loop_fingerprint_signals
# ---------------------------------------------------------------------------


class TestLoopFingerprint:
    def test_no_repetition_no_signal(self):
        calls = [call("a", "1"), call("b", "1"), call("a", "2")]
        assert loop_fingerprint_signals(calls) == []

    def test_consecutive_repeat_warns(self):
        calls = [call("search", "abc"), call("search", "abc"), call("search", "abc")]
        signals = loop_fingerprint_signals(calls)
        assert len(signals) == 1
        sig = signals[0]
        assert sig["name"] == "loop_fingerprint"
        assert sig["severity"] == "warn"
        assert sig["detail"] == "tool 'search' called 3x consecutively with identical args"
        assert sig["basis"] == "args_sha abc repeated"

    def test_same_tool_different_args_no_signal(self):
        calls = [call("search", "a"), call("search", "b"), call("search", "c")]
        assert loop_fingerprint_signals(calls) == []

    def test_non_consecutive_repeats_no_signal(self):
        calls = [call("a", "1"), call("b", "2"), call("a", "1")]
        assert loop_fingerprint_signals(calls) == []

    def test_one_signal_per_streak(self):
        calls = [
            call("a", "1"),
            call("a", "1"),
            call("b", "2"),
            call("a", "1"),
            call("a", "1"),
            call("a", "1"),
        ]
        signals = loop_fingerprint_signals(calls)
        assert len(signals) == 2
        assert "2x" in signals[0]["detail"]
        assert "3x" in signals[1]["detail"]

    def test_malformed_input_no_signal(self):
        assert loop_fingerprint_signals(None) == []
        assert loop_fingerprint_signals("nope") == []
        assert loop_fingerprint_signals([{"name": 1}, "x", None]) == []


# ---------------------------------------------------------------------------
# retry_storm_signals
# ---------------------------------------------------------------------------


class TestRetryStorm:
    def test_storm_with_errors_warns(self):
        calls = [
            call("fetch", "z", "error"),
            call("fetch", "z", "error"),
            call("fetch", "z", "ok"),
        ]
        signals = retry_storm_signals(calls)
        assert len(signals) == 1
        sig = signals[0]
        assert sig["name"] == "retry_storm"
        assert sig["severity"] == "warn"
        assert "fetch" in sig["detail"] and "3x" in sig["detail"]
        assert "args_sha z" in sig["basis"]

    def test_below_threshold_no_signal(self):
        calls = [call("fetch", "z", "error"), call("fetch", "z", "error")]
        assert retry_storm_signals(calls) == []

    def test_custom_threshold(self):
        calls = [call("fetch", "z", "error"), call("fetch", "z", "ok")]
        assert retry_storm_signals(calls, threshold=2) != []

    def test_no_errors_no_signal(self):
        calls = [call("fetch", "z"), call("fetch", "z"), call("fetch", "z")]
        assert retry_storm_signals(calls) == []

    def test_non_consecutive_still_counts(self):
        calls = [
            call("fetch", "z", "error"),
            call("other", "q"),
            call("fetch", "z", "error"),
            call("other", "q2"),
            call("fetch", "z", "ok"),
        ]
        assert len(retry_storm_signals(calls)) == 1

    def test_malformed_input_no_signal(self):
        assert retry_storm_signals(None) == []
        assert retry_storm_signals([1, "x"]) == []


# ---------------------------------------------------------------------------
# duplicate_side_effect_signals
# ---------------------------------------------------------------------------


class TestDuplicateSideEffect:
    def test_duplicate_send_fails(self):
        calls = [call("send_email", "mail-1"), call("send_email", "mail-1")]
        signals = duplicate_side_effect_signals(calls)
        assert len(signals) == 1
        sig = signals[0]
        assert sig["name"] == "duplicate_side_effect"
        assert sig["severity"] == "fail"
        assert (
            sig["detail"]
            == "side-effecting tool 'send_email' executed 2x with identical args"
        )
        assert "mail-1" in sig["basis"]

    def test_read_only_tool_no_signal(self):
        calls = [call("search_web", "q1"), call("search_web", "q1")]
        assert duplicate_side_effect_signals(calls) == []

    def test_error_among_calls_no_signal(self):
        # A failed attempt then a successful retry is NOT a duplicate effect.
        calls = [call("send_email", "m", "error"), call("send_email", "m", "ok")]
        assert duplicate_side_effect_signals(calls) == []

    def test_unknown_status_no_signal(self):
        calls = [call("send_email", "m", None), call("send_email", "m", "ok")]
        assert duplicate_side_effect_signals(calls) == []

    def test_different_args_no_signal(self):
        calls = [call("send_email", "m1"), call("send_email", "m2")]
        assert duplicate_side_effect_signals(calls) == []

    def test_marker_matching_is_casefold(self):
        calls = [call("PostMessage", "p"), call("PostMessage", "p")]
        assert len(duplicate_side_effect_signals(calls)) == 1

    def test_custom_markers(self):
        calls = [call("launch_rocket", "r"), call("launch_rocket", "r")]
        assert duplicate_side_effect_signals(calls) == []
        assert (
            len(duplicate_side_effect_signals(calls, side_effect_markers=("launch",)))
            == 1
        )

    def test_malformed_input_no_signal(self):
        assert duplicate_side_effect_signals(None) == []
        assert duplicate_side_effect_signals(["x", 1]) == []


# ---------------------------------------------------------------------------
# tool_args_signals
# ---------------------------------------------------------------------------

_SCHEMA = {
    "tool_name": "send_email",
    "json_schema": {
        "type": "object",
        "required": ["to", "subject"],
        "properties": {"to": {"type": "string"}, "subject": {"type": "string"}},
    },
}


class TestToolArgs:
    def test_valid_args_no_signal(self):
        calls = [call("send_email", "s", args={"to": "a@b.cz", "subject": "hi"})]
        assert tool_args_signals(None, calls, [_SCHEMA]) == []

    def test_missing_required_field_fails(self):
        calls = [call("send_email", "s", args={"to": "a@b.cz"})]
        signals = tool_args_signals(None, calls, [_SCHEMA])
        assert len(signals) == 1
        sig = signals[0]
        assert sig["name"] == "tool_args_invalid"
        assert sig["severity"] == "fail"
        assert "send_email" in sig["detail"]

    def test_wrong_type_fails(self):
        calls = [call("send_email", "s", args={"to": 42, "subject": "hi"})]
        assert len(tool_args_signals(None, calls, [_SCHEMA])) == 1

    def test_args_as_json_string_validated(self):
        calls = [call("send_email", "s", args=json.dumps({"to": "a@b.cz"}))]
        assert len(tool_args_signals(None, calls, [_SCHEMA])) == 1

    def test_no_args_key_documented_limitation(self):
        # v1 digest carries only args_sha: nothing to validate, no signal.
        calls = [call("send_email", "s")]
        assert tool_args_signals(None, calls, [_SCHEMA]) == []

    def test_no_matching_schema_no_signal(self):
        calls = [call("other_tool", "s", args={})]
        assert tool_args_signals(None, calls, [_SCHEMA]) == []

    def test_dedupe_per_tool(self):
        calls = [
            call("send_email", "s1", args={}),
            call("send_email", "s2", args={}),
        ]
        assert len(tool_args_signals(None, calls, [_SCHEMA])) == 1

    def test_unparsable_args_string_no_signal(self):
        calls = [call("send_email", "s", args="{broken")]
        assert tool_args_signals(None, calls, [_SCHEMA]) == []

    def test_malformed_schemas_no_signal(self):
        calls = [call("send_email", "s", args={})]
        assert tool_args_signals(None, calls, [None, {}, {"tool_name": "x"}]) == []
        assert tool_args_signals(None, calls, "not a list") == []

    def test_malformed_calls_no_signal(self):
        assert tool_args_signals(None, None, [_SCHEMA]) == []
        assert tool_args_signals(None, ["x"], [_SCHEMA]) == []


# ---------------------------------------------------------------------------
# cost_zscore_signals
# ---------------------------------------------------------------------------


def _stat(**kw) -> AgentStat:
    defaults = dict(
        tokens_out_mean=100.0,
        tokens_out_std=10.0,
        iterations_mean=None,
        iterations_std=None,
        sample_count=10,
    )
    defaults.update(kw)
    return AgentStat(**defaults)


class TestCostZscore:
    def test_token_anomaly_at_three_sigma(self):
        signals = cost_zscore_signals(
            "writer", cost=None, tokens_out=200.0, stat=_stat()
        )
        assert len(signals) == 1
        sig = signals[0]
        assert sig["name"] == "token_anomaly"
        assert sig["severity"] == "warn"
        assert sig["detail"] == "tokens_out=200 is 10.0σ above the rolling mean 100"
        assert sig["basis"] == "baseline n=10, mean=100, std=10"

    def test_within_band_no_signal(self):
        signals = cost_zscore_signals(
            "writer", cost=None, tokens_out=120.0, stat=_stat()
        )
        assert signals == []

    def test_exactly_three_sigma_fires(self):
        signals = cost_zscore_signals(
            "writer", cost=None, tokens_out=130.0, stat=_stat()
        )
        assert len(signals) == 1

    def test_below_not_above_no_signal(self):
        # z is signed: an unusually CHEAP run is not an anomaly signal.
        signals = cost_zscore_signals("writer", cost=None, tokens_out=10.0, stat=_stat())
        assert signals == []

    def test_thin_baseline_no_signal(self):
        signals = cost_zscore_signals(
            "writer", cost=None, tokens_out=500.0, stat=_stat(sample_count=4)
        )
        assert signals == []

    def test_zero_std_no_signal(self):
        signals = cost_zscore_signals(
            "writer", cost=None, tokens_out=500.0, stat=_stat(tokens_out_std=0.0)
        )
        assert signals == []

    def test_missing_mean_no_signal(self):
        signals = cost_zscore_signals(
            "writer", cost=None, tokens_out=500.0, stat=_stat(tokens_out_mean=None)
        )
        assert signals == []

    def test_no_stat_no_signal(self):
        assert cost_zscore_signals("writer", cost=1.0, tokens_out=1.0, stat=None) == []

    def test_no_metric_values_no_signal(self):
        assert (
            cost_zscore_signals("writer", cost=None, tokens_out=None, stat=_stat())
            == []
        )

    def test_cost_anomaly_via_forward_compat_baseline(self):
        # AgentStat has no cost baseline yet; the check reads it via getattr
        # so it lights up as soon as the stats row grows the attributes.
        stat = SimpleNamespace(
            sample_count=20,
            cost_mean=1.0,
            cost_std=0.1,
            tokens_out_mean=None,
            tokens_out_std=None,
        )
        signals = cost_zscore_signals("writer", cost=2.0, tokens_out=None, stat=stat)
        assert len(signals) == 1
        sig = signals[0]
        assert sig["name"] == "cost_anomaly"
        assert "cost_usd=2" in sig["detail"]
        assert "10.0σ" in sig["detail"]

    def test_both_anomalies_together(self):
        stat = SimpleNamespace(
            sample_count=20,
            cost_mean=1.0,
            cost_std=0.1,
            tokens_out_mean=100.0,
            tokens_out_std=10.0,
        )
        signals = cost_zscore_signals("writer", cost=5.0, tokens_out=500.0, stat=stat)
        assert {sig["name"] for sig in signals} == {"cost_anomaly", "token_anomaly"}
