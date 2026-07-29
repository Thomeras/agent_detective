"""Agent Detective, local mode: blame analysis for a trace file, no stack.

``pip install agent-detective`` then::

    detective analyze trace.json

runs the SAME tier1/tier2 pipeline the deployed worker runs — the processors
are imported, not reimplemented — against in-memory implementations of the
three seams they talk to (see ``detective_cli.analyze``). What you get is the
platform's verdict on one trace: did the run pass, and if not, at which node
quality broke, what kind of fault it was, and how confident that attribution
is.

Nothing is required to run it: no Postgres, no Redis, no object store, and by
default no LLM. Without a judge the deterministic evidence channel still runs
in full, and the report says the judged channel was off rather than presenting
half the evidence as the whole picture.

Before trusting any of that, ``detective doctor trace.json`` says whether the
trace can support a verdict at all — what instrumentation did not capture at
run time, no later analysis can manufacture (see :mod:`detective_cli.doctor`).

The library entry points, for use from Python:

- :func:`detective_cli.bundle.bundles_from_exports` — OTLP JSON to graphs
- :func:`detective_cli.analyze.analyze` — run the pipeline over those graphs
- :func:`detective_cli.doctor.diagnose` — what a trace can and cannot support
- :mod:`detective_cli.render` — terminal / Markdown / JSON output
"""

from .analyze import AnalysisRun, GraphAnalysis, analyze, analyze_async, local_settings
from .bundle import TraceFormatError, bundles_from_exports, bundles_from_mapping, load_trace
from .doctor import Check, Claim, Diagnosis, diagnose

__version__ = "0.3.0"

__all__ = [
    "AnalysisRun",
    "Check",
    "Claim",
    "Diagnosis",
    "GraphAnalysis",
    "TraceFormatError",
    "analyze",
    "analyze_async",
    "bundles_from_exports",
    "bundles_from_mapping",
    "diagnose",
    "load_trace",
    "local_settings",
    "__version__",
]
