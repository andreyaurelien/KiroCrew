"""Proxy-side half of the remote loopback preview.

The panel records which gateway-local page it wants mirrored; this proxy runs
where that URL resolves, so it polls for the want and injects a
``browser_navigate`` into its live MCP session — the same trick the active pump
uses for screenshots, so the agent never sees either.
"""
from __future__ import annotations

from typing import Any

import pytest

import kiro_crew.mcp_playwright_proxy as proxy


@pytest.fixture(autouse=True)
def reset_preview_state(monkeypatch):
    """Module-level generation/poll state leaks between tests otherwise."""
    monkeypatch.setattr(proxy, "_preview_generation", None, raising=True)
    monkeypatch.setattr(proxy, "_preview_polled_at", 0.0, raising=True)
    monkeypatch.setattr(proxy, "_preview_active_until", 0.0, raising=True)
    monkeypatch.setattr(proxy, "_pump_enabled", True, raising=True)
    monkeypatch.setattr(proxy, "_native_panel_seen", False, raising=True)


class _Writes:
    """Captures the JSON-RPC the proxy injects into the subprocess."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(
            proxy,
            "_write_message_to_subprocess",
            lambda _stdin, msg: self.sent.append(msg),
            raising=True,
        )


def _want(url: str, generation: int) -> dict[str, Any]:
    return {"url": url, "generation": generation}


class TestPreviewInjection:
    def test_a_new_want_injects_a_navigation(self, monkeypatch) -> None:
        writes = _Writes()
        writes.install(monkeypatch)
        monkeypatch.setattr(proxy, "_fetch_preview_want", lambda: _want("http://localhost:5173", 7))

        assert proxy._maybe_inject_preview(object(), 1000.0) is True
        assert len(writes.sent) == 1
        sent = writes.sent[0]
        assert sent["params"]["name"] == "browser_navigate"
        assert sent["params"]["arguments"]["url"] == "http://localhost:5173"
        assert proxy._is_preview_id(sent["id"])

    def test_an_unchanged_generation_does_not_renavigate(self, monkeypatch) -> None:
        """The panel heartbeats its want; re-navigating each time would reset the
        page's scroll position and re-run it under the watcher."""
        writes = _Writes()
        writes.install(monkeypatch)
        monkeypatch.setattr(proxy, "_fetch_preview_want", lambda: _want("http://localhost:5173", 7))

        assert proxy._maybe_inject_preview(object(), 1000.0) is True
        # Far enough ahead to clear the poll interval, same generation.
        assert proxy._maybe_inject_preview(object(), 1100.0) is False
        assert len(writes.sent) == 1

    def test_a_changed_url_navigates_again(self, monkeypatch) -> None:
        writes = _Writes()
        writes.install(monkeypatch)
        seq = iter([_want("http://localhost:5173", 7), _want("http://localhost:3000", 8)])
        monkeypatch.setattr(proxy, "_fetch_preview_want", lambda: next(seq))

        proxy._maybe_inject_preview(object(), 1000.0)
        proxy._maybe_inject_preview(object(), 1100.0)
        assert [m["params"]["arguments"]["url"] for m in writes.sent] == [
            "http://localhost:5173",
            "http://localhost:3000",
        ]

    def test_the_poll_is_rate_limited(self, monkeypatch) -> None:
        calls = {"n": 0}

        def _fetch() -> dict[str, Any]:
            calls["n"] += 1
            return _want("http://localhost:5173", 7)

        _Writes().install(monkeypatch)
        monkeypatch.setattr(proxy, "_fetch_preview_want", _fetch)

        proxy._maybe_inject_preview(object(), 1000.0)
        proxy._maybe_inject_preview(object(), 1000.1)  # inside the interval
        assert calls["n"] == 1

    def test_no_want_clears_the_preview_activity_window(self, monkeypatch) -> None:
        """A lapsed want must stop counting as activity, or the pump would keep
        screenshotting for a watcher who has gone away."""
        _Writes().install(monkeypatch)
        monkeypatch.setattr(proxy, "_fetch_preview_want", lambda: _want("http://localhost:5173", 7))
        proxy._maybe_inject_preview(object(), 1000.0)
        assert proxy._preview_active_until > 1000.0

        monkeypatch.setattr(proxy, "_fetch_preview_want", lambda: None)
        assert proxy._maybe_inject_preview(object(), 1100.0) is False
        assert proxy._preview_active_until == 0.0
        assert proxy._preview_generation is None

    def test_a_failed_write_is_retried_on_the_next_tick(self, monkeypatch) -> None:
        """Forgetting the generation is what makes the retry happen — remembering
        it would strand the panel on a page that never opened."""
        def _boom(_stdin, _msg):
            raise OSError("pipe closed")

        monkeypatch.setattr(proxy, "_write_message_to_subprocess", _boom, raising=True)
        monkeypatch.setattr(proxy, "_fetch_preview_want", lambda: _want("http://localhost:5173", 7))

        assert proxy._maybe_inject_preview(object(), 1000.0) is False
        assert proxy._preview_generation is None

    def test_native_view_suppresses_preview(self, monkeypatch) -> None:
        """With the Electron view live the user sees the real page; a mirror frame
        would paint a stale surface over it."""
        writes = _Writes()
        writes.install(monkeypatch)
        monkeypatch.setattr(proxy, "_native_panel_seen", True, raising=True)
        monkeypatch.setattr(proxy, "_fetch_preview_want", lambda: _want("http://localhost:5173", 7))

        assert proxy._maybe_inject_preview(object(), 1000.0) is False
        assert writes.sent == []

    def test_extension_mode_suppresses_preview(self, monkeypatch) -> None:
        writes = _Writes()
        writes.install(monkeypatch)
        monkeypatch.setattr(proxy, "_pump_enabled", False, raising=True)
        monkeypatch.setattr(proxy, "_fetch_preview_want", lambda: _want("http://localhost:5173", 7))

        assert proxy._maybe_inject_preview(object(), 1000.0) is False
        assert writes.sent == []


class TestPumpGating:
    def test_a_live_preview_keeps_the_pump_running_without_agent_activity(
        self, monkeypatch
    ) -> None:
        """A human watching a page is not a ``browser_*`` tool call, so without the
        preview activity source the mirror would go cold one window after the
        navigation."""
        monkeypatch.setattr(proxy, "_PENDING_REQUESTS", {}, raising=True)
        monkeypatch.setattr(proxy, "_pump_inflight_id", None, raising=True)
        monkeypatch.setattr(proxy, "_last_subscriber_count", 1, raising=True)
        monkeypatch.setattr(proxy, "_last_browse_activity", 0.0, raising=True)

        monkeypatch.setattr(proxy, "_preview_active_until", 0.0, raising=True)
        assert proxy._should_pump(1000.0) is False

        monkeypatch.setattr(proxy, "_preview_active_until", 1010.0, raising=True)
        assert proxy._should_pump(1000.0) is True

    def test_preview_and_pump_ids_are_distinguishable(self) -> None:
        """The relay demuxes on the id prefix; an overlap would send a navigation
        response down the frame path (or to the agent)."""
        pump_id = f"{proxy._PUMP_ID_PREFIX}1"
        preview_id = f"{proxy._PREVIEW_ID_PREFIX}1"
        assert proxy._is_pump_id(pump_id) and not proxy._is_preview_id(pump_id)
        assert proxy._is_preview_id(preview_id) and not proxy._is_pump_id(preview_id)
        assert proxy._is_preview_id("tools/call-42") is False
