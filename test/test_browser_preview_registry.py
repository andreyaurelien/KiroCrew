"""Remote loopback preview registry.

The Browser panel's iframe renders in the USER's browser, so a loopback URL
resolves on the user's machine, not the gateway's. The screenshot mirror is the
only channel that crosses that gap; this registry is how a panel names the page
it wants mirrored, and the proxy polls it to decide when to navigate.
"""
from __future__ import annotations

import pytest

from kiro_crew.browser.preview import (
    WANT_TTL_SECS,
    PreviewRegistry,
    is_loopback_host,
    normalize_target,
)


class TestNormalizeTarget:
    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:5173",
            "http://127.0.0.1:3000/app",
            "https://localhost:8443/",
            "http://kirocrew.localhost:7788/x?y=1",
            "http://[::1]:5173/",
        ],
    )
    def test_accepts_loopback_http_targets(self, url: str) -> None:
        assert normalize_target(url) == url

    @pytest.mark.parametrize(
        "url",
        [
            # Not the gateway's own host: these already render in the iframe or the
            # native view, so accepting them would only add a way to make the
            # gateway's browser fetch arbitrary URLs.
            "http://example.com/",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5:8080/",
            # Non-http schemes reach the filesystem or execute script.
            "file:///etc/passwd",
            "data:text/html,<script>alert(1)</script>",
            "javascript:alert(1)",
            # Credentials would be handed to the browser and could surface in a frame.
            "http://user:pw@localhost:5173/",
            # Malformed / abusive.
            "",
            "   ",
            "localhost:5173",
            "http://localhost:notaport/",
            "http://local host:5173/",
            "http://localhost:5173/\nHost: evil",
        ],
    )
    def test_refuses_everything_else(self, url: str) -> None:
        assert normalize_target(url) is None

    def test_refuses_an_overlong_url(self) -> None:
        assert normalize_target("http://localhost:5173/" + "a" * 4000) is None

    def test_a_hostname_that_merely_contains_localhost_is_not_loopback(self) -> None:
        """Suffix matching must be on a dot boundary: ``notlocalhost`` is a real
        internet name, and ``localhost.evil.com`` resolves wherever evil.com says."""
        assert is_loopback_host("notlocalhost") is False
        assert normalize_target("http://localhost.evil.com/") is None


class TestRegistry:
    def test_set_then_get_returns_the_want(self) -> None:
        reg = PreviewRegistry()
        want = reg.set_want("chat-1", "http://localhost:5173", now=100.0)
        assert want is not None
        assert reg.get_want("chat-1", now=100.0) == want
        assert want.url == "http://localhost:5173"

    def test_refreshing_the_same_url_keeps_the_generation(self) -> None:
        """The proxy navigates on a generation change, so a heartbeat for the page
        already showing must not bump it -- re-navigating would reset the page's
        scroll position and re-run it under the user."""
        reg = PreviewRegistry()
        first = reg.set_want("chat-1", "http://localhost:5173", now=100.0)
        again = reg.set_want("chat-1", "http://localhost:5173", now=110.0)
        assert first is not None and again is not None
        assert again.generation == first.generation
        assert again.expires_at > first.expires_at

    def test_a_new_url_bumps_the_generation(self) -> None:
        reg = PreviewRegistry()
        first = reg.set_want("chat-1", "http://localhost:5173", now=100.0)
        second = reg.set_want("chat-1", "http://localhost:3000", now=101.0)
        assert first is not None and second is not None
        assert second.generation > first.generation

    def test_generations_are_registry_wide_not_per_session(self) -> None:
        """A per-session counter would let a proxy tick see a generation number it
        had already acted on for a different session."""
        reg = PreviewRegistry()
        a = reg.set_want("chat-1", "http://localhost:5173", now=100.0)
        b = reg.set_want("chat-2", "http://localhost:5173", now=100.0)
        assert a is not None and b is not None
        assert a.generation != b.generation

    def test_a_want_expires_without_a_refresh(self) -> None:
        """Closing the tab must stop the mirror on its own."""
        reg = PreviewRegistry()
        reg.set_want("chat-1", "http://localhost:5173", now=100.0)
        assert reg.get_want("chat-1", now=100.0 + WANT_TTL_SECS - 1) is not None
        assert reg.get_want("chat-1", now=100.0 + WANT_TTL_SECS) is None

    def test_expiry_prunes_other_sessions_too(self) -> None:
        """Pruning on read keeps an abandoned session from pinning memory forever."""
        reg = PreviewRegistry()
        reg.set_want("gone", "http://localhost:5173", now=100.0)
        reg.set_want("live", "http://localhost:3000", now=100.0)
        # A read far in the future prunes both; the live one is then re-set.
        assert reg.get_want("gone", now=1000.0) is None
        assert reg.get_want("live", now=1000.0) is None

    def test_clear_want(self) -> None:
        reg = PreviewRegistry()
        reg.set_want("chat-1", "http://localhost:5173", now=100.0)
        assert reg.clear_want("chat-1") is True
        assert reg.clear_want("chat-1") is False
        assert reg.get_want("chat-1", now=100.0) is None

    def test_an_unusable_url_is_refused_without_disturbing_the_current_want(self) -> None:
        reg = PreviewRegistry()
        good = reg.set_want("chat-1", "http://localhost:5173", now=100.0)
        assert reg.set_want("chat-1", "http://example.com/", now=101.0) is None
        assert reg.get_want("chat-1", now=101.0) == good

    def test_an_empty_session_key_is_refused(self) -> None:
        """Frames are attributed per session; a want with no session could never be
        matched to a panel, so storing one would only ever pump for nobody."""
        reg = PreviewRegistry()
        assert reg.set_want("", "http://localhost:5173", now=100.0) is None
        assert reg.get_want("", now=100.0) is None
