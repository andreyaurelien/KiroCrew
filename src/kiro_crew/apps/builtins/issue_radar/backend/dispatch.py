"""Issue Radar — dispatch readiness (RFC phase 0).

Issue Radar reads everything through the user's own provider CLI and needs no
local clone. Asking an agent to *implement* an issue does need one, so a
connected repo carries an optional local checkout path and dispatch is gated on
it.

This module owns two things and no I/O beyond stat:

* :func:`resolve_checkout` — validate a user-supplied path, or refuse it.
* :func:`readiness` — turn a stored path into a ready flag plus a reason a UI
  can render without re-deriving the rule.

Nothing here runs an agent or touches git. The gate exists first, on its own, so
that the phase which does run an agent has no judgement left to make.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from kiro_crew.security import is_sensitive_path

    _HAS_SECURITY = True
except Exception:  # pragma: no cover - security module always present in prod
    _HAS_SECURITY = False

    def is_sensitive_path(path: str) -> bool:  # type: ignore[misc]
        """Fail CLOSED when the security module is unavailable.

        This decides whether an agent may be pointed at a directory. Without the
        module we cannot make that judgement, so refuse every path rather than
        admit all of them.
        """
        return True


#: Dispatch can proceed as far as this gate is concerned.
REASON_OK = "ok"
#: The repo has no local checkout recorded yet.
REASON_NO_LOCAL_PATH = "no_local_path"
#: A path is recorded but no longer validates (moved, deleted, or no longer a
#: git checkout). Deliberately distinct from :data:`REASON_NO_LOCAL_PATH`: one
#: asks the user to set a value, the other tells them the value they set broke.
REASON_CHECKOUT_UNUSABLE = "checkout_unusable"


def resolve_checkout(raw: str) -> Path | None:
    """Return *raw* as a usable git work-tree root, or ``None`` if it is not one.

    The rules, in order, each of which has a reason:

    * ``~`` expanded and symlinks resolved BEFORE the sensitivity test, so a
      symlink planted in a benign directory cannot smuggle its target past it.
    * Must be absolute, asserted on the EXPANDED INPUT and before ``realpath``.
      ``realpath`` resolves a relative value against the gateway's own cwd and
      always returns an absolute path, so testing afterwards can never fail and
      the guarantee would be vacuous.
    * Must not be a sensitive path (credential stores, ``.ssh``, ``.aws``, the
      governance policy files) per :func:`kiro_crew.security.is_sensitive_path`.
    * Must be an existing directory holding a ``.git`` entry. ``.git`` may be a
      directory (an ordinary clone) or a FILE (a linked worktree's ``gitdir:``
      pointer), so both are accepted; a bare repository has neither and is
      refused, which is correct — it has no work tree to edit.
    """
    if not raw or not raw.strip():
        return None
    expanded = os.path.expanduser(raw.strip())
    if not os.path.isabs(expanded):
        return None
    resolved = Path(os.path.realpath(expanded))
    if is_sensitive_path(str(resolved)):
        return None
    if not resolved.is_dir():
        return None
    if not (resolved / ".git").exists():
        return None
    return resolved


def readiness(local_path: str | None) -> tuple[bool, str]:
    """Whether dispatch may proceed for a repo whose stored path is *local_path*.

    Re-validates on every read rather than trusting the stored value: a checkout
    that was deleted or moved after being recorded must not keep reporting ready,
    for the same reason a check that never ran must not render as a check that
    passed.
    """
    if not local_path or not str(local_path).strip():
        return False, REASON_NO_LOCAL_PATH
    if resolve_checkout(str(local_path)) is None:
        return False, REASON_CHECKOUT_UNUSABLE
    return True, REASON_OK
