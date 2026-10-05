"""Tests for the process multiplexing module."""

import os
import time

import pytest

from eosh.process import OutputBuffer, ProcessSlot, _tail_lines_from_bytes


class TestOutputBuffer:
    def test_append_and_drain(self):
        buf = OutputBuffer()
        buf.append(b"hello")
        buf.append(b"world")
        chunks = buf.drain()
        assert chunks == [b"hello", b"world"]

    def test_drain_clears(self):
        buf = OutputBuffer()
        buf.append(b"data")
        buf.drain()
        assert buf.drain() == []

    def test_max_chunks(self):
        buf = OutputBuffer(max_chunks=3)
        for i in range(5):
            buf.append(f"chunk{i}".encode())
        chunks = buf.drain()
        assert len(chunks) == 3
        assert chunks == [b"chunk2", b"chunk3", b"chunk4"]

    def test_peek_does_not_drain(self):
        buf = OutputBuffer()
        buf.append(b"hello\n")
        buf.append(b"world\n")
        assert buf.peek() == b"hello\nworld\n"
        # peek must be non-destructive
        assert buf.peek() == b"hello\nworld\n"
        assert buf.drain() == [b"hello\n", b"world\n"]


class TestTailLines:
    def test_returns_last_n_lines(self):
        data = b"a\nb\nc\nd\ne\n"
        assert _tail_lines_from_bytes(data, 3) == ["c", "d", "e"]

    def test_skips_blank_lines(self):
        data = b"first\n\n\nsecond\n"
        assert _tail_lines_from_bytes(data, 5) == ["first", "second"]

    def test_strips_ansi(self):
        data = b"\x1b[31mred line\x1b[0m\nplain\n"
        assert _tail_lines_from_bytes(data, 2) == ["red line", "plain"]

    def test_normalizes_crlf(self):
        data = b"a\r\nb\r\nc\r\n"
        assert _tail_lines_from_bytes(data, 5) == ["a", "b", "c"]

    def test_empty_input(self):
        assert _tail_lines_from_bytes(b"", 3) == []
        assert _tail_lines_from_bytes(b"data\n", 0) == []


@pytest.mark.skipif(
    os.name == "nt",
    reason="ProcessSlot is PTY-backed (pty/fcntl/termios) — POSIX only",
)
class TestProcessSlot:
    def test_start_and_exit(self):
        slot = ProcessSlot()
        slot.start(argv=["echo", "hello"], env=dict(os.environ), cwd=os.getcwd())
        slot.activate()
        slot.wait(timeout=5)
        assert not slot.is_alive()
        assert slot.exit_code == 0

    def test_output_buffered_when_inactive(self):
        slot = ProcessSlot()
        slot.start(argv=["echo", "buffered-output"], env=dict(os.environ), cwd=os.getcwd())
        # Don't activate — output goes to buffer only
        slot.wait(timeout=5)
        chunks = slot.buffer.drain()
        combined = b"".join(chunks)
        assert b"buffered-output" in combined

    def test_write_stdin(self):
        slot = ProcessSlot()
        slot.start(
            argv=["cat"],
            env=dict(os.environ),
            cwd=os.getcwd(),
        )
        slot.activate()
        slot.write_stdin(b"test-input\n")
        # Send EOF to cat
        slot.write_stdin(b"\x04")
        slot.wait(timeout=5)
        assert slot.exit_code == 0
        chunks = slot.buffer.drain()
        combined = b"".join(chunks)
        assert b"test-input" in combined

    def test_kill(self):
        slot = ProcessSlot()
        slot.start(argv=["sleep", "60"], env=dict(os.environ), cwd=os.getcwd())
        assert slot.is_alive()
        slot.kill()
        slot.wait(timeout=5)
        assert not slot.is_alive()

    def test_exit_code_nonzero(self):
        slot = ProcessSlot()
        slot.start(argv=["false"], env=dict(os.environ), cwd=os.getcwd())
        slot.wait(timeout=5)
        assert slot.exit_code == 1
