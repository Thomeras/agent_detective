"""The shipped example, replayed via the pytest plugin's fixture — this is the
exact usage a downstream repo gets after `pip install detective-ci`."""

from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def test_wedge_example_via_plugin_fixture(detective_golden) -> None:
    detective_golden.assert_matches_golden(
        EXAMPLES / "wedge_fixture.json", EXAMPLES / "wedge_golden.json"
    )


def test_plugin_fixture_exposes_full_helper_surface(detective_golden) -> None:
    inp = detective_golden.load_fixture(EXAMPLES / "wedge_fixture.json")
    assert inp.agent_names["think"] == "think"
    assert callable(detective_golden.record)
    assert callable(detective_golden.stable_surface)


def test_plugin_fixture_diff_mentions_changed_field(
    detective_golden, tmp_path: Path
) -> None:
    golden = tmp_path / "golden.json"
    detective_golden.record(EXAMPLES / "wedge_fixture.json", golden)
    golden.write_text(
        golden.read_text(encoding="utf-8").replace("think", "render"),
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="culprit_agents"):
        detective_golden.assert_matches_golden(
            EXAMPLES / "wedge_fixture.json", golden
        )
