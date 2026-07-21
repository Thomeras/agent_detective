"""Synthetic 5-agent OpenInference pipeline for the Agent Detective demo.

See ``build_and_run`` in :mod:`synthetic_pipeline.pipeline` for the entry point
and the module docstring there for the emitted graph shape.
"""

from .pipeline import build_and_run, build_payload_only

__all__ = ["build_and_run", "build_payload_only"]
