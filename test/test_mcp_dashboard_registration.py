"""Managed-MCP registration for ``kirocrew-dashboard``, and why it is its own server.

The dashboard-control tools are deliberately NOT in ``kirocrew-core``. Core is the
surface every session carries and kiro-cli reads ``tools/list`` once per session,
so a capability the user opts into occasionally would otherwise spend context in
every request of every session. Two properties encode that decision and must not
regress:

* **The server advertises NOTHING while ``agent.dashboard_control`` is off** — the
  only shape that costs a non-user literally zero context (an always-refusing
  tool still ships its description every turn).
* **The managed spec carries NO ``autoApprove`` key.** An autoApproved MCP tool is
  approved inside kiro-cli and never reaches ``hooks.on_tool_call``, so the deny
  floor and governance ceiling would be bypassed for tools that rewrite the
  user's session layout.

The registry assertions mirror ``test_computer_use_registration.py``: a managed
server has to be named in several places, and a half-registered server is the
failure mode that test was written to prevent.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from kiro_crew import agent, mcp_cleanup, mcp_discovery, onboarding_import
from kiro_crew.config.loader import KiroCrewConfig

DASH_SERVER = "kirocrew-dashboard"
DASH_SUBCOMMAND = "mcp-dashboard"


class TestRegistryParity:
    def test_named_in_every_managed_registry(self) -> None:
        assert DASH_SERVER in agent._MANAGED_MCP_SERVERS
        assert DASH_SERVER in mcp_cleanup.KIROCREW_BIN_MCP_SERVERS
        assert mcp_discovery._MANAGED_SERVER_SUBCOMMANDS.get(DASH_SERVER) == DASH_SUBCOMMAND
        assert DASH_SERVER in mcp_discovery._MANAGED_SERVER_NAMES
        assert DASH_SERVER in onboarding_import._MANAGED_MCP_NAMES

    def test_tool_module_is_mapped_for_in_process_listing(self) -> None:
        """Discovery reads tool names in-process; an unmapped server lists zero."""
        assert (
            mcp_discovery._MANAGED_SERVER_TOOL_MODULES.get(DASH_SERVER)
            == "kiro_crew.mcp_dashboard"
        )

    def test_spec_carries_no_auto_approve(self) -> None:
        assert "autoApprove" not in agent._MANAGED_MCP_SERVERS[DASH_SERVER]

    def test_server_key_is_slash_free(self) -> None:
        """A slash in the key would be rewritten by the alias normalization pass."""
        assert "/" not in DASH_SERVER and "\\" not in DASH_SERVER


class TestTheDefaultIsSilent:
    """A fresh install must not spend context on a feature nobody asked for."""

    def test_config_default_is_off(self) -> None:
        assert KiroCrewConfig().agent.dashboard_control is False

    def test_a_non_bool_config_value_does_not_enable_it(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """`"dashboard_control": "false"` must not read as True.

        A truthy STRING is the classic fail-open on a gate: `bool("false")` is
        True. The field loads through `_safe_bool`, so any non-bool degrades to
        the default and a hand-edited or script-written config cannot silently
        grant folder writes.
        """
        import json

        monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
        cfg_file = tmp_path / "config.json"
        for bad in ("false", "true", "0", 1, ["yes"]):
            cfg_file.write_text(
                json.dumps({"agent": {"dashboard_control": bad}}), encoding="utf-8"
            )
            cfg = KiroCrewConfig.load()
            assert cfg.agent.dashboard_control is False, f"{bad!r} enabled the gate"

    def test_a_real_true_still_enables_it(self, tmp_path: Any, monkeypatch: Any) -> None:
        import json

        monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
        (tmp_path / "config.json").write_text(
            json.dumps({"agent": {"dashboard_control": True}}), encoding="utf-8"
        )
        assert KiroCrewConfig.load().agent.dashboard_control is True

    def test_tools_list_is_empty_on_a_default_install(self) -> None:
        from kiro_crew import mcp_dashboard

        with patch.object(mcp_dashboard.KiroCrewConfig, "load", staticmethod(KiroCrewConfig)):
            assert mcp_dashboard.is_enabled() is False
            assert mcp_dashboard._list_tools() == []

    def test_the_toggle_is_what_reveals_the_tools(self) -> None:
        from kiro_crew import mcp_dashboard

        cfg = KiroCrewConfig()
        cfg.agent.dashboard_control = True
        with patch.object(mcp_dashboard.KiroCrewConfig, "load", staticmethod(lambda: cfg)):
            assert mcp_dashboard.is_enabled() is True
            assert len(mcp_dashboard._list_tools()) == 4


class TestWhatThisEnableAuthorizes:
    """Nothing needing AUTHORIZATION may ride this load switch.

    `agent.dashboard_control` lives in `config.json`, which an auto-approved agent
    shell can write, so it is a context-economy switch and not a consent control.
    That is sound for the current tools — they grant no read the agent lacks
    (`list_sessions` in core already returns every session's title and key) and
    delete nothing. It stops being sound the moment a capability with real blast
    radius is added here: that one needs its own keystone leaf, the way
    `computer_use.json` and `browser-mode-enabled` do.

    This ratchet pins the set riding the switch, so adding such a capability fails
    until the author picks the right storage for its gate rather than inheriting
    one that was never meant to authorize anything.
    """

    FOLDER_TOOLS = {
        "chat_folder_tree",
        "chat_folder_create",
        "chat_folder_move",
        "chat_folder_move_session",
    }

    def test_the_enable_covers_exactly_the_folder_tools(self) -> None:
        from kiro_crew import mcp_dashboard

        assert {t["name"] for t in mcp_dashboard._tool_definitions()} == self.FOLDER_TOOLS

    def test_no_session_driving_tool_rides_this_enable(self) -> None:
        """A tool that messages/stops another session needs its own gate."""
        from kiro_crew import mcp_dashboard

        names = {t["name"] for t in mcp_dashboard._tool_definitions()}
        forbidden = {n for n in names if "message" in n or "stop" in n or "steer" in n}
        assert not forbidden, (
            f"{sorted(forbidden)} drive another session but would ride the "
            "folder-organization enable — give that class its own switch"
        )
