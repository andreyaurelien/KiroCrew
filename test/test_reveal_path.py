"""Tests for POST /api/reveal — local gate and action routing."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.files import api_reveal_path


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/api/reveal", api_reveal_path)
    return app


@pytest.fixture
def mock_sel():
    with patch("kiro_crew.sel.sel") as m:
        instance = MagicMock()
        m.return_value = instance
        yield instance


@pytest.mark.asyncio
async def test_reveal_path_no_crash(mock_sel, tmp_path):
    """Given a valid file path, when POST /api/reveal is called with action="reveal",
    then response status is 200 (not 500 TypeError)."""
    f = tmp_path / "hello.txt"
    f.write_text("hi")
    # Mock xdg-open as available so the full code path (including SEL) executes
    with patch("shutil.which", return_value="/usr/bin/xdg-open"), patch("subprocess.Popen"):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/reveal",
                json={"path": str(f), "action": "reveal"},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body == {"ok": True}
            # Verify log_tool_invocation called with correct kwargs (no TypeError)
            mock_sel.log_tool_invocation.assert_called_with(
                session_key="api",
                source="api",
                tool_name="reveal_path",
                outcome="success",
                resources=str(f),
                metadata={"action": "reveal"},
            )


@pytest.mark.asyncio
async def test_reveal_path_sensitive_denied(mock_sel):
    """Given a path containing ~/.ssh/id_rsa, when POST /api/reveal is called,
    then response is 403 with {"error": "access denied"} and SEL logs the denial."""
    with patch("kiro_crew.dashboard.handlers.files.is_sensitive_path", return_value=True):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/reveal",
                json={"path": "/home/user/.ssh/id_rsa", "action": "reveal"},
            )
            assert resp.status == 403
            body = await resp.json()
            assert body == {"error": "access denied"}
            mock_sel.log_tool_invocation.assert_called_with(
                session_key="api",
                source="api",
                tool_name="reveal_path",
                outcome="denied",
                error="sensitive_path",
                resources="/home/user/.ssh/id_rsa",
                metadata={"action": "reveal"},
            )


@pytest.mark.asyncio
async def test_reveal_path_traversal_rejected(mock_sel):
    """Given a path containing '..', when POST /api/reveal is called,
    then response is 400 with {"error": "invalid path"}."""
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post(
            "/api/reveal",
            json={"path": "/tmp/../etc/passwd", "action": "reveal"},
        )
        assert resp.status == 400
        body = await resp.json()
        assert body == {"error": "invalid path"}
        mock_sel.log_tool_invocation.assert_not_called()


# ── Remote fallback gate tests ──


@pytest.mark.asyncio
async def test_reveal_remote_request_returns_copy_fallback(mock_sel, tmp_path):
    """A non-local request gets the copy fallback and spawns no process."""
    f = tmp_path / "readme.md"
    f.write_text("content")
    with (
        patch(
            "kiro_crew.dashboard.handlers.files.is_direct_local_request",
            return_value=False,
        ),
        patch("subprocess.Popen") as mock_popen,
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/reveal",
                json={"path": str(f), "action": "reveal"},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body == {"ok": True, "copy": str(f)}
            mock_popen.assert_not_called()
            mock_sel.log_tool_invocation.assert_called_with(
                session_key="api",
                source="api",
                tool_name="reveal_path",
                outcome="denied",
                error="remote_request",
                resources=str(f),
                metadata={"action": "reveal"},
            )


@pytest.mark.asyncio
async def test_reveal_remote_request_open_action_returns_copy(mock_sel, tmp_path):
    """Remote + action=open also returns the copy fallback, no subprocess."""
    f = tmp_path / "doc.txt"
    f.write_text("hello")
    with (
        patch(
            "kiro_crew.dashboard.handlers.files.is_direct_local_request",
            return_value=False,
        ),
        patch("subprocess.Popen") as mock_popen,
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/reveal",
                json={"path": str(f), "action": "open"},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body == {"ok": True, "copy": str(f)}
            mock_popen.assert_not_called()


# ── action="open" tests ──


@pytest.mark.asyncio
async def test_open_action_spawns_opener_for_file(mock_sel, tmp_path):
    """Local request with action=open on a regular file spawns the opener."""
    f = tmp_path / "report.pdf"
    f.write_text("data")
    with (
        patch("shutil.which", return_value="/usr/bin/xdg-open"),
        patch("subprocess.Popen") as mock_popen,
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/reveal",
                json={"path": str(f), "action": "open"},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body == {"ok": True}
            mock_popen.assert_called_once()
            mock_sel.log_tool_invocation.assert_called_with(
                session_key="api",
                source="api",
                tool_name="reveal_path",
                outcome="success",
                resources=str(f),
                metadata={"action": "open"},
            )


@pytest.mark.asyncio
async def test_open_action_on_directory_returns_400(mock_sel, tmp_path):
    """action=open on a directory returns 400 (not a regular file)."""
    d = tmp_path / "subdir"
    d.mkdir()
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post(
            "/api/reveal",
            json={"path": str(d), "action": "open"},
        )
        assert resp.status == 400
        body = await resp.json()
        assert body == {"error": "not a regular file"}
        # No SEL success log should fire
        for call in mock_sel.log_tool_invocation.call_args_list:
            assert call.kwargs.get("outcome") != "success"
