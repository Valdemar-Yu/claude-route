"""Colour themes and terminal-background detection for claude-route.

Every colour rendered by the TUI comes from a :class:`Theme`; the palettes use
explicit hex values so a light terminal and a dark terminal each get a readable
set instead of rich's named colours, which resolve against the terminal palette.

``auto`` detection tries ``COLORFGBG`` first, then an OSC 11 background query on
POSIX terminals, and falls back to the light theme.
"""

from __future__ import annotations

import os
import re
import select
import sys
import time
from dataclasses import dataclass

THEME_CHOICES = ("auto", "light", "dark")

OSC11_QUERY = b"\x1b]11;?\x07"
OSC11_TIMEOUT = 0.15
OSC11_MAX_BYTES = 256

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")
_RGB_RE = re.compile(r"rgb:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})")


def _rgb(color: str) -> tuple[int, int, int]:
    match = _HEX_RE.match(color.strip())
    if not match:
        raise ValueError(f"not a hex colour: {color!r}")
    value = match.group(1)
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def blend(color: str, background: str, ratio: float) -> str:
    """Mix ``color`` into ``background``; used for the bar's empty cells."""
    fore = _rgb(color)
    back = _rgb(background)
    mixed = (int(fore[i] * ratio + back[i] * (1.0 - ratio)) for i in range(3))
    return "#%02x%02x%02x" % tuple(mixed)


@dataclass(frozen=True)
class Theme:
    """One palette with the semantic roles the renderer asks for."""

    name: str
    background: str
    text: str
    secondary: str
    muted: str
    clock: str
    ready: str
    tight: str
    cool: str
    unknown: str
    severity_low: str
    severity_mid: str
    severity_high: str
    severity_full: str
    severity_none: str
    bar_filled: str
    bar_rest: str
    spark: str
    burn_low: str
    burn_mid: str
    burn_high: str
    hint: str
    notice: str
    border: str
    bar_mix: float = 0.25

    def severity_color(self, pct: float | None) -> str:
        if pct is None:
            return self.severity_none
        if pct >= 99.5:
            return self.severity_full
        if pct >= 80:
            return self.severity_high
        if pct >= 50:
            return self.severity_mid
        return self.severity_low

    def severity_style(self, pct: float | None) -> str:
        color = self.severity_color(pct)
        return f"bold {color}" if pct is not None and pct >= 99.5 else color

    def state_color(self, state: str) -> str:
        colors = {
            "READY": self.ready,
            "TIGHT": self.tight,
            "COOL": f"bold {self.cool}",
            "UNKNOWN": self.unknown,
        }
        return colors.get(state, self.text)

    def burn_style(self, rate: float) -> str:
        if rate > 20:
            return self.burn_high
        if rate > 5:
            return self.burn_mid
        return self.burn_low

    def bar_rest_for(self, color: str) -> str:
        """The pale solid-block colour paired with a severity colour."""
        try:
            return blend(color, self.background, self.bar_mix)
        except ValueError:
            return self.bar_rest


# Light values follow the reference screenshot: teal clock and sparkline,
# #0c8761 green, #39836a for the 50-80% band, dark olive for 80-100%, and a
# 25% mint tint (#c2e1d7) for the rest of the bar.
LIGHT_THEME = Theme(
    name="light",
    background="#ffffff",
    text="#1f1f1f",
    secondary="#585858",
    muted="#8a8a8a",
    clock="#198292",
    ready="#0c8761",
    tight="#81780f",
    cool="#bf3b2b",
    unknown="#8a8a8a",
    severity_low="#0c8761",
    severity_mid="#39836a",
    severity_high="#81780f",
    severity_full="#bf3b2b",
    severity_none="#8a8a8a",
    bar_filled="#0c8761",
    bar_rest="#c2e1d7",
    spark="#198292",
    burn_low="#8a8a8a",
    burn_mid="#81780f",
    burn_high="#bf3b2b",
    hint="#0f7ea8",
    notice="#8a6100",
    border="#585858",
)

# Dark keeps the old look (Campbell-ish ANSI palette) as explicit RGB values.
DARK_THEME = Theme(
    name="dark",
    background="#1b1b1b",
    text="#e5e5e5",
    secondary="#9e9e9e",
    muted="#808080",
    clock="#29b8db",
    ready="#0dbc79",
    tight="#e5e510",
    cool="#cd3131",
    unknown="#808080",
    severity_low="#0dbc79",
    severity_mid="#23d18b",
    severity_high="#e5e510",
    severity_full="#cd3131",
    severity_none="#808080",
    bar_filled="#0dbc79",
    bar_rest="#174332",
    spark="#11a8cd",
    burn_low="#9e9e9e",
    burn_mid="#e5e510",
    burn_high="#cd3131",
    hint="#11a8cd",
    notice="#e5e510",
    border="#585858",
)

THEMES = {"light": LIGHT_THEME, "dark": DARK_THEME}


def parse_colorfgbg(value: str | None) -> str | None:
    """Read the background colour number from ``COLORFGBG``."""
    if not value:
        return None
    parts = str(value).strip().split(";")
    if len(parts) < 2:
        return None
    try:
        index = int(parts[-1].strip())
    except (TypeError, ValueError):
        return None
    if 0 <= index <= 6 or index == 8:
        return "dark"
    if index == 7 or 9 <= index <= 15:
        return "light"
    return None


def parse_osc11(payload: str | bytes | None) -> str | None:
    """Parse an OSC 11 reply like ``rgb:ffff/ffff/ffff`` into a theme name."""
    if payload is None:
        return None
    if isinstance(payload, bytes):
        text = payload.decode("latin-1", "replace")
    else:
        text = str(payload)
    match = _RGB_RE.search(text)
    if not match:
        return None
    channels = []
    for part in match.groups():
        scale = float(16 ** len(part) - 1)
        channels.append(int(part, 16) / scale)
    luminance = 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]
    return "light" if luminance >= 0.5 else "dark"


def _write_osc_query(fd: int) -> None:
    try:
        os.write(fd, OSC11_QUERY)
    except OSError:
        pass


def _read_osc_reply(fd: int, timeout: float) -> str | None:
    deadline = time.monotonic() + max(0.0, timeout)
    data = bytearray()
    while len(data) < OSC11_MAX_BYTES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            ready, _, _ = select.select([fd], [], [], remaining)
        except (OSError, ValueError):
            break
        if not ready:
            break
        try:
            chunk = os.read(fd, 64)
        except OSError:
            break
        if not chunk:
            break
        data.extend(chunk)
        if b"\x07" in data or b"\x1b\\" in data:
            break
    return data.decode("latin-1", "replace") if data else None


def _drain(fd: int) -> None:
    """Drop bytes the terminal had already queued, so no reply becomes a key."""
    while True:
        try:
            ready, _, _ = select.select([fd], [], [], 0)
        except (OSError, ValueError):
            return
        if not ready:
            return
        try:
            if not os.read(fd, 64):
                return
        except OSError:
            return


def _query_osc11(stdin, stdout, timeout: float = OSC11_TIMEOUT) -> str | None:
    """Ask a POSIX terminal for its background colour, restoring termios."""
    if os.name == "nt":
        return None
    try:
        import termios
        import tty
    except ImportError:
        return None
    try:
        in_fd = stdin.fileno()
        out_fd = stdout.fileno()
        if not (os.isatty(in_fd) and os.isatty(out_fd)):
            return None
        attrs = termios.tcgetattr(in_fd)
    except (AttributeError, OSError, ValueError, termios.error):
        return None
    try:
        try:
            tty.setcbreak(in_fd)
        except (OSError, ValueError, termios.error):
            return None
        _write_osc_query(out_fd)
        reply = _read_osc_reply(in_fd, timeout)
        _drain(in_fd)
    finally:
        try:
            termios.tcsetattr(in_fd, termios.TCSADRAIN, attrs)
        except (OSError, ValueError, termios.error):
            pass
    return reply


def detect_theme(env=None, stdin=None, stdout=None,
                 timeout: float = OSC11_TIMEOUT, allow_query: bool = True) -> str:
    """Pick ``light`` or ``dark`` from COLORFGBG, OSC 11, then the light fallback."""
    env = os.environ if env is None else env
    from_var = parse_colorfgbg(env.get("COLORFGBG"))
    if from_var:
        return from_var
    if allow_query:
        reply = _query_osc11(sys.stdin if stdin is None else stdin,
                             sys.stdout if stdout is None else stdout,
                             timeout)
        from_terminal = parse_osc11(reply)
        if from_terminal:
            return from_terminal
    return "light"


def resolve_theme(choice: str | None = None, env=None, stdin=None, stdout=None,
                  timeout: float = OSC11_TIMEOUT, allow_query: bool = True) -> Theme:
    """Resolve command line > ``CLAUDE_ROUTE_THEME`` > auto detection."""
    env = os.environ if env is None else env
    if choice is None:
        choice = env.get("CLAUDE_ROUTE_THEME")
    name = (choice or "auto").strip().lower()
    if name not in THEME_CHOICES:
        name = "auto"
    if name == "auto":
        name = detect_theme(env=env, stdin=stdin, stdout=stdout,
                            timeout=timeout, allow_query=allow_query)
    return THEMES[name]
