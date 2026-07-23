"""Tests for detect_kind / artifact_meta / artifact_meta_block."""

import hashlib
import json
import zipfile

import pytest

from detective_sdk import artifact_meta, artifact_meta_block, detect_kind

META_KEYS = {"size", "sha256", "declared_ext", "detected_kind", "parse_ok", "nonempty"}


def make_docx(path, main_part="word/document.xml"):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr(main_part, "<w:document/>")
    return path


# --- detect_kind ----------------------------------------------------------


class TestDetectKind:
    def test_empty(self):
        assert detect_kind(b"") == "empty"

    def test_zip_magic(self):
        assert detect_kind(b"PK\x03\x04\x14\x00\x00") == "zip"

    def test_pdf_magic(self):
        assert detect_kind(b"%PDF-1.7\n%\xe2\xe3") == "pdf"

    def test_utf8_text(self):
        assert detect_kind("# Zpráva\n\nVýsledky měření.\n".encode("utf-8")) == "text"

    def test_ascii_text(self):
        assert detect_kind(b"hello world\nline two\n") == "text"

    def test_invalid_utf8_is_binary(self):
        assert detect_kind(b"\xff\xfe\x00\x01garbage") == "binary"

    def test_low_printable_ratio_is_binary(self):
        # Decodable as utf-8 (latin range control chars) but mostly unprintable.
        assert detect_kind(b"ok" + b"\x00\x01\x02\x03\x04\x05\x06\x07" * 4) == "binary"

    def test_truncated_multibyte_tail_still_text(self):
        # A 4096-byte head can cut a multibyte char in half; that must not
        # flip the classification to binary.
        sample = ("é" * 100).encode("utf-8")[:-1]
        assert detect_kind(sample) == "text"

    def test_zip_wins_over_text(self):
        # PK magic followed by printable bytes is still a zip container.
        assert detect_kind(b"PK\x03\x04 lots of printable text here") == "zip"


# --- artifact_meta --------------------------------------------------------


class TestArtifactMeta:
    def test_valid_docx(self, tmp_path):
        path = make_docx(tmp_path / "report.docx")
        meta = artifact_meta(str(path))
        assert set(meta) == META_KEYS
        assert meta["declared_ext"] == "docx"
        assert meta["detected_kind"] == "zip"
        assert meta["parse_ok"] is True
        assert meta["nonempty"] is True
        assert meta["size"] == path.stat().st_size
        assert meta["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()

    def test_docx_zip_without_main_part(self, tmp_path):
        path = make_docx(tmp_path / "report.docx", main_part="other/part.xml")
        meta = artifact_meta(str(path))
        assert meta["detected_kind"] == "zip"
        assert meta["parse_ok"] is False

    @pytest.mark.parametrize(
        ("ext", "main_part"),
        [("xlsx", "xl/workbook.xml"), ("pptx", "ppt/presentation.xml")],
    )
    def test_ooxml_family(self, tmp_path, ext, main_part):
        good = make_docx(tmp_path / f"good.{ext}", main_part=main_part)
        bad = make_docx(tmp_path / f"bad.{ext}", main_part="word/document.xml")
        assert artifact_meta(str(good))["parse_ok"] is True
        assert artifact_meta(str(bad))["parse_ok"] is False

    def test_declared_docx_but_plain_text(self, tmp_path):
        path = tmp_path / "fake.docx"
        path.write_text("just plain text, not a zip", encoding="utf-8")
        meta = artifact_meta(str(path))
        assert meta["declared_ext"] == "docx"
        assert meta["detected_kind"] == "text"
        assert meta["parse_ok"] is False

    def test_valid_pdf(self, tmp_path):
        path = tmp_path / "doc.pdf"
        path.write_bytes(b"%PDF-1.4\n1 0 obj\nendobj\ntrailer\n%%EOF\n")
        meta = artifact_meta(str(path))
        assert meta["detected_kind"] == "pdf"
        assert meta["parse_ok"] is True

    def test_pdf_missing_eof(self, tmp_path):
        path = tmp_path / "doc.pdf"
        path.write_bytes(b"%PDF-1.4\n1 0 obj\nendobj\n")
        assert artifact_meta(str(path))["parse_ok"] is False

    def test_pdf_eof_must_be_in_last_kb(self, tmp_path):
        path = tmp_path / "doc.pdf"
        path.write_bytes(b"%PDF-1.4\n%%EOF\n" + b"x" * 2048)
        assert artifact_meta(str(path))["parse_ok"] is False

    @pytest.mark.parametrize("ext", ["md", "txt", "html"])
    def test_utf8_text_formats(self, tmp_path, ext):
        path = tmp_path / f"note.{ext}"
        path.write_text("Zpráva o výsledcích\n", encoding="utf-8")
        meta = artifact_meta(str(path))
        assert meta["detected_kind"] == "text"
        assert meta["parse_ok"] is True

    def test_md_invalid_utf8(self, tmp_path):
        path = tmp_path / "note.md"
        path.write_bytes(b"\xff\xfe not utf-8")
        assert artifact_meta(str(path))["parse_ok"] is False

    def test_json_valid(self, tmp_path):
        path = tmp_path / "data.json"
        path.write_text('{"ok": true}', encoding="utf-8")
        assert artifact_meta(str(path))["parse_ok"] is True

    def test_json_invalid_syntax(self, tmp_path):
        path = tmp_path / "data.json"
        path.write_text("{not json", encoding="utf-8")
        meta = artifact_meta(str(path))
        assert meta["detected_kind"] == "text"
        assert meta["parse_ok"] is False

    def test_unknown_ext_nonempty(self, tmp_path):
        path = tmp_path / "blob.bin"
        path.write_bytes(b"\x00\x01\x02\x03")
        meta = artifact_meta(str(path))
        assert meta["detected_kind"] == "binary"
        assert meta["parse_ok"] is True
        assert meta["nonempty"] is True

    def test_unknown_ext_empty(self, tmp_path):
        path = tmp_path / "blob.dat"
        path.write_bytes(b"")
        meta = artifact_meta(str(path))
        assert meta["detected_kind"] == "empty"
        assert meta["parse_ok"] is False
        assert meta["nonempty"] is False
        assert meta["size"] == 0

    def test_no_extension(self, tmp_path):
        path = tmp_path / "README"
        path.write_text("plain", encoding="utf-8")
        meta = artifact_meta(str(path))
        assert meta["declared_ext"] == ""
        assert meta["parse_ok"] is True

    def test_extension_is_lowercased(self, tmp_path):
        path = tmp_path / "NOTE.MD"
        path.write_text("hello", encoding="utf-8")
        assert artifact_meta(str(path))["declared_ext"] == "md"

    def test_missing_file(self, tmp_path):
        meta = artifact_meta(str(tmp_path / "gone.docx"))
        assert meta == {
            "size": 0,
            "sha256": None,
            "declared_ext": "docx",
            "detected_kind": "missing",
            "parse_ok": False,
            "nonempty": False,
        }

    def test_large_text_file_beyond_head_sample(self, tmp_path):
        # Multibyte chars straddling the 4096-byte head boundary must not
        # break detection.
        path = tmp_path / "big.md"
        path.write_text("ě" * 5000, encoding="utf-8")
        meta = artifact_meta(str(path))
        assert meta["detected_kind"] == "text"
        assert meta["parse_ok"] is True


# --- artifact_meta_block --------------------------------------------------


class TestArtifactMetaBlock:
    def test_shape_and_roundtrip(self, tmp_path):
        path = tmp_path / "note.md"
        path.write_text("hello\n", encoding="utf-8")
        block = artifact_meta_block(str(path))
        prefix = "\n\n[artifact_meta " + str(path) + "]:\n"
        assert block.startswith(prefix)
        payload = json.loads(block[len(prefix):])
        assert payload == artifact_meta(str(path))

    def test_marker_does_not_contain_artifact_text(self, tmp_path):
        path = tmp_path / "note.md"
        path.write_text("hello\n", encoding="utf-8")
        assert "artifact_text" not in artifact_meta_block(str(path))

    def test_missing_file_block(self, tmp_path):
        block = artifact_meta_block(str(tmp_path / "gone.pdf"))
        assert "[artifact_meta " in block
        payload = json.loads(block.split("]:\n", 1)[1])
        assert payload["detected_kind"] == "missing"
        assert payload["sha256"] is None
