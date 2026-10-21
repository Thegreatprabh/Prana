"""Tests for prana.setup — instant installer. All offline, tmp HOME."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from prana import config, setup
from prana.providers.base import ProviderError


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """Redirect Path.home() (and thus ~/.prana) into a tmp dir."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Neutralize binary detection: tests control detection via config files.
    monkeypatch.setattr(setup.shutil, "which", lambda *a, **k: None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    return tmp_path


# -- API key -----------------------------------------------------------------

def test_save_key_roundtrip_detects_provider(tmp_home):
    name, path = setup.save_api_key("sk-ant-test123")
    assert name == "anthropic"
    assert path.exists()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert config.get_key("anthropic") == "sk-ant-test123"
    assert setup.key_configured() == "anthropic"


def test_save_key_gemini_and_openai_prefixes(tmp_home):
    assert setup.save_api_key("AIzaABC123")[0] == "gemini"
    assert setup.save_api_key("sk-xyz789")[0] == "openai"


def test_save_key_unknown_prefix_needs_provider(tmp_home):
    with pytest.raises(ProviderError):
        setup.save_api_key("weird-prefix-key")
    # explicit provider overrides sniffing
    name, _ = setup.save_api_key("weird-prefix-key", provider="openai")
    assert name == "openai"


def test_save_key_rejects_empty(tmp_home):
    with pytest.raises(ProviderError):
        setup.save_api_key("   ")


def test_key_never_echoed_in_output(tmp_home, capsys):
    rc = setup.main(["--key", "sk-ant-SUPERSECRET999", "--yes",
                     "--skip-agents", "--skip-channels"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "SUPERSECRET999" not in out
    assert "sk-ant-" not in out  # not even the prefix


# -- JSON merge -----------------------------------------------------------------

def test_json_merge_preserves_existing_servers(tmp_home):
    cursor_cfg = tmp_home / ".cursor" / "mcp.json"
    cursor_cfg.parent.mkdir(parents=True)
    cursor_cfg.write_text(json.dumps({
        "mcpServers": {"other": {"command": "other-cmd", "args": []}},
        "otherTopLevel": True,
    }))
    items = setup.plan_agents(["cursor"])
    report = setup.apply_agents(items)
    assert any("configured" in r for r in report)
    data = json.loads(cursor_cfg.read_text())
    assert data["mcpServers"]["other"] == {"command": "other-cmd", "args": []}
    assert data["otherTopLevel"] is True
    prana = data["mcpServers"]["prana"]
    assert prana["args"] == ["-m", "prana.mcp_server"]
    assert os.path.isabs(prana["command"])  # full python path, not bare
    assert os.path.basename(prana["command"]).startswith("python")


def test_json_merge_tolerates_corrupt_file(tmp_home):
    bad = tmp_home / ".gemini" / "settings.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{not valid json")
    items = setup.plan_agents(["gemini"])
    setup.apply_agents(items)
    data = json.loads(bad.read_text())
    assert "prana" in data["mcpServers"]


# -- TOML append -----------------------------------------------------------------

def test_toml_append_preserves_and_is_idempotent(tmp_home):
    toml_path = tmp_home / ".codex" / "config.toml"
    toml_path.parent.mkdir(parents=True)
    toml_path.write_text('[mcp_servers.other]\ncommand = "/usr/bin/other"\n')
    items = setup.plan_agents(["codex"])
    setup.apply_agents(items)
    text = toml_path.read_text()
    assert '[mcp_servers.other]' in text
    assert '[mcp_servers.prana]' in text
    assert 'args = ["-m", "prana.mcp_server"]' in text
    # valid TOML (tomllib is stdlib on 3.11+; on 3.10 the parse check is
    # skipped -- the no-tomllib fallback path has its own test below)
    try:
        import tomllib
    except ModuleNotFoundError:
        tomllib = None
    if tomllib is not None:
        data = tomllib.loads(text)
        assert data["mcp_servers"]["prana"]["args"] == ["-m", "prana.mcp_server"]
    # second run: no duplicate block
    setup.apply_agents(setup.plan_agents(["codex"]))
    assert toml_path.read_text().count("[mcp_servers.prana]") == 1


def test_toml_append_without_tomllib_falls_back(tmp_home, monkeypatch):
    monkeypatch.setattr(setup, "tomllib", None)
    toml_path = tmp_home / ".codex" / "config.toml"
    items = setup.plan_agents(["codex"])
    setup.apply_agents(items)
    assert "[mcp_servers.prana]" in toml_path.read_text()


# -- dry-run -----------------------------------------------------------------

def test_dry_run_writes_nothing(tmp_home, capsys):
    items = setup.plan_agents(["cursor", "codex", "claude"])
    report = setup.apply_agents(items, dry_run=True)
    assert all("dry-run" in r for r in report)
    assert not (tmp_home / ".cursor").exists()
    assert not (tmp_home / ".codex").exists()
    assert not (tmp_home / ".claude.json").exists()


# -- agent selection -----------------------------------------------------------------

def test_parse_agents_all_and_unknown():
    keys = setup._parse_agents("all")
    assert set(keys) == set(setup.AGENTS)
    assert setup._parse_agents("cursor,codex") == ["cursor", "codex"]
    with pytest.raises(ValueError):
        setup._parse_agents("nope")


def test_antigravity_is_manual_only(tmp_home, capsys):
    items = setup.plan_agents(["antigravity"])
    assert items[0][2] is None  # no path — never written
    report = setup.apply_agents(items)
    assert "manual" in report[0].lower()
    assert "mcpServers" in report[0]  # the JSON block is shown
    # nothing written anywhere sensible
    assert not list(tmp_home.iterdir())


def test_detect_agents(tmp_home, monkeypatch):
    assert setup.detect_agents() == []
    (tmp_home / ".cursor").mkdir()
    (tmp_home / ".cursor" / "mcp.json").write_text("{}")
    assert "cursor" in setup.detect_agents()


# -- main() end to end -----------------------------------------------------------------

def test_main_noninteractive_full_flow(tmp_home, capsys):
    rc = setup.main(["--key", "sk-ant-e2e123", "--agents", "cursor,codex",
                     "--yes", "--skip-channels"])
    assert rc == 0
    assert config.get_key("anthropic") == "sk-ant-e2e123"
    cursor_data = json.loads((tmp_home / ".cursor" / "mcp.json").read_text())
    assert "prana" in cursor_data["mcpServers"]
    assert "[mcp_servers.prana]" in (tmp_home / ".codex" / "config.toml").read_text()
    out = capsys.readouterr().out
    assert "sk-ant-e2e123" not in out


def test_main_bad_key_exits_2(tmp_home):
    rc = setup.main(["--key", "bogus", "--yes", "--skip-agents", "--skip-channels"])
    assert rc == 2


def test_main_skip_everything(tmp_home, capsys):
    rc = setup.main(["--skip-key", "--skip-agents", "--skip-channels", "--yes"])
    assert rc == 0
    assert capsys.readouterr().out.count("done") >= 1


# -- --check mode -----------------------------------------------------------------

def test_check_all_green(tmp_home, capsys):
    setup.save_api_key("sk-ant-check1")
    (tmp_home / ".cursor").mkdir(parents=True)
    (tmp_home / ".cursor" / "mcp.json").write_text(json.dumps({
        "mcpServers": {"prana": {"command": "/usr/bin/python",
                                 "args": ["-m", "prana.mcp_server"]}}
    }))
    rc = setup.run_check()
    assert rc == 0
    out = capsys.readouterr().out
    assert "All green" in out


def test_check_missing_key_fails(tmp_home, capsys):
    rc = setup.run_check()
    assert rc == 1
    assert "no API key" in capsys.readouterr().out


def test_check_flags_bad_json(tmp_home, capsys):
    setup.save_api_key("sk-ant-check2")
    (tmp_home / ".cursor").mkdir(parents=True)
    (tmp_home / ".cursor" / "mcp.json").write_text("{broken")
    rc = setup.run_check()
    assert rc == 1
    assert "invalid JSON" in capsys.readouterr().out


def test_check_flags_missing_prana_entry(tmp_home, capsys):
    setup.save_api_key("sk-ant-check3")
    (tmp_home / ".cursor").mkdir(parents=True)
    (tmp_home / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {}}))
    rc = setup.run_check()
    assert rc == 1
    assert "MISSING" in capsys.readouterr().out


def test_check_toml_entry(tmp_home, capsys):
    setup.save_api_key("sk-ant-check4")
    (tmp_home / ".codex").mkdir(parents=True)
    (tmp_home / ".codex" / "config.toml").write_text(
        '[mcp_servers.prana]\ncommand = "/usr/bin/python"\n')
    rc = setup.run_check()
    assert rc == 0


# -- channels summary -----------------------------------------------------------------

def test_channels_summary_mentions_commands():
    text = setup.channels_summary()
    assert "prana-telegram" in text
    assert "@BotFather" in text
    assert "WhatsApp" in text
    assert "Discord" in text
