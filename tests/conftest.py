"""Test-suite-wide pytest hooks.

Provides the ``requires_real_stdio`` marker for tests that exercise
eosh's in-process pipeline (``_start_stage_thread``).  Those
workers route a Python stage's ``print()`` through a thread-local
override on ``sys.stdout``; pytest's per-test stdio capture replaces
``sys.stdout`` with its own ``EncodedFile`` between the ``shell``
fixture's ``Shell()`` call and the test body, breaking the routing.

When a test is marked, this plugin suspends global stdio capture for
the duration of the *call* phase (after setup, before teardown), so
``sys.stdout`` stays at the real ``TextIOWrapper`` that ``Shell.__init__``
wraps with ``_ThreadLocalStream``.  Output from the test still appears
on the terminal, but the routing keeps working.
"""

from __future__ import annotations

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "requires_real_stdio: keep pytest's stdio capture suspended during "
        "the test body so the in-process pipeline's thread-local stdout "
        "router stays bound to the real terminal stream",
    )


@pytest.fixture(autouse=True)
def _no_user_config(monkeypatch):
    """Keep ``Shell()`` from exec'ing the developer's own ``~/.eosh/config.py``.

    A config file is arbitrary Python run at startup: it registers commands and
    aliases, replaces the prompt, and can assign to any module global (a real
    one sets ``jobs.EXTRA_JOB_CATEGORIES``).  Loading it made the suite's result
    depend on whose machine it ran on — every test that builds a ``Shell``
    silently leaked one developer's settings into every test after it.

    Tests that mean to exercise config loading should call
    ``Shell._load_user_config`` explicitly against a path they control.
    """
    monkeypatch.setattr("eosh.shell.Shell._load_user_config",
                        lambda self: None)


@pytest.fixture(autouse=True)
def _clear_completion_cache():
    """The completion cache is process-global; tests that exercise
    cached completers must start from a clean slate."""
    from eosh import completion_cache
    completion_cache.invalidate_all()
    yield
    completion_cache.invalidate_all()


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    """Suspend global stdio capture and reinstall the thread-local stdio
    routers around the actual function call of a marked item.

    Using ``pytest_pyfunc_call`` (not ``pytest_runtest_call``) ensures
    our hookwrapper nests *inside* CaptureManager's ``item_capture``
    context manager, so when CaptureManager restores ``sys.stdout`` to
    its capture stream at the start of the call phase, we then suspend
    capture *again* and reinstall the thread-local routers — and that
    state is what the test body sees.

    Without the reinstall, the in-process pipeline's worker threads
    reach ``sys.stdout.set_override`` and find pytest's ``EncodedFile``
    capture stream there, which has no ``set_override`` method, so the
    producer's ``print()`` goes to pytest's capture buffer instead of
    the inter-stage pipe.
    """
    if pyfuncitem.get_closest_marker("requires_real_stdio") is None:
        yield
        return
    capman = pyfuncitem.config.pluginmanager.getplugin("capturemanager")
    if capman is None:
        yield
        return
    import sys
    from eosh.shell import _ThreadLocalStream

    capman.suspend_global_capture(in_=True)
    saved = (sys.stdin, sys.stdout, sys.stderr)
    for stream in ("stdin", "stdout", "stderr"):
        if not isinstance(getattr(sys, stream), _ThreadLocalStream):
            setattr(sys, stream, _ThreadLocalStream(getattr(sys, stream)))
    try:
        yield
    finally:
        sys.stdin, sys.stdout, sys.stderr = saved
        capman.resume_global_capture()
