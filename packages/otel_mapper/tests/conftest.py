"""Shared fixtures: loaders for the OTLP JSON payloads in testdata/."""

import json
from pathlib import Path
from typing import Any, Callable

import pytest

TESTDATA_DIR = Path(__file__).resolve().parent.parent / "testdata"


@pytest.fixture()
def fixture_json() -> Callable[[str], Any]:
    def _load(name: str) -> Any:
        return json.loads((TESTDATA_DIR / name).read_text(encoding="utf-8"))

    return _load
