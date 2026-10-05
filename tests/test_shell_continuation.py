"""Unit tests for backslash line-continuation helpers in shell.py."""

import pytest

from eosh.shell import _is_continuation, _strip_continuation


class TestIsContinuation:
    def test_plain_backslash_at_end(self):
        assert _is_continuation("docker run --rm \\") is True

    def test_no_backslash(self):
        assert _is_continuation("docker run --rm") is False

    def test_double_backslash_not_continuation(self):
        # Two backslashes = escaped backslash, not continuation.
        assert _is_continuation("echo \\\\") is False

    def test_triple_backslash_is_continuation(self):
        # Three = one escaped backslash + one continuation backslash.
        assert _is_continuation("echo \\\\\\") is True

    def test_trailing_space_after_backslash_ignored(self):
        # Accidental space after the \ should still be recognised as continuation.
        assert _is_continuation("docker run \\   ") is True

    def test_empty_string(self):
        assert _is_continuation("") is False

    def test_only_backslash(self):
        assert _is_continuation("\\") is True

    def test_continuation_within_word_not_at_end(self):
        # \ not at the end — not a line continuation.
        assert _is_continuation("echo \\n foo") is False


class TestStripContinuation:
    def test_removes_trailing_backslash(self):
        assert _strip_continuation("docker run --rm \\") == "docker run --rm "

    def test_removes_trailing_whitespace_and_backslash(self):
        # Trailing spaces before \ are also stripped.
        assert _strip_continuation("foo  \\   ") == "foo  "

    def test_only_backslash(self):
        assert _strip_continuation("\\") == ""

    def test_preserves_leading_whitespace(self):
        # Leading indentation of the *next* line is not affected (this function
        # only touches the current line).
        result = _strip_continuation("  docker \\")
        assert result == "  docker "


class TestContinuationJoining:
    """Integration-style checks that _strip_continuation + next line join correctly."""

    def test_single_continuation(self):
        line1 = "docker run --rm \\"
        line2 = "  -v /foo:/bar"
        joined = _strip_continuation(line1) + line2
        assert joined == "docker run --rm   -v /foo:/bar"

    def test_multi_continuation(self):
        lines = [
            "docker run --rm \\",
            "  -v /foo:/bar \\",
            "  my-image",
        ]
        acc = lines[0]
        for line in lines[1:]:
            acc = _strip_continuation(acc) + line
        assert acc == "docker run --rm   -v /foo:/bar   my-image"
