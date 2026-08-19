"""Runs the kiosk JavaScript state machine under Node.

The hands-free countdown lives in browser code, so the Python suite cannot reach
it - and that code holds the client half of the "do not clock people out as they
walk past" guarantee. ``tests/js/kiosk_harness.js`` stubs the DOM, the camera and
the network, then drives the real ``kiosk.js`` with fake timers and asserts that:

* an empty doorway produces no requests at all;
* nothing is committed while the countdown is running;
* letting the countdown finish commits exactly once;
* **Cancel prevents the commit**;
* an already-clocked or unrecognised person never commits.

This caught a real bug: the recognition poll timer was not stopped when a
countdown began, so once the screen returned to idle the stale poll kept calling
/identify with nobody there and started a fresh countdown that then committed.

Skipped when Node is not installed - it is a development convenience, not a
runtime dependency of the application.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parent / "js" / "kiosk_harness.js"
NODE = shutil.which("node")

needs_node = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")


@needs_node
def test_kiosk_state_machine():
    result = subprocess.run(
        [NODE, str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=HARNESS.parent.parent.parent,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, f"kiosk.js harness reported failures:\n{output}"
    assert "checks passed" in output
    assert "FAIL" not in output, output


@needs_node
@pytest.mark.parametrize("script", ["kiosk.js", "capture.js", "enrol.js"])
def test_browser_scripts_parse(script):
    """A syntax error in kiosk JavaScript breaks clocking with no server error."""
    path = HARNESS.parent.parent.parent / "app" / "static" / "js" / script
    result = subprocess.run(
        [NODE, "--check", str(path)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
