"""Tests for channel pairing auth (prana/channels/auth.py).

First-ever sender becomes the owner; everyone else is silently ignored.
Config is isolated per-test via a fake HOME.
"""

import json

import pytest

from prana import config
from prana.channels import auth as auth_mod


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _allowed_ids(home, channel):
    raw = json.loads((home / ".prana" / "config.json").read_text())
    return raw["channels"][channel]["allowed_ids"]


# -- check_sender unit tests ------------------------------------------------


def test_first_sender_becomes_owner(fake_home):
    assert auth_mod.check_sender("telegram", "111") == "owner"
    assert _allowed_ids(fake_home, "telegram") == ["111"]


def test_second_sender_denied(fake_home):
    assert auth_mod.check_sender("telegram", "111") == "owner"
    assert auth_mod.check_sender("telegram", "222") == "denied"
    # allowlist unchanged — the stranger was not added
    assert _allowed_ids(fake_home, "telegram") == ["111"]


def test_owner_allowed_on_later_messages(fake_home):
    auth_mod.check_sender("telegram", "111")
    assert auth_mod.check_sender("telegram", "111") == "allowed"


def test_channels_are_independent(fake_home):
    assert auth_mod.check_sender("telegram", "111") == "owner"
    # a different channel pairs independently
    assert auth_mod.check_sender("whatsapp", "222") == "owner"
    assert auth_mod.check_sender("whatsapp", "111") == "denied"


def test_pairing_persists_across_loads(fake_home):
    auth_mod.check_sender("discord", "999")
    # simulate a fresh process: re-read from disk
    assert config.get_channel_allowed_ids("discord") == ["999"]
    assert auth_mod.check_sender("discord", "999") == "allowed"
    assert auth_mod.check_sender("discord", "123") == "denied"


def test_check_sender_never_raises_on_broken_config(fake_home, monkeypatch):
    (fake_home / ".prana").mkdir(exist_ok=True)
    (fake_home / ".prana" / "config.json").write_text("{not json")
    # fail closed, not crash
    assert auth_mod.check_sender("telegram", "111") == "denied"


def test_ignore_unpaired_never_logs_sender_id(fake_home, capfd):
    auth_mod.ignore_unpaired("telegram")
    out, err = capfd.readouterr()
    assert "telegram" in err
    # no sender id anywhere (we never even passed one)
    assert "999" not in err and "111" not in err


# -- runner integration -----------------------------------------------------

class _FakeBot:
    def __init__(self):
        self.sent = []

    def parse_update(self, update):
        return update["chat_id"], update["text"]

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


class _FakeLoop:
    class _R:
        final_text = "done"

    def run(self, event):
        return self._R()


def _denied_scenario_stderr(runner_fn, capfd):
    _, err = capfd.readouterr()
    return err


def test_telegram_denied_sender_gets_nothing(fake_home, capfd):
    from prana.channels.run import ConversationStore, handle_update

    auth_mod.check_sender("telegram", "111")  # owner pairs first
    bot = _FakeBot()
    result = handle_update(
        bot, _FakeLoop(), ConversationStore(),
        {"chat_id": "222", "text": "rm -rf ~"},
    )
    assert result is False
    assert bot.sent == []  # no reply at all
    _, err = capfd.readouterr()
    assert "222" not in err  # stranger's id never logged
    assert "unpaired sender ignored on telegram" in err


def test_telegram_owner_gets_pairing_notice_then_service(fake_home):
    from prana.channels.run import ConversationStore, handle_update

    bot = _FakeBot()
    result = handle_update(
        bot, _FakeLoop(), ConversationStore(),
        {"chat_id": "111", "text": "/start"},
    )
    assert result is True
    # pairing notice sent first, then the /start intro
    assert bot.sent[0][1].startswith("\U0001f517 Paired!")


def test_whatsapp_denied_sender_gets_nothing(fake_home, capfd):
    from prana.channels.whatsapp_run import ConversationStore, handle_message

    class FakeClient:
        def __init__(self):
            self.sent = []

        def send_message(self, to, text):
            self.sent.append((to, text))

        def message_to_event(self, from_number, text):
            from prana import events
            return events.user_message(text, channel="whatsapp")

    auth_mod.check_sender("whatsapp", "+1555000111")
    client = FakeClient()
    result = handle_message(
        client, _FakeLoop(), ConversationStore(), "+1555000222", "hello"
    )
    assert result is False
    assert client.sent == []
    _, err = capfd.readouterr()
    assert "+1555000222" not in err
    assert "unpaired sender ignored on whatsapp" in err


def test_discord_denied_sender_gets_nothing(fake_home, capfd):
    from prana.channels.discord_run import handle_dispatch
    from prana.channels.run import ConversationStore

    class FakeClient:
        def __init__(self):
            self.sent = []

        def send_message(self, channel_id, text):
            self.sent.append((channel_id, text))

    class FakeGateway:
        bot_user_id = "bot1"

    auth_mod.check_sender("discord", "111")
    client = FakeClient()
    payload = {"channel_id": "c1", "author": {"id": "222"}, "content": "hello"}
    result = handle_dispatch(
        client, FakeGateway(), _FakeLoop(), ConversationStore(), payload
    )
    assert result is False
    assert client.sent == []
    _, err = capfd.readouterr()
    assert "222" not in err
    assert "unpaired sender ignored on discord" in err
