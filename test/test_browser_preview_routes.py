"""Remote loopback preview routes.

The panel names a gateway-local page (cookie-authed PUT); the Playwright proxy
asks what to open (loopback + internal-secret POST). The split matters: the poll
answer names a page the gateway's own browser will navigate to, and its identity
must come from the signed pid mapping rather than a body field, or one browse
session could pick up another's target.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

import kiro_crew.dashboard.handlers.messaging as mod
from kiro_crew.browser.preview import PreviewRegistry


class _Req:
    """Minimal aiohttp-request stand-in for the handlers under test."""

    def __init__(
        self,
        body: Any = None,
        *,
        remote: str = "127.0.0.1",
        query: dict[str, str] | None = None,
        raise_on_json: bool = False,
    ) -> None:
        self._body = body
        self._raise = raise_on_json
        self.remote = remote
        self.query = query or {}
        self.app: dict[str, Any] = {}

    async def json(self) -> Any:
        if self._raise:
            raise ValueError("not json")
        return self._body


def _run(coro):
    return asyncio.run(coro)


def _payload(resp) -> Any:
    import json

    return json.loads(resp.body.decode("utf-8"))


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch):
    """Each test gets its own registry — the module-level one is process-wide."""
    reg = PreviewRegistry()
    monkeypatch.setattr(mod, "preview_registry", lambda: reg, raising=True)
    return reg


class TestPreviewSet:
    def test_records_a_loopback_want(self, fresh_registry) -> None:
        resp = _run(
            mod.api_browser_preview_set(
                _Req({"session_key": "chat-1", "url": "http://localhost:5173"})
            )
        )
        assert resp.status == 200
        body = _payload(resp)
        assert body["ok"] is True and body["generation"] >= 1
        want = fresh_registry.get_want("chat-1")
        assert want is not None and want.url == "http://localhost:5173"

    def test_refuses_a_non_loopback_target(self, fresh_registry) -> None:
        """A public page already renders in the iframe or the native view, so
        accepting one here would only add a way to make the gateway's browser
        fetch arbitrary URLs."""
        resp = _run(
            mod.api_browser_preview_set(
                _Req({"session_key": "chat-1", "url": "http://example.com/"})
            )
        )
        assert resp.status == 400
        assert "loopback" in _payload(resp)["error"]
        assert fresh_registry.get_want("chat-1") is None

    @pytest.mark.parametrize(
        "url", ["file:///etc/passwd", "javascript:alert(1)", "http://169.254.169.254/"]
    )
    def test_refuses_dangerous_schemes_and_metadata_ips(self, url: str, fresh_registry) -> None:
        resp = _run(mod.api_browser_preview_set(_Req({"session_key": "chat-1", "url": url})))
        assert resp.status == 400
        assert fresh_registry.get_want("chat-1") is None

    def test_requires_a_session_key(self) -> None:
        resp = _run(mod.api_browser_preview_set(_Req({"url": "http://localhost:5173"})))
        assert resp.status == 400
        assert "session_key" in _payload(resp)["error"]

    def test_invalid_json_is_a_400_not_a_500(self) -> None:
        resp = _run(mod.api_browser_preview_set(_Req(raise_on_json=True)))
        assert resp.status == 400


class TestPreviewClear:
    def test_clears_the_want(self, fresh_registry) -> None:
        fresh_registry.set_want("chat-1", "http://localhost:5173")
        resp = _run(mod.api_browser_preview_clear(_Req(query={"session_key": "chat-1"})))
        assert resp.status == 200
        assert _payload(resp)["cleared"] is True
        assert fresh_registry.get_want("chat-1") is None

    def test_clearing_an_absent_want_is_not_an_error(self, fresh_registry) -> None:
        resp = _run(mod.api_browser_preview_clear(_Req(query={"session_key": "nope"})))
        assert resp.status == 200
        assert _payload(resp)["cleared"] is False


class TestPreviewPoll:
    def test_off_host_is_refused(self, monkeypatch, fresh_registry) -> None:
        """The answer names a page the gateway's browser will open; only a caller
        on this host (the proxy) may ask."""
        monkeypatch.setattr(mod, "is_loopback", lambda addr: False, raising=True)
        resp = _run(mod.api_browser_preview_poll(_Req({"host_pid": 123}, remote="10.0.0.9")))
        assert resp.status == 403

    def test_resolves_the_session_from_the_pid_not_the_body(
        self, monkeypatch, fresh_registry
    ) -> None:
        """A body-supplied session key would let one browse session read another's
        target; identity comes from the gateway-signed pid mapping."""
        fresh_registry.set_want("chat-mine", "http://localhost:5173")
        fresh_registry.set_want("chat-theirs", "http://localhost:9999")
        monkeypatch.setattr(mod, "is_loopback", lambda addr: True, raising=True)
        monkeypatch.setattr(
            mod, "_resolve_browse_session_key", lambda pid: "dashboard:chat-mine", raising=True
        )

        resp = _run(
            mod.api_browser_preview_poll(
                _Req({"host_pid": 4242, "session_key": "chat-theirs"})
            )
        )

        assert resp.status == 200
        assert _payload(resp)["url"] == "http://localhost:5173"

    def test_an_unresolvable_pid_gets_nothing(self, monkeypatch, fresh_registry) -> None:
        fresh_registry.set_want("chat-1", "http://localhost:5173")
        monkeypatch.setattr(mod, "is_loopback", lambda addr: True, raising=True)
        monkeypatch.setattr(mod, "_resolve_browse_session_key", lambda pid: "", raising=True)
        resp = _run(mod.api_browser_preview_poll(_Req({"host_pid": 1})))
        assert _payload(resp) == {}

    def test_no_want_for_this_session_gets_nothing(self, monkeypatch, fresh_registry) -> None:
        monkeypatch.setattr(mod, "is_loopback", lambda addr: True, raising=True)
        monkeypatch.setattr(
            mod, "_resolve_browse_session_key", lambda pid: "dashboard:chat-1", raising=True
        )
        resp = _run(mod.api_browser_preview_poll(_Req({"host_pid": 4242})))
        assert _payload(resp) == {}


def test_the_poll_route_is_a_strict_internal_path() -> None:
    """Machine endpoint: loopback + internal secret, no cookie fall-through. The
    browser-called PUT/DELETE pair must NOT be in that set, or the panel could not
    call it."""
    from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS

    assert "/api/browser/preview/poll" in _STRICT_INTERNAL_API_PATHS
    assert "/api/browser/preview" not in _STRICT_INTERNAL_API_PATHS
