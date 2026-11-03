import io
import json
import os
import re
from dataclasses import fields

import pytest

from claude_route import cli, theme
from claude_route.theme import DARK_THEME, LIGHT_THEME, Theme


def test_colorfgbg_background_numbers():
    assert theme.parse_colorfgbg("15;0") == "dark"
    assert theme.parse_colorfgbg("0;15") == "light"
    assert theme.parse_colorfgbg("default;default") is None
    assert theme.parse_colorfgbg("default;8") == "dark"
    assert theme.parse_colorfgbg("default;7") == "light"
    assert theme.parse_colorfgbg("") is None
    assert theme.parse_colorfgbg(None) is None
    assert theme.parse_colorfgbg("15;99") is None


@pytest.mark.parametrize(
    "reply, expected",
    [
        ("\x1b]11;rgb:ffff/ffff/ffff\x07", "light"),
        ("\x1b]11;rgb:0000/0000/0000\x07", "dark"),
        ("\x1b]11;rgb:ff/ff/ff\x07", "light"),
        ("\x1b]11;rgb:00/00/00\x07", "dark"),
        (b"\x1b]11;rgb:ffff/ffff/ffff\x1b\\", "light"),
    ],
)
def test_osc11_reply_parsing(reply, expected):
    assert theme.parse_osc11(reply) == expected


@pytest.mark.parametrize(
    "reply",
    [None, "", "nonsense", "\x1b]11;rgb:ffff/ffff\x07",
     "\x1b]11;rgb:zz/00/00\x07", "\x1b]11;rgb:12345/0000/0000\x07"],
)
def test_osc11_malformed_replies(reply):
    assert theme.parse_osc11(reply) is None


def test_theme_fields_are_explicit_hex():
    for palette in (LIGHT_THEME, DARK_THEME):
        for item in fields(Theme):
            if item.name in ("name", "bar_mix"):
                continue
            value = getattr(palette, item.name)
            assert isinstance(value, str), (palette.name, item.name, value)
            assert re.fullmatch(r"#[0-9a-f]{6}", value), (palette.name, item.name, value)


def test_theme_role_mapping_and_bar_tint():
    assert LIGHT_THEME.severity_color(None) == LIGHT_THEME.severity_none
    assert LIGHT_THEME.severity_color(10) == LIGHT_THEME.severity_low
    assert LIGHT_THEME.severity_color(60) == LIGHT_THEME.severity_mid
    assert LIGHT_THEME.severity_color(85) == LIGHT_THEME.severity_high
    assert LIGHT_THEME.severity_color(100) == LIGHT_THEME.severity_full
    assert LIGHT_THEME.severity_style(100).startswith("bold ")
    assert LIGHT_THEME.state_color("READY") == LIGHT_THEME.ready
    assert LIGHT_THEME.state_color("COOL").startswith("bold ")
    assert LIGHT_THEME.bar_rest_for("#0c8761") == "#c2e1d7"
    assert DARK_THEME.bar_rest_for(DARK_THEME.bar_filled) == DARK_THEME.bar_rest


def test_explicit_choice_beats_environment(monkeypatch):
    monkeypatch.setenv("CLAUDE_ROUTE_THEME", "dark")
    assert theme.resolve_theme("light").name == "light"
    assert theme.resolve_theme(None).name == "dark"


def test_environment_beats_auto_detection(monkeypatch):
    monkeypatch.setenv("CLAUDE_ROUTE_THEME", "dark")
    monkeypatch.setenv("COLORFGBG", "0;15")
    assert theme.resolve_theme(None).name == "dark"
    assert theme.resolve_theme("auto").name == "light"


def test_auto_without_a_tty_is_light_and_never_queries(monkeypatch):
    calls = []

    def fake_write(fd):
        calls.append(fd)

    monkeypatch.setattr(theme, "_write_osc_query", fake_write)
    resolved = theme.resolve_theme("auto", env={}, stdin=io.StringIO(),
                                   stdout=io.StringIO())
    assert resolved.name == "light"
    assert calls == []


def test_auto_uses_colorfgbg_before_the_terminal(monkeypatch):
    calls = []
    monkeypatch.setattr(theme, "_write_osc_query", lambda fd: calls.append(fd))
    resolved = theme.resolve_theme("auto", env={"COLORFGBG": "15;0"},
                                   stdin=io.StringIO(), stdout=io.StringIO())
    assert resolved.name == "dark"
    assert calls == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX terminal APIs are unavailable")
def test_osc11_query_restores_termios(monkeypatch):
    import termios
    import tty

    calls = []
    attrs = [1, 2, 3, 4]

    class FakeStream:
        def __init__(self, fd):
            self.fd = fd

        def fileno(self):
            return self.fd

    monkeypatch.setattr(theme.os, "isatty", lambda fd: True)
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: attrs)
    monkeypatch.setattr(termios, "tcsetattr",
                        lambda fd, when, value: calls.append(("restore", fd, value)))
    monkeypatch.setattr(tty, "setcbreak", lambda fd: calls.append(("cbreak", fd)))
    monkeypatch.setattr(theme, "_write_osc_query", lambda fd: calls.append(("write", fd)))
    monkeypatch.setattr(theme, "_read_osc_reply",
                        lambda fd, timeout: "\x1b]11;rgb:ffff/ffff/ffff\x07")
    monkeypatch.setattr(theme, "_drain", lambda fd: calls.append(("drain", fd)))

    reply = theme._query_osc11(FakeStream(10), FakeStream(11))
    assert reply == "\x1b]11;rgb:ffff/ffff/ffff\x07"
    assert calls[0] == ("cbreak", 10)
    assert calls[1] == ("write", 11)
    assert ("drain", 10) in calls
    assert calls[-1] == ("restore", 10, attrs)


def test_auto_falls_back_to_light_when_the_query_is_empty(monkeypatch):
    monkeypatch.setattr(theme, "_query_osc11", lambda *args, **kwargs: None)
    assert theme.detect_theme(env={}) == "light"


@pytest.mark.skipif(os.name == "nt", reason="POSIX terminal APIs are unavailable")
def test_read_keys_discards_osc_replies(monkeypatch):
    cli.restore_keyboard()
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(cli, "_posix_keyboard_fd", read_fd)
    try:
        os.write(write_fd, b"\x1b]11;rgb:ffff/0000/0000\x07q")
        assert cli.read_keys() == ["q"]
        os.write(write_fd, b"\x1b]11;rgb:0000/0000/0000\x1b\\r")
        assert cli.read_keys() == ["r"]
    finally:
        cli.restore_keyboard()
        os.close(read_fd)
        os.close(write_fd)


def test_cli_forwards_theme_flag(monkeypatch, tmp_path):
    base = tmp_path / "base"
    (base / "usage").mkdir(parents=True)
    (base / "accounts.tsv").write_text(
        "00\t00\tClaude Code Subscription: claude-00\n", "utf-8")
    (base / "usage" / "00.json").write_text(json.dumps({"rate_limits": {
        "five_hour": {"used_percentage": 10},
    }}), "utf-8")

    seen = {}
    real = cli.resolve_theme

    def fake(choice=None, **kwargs):
        seen["choice"] = choice
        return real(choice, **kwargs)

    monkeypatch.setattr(cli, "resolve_theme", fake)
    code = cli.main(["--once", "--offline", "--width", "80", "--theme", "dark",
                     "--base", str(base), "--state-dir", str(tmp_path / "state")])
    assert code == 0
    assert seen["choice"] == "dark"
