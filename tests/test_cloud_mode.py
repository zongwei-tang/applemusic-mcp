"""Cloud deployment pieces: --no-playback tool hiding and `export-token`."""

import json
import subprocess

import pytest

from applemusic_mcp import capabilities, cli, server


class TestNoPlayback:
    def test_api_level_without_playback_keeps_only_library_tools(self, monkeypatch):
        monkeypatch.setenv(server.ENV_NO_PLAYBACK, "1")
        lvl = capabilities.parse("api", None)
        assert server.hidden_tools(lvl) == {"playback", "queue"}

    def test_full_level_without_playback(self, monkeypatch):
        monkeypatch.setenv(server.ENV_NO_PLAYBACK, "1")
        assert {"playback", "queue"} <= server.hidden_tools(capabilities.parse("full", None))

    def test_default_keeps_playback(self, monkeypatch):
        monkeypatch.delenv(server.ENV_NO_PLAYBACK, raising=False)
        assert "playback" not in server.hidden_tools(capabilities.parse("api", None))

    def test_only_exact_one_enables_it(self, monkeypatch):
        monkeypatch.setenv(server.ENV_NO_PLAYBACK, "0")
        assert "playback" not in server.hidden_tools(capabilities.parse("api", None))

    def test_apply_level_removes_them_from_a_server(self, monkeypatch):
        try:
            from mcp.server.fastmcp import FastMCP
        except ModuleNotFoundError:
            from mcp.server.mcpserver import MCPServer as FastMCP

        monkeypatch.setenv(server.ENV_NO_PLAYBACK, "1")
        monkeypatch.setenv(capabilities.ENV_LEVEL, "api")
        m = FastMCP("t")
        for fn in (server.playback, server.queue, server.catalog, server.playlist):
            m.tool()(fn)
        assert server.apply_level(m) == ["playback", "queue"]


class TestExportToken:
    def _run(self, monkeypatch, argv):
        monkeypatch.setattr("sys.argv", ["applemusic-mcp", "export-token", *argv])
        with pytest.raises(SystemExit) as e:
            cli.main()
        return e.value.code

    @pytest.fixture
    def signed_in(self, mock_config_dir, monkeypatch):
        monkeypatch.delenv("APPLEMUSIC_USER_TOKEN", raising=False)
        monkeypatch.setenv("APPLEMUSIC_NO_KEYRING", "1")
        (mock_config_dir / "music_user_token.json").write_text(
            json.dumps({"music_user_token": "Amut-123"})
        )

    def test_copies_to_clipboard_without_printing(self, monkeypatch, capsys, signed_in):
        seen = {}

        def fake_run(cmd, input=None, check=False):
            seen["cmd"], seen["input"] = cmd, input

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert self._run(monkeypatch, []) == 0
        assert seen == {"cmd": ["pbcopy"], "input": b"Amut-123"}
        out = capsys.readouterr()
        assert "Amut-123" not in out.out + out.err
        assert "8 characters" in out.err

    def test_print_flag(self, monkeypatch, capsys, signed_in):
        assert self._run(monkeypatch, ["--print"]) == 0
        assert capsys.readouterr().out.strip() == "Amut-123"

    def test_clipboard_failure(self, monkeypatch, capsys, signed_in):
        def boom(*a, **k):
            raise OSError("no pbcopy")

        monkeypatch.setattr(subprocess, "run", boom)
        assert self._run(monkeypatch, []) == 1
        assert "--print" in capsys.readouterr().err

    def test_not_signed_in(self, mock_config_dir, monkeypatch, capsys):
        monkeypatch.delenv("APPLEMUSIC_USER_TOKEN", raising=False)
        monkeypatch.setenv("APPLEMUSIC_NO_KEYRING", "1")
        assert self._run(monkeypatch, []) == 1
        assert "login" in capsys.readouterr().err
