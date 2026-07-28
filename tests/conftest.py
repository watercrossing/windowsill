"""Suite-wide guards (design/00-instructions.md §0.3: "No network, no ``claude``
invocation, and no real clock in the test suite").

``windowsill.runner.run_job`` probes ``/usage`` before and after every run (§4.3), so
a test that only cares about, say, the timeout path would otherwise shell out
to a real ``claude`` just by existing. The autouse fixture below makes the
default oracle a fixed *closed-window* reading, and makes the one function that
actually spawns ``claude`` fail loudly if anything still reaches it.

Tests that care about window state inject their own probe explicitly; this is
only the floor under the ones that don't.
"""

from __future__ import annotations

from typing import Sequence

import pytest

from windowsill import usage

#: What an un-injected probe answers: no window open (§4.1's closed state).
CLOSED_WINDOW = usage.ParsedUsage(pct_used=0, reset_at=None)


@pytest.fixture(autouse=True)
def _no_real_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    real_probe = usage.probe

    def _guarded_probe(**kwargs) -> usage.ParsedUsage:
        # A test that passes its own `run` transport is testing the probe
        # itself and never reaches a binary -- let it through untouched.
        # Anything else got here by default and gets the canned reading.
        if "run" in kwargs:
            return real_probe(**kwargs)
        return CLOSED_WINDOW

    def _forbidden(argv: Sequence[str]) -> str:
        raise AssertionError(f"a test tried to run a real claude: {list(argv)}")

    monkeypatch.setattr(usage, "probe", _guarded_probe)
    monkeypatch.setattr(usage, "_default_run", _forbidden)
