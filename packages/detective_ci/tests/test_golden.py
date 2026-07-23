"""detective-ci core behavior: stability, readable diffs, round-trip, honesty."""

import json
from pathlib import Path

import pytest

from blame_engine import find_blame
from detective_ci import (
    assert_matches_golden,
    load_fixture,
    record,
    stable_surface,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
FIXTURE = EXAMPLES / "wedge_fixture.json"
GOLDEN = EXAMPLES / "wedge_golden.json"


def test_surface_stability_same_fixture_twice() -> None:
    """Deterministic by construction: two replays -> byte-identical surfaces."""
    a = stable_surface(find_blame(load_fixture(FIXTURE)),
                       agent_names=load_fixture(FIXTURE).agent_names)
    b = stable_surface(find_blame(load_fixture(FIXTURE)),
                       agent_names=load_fixture(FIXTURE).agent_names)
    assert a == b
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_wedge_surface_is_the_flagship_silent_rewrite() -> None:
    """The shipped example reproduces the wedge: silent docx->md rewrite,
    healthy successors, ok terminal -> degraded_recovered pinned on think."""
    inp = load_fixture(FIXTURE)
    surface = stable_surface(find_blame(inp), agent_names=inp.agent_names)
    assert surface == {
        "report_type": "degraded_recovered",
        "culprit_agents": ["think"],
        "deterministic_signals": ["contract_violation"],
        "flags": ["missing_required_content"],
    }


def test_mismatch_raises_with_readable_unified_diff(tmp_path: Path) -> None:
    """A regressed surface fails with a diff that NAMES the changed field."""
    tampered = json.loads(GOLDEN.read_text(encoding="utf-8"))
    tampered["report_type"] = "cut_point"
    bad_golden = tmp_path / "tampered_golden.json"
    bad_golden.write_text(json.dumps(tampered, sort_keys=True, indent=2),
                          encoding="utf-8")

    with pytest.raises(AssertionError) as exc:
        assert_matches_golden(FIXTURE, bad_golden)
    msg = str(exc.value)
    assert "report_type" in msg
    assert '-  "report_type": "cut_point"' in msg
    assert '+  "report_type": "degraded_recovered"' in msg
    assert "---" in msg and "+++" in msg  # unified diff headers


def test_record_round_trip(tmp_path: Path) -> None:
    """record() writes sorted-keys JSON that assert_matches_golden accepts."""
    golden = tmp_path / "golden.json"
    surface = record(FIXTURE, golden)

    text = golden.read_text(encoding="utf-8")
    assert json.loads(text) == surface
    # sorted-keys, trailing newline
    assert text == json.dumps(surface, sort_keys=True, indent=2,
                              ensure_ascii=False) + "\n"
    assert list(json.loads(text)) == sorted(json.loads(text))
    assert_matches_golden(FIXTURE, golden)  # no raise


def test_confidences_deliberately_absent_from_surface() -> None:
    """LLM judge scores are not reproducible; deterministic outputs are — so
    NO confidence number may appear anywhere in the surface or the golden."""
    inp = load_fixture(FIXTURE)
    report = find_blame(inp)
    assert report.confidence is not None  # the report HAS one...
    surface = stable_surface(report, agent_names=inp.agent_names)
    assert "confidence" not in json.dumps(surface).lower()
    assert set(surface) <= {"report_type", "culprit_agents",
                            "deterministic_signals", "flags"}
    assert not any(isinstance(v, float) for v in surface.values())
    assert "confidence" not in GOLDEN.read_text(encoding="utf-8").lower()


def test_flags_key_absent_when_fixture_has_no_flags(tmp_path: Path) -> None:
    """'tier1-style flags if present in the fixture' — no flags, no key."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for score in data["scores"].values():
        score.pop("flags", None)
    fixture = tmp_path / "no_flags.json"
    fixture.write_text(json.dumps(data), encoding="utf-8")
    inp = load_fixture(fixture)
    surface = stable_surface(find_blame(inp), agent_names=inp.agent_names)
    assert "flags" not in surface


def test_load_fixture_rejects_unknown_keys(tmp_path: Path) -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["confidences"] = {"think": 0.9}
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown top-level keys.*confidences"):
        load_fixture(bad)
