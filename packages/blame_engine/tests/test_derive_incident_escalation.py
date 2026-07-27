"""Fixture-lock for derive_incident + derive_escalation (verdict refactor §2.3).

These moved from the worker (classify_incident / escalate_shipped_latent_defect)
into the engine as the single home of the mapping. The worker now delegates
here, so this table is the CONTRACT: it reproduces the pre-F2 worker behaviour
byte-for-byte. If a row here changes, the worker's incident behaviour changed —
that must be a deliberate, reviewed decision, never a silent drift.
"""

import pytest

from blame_engine import derive_escalation, derive_incident


# (report_type, flags, terminal_bad) -> (incident_key, trigger)
INCIDENT_TABLE = [
    # loop wins over everything
    (("loop_detected", [], False), ("loop_detected", "loop_detected")),
    (("cut_point", ["loop_anomaly"], False), ("loop_detected", "loop_detected")),
    # latent defect (escalated verdict) has its own high-severity trigger
    (("shipped_with_latent_defect", [], False), ("latent_defect", "latent_defect")),
    # every quality report -> degraded_quality (blame beats terminal)
    (("cut_point", [], True), ("degraded_quality", "degraded_quality")),
    (("multi_culprit", [], False), ("degraded_quality", "degraded_quality")),
    (("composition_failure", [], True), ("degraded_quality", "degraded_quality")),
    (("root_cause_external", [], False), ("degraded_quality", "degraded_quality")),
    (("verification_gap", [], True), ("degraded_quality", "degraded_quality")),
    (("degraded_recovered", [], False), ("degraded_quality", "degraded_quality")),
    (("terminal_defect_unlocalized", [], True), ("degraded_quality", "degraded_quality")),
    # deterministic flags on an unclassified report
    (("unclassified", ["failed_runs"], False), ("terminal_failure", "terminal_failure")),
    # form breach pages latent_defect when nothing else localised
    (("unclassified", ["terminal_form_breach"], False), ("latent_defect", "latent_defect")),
    (("unclassified", ["cost_overrun"], False), ("cost_overrun", "cost_overrun")),
    # bad terminal with no classification -> terminal_failure
    (("unclassified", [], True), ("terminal_failure", "terminal_failure")),
    # nothing to page
    (("unclassified", [], False), (None, None)),
    # precedence: failed_runs beats form/cost; form beats cost
    (("unclassified", ["failed_runs", "cost_overrun"], False), ("terminal_failure", "terminal_failure")),
    (("unclassified", ["terminal_form_breach", "cost_overrun"], False), ("latent_defect", "latent_defect")),
]


@pytest.mark.parametrize("args,expected", INCIDENT_TABLE)
def test_derive_incident_table(args, expected):
    assert derive_incident(*args) == expected


def _propagated(key="file_type", frm="docx", to="md", basis="path-ext"):
    return {"status": "propagated", "key": key, "from": frm, "to": to, "basis": basis}


def test_escalation_upgrades_degraded_recovered_on_propagated_breach():
    rt, notes = derive_escalation("degraded_recovered", [_propagated()])
    assert rt == "shipped_with_latent_defect"
    assert len(notes) == 1
    assert "docx" in notes[0] and "md" in notes[0]


def test_escalation_noop_without_propagated_breach():
    # corrected-downstream / unverified breaches do NOT escalate
    assert derive_escalation("degraded_recovered", [{"status": "corrected", "key": "x", "from": "a", "to": "b", "basis": "c"}]) == ("degraded_recovered", [])
    assert derive_escalation("degraded_recovered", []) == ("degraded_recovered", [])


def test_escalation_noop_when_not_degraded_recovered():
    # only degraded_recovered is escalatable; a cut_point stays a cut_point
    assert derive_escalation("cut_point", [_propagated()]) == ("cut_point", [])
