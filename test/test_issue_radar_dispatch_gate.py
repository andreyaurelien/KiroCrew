"""Tests for Issue Radar's dispatch gate (RFC phase 0).

The gate decides whether an issue may be handed to an implementation attempt. It
ships ahead of anything that runs an agent, so these tests are the whole contract
for now:

* ``dispatch.resolve_checkout`` accepts an absolute, non-sensitive, existing git
  work tree and refuses everything else -- including a symlink whose TARGET is
  sensitive, which is why resolution happens before the sensitivity test.
* ``dispatch.readiness`` distinguishes "no path set" from "the path you set
  broke", because those need different sentences in the UI.
* the store round-trips the path per provider+host, and a permissions self-heal
  write does not drop it.
* the routes refuse rather than fall back: a rejected path stores nothing.

Handlers are driven directly with ``aiohttp.test_utils.make_mocked_request``, the
same shape the other Issue Radar route tests use.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock
from urllib.parse import urlencode

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.issue_radar.backend import dispatch, routes, store

BASE = "/api/apps/issue-radar"


def _git_dir(parent: Path, name: str = "checkout", *, marker: str = "dir") -> Path:
    """An absolute directory that looks like a git work tree.

    ``marker="file"`` writes ``.git`` as a FILE holding a ``gitdir:`` pointer,
    which is exactly the shape ``git worktree add`` produces -- a linked worktree
    must be accepted, since that is where dispatch will do its work.
    """
    root = parent / name
    root.mkdir(parents=True)
    if marker == "dir":
        (root / ".git").mkdir()
    else:
        (root / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n", encoding="utf-8")
    return root


class TestResolveCheckout(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_blank_is_refused(self):
        self.assertIsNone(dispatch.resolve_checkout(""))
        self.assertIsNone(dispatch.resolve_checkout("   "))

    def test_relative_path_is_refused(self):
        # realpath() would resolve this against the gateway's own cwd and hand back
        # an absolute path, so the check has to happen on the expanded input.
        self.assertIsNone(dispatch.resolve_checkout("some/checkout"))
        self.assertIsNone(dispatch.resolve_checkout("."))

    def test_missing_directory_is_refused(self):
        self.assertIsNone(dispatch.resolve_checkout(str(self.tmp / "nope")))

    def test_file_is_refused(self):
        path = self.tmp / "a-file"
        path.write_text("x", encoding="utf-8")
        self.assertIsNone(dispatch.resolve_checkout(str(path)))

    def test_directory_without_git_is_refused(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertIsNone(dispatch.resolve_checkout(str(plain)))

    def test_clone_is_accepted(self):
        root = _git_dir(self.tmp)
        self.assertEqual(dispatch.resolve_checkout(str(root)), root.resolve())

    def test_linked_worktree_is_accepted(self):
        root = _git_dir(self.tmp, "wt", marker="file")
        self.assertEqual(dispatch.resolve_checkout(str(root)), root.resolve())

    def test_sensitive_path_is_refused(self):
        root = _git_dir(self.tmp)
        with mock.patch.object(dispatch, "is_sensitive_path", return_value=True):
            self.assertIsNone(dispatch.resolve_checkout(str(root)))

    def test_symlink_is_resolved_before_the_sensitivity_test(self):
        """A symlink in a benign directory must not smuggle its target past the
        sensitivity check, so the value tested is the RESOLVED one."""
        real = _git_dir(self.tmp, "secret-checkout")
        link = self.tmp / "innocent"
        link.symlink_to(real, target_is_directory=True)
        seen: list[str] = []

        def _only_the_target(path: str) -> bool:
            seen.append(path)
            return path == str(real.resolve())

        with mock.patch.object(dispatch, "is_sensitive_path", side_effect=_only_the_target):
            self.assertIsNone(dispatch.resolve_checkout(str(link)))
        # The link's own path was never what got judged.
        self.assertEqual(seen, [str(real.resolve())])

    def test_symlink_to_a_benign_checkout_resolves(self):
        real = _git_dir(self.tmp, "real")
        link = self.tmp / "link"
        link.symlink_to(real, target_is_directory=True)
        self.assertEqual(dispatch.resolve_checkout(str(link)), real.resolve())

    def test_security_module_is_present_in_this_build(self):
        """The fallback in dispatch.py fails CLOSED, which is only correct if the
        real predicate is normally in use -- assert that it is, so a silent import
        break shows up here rather than as every path being refused."""
        self.assertTrue(dispatch._HAS_SECURITY)


class TestReadiness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_unset_reports_no_local_path(self):
        for value in ("", "   ", None):
            ready, reason = dispatch.readiness(value)
            self.assertFalse(ready)
            self.assertEqual(reason, dispatch.REASON_NO_LOCAL_PATH)

    def test_valid_checkout_is_ready(self):
        root = _git_dir(self.tmp)
        self.assertEqual(dispatch.readiness(str(root)), (True, dispatch.REASON_OK))

    def test_a_recorded_path_that_broke_is_its_own_reason(self):
        """A checkout deleted after being recorded must not keep reporting ready,
        and must not be confused with never having been set."""
        root = _git_dir(self.tmp)
        stored = str(root)
        self.assertTrue(dispatch.readiness(stored)[0])
        shutil.rmtree(root)
        ready, reason = dispatch.readiness(stored)
        self.assertFalse(ready)
        self.assertEqual(reason, dispatch.REASON_CHECKOUT_UNUSABLE)
        self.assertNotEqual(reason, dispatch.REASON_NO_LOCAL_PATH)


class TestLocalPathStore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_unset_and_unconnected_both_read_empty(self):
        self.assertEqual(store.read_repo_local_path("no", "pe", root=self.tmp), "")
        store.add_connected_repo("o", "r", root=self.tmp)
        self.assertEqual(store.read_repo_local_path("o", "r", root=self.tmp), "")

    def test_roundtrip(self):
        store.add_connected_repo("o", "r", root=self.tmp)
        store.set_repo_local_path("o", "r", "/srv/checkout", root=self.tmp)
        self.assertEqual(store.read_repo_local_path("o", "r", root=self.tmp), "/srv/checkout")

    def test_empty_clears_the_key_entirely(self):
        store.add_connected_repo("o", "r", root=self.tmp)
        store.set_repo_local_path("o", "r", "/srv/checkout", root=self.tmp)
        store.set_repo_local_path("o", "r", "", root=self.tmp)
        self.assertEqual(store.read_repo_local_path("o", "r", root=self.tmp), "")
        entry = store.list_connected_repos(self.tmp)[0]
        # Cleared, not stored as an empty string: a cleared repo has to be
        # indistinguishable from one that never had a path.
        self.assertNotIn("local_path", entry)

    def test_scoped_by_provider_and_host(self):
        store.add_connected_repo("o", "r", root=self.tmp)
        store.add_connected_repo(
            "o", "r", provider="gitlab", host="gitlab.com", root=self.tmp
        )
        store.set_repo_local_path("o", "r", "/srv/gh", root=self.tmp)
        self.assertEqual(store.read_repo_local_path("o", "r", root=self.tmp), "/srv/gh")
        self.assertEqual(
            store.read_repo_local_path(
                "o", "r", provider="gitlab", host="gitlab.com", root=self.tmp
            ),
            "",
        )

    def test_survives_a_permissions_selfheal_write(self):
        store.add_connected_repo("o", "r", root=self.tmp)
        store.set_repo_local_path("o", "r", "/srv/checkout", root=self.tmp)
        store.set_repo_permissions("o", "r", {"push": True}, root=self.tmp)
        self.assertEqual(store.read_repo_local_path("o", "r", root=self.tmp), "/srv/checkout")


def _get(query: dict | None = None) -> web.Request:
    full = f"{BASE}/dispatch-readiness"
    if query:
        full = f"{full}?{urlencode(query)}"
    return make_mocked_request("GET", full, app=web.Application())


def _post(body: object) -> web.Request:
    req = make_mocked_request("POST", f"{BASE}/repo/local-path", app=web.Application())
    if body is None:
        req.json = AsyncMock(side_effect=ValueError("not json"))  # type: ignore[method-assign]
    else:
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def _body(response: web.Response) -> dict:
    raw = response.body
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


def _connected(value: bool = True):
    return mock.patch.object(store, "is_repo_connected", return_value=value)


class TestDispatchRoutes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # Point the app's data dir at a tmp tree so the routes exercise the REAL
        # store instead of a mock of it.
        patcher = mock.patch.object(store, "app_data_dir", return_value=self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        store.add_connected_repo("o", "r")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_get_requires_owner_and_repo(self):
        resp = await routes._handle_get_dispatch_readiness(_get())
        self.assertEqual(resp.status, 400)
        self.assertEqual(_body(resp)["code"], "repo_required")

    async def test_get_refuses_an_unconnected_repo(self):
        with _connected(False):
            resp = await routes._handle_get_dispatch_readiness(_get({"owner": "o", "repo": "r"}))
        self.assertEqual(resp.status, 404)
        self.assertEqual(_body(resp)["code"], "repo_not_connected")

    async def test_get_reports_no_local_path(self):
        with _connected():
            resp = await routes._handle_get_dispatch_readiness(_get({"owner": "o", "repo": "r"}))
        self.assertEqual(resp.status, 200)
        payload = _body(resp)
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["reason"], dispatch.REASON_NO_LOCAL_PATH)
        self.assertEqual(payload["local_path"], "")

    async def test_get_reports_ready_for_a_real_checkout(self):
        root = _git_dir(self.tmp, "co")
        store.set_repo_local_path("o", "r", str(root))
        with _connected():
            resp = await routes._handle_get_dispatch_readiness(_get({"owner": "o", "repo": "r"}))
        payload = _body(resp)
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["reason"], dispatch.REASON_OK)

    async def test_post_rejects_a_malformed_body(self):
        for body in (None, ["not", "a", "dict"]):
            resp = await routes._handle_set_repo_local_path(_post(body))
            self.assertEqual(resp.status, 400)
            self.assertEqual(_body(resp)["code"], "invalid_body")

    async def test_post_requires_owner_and_repo(self):
        resp = await routes._handle_set_repo_local_path(_post({"local_path": "/srv/x"}))
        self.assertEqual(resp.status, 400)
        self.assertEqual(_body(resp)["code"], "repo_required")

    async def test_post_refuses_an_unconnected_repo(self):
        with _connected(False):
            resp = await routes._handle_set_repo_local_path(
                _post({"owner": "o", "repo": "r", "local_path": "/srv/x"})
            )
        self.assertEqual(resp.status, 404)
        self.assertEqual(_body(resp)["code"], "repo_not_connected")

    async def test_post_refuses_a_non_string_path(self):
        with _connected():
            resp = await routes._handle_set_repo_local_path(
                _post({"owner": "o", "repo": "r", "local_path": 17})
            )
        self.assertEqual(resp.status, 400)
        self.assertEqual(_body(resp)["code"], "invalid_local_path")

    async def test_post_refuses_a_bad_path_and_stores_nothing(self):
        """The refusal is the feature: a path that does not validate must not be
        stored as if it were fine, and must not become a fallback."""
        with _connected():
            resp = await routes._handle_set_repo_local_path(
                _post({"owner": "o", "repo": "r", "local_path": str(self.tmp / "not-a-repo")})
            )
        self.assertEqual(resp.status, 400)
        self.assertEqual(_body(resp)["code"], "invalid_local_path")
        self.assertEqual(store.read_repo_local_path("o", "r"), "")

    async def test_post_stores_the_resolved_path_and_reports_ready(self):
        real = _git_dir(self.tmp, "real")
        link = self.tmp / "link"
        link.symlink_to(real, target_is_directory=True)
        with _connected():
            resp = await routes._handle_set_repo_local_path(
                _post({"owner": "o", "repo": "r", "local_path": str(link)})
            )
        self.assertEqual(resp.status, 200)
        payload = _body(resp)
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["reason"], dispatch.REASON_OK)
        # Resolved, not the symlink: readiness later re-checks the directory the
        # validator accepted.
        self.assertEqual(payload["local_path"], str(real.resolve()))
        self.assertEqual(store.read_repo_local_path("o", "r"), str(real.resolve()))

    async def test_post_with_an_empty_path_clears_it(self):
        root = _git_dir(self.tmp, "co")
        store.set_repo_local_path("o", "r", str(root))
        with _connected():
            resp = await routes._handle_set_repo_local_path(
                _post({"owner": "o", "repo": "r", "local_path": ""})
            )
        payload = _body(resp)
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["reason"], dispatch.REASON_NO_LOCAL_PATH)
        self.assertEqual(store.read_repo_local_path("o", "r"), "")


class TestRouteRegistration(unittest.TestCase):
    def test_both_routes_are_registered_and_gated(self):
        app = web.Application()
        routes.register_routes(app)
        paths = {
            (r.method, r.resource.canonical)  # type: ignore[union-attr]
            for r in app.router.routes()
        }
        self.assertIn(("GET", f"{BASE}/dispatch-readiness"), paths)
        self.assertIn(("POST", f"{BASE}/repo/local-path"), paths)

    def test_dispatch_routes_are_not_reachable_with_the_internal_secret(self):
        """Only ``/investigation`` is admitted there, and deliberately as a full
        path rather than a prefix. A session that could write a checkout path
        could point a later dispatch at a directory the user never named."""
        from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS

        self.assertNotIn(f"{BASE}/dispatch-readiness", _MIXED_INTERNAL_API_PATHS)
        self.assertNotIn(f"{BASE}/repo/local-path", _MIXED_INTERNAL_API_PATHS)
        self.assertNotIn(BASE, _MIXED_INTERNAL_API_PATHS)
