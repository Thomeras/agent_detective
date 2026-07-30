"""``detective`` — the command line entry point.

    detective doctor  trace.json          # can this trace support a verdict?
    detective analyze trace.json          # the verdict, in the terminal
    detective analyze trace.json --json   # the same verdict as data
    detective analyze trace.json --markdown > findings.md

Exit codes are chosen so the command can gate a build without any wrapper
logic: **0** clean, **1** at least one incident, **2** the analysis could not
run (unreadable file, no agent spans). "Incident" is the pipeline's own
judgement, not a threshold invented here — the same condition that pages in the
deployed system.

``doctor`` is the exception and deliberately so: it is a diagnostic, never a
gate, and exits 0 whatever it finds (see :mod:`detective_cli.doctor`).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import __version__
from .analyze import AnalysisRun, analyze, local_settings
from .bundle import TraceFormatError, bundles_from_exports, load_trace
from .capture import Capture, serve
from .doctor import diagnose, render_doctor_json, render_doctor_terminal, unreadable_diagnosis
from .judge import select_judge
from .render import (
    color_enabled,
    render_json,
    render_markdown,
    render_terminal,
    unverified_graphs,
)

EXIT_CLEAN = 0
EXIT_INCIDENT = 1
EXIT_ERROR = 2


def _add_shared_options(parser: argparse.ArgumentParser) -> None:
    """Options that mean the same thing however the trace was obtained."""
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="emit the full verdict as JSON")
    output.add_argument(
        "--markdown",
        action="store_true",
        help="emit a Markdown findings brief (hand it to a coding agent)",
    )
    parser.add_argument(
        "--no-judge",
        action="store_true",
        help="skip the LLM judge even if one is configured (deterministic channel only)",
    )
    parser.add_argument(
        "--tier1-only",
        action="store_true",
        help="run the cheap detection pass only, without per-node scoring or blame",
    )
    parser.add_argument(
        "--a2a",
        action="store_true",
        help="enable agent-to-agent edge detection in the OTEL mapper",
    )
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="colourise terminal output (default: auto — off when piped or NO_COLOR)",
    )
    parser.add_argument(
        "--exit-zero",
        action="store_true",
        help="always exit 0, even when an incident is found",
    )
    parser.add_argument(
        "--fail-on-unverified",
        action="store_true",
        help=(
            "also exit 1 when a graph could not be measured at all "
            "(no scored node, no deterministic signal, no terminal verdict)"
        ),
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="show pipeline log output on stderr"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="detective",
        description=(
            "Blame analysis for multi-agent runs: read an OpenTelemetry trace "
            "and name where quality broke."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    analyze_cmd = sub.add_parser(
        "analyze",
        help="analyse an OTLP/HTTP JSON trace file",
        description=(
            "Runs the real tier1/tier2 pipeline in-process: no database, no "
            "broker, no object store. Exits 1 when an incident is found."
        ),
    )
    analyze_cmd.add_argument("trace", type=Path, help="OTLP/HTTP JSON trace file")
    _add_shared_options(analyze_cmd)

    capture_cmd = sub.add_parser(
        "capture",
        help="receive a trace over OTLP/HTTP, then analyse it",
        description=(
            "Serves POST /v1/traces so an already-instrumented agent can send "
            "its spans here instead of to a stack — point its existing OTLP "
            "exporter at this address, no code change. Runs your agent, then "
            "Ctrl-C (or --once) prints the verdict."
        ),
    )
    capture_cmd.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "interface to bind (default: 127.0.0.1 — loopback only; pass 0.0.0.0 "
            "deliberately to receive from another host or a container)"
        ),
    )
    capture_cmd.add_argument("--port", type=int, default=8900, help="port to bind (default: 8900)")
    capture_cmd.add_argument(
        "--out",
        type=Path,
        help="also save the received trace here, so it can be re-analysed later",
    )
    capture_cmd.add_argument(
        "--once",
        action="store_true",
        help="stop after the first export arrives (the usual shape: one payload on shutdown)",
    )
    capture_cmd.add_argument(
        "--no-analyze",
        action="store_true",
        help="only capture (requires --out); do not analyse what arrives",
    )
    _add_shared_options(capture_cmd)

    doctor_cmd = sub.add_parser(
        "doctor",
        help="check whether a trace can support an analysis at all",
        description=(
            "Pre-flight check on the instrumentation: reads the trace the way "
            "`analyze` would and reports what it can and cannot support, each "
            "finding with its consequence and a concrete fix. It produces no "
            "verdict and never gates — it always exits 0, whatever it finds."
        ),
    )
    doctor_cmd.add_argument("trace", type=Path, help="OTLP/HTTP JSON trace file")
    doctor_cmd.add_argument(
        "--json", action="store_true", help="emit the diagnosis as JSON"
    )
    doctor_cmd.add_argument(
        "--a2a",
        action="store_true",
        help="enable agent-to-agent edge detection in the OTEL mapper",
    )
    doctor_cmd.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="colourise terminal output (default: auto — off when piped or NO_COLOR)",
    )
    return parser


def _analyse_exports(
    exports: list[dict], source: str, args: argparse.Namespace
) -> AnalysisRun:
    bundles = bundles_from_exports(exports, a2a_detection=args.a2a)
    if not bundles:
        raise TraceFormatError(
            f"{source} contained no agent runs. Agent Detective reads AGENT "
            "spans (OpenInference / OpenLLMetry conventions); a trace with only "
            "LLM or tool spans has no graph to analyse."
        )
    return analyze(
        bundles,
        settings=local_settings(),
        no_judge=args.no_judge,
        tier1_only=args.tier1_only,
    )


def _report(run: AnalysisRun, source: str, args: argparse.Namespace) -> None:
    if args.json:
        print(json.dumps(render_json(run, source), indent=2, ensure_ascii=False, default=str))
    elif args.markdown:
        print(render_markdown(run, source))
    else:
        force = {"always": True, "never": False, "auto": None}[args.color]
        print(render_terminal(run, source, color=color_enabled(sys.stdout, force=force)))


def _gate(run: AnalysisRun, args: argparse.Namespace) -> int:
    if args.exit_zero:
        return EXIT_CLEAN
    if run.incidents:
        return EXIT_INCIDENT
    if args.fail_on_unverified and unverified_graphs(run):
        return EXIT_INCIDENT
    return EXIT_CLEAN


def _configure_logging(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.ERROR,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def cmd_analyze(args: argparse.Namespace) -> int:
    _configure_logging(args)
    try:
        run = _analyse_exports(load_trace(args.trace), str(args.trace), args)
    except TraceFormatError as exc:
        print(f"detective: {exc}", file=sys.stderr)
        return EXIT_ERROR
    _report(run, str(args.trace), args)
    return _gate(run, args)


def cmd_capture(args: argparse.Namespace) -> int:
    _configure_logging(args)
    if args.no_analyze and args.out is None:
        print(
            "detective: --no-analyze discards everything unless --out is given",
            file=sys.stderr,
        )
        return EXIT_ERROR

    capture = Capture()
    # The report goes to stdout, so everything conversational goes to stderr —
    # `detective capture --json > verdict.json` has to stay pipeable.
    def _ready(address: str) -> None:
        print(f"detective: listening for OTLP/HTTP JSON on {address}/v1/traces", file=sys.stderr)
        print(f"detective: point your agent at it, e.g.", file=sys.stderr)
        print(f"    OTEL_EXPORTER_OTLP_PROTOCOL=http/json \\", file=sys.stderr)
        print(f"    OTEL_EXPORTER_OTLP_ENDPOINT={address} <your agent>", file=sys.stderr)
        if not args.once:
            print("detective: Ctrl-C when the run is done", file=sys.stderr)

    def _exported(count: int) -> None:
        print(f"detective: received {count} span(s) ({capture.spans} total)", file=sys.stderr)

    try:
        serve(
            capture,
            host=args.host,
            port=args.port,
            once=args.once,
            verbose=args.verbose,
            on_export=_exported,
            on_ready=_ready,
        )
    except OSError as exc:
        print(f"detective: cannot listen on {args.host}:{args.port} — {exc}", file=sys.stderr)
        return EXIT_ERROR

    if capture.empty:
        # Nothing arrived. Silence here would look like a clean run.
        print("detective: no trace was received — nothing to analyse", file=sys.stderr)
        return EXIT_ERROR

    source = f"{args.host}:{args.port}"
    if args.out is not None:
        # One export stays an export object; several become the JSON array
        # `load_trace` accepts, so the saved file re-analyses identically.
        payload = capture.exports[0] if len(capture.exports) == 1 else capture.exports
        args.out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        print(f"detective: trace saved to {args.out}", file=sys.stderr)
        source = str(args.out)

    if args.no_analyze:
        return EXIT_CLEAN

    try:
        run = _analyse_exports(capture.exports, source, args)
    except TraceFormatError as exc:
        print(f"detective: {exc}", file=sys.stderr)
        return EXIT_ERROR
    _report(run, source, args)
    return _gate(run, args)


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report what the trace can support. Always 0 — a diagnostic, not a gate.

    The one non-zero case is a path that cannot be opened: "no such file" is a
    fact about the command line, not about instrumentation, and a script that
    typo'd the path must not read the silence as a clean bill of health. A file
    that opens but is not OTLP/HTTP JSON IS a diagnosis (protobuf exporter,
    truncated flush) and is reported as one.
    """
    if not args.trace.is_file():
        print(f"detective: cannot read {args.trace}", file=sys.stderr)
        return EXIT_ERROR
    try:
        exports = load_trace(args.trace)
    except TraceFormatError as exc:
        diagnosis = unreadable_diagnosis(str(args.trace), str(exc))
    except UnicodeDecodeError:
        # A protobuf export lands here, and it is the single most common "why is
        # my trace empty" — `load_trace` only guards OSError, so the bytes reach
        # the decoder. Diagnosed rather than raised: that is what doctor is for.
        diagnosis = unreadable_diagnosis(
            str(args.trace), f"{args.trace} is not UTF-8 text (binary, e.g. protobuf)"
        )
    else:
        settings = local_settings()
        judge = select_judge(settings)
        diagnosis = diagnose(
            exports,
            str(args.trace),
            a2a_detection=args.a2a,
            judge_available=judge.enabled,
            judge_detail=judge.description,
            settings=settings,
        )

    if args.json:
        print(json.dumps(render_doctor_json(diagnosis), indent=2, ensure_ascii=False))
    else:
        force = {"always": True, "never": False, "auto": None}[args.color]
        print(
            render_doctor_terminal(
                diagnosis, color=color_enabled(sys.stdout, force=force)
            )
        )
    return EXIT_CLEAN


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "analyze":
        return cmd_analyze(args)
    if args.command == "capture":
        return cmd_capture(args)
    if args.command == "doctor":
        return cmd_doctor(args)
    return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
