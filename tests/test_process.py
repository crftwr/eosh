"""Tests for the process multiplexing module."""

from eosh.process import OutputBuffer, _tail_lines_from_bytes


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
