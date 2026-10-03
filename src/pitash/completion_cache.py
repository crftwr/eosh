"""TTL-based cache for completer fetches that hit external services.

TAB completion fires every keystroke while the picker is open
(``refresh_fn`` in :mod:`pitash.lineedit` re-runs the completer on every
typed char). Completers that call AWS APIs would otherwise re-fetch the
same data four or five times for a single typed token.

Two primitives:

* :func:`get_or_fetch` — look up a cached value by *key* or compute it.
  TTL applies per entry. Exceptions are not cached: a failed fetch
  simply propagates and leaves the cache untouched.
* :func:`invalidate_all` — wipe every entry. The shell calls this after
  each pipeline finishes so a freshly-mutated cluster (e.g. after
  ``awsut sagemaker hyperpod scale``) doesn't return stale completions on
   the next
  TAB.

Also exposes :func:`aws_env_key` so all AWS-style completers can salt
their cache keys with the active profile/region without each importing
``os.environ`` directly.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Tuple, TypeVar

DEFAULT_TTL = 60.0

T = TypeVar("T")

_lock = threading.Lock()
_store: dict[tuple, tuple[float, Any]] = {}


def get_or_fetch(key: tuple, fetch: Callable[[], T], ttl: float = DEFAULT_TTL) -> T:
    """Return cached value for *key*, or call *fetch* and cache its result.

    *key* must be a hashable tuple. *ttl* is in seconds (monotonic clock).
    If *fetch* raises, the exception propagates and nothing is cached.
    """
    now = time.monotonic()
    with _lock:
        entry = _store.get(key)
        if entry is not None and (now - entry[0]) < ttl:
            return entry[1]
    value = fetch()
    with _lock:
        _store[key] = (time.monotonic(), value)
    return value


def invalidate_all() -> None:
    """Drop every cached entry.

    Cheap (``dict.clear()``). The shell calls this after each user
    pipeline so any side-effecting command rebuilds the next TAB session
    from scratch.
    """
    with _lock:
        _store.clear()


def aws_env_key() -> Tuple[str, str]:
    """Return ``(AWS_PROFILE, AWS_REGION)`` for use as a cache-key salt.

    Empty strings when unset — what matters is that switching profile or
    region produces a different tuple so the cache doesn't return data
    from the previous account.
    """
    return (
        os.environ.get("AWS_PROFILE", ""),
        os.environ.get("AWS_REGION", "") or os.environ.get("AWS_DEFAULT_REGION", ""),
    )
