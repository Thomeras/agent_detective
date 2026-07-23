"""CLI gate: python -m detective_ci record/check."""

import json
import subprocess
import sys
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
FIXTURE = EXAMPLES / "wedge_fixture.json"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "detective_ci", *args],
        capture_output=True,
        text=True,
    )


def test_cli_record_then_check(tmp_path: Path) -> None:
    golden = tmp_path / "golden.json"
    rec = _run("record", str(FIXTURE), str(golden))
    assert rec.returncode == 0, rec.stderr
    assert json.loads(golden.read_text(encoding="utf-8"))["report_type"] == (
        "degraded_recovered"
    )

    chk = _run("check", str(FIXTURE), str(golden))
    assert chk.returncode == 0, chk.stderr
    assert "ok:" in chk.stdout


def test_cli_check_fails_build_on_regression(tmp_path: Path) -> None:
    golden = tmp_path / "golden.json"
    _run("record", str(FIXTURE), str(golden))
    data = json.loads(golden.read_text(encoding="utf-8"))
    data["deterministic_signals"] = ["artifact_integrity_fail"]
    golden.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")

    chk = _run("check", str(FIXTURE), str(golden))
    assert chk.returncode == 1
    assert "deterministic_signals" in chk.stderr
