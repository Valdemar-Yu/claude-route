import io
import os
from types import SimpleNamespace

import pytest

from claude_route import cli
from claude_route import core
from claude_route.core import Store


class _OsView:
    """The real os module with a forced ``name`` for platform-branch tests."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __getattr__(self, name):
        return getattr(os, name)


def _cli_sees_platform(monkeypatch, platform: str) -> None:
    """Make cli.default_state() take ``platform``'s branch on any host.

    The implementation currently gates on ``sys.platform``, while older
    revisions used ``os.name``; forcing both keeps the test host-independent.
    """
    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(cli, "os", _OsView("nt" if platform == "win32" else "posix"))


def test_accounts_tsv_three_column_and_legacy_formats(tmp_path):
    base = tmp_path / "subscriptions"
    base.mkdir()
    (base / "accounts.tsv").write_text(
        "new\tNew label\tClaude Code Subscription: claude-new\n"
        "legacy\tignored\tLegacy label\n"
        "short\tShort label\n",
        "utf-8",
    )

    accounts = Store(base).load_accounts()

    assert [(a.slug, a.label, a.service) for a in accounts] == [
        ("legacy", "Legacy label", ""),
        ("new", "New label", "Claude Code Subscription: claude-new"),
        ("short", "Short label", ""),
    ]


def test_default_base_environment_priority(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_SUBSCRIPTIONS_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_SUBSCRIPTIONS_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert cli.default_base() == tmp_path / "xdg" / "claude-subscriptions"

    explicit = tmp_path / "explicit"
    monkeypatch.setenv("CLAUDE_SUBSCRIPTIONS_DIR", str(explicit))
    assert cli.default_base() == explicit

    monkeypatch.setenv("CLAUDE_SUBSCRIPTIONS_CONFIG_DIR", str(tmp_path / "wrong"))
    assert cli.default_base() == explicit


def test_store_environment_overrides(tmp_path, monkeypatch):
    base = tmp_path / "base"
    base.mkdir()
    accounts_file = tmp_path / "accounts.tsv"
    usage_dir = tmp_path / "custom-usage"
    accounts_file.write_text(
        "00\tAccount\tClaude Code Subscription: claude-00\n", "utf-8"
    )
    monkeypatch.setenv("CLAUDE_SUBSCRIPTIONS_FILE", str(accounts_file))
    monkeypatch.setenv("CLAUDE_SUBSCRIPTIONS_USAGE_DIR", str(usage_dir))

    store = Store(base)

    assert store.accounts_file == accounts_file
    assert store.usage_dir == usage_dir
    assert store.usage_path("00") == usage_dir / "00.json"
    assert store.load_accounts()[0].service == "Claude Code Subscription: claude-00"


@pytest.mark.parametrize(
    "platform, env_var, home_parts",
    [
        ("linux", "XDG_STATE_HOME", (".local", "state")),
        ("darwin", "XDG_STATE_HOME", (".local", "state")),
        ("win32", "LOCALAPPDATA", ("AppData", "Local")),
    ],
)
def test_default_state_uses_new_name_and_legacy_fallback(
    tmp_path, monkeypatch, platform, env_var, home_parts
):
    root = tmp_path / "state"
    root.mkdir()
    other_var = "LOCALAPPDATA" if env_var == "XDG_STATE_HOME" else "XDG_STATE_HOME"
    monkeypatch.delenv(other_var, raising=False)
    monkeypatch.setenv(env_var, str(root))
    _cli_sees_platform(monkeypatch, platform)

    assert cli.default_state() == root / "claude-route"

    legacy = root / "claude-pool-monitor"
    legacy.mkdir()
    assert cli.default_state() == legacy

    (root / "claude-route").mkdir()
    assert cli.default_state() == root / "claude-route"

    # Without the platform variable the code falls back to Path.home(); pin
    # that too so the test never reads the host's HOME/USERPROFILE.
    home = tmp_path / "home"
    monkeypatch.delenv(env_var)
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: home))
    assert cli.default_state() == home.joinpath(*home_parts, "claude-route")


def test_token_for_uses_macos_keychain(monkeypatch, tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    (base / "accounts.tsv").write_text(
        "00\tAccount\tClaude Code Subscription: claude-00\n", "utf-8"
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="keychain-token\n")

    monkeypatch.setattr(core, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setenv("USER", "tester")
    monkeypatch.setattr(core.subprocess, "run", fake_run)

    account = Store(base).load_accounts()[0]
    assert Store(base).token_for(account) == "keychain-token"
    assert calls[0][0] == [
        "/usr/bin/security",
        "find-generic-password",
        "-a",
        "tester",
        "-s",
        "Claude Code Subscription: claude-00",
        "-w",
    ]


def test_keychain_is_not_called_off_macos(monkeypatch):
    def fail_run(*args, **kwargs):
        raise AssertionError("security must not run off macOS")

    monkeypatch.setattr(core, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(core.subprocess, "run", fail_run)
    assert core.keychain_token("Claude Code Subscription: claude-00") == ""


@pytest.mark.skipif(os.name == "nt", reason="POSIX terminal APIs are unavailable")
def test_posix_read_keys_parses_escape_sequences_without_a_tty(monkeypatch):
    # The parser only needs a readable descriptor, so a pipe keeps this test
    # runnable in sandboxes that refuse to allocate a pty.
    pytest.importorskip("termios")

    cli.restore_keyboard()
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(cli, "_posix_keyboard_fd", read_fd)
    try:
        os.write(write_fd, b"\x1b[")
        assert cli.read_keys() == []
        os.write(write_fd, b"Aq")
        assert cli.read_keys() == ["q"]
        os.write(write_fd, b"r")
        assert cli.read_keys() == ["r"]
    finally:
        cli.restore_keyboard()
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.skipif(os.name == "nt", reason="POSIX terminal APIs are unavailable")
def test_posix_read_keys_discards_escape_sequences_and_restores_terminal(monkeypatch):
    pty = pytest.importorskip("pty")
    termios = pytest.importorskip("termios")

    cli.restore_keyboard()
    try:
        master, slave = pty.openpty()
    except OSError as exc:
        pytest.skip(f"this environment cannot allocate a pty: {exc}")
    stdin = os.fdopen(slave, "r", buffering=1)
    monkeypatch.setattr(cli.sys, "stdin", stdin)
    before = termios.tcgetattr(slave)
    try:
        assert cli.start_keyboard() is True
        changed = termios.tcgetattr(slave)
        assert changed[3] & termios.ICANON == 0

        os.write(master, b"\x1b[")
        assert cli.read_keys() == []
        os.write(master, b"Aq")
        assert cli.read_keys() == ["q"]

        os.write(master, b"r")
        assert cli.read_keys() == ["r"]
    finally:
        cli.restore_keyboard()
        restored = termios.tcgetattr(slave)
        stdin.close()
        os.close(master)
    # macOS may set PENDIN while the tty driver returns to canonical mode.
    pendin = getattr(termios, "PENDIN", 0)
    if pendin:
        restored[3] &= ~pendin
        before[3] &= ~pendin
    assert restored == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX terminal APIs are unavailable")
def test_posix_read_keys_ignores_non_tty(monkeypatch):
    cli.restore_keyboard()
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("q"))
    assert cli.read_keys() == []

