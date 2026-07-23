"""Artifact integrity helpers (pure stdlib).

The instrumentation side runs on the host that produced the artifacts, so it is
the only place a file can actually be opened. These helpers compute the
deterministic integrity payload that ships beside ``artifact_text``:

    [artifact_meta <path>]: {"size": ..., "sha256": ..., ...}

The worker never opens files; it only parses this block.
"""

from __future__ import annotations

import hashlib
import json
import os
import zipfile

# Extensions of the OOXML (zip container) family -> the main part whose
# presence makes the container a valid document of that format.
_OOXML_MAIN_PART = {
    "docx": "word/document.xml",
    "xlsx": "xl/workbook.xml",
    "pptx": "ppt/presentation.xml",
}

_TEXT_EXTS = frozenset({"md", "txt", "html", "json"})

_PRINTABLE_THRESHOLD = 0.85


def detect_kind(head: bytes) -> str:
    """Classify a leading byte sample: 'zip' | 'pdf' | 'empty' | 'text' | 'binary'."""
    if head == b"":
        return "empty"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if head.startswith(b"%PDF"):
        return "pdf"
    text = _decode_sample(head)
    if text is not None and _printable_ratio(text) >= _PRINTABLE_THRESHOLD:
        return "text"
    return "binary"


def _decode_sample(head: bytes) -> str | None:
    """UTF-8 decode a sample, tolerating a multibyte char truncated at the end."""
    try:
        return head.decode("utf-8")
    except UnicodeDecodeError as exc:
        # A sample cut mid-character fails within the last 3 bytes; that is a
        # sampling artifact, not binary content.
        if exc.start >= len(head) - 3:
            try:
                return head[: exc.start].decode("utf-8")
            except UnicodeDecodeError:
                return None
        return None


def _printable_ratio(text: str) -> float:
    if not text:
        return 1.0
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\t\n\r")
    return printable / len(text)


def artifact_meta(path: str, head_tail_bytes: int = 4096) -> dict:
    """Deterministic integrity metadata for one artifact file.

    Returns ``{size, sha256, declared_ext, detected_kind, parse_ok, nonempty}``.
    A missing/unreadable file yields ``detected_kind='missing'``, ``sha256=None``.
    """
    declared_ext = os.path.splitext(path)[1].lstrip(".").lower()
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return {
            "size": 0,
            "sha256": None,
            "declared_ext": declared_ext,
            "detected_kind": "missing",
            "parse_ok": False,
            "nonempty": False,
        }

    size = len(data)
    return {
        "size": size,
        "sha256": hashlib.sha256(data).hexdigest(),
        "declared_ext": declared_ext,
        "detected_kind": detect_kind(data[:head_tail_bytes]),
        "parse_ok": _parse_ok(path, declared_ext, data, head_tail_bytes),
        "nonempty": size > 0,
    }


def _parse_ok(path: str, ext: str, data: bytes, head_tail_bytes: int) -> bool:
    if ext in _OOXML_MAIN_PART:
        try:
            with zipfile.ZipFile(path) as zf:
                return _OOXML_MAIN_PART[ext] in zf.namelist()
        except (zipfile.BadZipFile, OSError):
            return False
    if ext == "pdf":
        return data.startswith(b"%PDF") and b"%%EOF" in data[-1024:]
    if ext in _TEXT_EXTS:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return False
        if ext == "json":
            try:
                json.loads(text)
            except json.JSONDecodeError:
                return False
        return True
    # Unknown/other extension: the file opened; require it to be non-empty.
    return len(data) > 0


def artifact_meta_block(path: str) -> str:
    """The block appended beside ``artifact_text`` in span payloads.

    The marker is exactly ``[artifact_meta <path>]:`` followed by compact JSON.
    It deliberately does not contain the substring ``artifact_text``.
    """
    meta = artifact_meta(path)
    return (
        "\n\n[artifact_meta "
        + path
        + "]:\n"
        + json.dumps(meta, separators=(",", ":"), sort_keys=True)
    )
