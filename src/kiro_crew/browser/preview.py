"""Remote loopback preview — let a dashboard on another machine see the gateway's
own ``localhost``.

The Browser panel's preview is an ``iframe`` rendered in the USER's browser, so a
loopback URL resolves on the user's machine. When the dashboard is reached over a
network the gateway's ``127.0.0.1:5173`` is therefore unreachable, and the panel
can only ever paint a blank frame. The one channel that does cross the gap is the
screenshot mirror: Playwright runs on the gateway host and the MCP proxy already
POSTs frames to ``/api/browser/frame`` for the panel to render.

This module is the missing half of that channel — a way for the panel to say
*which* page it wants mirrored, since until now frames only appeared when the
AGENT happened to browse. It holds one "want" per browse session:

    panel → PUT /api/browser/preview {session_key, url}   (dashboard-authenticated)
    proxy → POST /api/browser/preview/poll {host_pid}      (loopback-gated)
            → sees a new generation → injects ``browser_navigate`` into its live
              MCP session, exactly as the active pump injects screenshots

Read-only by construction: the panel can name a page, and gets pixels back. There
is no input channel to the mirrored page, which is what keeps this out of
reverse-proxy territory (a proxy on the dashboard's own origin would make the
previewed app same-origin with the dashboard and hand it the auth cookie).

Two invariants the tests pin:

* **Loopback targets only.** A want names a page on the gateway host. Anything
  else already renders in the iframe or the native view, so accepting it would
  add a way to make the gateway's browser fetch arbitrary URLs for no gain.
* **Wants expire.** The panel refreshes its want while it is open, so closing the
  tab (or losing the network) stops the mirror on its own rather than leaving the
  browser pumping frames at a page nobody is watching.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

# Hosts that name the machine the gateway runs on. Mirrors the frontend's
# ``isLoopbackHost`` (WebPreviewPanel.tsx), including the reserved ``.localhost``
# TLD the desktop shell uses for its own aliases.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"})

#: How long a want stays live without a refresh. The panel heartbeats well inside
#: this, so the ceiling only decides how long the mirror keeps running after the
#: watcher goes away (tab closed, laptop asleep, network dropped).
WANT_TTL_SECS = 45.0

#: Upper bound on a stored URL. Long enough for a dev-server URL with query
#: parameters, short enough that the registry cannot be grown into a memory sink
#: by a caller that can already authenticate.
_MAX_URL_LEN = 2048


def is_loopback_host(host: str) -> bool:
    """True when *host* names the machine the gateway runs on.

    Accepts the ``*.localhost`` reserved TLD as well as the literal loopback
    names, so ``kirocrew.localhost`` (the desktop shell's alias) is recognized.
    """
    h = (host or "").strip().lower()
    if not h:
        return False
    if h in _LOOPBACK_HOSTS:
        return True
    return h == "localhost" or h.endswith(".localhost")


def normalize_target(raw: str) -> str | None:
    """Validate *raw* as a previewable loopback URL, or return ``None``.

    Fail-closed on everything that is not plainly a loopback ``http(s)`` page:
    a non-http scheme (``file:``, ``data:``, ``javascript:``), a non-loopback
    host, embedded credentials (which would be handed to the browser and could
    end up in a frame), or an over-long value. The result is the URL to navigate;
    it is returned rather than a boolean so the caller stores exactly what was
    validated.
    """
    if not raw or len(raw) > _MAX_URL_LEN:
        return None
    candidate = raw.strip()
    if not candidate or any(c in candidate for c in ("\n", "\r", "\t", " ")):
        return None
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    # Credentials in the authority would be passed to the browser; a preview
    # never needs them and they have no business in a stored want.
    if parts.username or parts.password:
        return None
    if not is_loopback_host(parts.hostname or ""):
        return None
    # A port that does not parse raises in ``.port``; treat it as malformed.
    try:
        parts.port
    except ValueError:
        return None
    return candidate


@dataclass(frozen=True)
class PreviewWant:
    """A page a panel is currently asking the gateway's browser to mirror.

    ``generation`` is what the proxy compares against: it increments on every
    NEW target, so a refresh of the same URL extends the deadline without
    re-navigating (which would reset scroll position and re-run the page).
    """

    url: str
    generation: int
    expires_at: float


class PreviewRegistry:
    """Per-session preview wants, with expiry. Thread-safe.

    Small and synchronous on purpose: it is read from the loopback poll route
    (per proxy tick) and written from the dashboard route, so the whole thing is
    a dict behind a lock rather than anything that could block the event loop.
    """

    def __init__(self, ttl_secs: float = WANT_TTL_SECS) -> None:
        self._ttl = ttl_secs
        self._lock = threading.Lock()
        self._wants: dict[str, PreviewWant] = {}
        self._generation = 0

    def set_want(self, session_key: str, url: str, *, now: float | None = None) -> PreviewWant | None:
        """Record (or refresh) *session_key*'s want. ``None`` if *url* is unusable.

        Refreshing an unchanged URL keeps the existing generation so the proxy
        does not re-navigate; only a different target bumps it. Generations come
        from a registry-wide counter rather than a per-session one so a stale
        proxy tick can never see a generation it has already acted on.
        """
        target = normalize_target(url)
        if not target or not session_key:
            return None
        stamp = time.monotonic() if now is None else now
        with self._lock:
            self._prune_locked(stamp)
            current = self._wants.get(session_key)
            if current is not None and current.url == target:
                refreshed = PreviewWant(target, current.generation, stamp + self._ttl)
            else:
                self._generation += 1
                refreshed = PreviewWant(target, self._generation, stamp + self._ttl)
            self._wants[session_key] = refreshed
            return refreshed

    def get_want(self, session_key: str, *, now: float | None = None) -> PreviewWant | None:
        """The live want for *session_key*, or ``None`` when absent or expired."""
        if not session_key:
            return None
        stamp = time.monotonic() if now is None else now
        with self._lock:
            self._prune_locked(stamp)
            return self._wants.get(session_key)

    def clear_want(self, session_key: str) -> bool:
        """Drop *session_key*'s want. True when one was present."""
        with self._lock:
            return self._wants.pop(session_key, None) is not None

    def _prune_locked(self, now: float) -> None:
        expired = [key for key, want in self._wants.items() if want.expires_at <= now]
        for key in expired:
            del self._wants[key]


#: Process-wide registry. One browser per gateway, so one registry.
_REGISTRY = PreviewRegistry()


def registry() -> PreviewRegistry:
    """The gateway's preview registry."""
    return _REGISTRY
