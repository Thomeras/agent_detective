"""detective-ci: deterministic blame-level golden replay + CI gate (roadmap 2.5).

Replays a blame fixture through the pure ``blame_engine`` and gates CI on the
STABLE surface of the resulting report — ``{report_type, culprit agent names,
deterministic signal names, tier1-style flags}`` — never confidences (LLM judge
scores are not reproducible; deterministic outputs are, which is why they
anchor the snapshot). Fixture JSON schema and surface definition:
``detective_ci.golden`` module docstring.

Usage:
    python -m detective_ci record fixture.json golden.json   # (re)record
    python -m detective_ci check  fixture.json golden.json   # CI gate, exit 1
    # or in pytest, via the shipped plugin fixture:
    def test_wedge(detective_golden):
        detective_golden.assert_matches_golden("f.json", "g.json")
"""

__version__ = "0.1.0"

__all__ = [
    "load_fixture",
    "stable_surface",
    "record",
    "assert_matches_golden",
]


def __getattr__(name: str):
    # Lazy re-exports (PEP 562): this package is imported at pytest plugin-load
    # time (pytest11 entry point) in every suite that installs detective-ci,
    # and an eager `from .golden import ...` would drag blame_engine in before
    # pytest-cov starts measuring — zeroing import-time coverage of
    # types.py/__init__.py in the blame_engine suite's own coverage gate.
    if name in __all__:
        from . import golden

        return getattr(golden, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
