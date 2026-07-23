"""pytest plugin (entry point ``pytest11``): the ``detective_golden`` fixture.

Exposes the golden-replay helpers to any test suite that has detective-ci
installed:

    def test_wedge(detective_golden):
        detective_golden.assert_matches_golden("fixture.json", "golden.json")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import pytest


@dataclass(frozen=True)
class DetectiveGolden:
    """Helper handed out by the ``detective_golden`` fixture."""

    assert_matches_golden: Callable
    record: Callable
    load_fixture: Callable
    stable_surface: Callable


@pytest.fixture
def detective_golden() -> DetectiveGolden:
    """Golden blame-surface replay helper (see detective_ci.golden)."""
    # Imported lazily: this module loads as a pytest11 entry-point plugin in
    # EVERY suite that has detective-ci installed, and a plugin-load import of
    # blame_engine would execute its import-time lines before pytest-cov starts
    # measuring — silently zeroing coverage of types.py/__init__.py in the
    # blame_engine suite's own coverage gate.
    from . import golden as _golden

    return DetectiveGolden(
        assert_matches_golden=_golden.assert_matches_golden,
        record=_golden.record,
        load_fixture=_golden.load_fixture,
        stable_surface=_golden.stable_surface,
    )
