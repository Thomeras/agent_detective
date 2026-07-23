"""CLI: record or check a golden blame surface.

    python -m detective_ci record fixture.json golden.json
    python -m detective_ci check  fixture.json golden.json
"""

from __future__ import annotations

import argparse
import json
import sys

from .golden import assert_matches_golden, record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="detective_ci",
        description="Deterministic blame-level golden replay (stable surface only).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("record", "replay the fixture and (over)write the golden surface"),
        ("check", "replay the fixture and fail (exit 1) on surface regression"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("fixture", help="fixture JSON path")
        p.add_argument("golden", help="golden surface JSON path")
    args = parser.parse_args(argv)

    if args.command == "record":
        surface = record(args.fixture, args.golden)
        print(f"recorded {args.golden}:")
        print(json.dumps(surface, sort_keys=True, indent=2, ensure_ascii=False))
        return 0
    try:
        assert_matches_golden(args.fixture, args.golden)
    except AssertionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"ok: {args.fixture} matches {args.golden}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
