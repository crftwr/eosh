"""Tests for the TTL-based completion cache."""

from __future__ import annotations

from unittest.mock import patch

from eosh import completion_cache


def test_get_or_fetch_caches_value():
    calls = []

    def fetch():
        calls.append(1)
        return "value"

    assert completion_cache.get_or_fetch(("k",), fetch, ttl=10) == "value"
    assert completion_cache.get_or_fetch(("k",), fetch, ttl=10) == "value"
    assert calls == [1]


def test_get_or_fetch_distinct_keys_are_independent():
    calls = []
    completion_cache.get_or_fetch(("a",), lambda: calls.append("a") or "A", ttl=10)
    completion_cache.get_or_fetch(("b",), lambda: calls.append("b") or "B", ttl=10)
    completion_cache.get_or_fetch(("a",), lambda: calls.append("a2") or "A2", ttl=10)
    assert calls == ["a", "b"]


def test_get_or_fetch_respects_ttl():
    calls = []

    def fetch():
        calls.append(1)
        return "value"

    base = 1000.0
    with patch("eosh.completion_cache.time.monotonic", return_value=base):
        completion_cache.get_or_fetch(("k",), fetch, ttl=10)
    # Within TTL: cached.
    with patch("eosh.completion_cache.time.monotonic", return_value=base + 5):
        completion_cache.get_or_fetch(("k",), fetch, ttl=10)
    assert calls == [1]
    # Past TTL: refetch.
    with patch("eosh.completion_cache.time.monotonic", return_value=base + 11):
        completion_cache.get_or_fetch(("k",), fetch, ttl=10)
    assert calls == [1, 1]


def test_get_or_fetch_does_not_cache_exceptions():
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("nope")

    for _ in range(3):
        try:
            completion_cache.get_or_fetch(("k",), boom, ttl=10)
        except RuntimeError:
            pass
    # Each call re-runs fetch; nothing was cached.
    assert calls == [1, 1, 1]


def test_invalidate_all_clears_store():
    completion_cache.get_or_fetch(("k",), lambda: "v", ttl=10)
    completion_cache.invalidate_all()
    calls = []
    completion_cache.get_or_fetch(("k",), lambda: calls.append(1) or "v2", ttl=10)
    assert calls == [1]


def test_aws_env_key_uses_environ(monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "prod")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    assert completion_cache.aws_env_key() == ("prod", "us-west-2")


def test_aws_env_key_falls_back_to_default_region(monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    assert completion_cache.aws_env_key() == ("", "us-east-1")


def test_aws_env_key_returns_blanks_when_unset(monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    assert completion_cache.aws_env_key() == ("", "")
