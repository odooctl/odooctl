"""Contract tests for the v0.3.0 web UI enhancements: runner-awareness, cancel,
and refresh affordances in the bundled SPA."""
from __future__ import annotations

from pathlib import Path

_DIST = Path(__file__).parent.parent / "odooctl" / "web" / "dist"
_APP = (_DIST / "app.js").read_text()


def test_app_js_polls_runner_status_endpoint():
    assert "/runner/status" in _APP


def test_app_js_surfaces_runner_offline_remediation():
    # The exact command to fix the "queued forever" situation must be shown.
    assert "odooctl runner" in _APP
    assert "runner-pill" in _APP


def test_app_js_has_cancel_operation_affordance():
    assert "cancel-op" in _APP
    assert "/cancel" in _APP


def test_app_js_has_refresh_control():
    assert "refresh-btn" in _APP


def test_style_has_runner_pill_states():
    css = (_DIST / "style.css").read_text()
    assert ".runner-online" in css
    assert ".runner-offline" in css
