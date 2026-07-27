"""The command line: exit codes, output modes, and failure messages.

Exit codes are the contract a CI job depends on, so they are tested as
behaviour rather than left implicit.
"""

from __future__ import annotations

import json

import pytest

from detective_cli.cli import EXIT_CLEAN, EXIT_ERROR, EXIT_INCIDENT, build_parser, main

from conftest import export, linear_pipeline, span


def run_cli(argv, capsys):
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


class TestExitCodes:
    def test_a_clean_trace_exits_zero(self, trace_file, capsys):
        path = trace_file(linear_pipeline())
        code, _, _ = run_cli(["analyze", str(path), "--no-judge"], capsys)
        assert code == EXIT_CLEAN

    def test_an_unreadable_trace_exits_two(self, tmp_path, capsys):
        code, _, err = run_cli(
            ["analyze", str(tmp_path / "missing.json"), "--no-judge"], capsys
        )
        assert code == EXIT_ERROR
        assert "cannot read" in err

    def test_a_trace_without_agent_spans_exits_two_with_an_explanation(
        self, trace_file, capsys
    ):
        # Silence would be the wrong answer here: "no graph" is not "no defect".
        llm_only = span(name="chat", span_id="0" * 16, agent_name=None)
        llm_only["attributes"][0]["value"]["stringValue"] = "LLM"
        path = trace_file(export([llm_only]))
        code, _, err = run_cli(["analyze", str(path), "--no-judge"], capsys)
        assert code == EXIT_ERROR
        assert "no agent runs" in err
        assert "AGENT spans" in err

    def test_fail_on_unverified_turns_an_unmeasured_run_into_a_failure(
        self, trace_file, capsys
    ):
        path = trace_file(linear_pipeline())
        code, _, _ = run_cli(
            ["analyze", str(path), "--no-judge", "--fail-on-unverified"], capsys
        )
        assert code == EXIT_INCIDENT

    def test_unmeasured_runs_pass_by_default(self, trace_file, capsys):
        # An absent judge is not itself an incident; the operator opts in to
        # treating it as one.
        path = trace_file(linear_pipeline())
        code, out, _ = run_cli(["analyze", str(path), "--no-judge"], capsys)
        assert code == EXIT_CLEAN
        assert "NOT VERIFIED" in out

    def test_exit_zero_suppresses_the_gate(self, trace_file, capsys):
        path = trace_file(linear_pipeline())
        code, _, _ = run_cli(
            ["analyze", str(path), "--no-judge", "--fail-on-unverified", "--exit-zero"],
            capsys,
        )
        assert code == EXIT_CLEAN


class TestOutputModes:
    def test_json_mode_emits_a_parsable_document(self, trace_file, capsys):
        path = trace_file(linear_pipeline())
        _, out, _ = run_cli(["analyze", str(path), "--no-judge", "--json"], capsys)
        payload = json.loads(out)
        assert payload["source"] == str(path)
        assert len(payload["graphs"]) == 1

    def test_markdown_mode_emits_a_brief(self, trace_file, capsys):
        path = trace_file(linear_pipeline())
        _, out, _ = run_cli(["analyze", str(path), "--no-judge", "--markdown"], capsys)
        assert out.startswith("# Agent Detective")

    def test_terminal_mode_is_the_default(self, trace_file, capsys):
        path = trace_file(linear_pipeline())
        _, out, _ = run_cli(["analyze", str(path), "--no-judge"], capsys)
        assert out.startswith("Agent Detective — ")

    def test_color_never_strips_escapes(self, trace_file, capsys):
        path = trace_file(linear_pipeline())
        _, out, _ = run_cli(
            ["analyze", str(path), "--no-judge", "--color", "never"], capsys
        )
        assert "\033[" not in out

    def test_color_always_emits_escapes(self, trace_file, capsys):
        path = trace_file(linear_pipeline())
        _, out, _ = run_cli(
            ["analyze", str(path), "--no-judge", "--color", "always"], capsys
        )
        assert "\033[" in out

    def test_json_and_markdown_are_mutually_exclusive(self, trace_file):
        path = trace_file(linear_pipeline())
        with pytest.raises(SystemExit):
            build_parser().parse_args(
                ["analyze", str(path), "--json", "--markdown"]
            )


class TestParser:
    def test_a_command_is_required(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_version_is_reported(self, capsys):
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(["--version"])
        assert exc.value.code == 0
        assert "detective" in capsys.readouterr().out

    def test_tier1_only_is_accepted(self, trace_file, capsys):
        path = trace_file(linear_pipeline())
        code, out, _ = run_cli(["analyze", str(path), "--no-judge", "--tier1-only"], capsys)
        assert code == EXIT_CLEAN
        assert "no per-node scoring ran" in out
