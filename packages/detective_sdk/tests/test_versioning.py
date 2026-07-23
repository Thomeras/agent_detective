"""Tests for git_version / content_hash / tool_schema_hash."""

import hashlib
import json
import subprocess

import pytest

from detective_sdk import content_hash, git_version, tool_schema_hash
from detective_sdk import versioning


@pytest.fixture(autouse=True)
def clear_cache():
    versioning._git_version_cache.clear()
    yield
    versioning._git_version_cache.clear()


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(tmp_path),
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@example.com",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.com",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null",
            },
        )

    git("init", "-q")
    (repo / "a.txt").write_text("one\n")
    git("add", "a.txt")
    git("commit", "-q", "-m", "init")
    return repo


class TestGitVersion:
    def test_clean_repo_short_sha(self, git_repo):
        version = git_version(str(git_repo))
        assert version is not None
        assert not version.endswith("-dirty")
        assert 7 <= len(version) <= 40
        assert all(c in "0123456789abcdef" for c in version)

    def test_dirty_repo_suffix(self, git_repo):
        (git_repo / "a.txt").write_text("changed\n")
        version = git_version(str(git_repo))
        assert version is not None
        assert version.endswith("-dirty")

    def test_untracked_file_is_dirty(self, git_repo):
        (git_repo / "new.txt").write_text("x\n")
        assert git_version(str(git_repo)).endswith("-dirty")

    def test_non_repo_returns_none(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert git_version(str(plain)) is None

    def test_missing_dir_returns_none(self, tmp_path):
        assert git_version(str(tmp_path / "nope")) is None

    def test_cached_per_repo_dir(self, git_repo, monkeypatch):
        first = git_version(str(git_repo))

        def boom(*args, **kwargs):  # pragma: no cover - must not be called
            raise AssertionError("subprocess should not run on a cache hit")

        monkeypatch.setattr(versioning.subprocess, "run", boom)
        assert git_version(str(git_repo)) == first

    def test_none_result_is_cached(self, tmp_path, monkeypatch):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert git_version(str(plain)) is None
        monkeypatch.setattr(
            versioning.subprocess,
            "run",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("cache miss")),
        )
        assert git_version(str(plain)) is None


class TestContentHash:
    def test_shape(self, tmp_path):
        path = tmp_path / "p.py"
        path.write_text("PROMPT = 'x'\n")
        digest = content_hash([str(path)])
        assert len(digest) == 12
        assert all(c in "0123456789abcdef" for c in digest)

    def test_matches_manual_sha256(self, tmp_path):
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_bytes(b"alpha")
        b.write_bytes(b"beta")
        expected = hashlib.sha256(b"alphabeta").hexdigest()[:12]
        assert content_hash([str(a), str(b)]) == expected

    def test_deterministic(self, tmp_path):
        path = tmp_path / "a.txt"
        path.write_bytes(b"same")
        assert content_hash([str(path)]) == content_hash([str(path)])

    def test_changes_with_content(self, tmp_path):
        path = tmp_path / "a.txt"
        path.write_bytes(b"one")
        before = content_hash([str(path)])
        path.write_bytes(b"two")
        assert content_hash([str(path)]) != before

    def test_order_matters(self, tmp_path):
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_bytes(b"alpha")
        b.write_bytes(b"beta")
        assert content_hash([str(a), str(b)]) != content_hash([str(b), str(a)])

    def test_missing_file_deterministic(self, tmp_path):
        missing = str(tmp_path / "gone.md")
        expected = hashlib.sha256(
            missing.encode("utf-8") + b"<missing>"
        ).hexdigest()[:12]
        assert content_hash([missing]) == expected
        assert content_hash([missing]) == content_hash([missing])

    def test_missing_differs_from_present(self, tmp_path):
        path = tmp_path / "p.md"
        missing = content_hash([str(path)])
        path.write_bytes(b"now present")
        assert content_hash([str(path)]) != missing

    def test_empty_list(self):
        assert content_hash([]) == hashlib.sha256(b"").hexdigest()[:12]


SCHEMA_A = {
    "name": "fetch_page",
    "parameters": {"type": "object", "properties": {"url": {"type": "string"}}},
}
SCHEMA_B = {
    "name": "send_email",
    "parameters": {"type": "object", "properties": {"to": {"type": "string"}}},
}


class TestToolSchemaHash:
    def test_shape(self):
        digest = tool_schema_hash([SCHEMA_A])
        assert len(digest) == 12
        assert all(c in "0123456789abcdef" for c in digest)

    def test_deterministic(self):
        assert tool_schema_hash([SCHEMA_A, SCHEMA_B]) == tool_schema_hash(
            [SCHEMA_A, SCHEMA_B]
        )

    def test_list_order_insensitive(self):
        assert tool_schema_hash([SCHEMA_A, SCHEMA_B]) == tool_schema_hash(
            [SCHEMA_B, SCHEMA_A]
        )

    def test_dict_key_order_insensitive(self):
        reordered = {
            "parameters": {"properties": {"url": {"type": "string"}}, "type": "object"},
            "name": "fetch_page",
        }
        assert tool_schema_hash([SCHEMA_A]) == tool_schema_hash([reordered])

    def test_changes_with_schema_content(self):
        changed = dict(SCHEMA_A, name="fetch_page_v2")
        assert tool_schema_hash([SCHEMA_A]) != tool_schema_hash([changed])

    def test_matches_manual_sha256(self):
        dumps = sorted(
            json.dumps(s, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            for s in [SCHEMA_A, SCHEMA_B]
        )
        expected = hashlib.sha256("".join(dumps).encode("utf-8")).hexdigest()[:12]
        assert tool_schema_hash([SCHEMA_A, SCHEMA_B]) == expected

    def test_empty_list(self):
        assert tool_schema_hash([]) == hashlib.sha256(b"").hexdigest()[:12]
