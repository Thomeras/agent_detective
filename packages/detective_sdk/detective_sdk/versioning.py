"""Per-run identity helpers (pure stdlib).

Universal attribute contract (see docs/instrumentation.md):
- ``gen_ai.agent.version``       <- git_version(repo_dir)
- ``gen_ai.request.model``       <- the model identifier the run used
- ``agent_detective.prompt_hash`` <- content_hash(prompt_defining_paths)
- ``agent_detective.tool_schema_hash`` <- tool_schema_hash(tool_json_schemas)
"""

from __future__ import annotations

import hashlib
import json
import subprocess

_git_version_cache: dict[str, str | None] = {}


def git_version(repo_dir: str) -> str | None:
    """Short git sha of ``repo_dir``'s HEAD, with a ``-dirty`` suffix when the
    working tree has uncommitted changes. ``None`` when ``repo_dir`` is not a
    git repository or git is unavailable. Cached per repo_dir in-process."""
    if repo_dir in _git_version_cache:
        return _git_version_cache[repo_dir]
    version = _compute_git_version(repo_dir)
    _git_version_cache[repo_dir] = version
    return version


def _compute_git_version(repo_dir: str) -> str | None:
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if rev.returncode != 0:
        return None
    sha = rev.stdout.strip()
    if not sha:
        return None
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return sha
    if status.returncode == 0 and status.stdout.strip():
        return sha + "-dirty"
    return sha


def tool_schema_hash(schemas: list[dict]) -> str:
    """12 hex chars of sha256 over the canonical sorted JSON of ``schemas``.

    Canonicalization makes the hash independent of both dict key order and the
    order the schemas are listed in: each schema is dumped with sorted keys and
    compact separators, the dumps are sorted, and the concatenation is hashed.
    The same tool set therefore always produces the same 12-hex value — the
    ``agent_detective.tool_schema_hash`` attribute (TOOL_SCHEMA_HASH_ATTRIBUTE).
    """
    dumps = sorted(
        json.dumps(schema, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        for schema in schemas
    )
    hasher = hashlib.sha256()
    for dump in dumps:
        hasher.update(dump.encode("utf-8"))
    return hasher.hexdigest()[:12]


def content_hash(paths: list[str]) -> str:
    """12 hex chars of sha256 over the concatenated bytes of ``paths``.

    Missing files are folded in deterministically as ``<path> + b'<missing>'``
    so the hash is stable and still changes when a file disappears."""
    hasher = hashlib.sha256()
    for path in paths:
        try:
            with open(path, "rb") as fh:
                hasher.update(fh.read())
        except OSError:
            hasher.update(path.encode("utf-8") + b"<missing>")
    return hasher.hexdigest()[:12]
