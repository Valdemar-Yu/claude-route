"""Rich rendering for the pool dashboard."""

from __future__ import annotations

import time
from datetime import datetime

from .core import (
    BAR_FULL,
    BAR_REST,
    HARD_STALE,
    LIVE_WINDOW,
    Account,
    Pool,
    fmt_duration,
    severity,
    sparkline,
)
from .theme import LIGHT_THEME, Theme

MAX_PANEL_WIDTH = 100


def _bar(pct: float | None, width: int, text, theme: Theme):
    """A two-tone solid bar: severity colour, then a pale tint of the same hue."""
    value = 0.0 if pct is None else max(0.0, min(100.0, pct))
    filled = int(round(value / 100.0 * width))
    fill = theme.severity_color(pct)
    text.append(BAR_FULL * filled, style=severity(pct, theme))
    text.append(BAR_REST * (width - filled), style=theme.bar_rest_for(fill))
    return text


def _account_line(pool: Pool, account: Account, now: float, theme: Theme):
    """The three lines of one account block."""
    from rich.text import Text

    five, reset_five = account.five.effective(now)
    seven, reset_seven = account.seven.effective(now)
    state = account.state(now)

    head = Text()
    head.append(f"{account.slug:>2} ", style=theme.secondary)
    head.append(f"{account.name:<10}", style=f"bold {theme.text}")
    head.append(f" {state:<7}", style=Account.state_color(state, theme))
    if account.live:
        head.append(" live", style=theme.ready)
    head.append(f"  data {fmt_duration(account.age) if account.age != float('inf') else '--'}",
                style=theme.muted)

    five_line = _bar(five, 12, Text("     5h ", style=theme.secondary), theme)
    five_line.append(" ")
    five_line.append("  --" if five is None else f"{five:3.0f}%", style=severity(five, theme))
    if reset_five:
        five_line.append("  window reset", style=theme.muted)
    elif account.five.remaining(now) is not None:
        five_line.append(f"  reset {fmt_duration(account.five.remaining(now))}",
                         style=theme.secondary)

    seven_line = _bar(seven, 12, Text("     7d ", style=theme.secondary), theme)
    seven_line.append(" ")
    seven_line.append("  --" if seven is None else f"{seven:3.0f}%", style=severity(seven, theme))
    if reset_seven:
        seven_line.append("  window reset", style=theme.muted)
    elif account.seven.remaining(now) is not None:
        seven_line.append(f"  reset {fmt_duration(account.seven.remaining(now))}",
                          style=theme.secondary)
    if account.probe_error:
        seven_line.append(f"   probe: {account.probe_error}", style=theme.notice)

    return [head, five_line, seven_line]


def _history_line(pool: Pool, account: Account, now: float, theme: Theme):
    from rich.text import Text

    series = pool.history.series(account.slug, now)
    estimate = pool.history.burn_rate(account.slug, now)
    line = Text()
    line.append(f"{account.slug:>2}  ", style=theme.secondary)
    if series:
        line.append(sparkline(series), style=theme.spark)
    else:
        line.append("(no history yet)", style=theme.muted)
    if estimate is None:
        line.append("    --", style=theme.muted)
    else:
        rate, span = estimate
        line.append(f"  {rate:+5.1f}%/h", style=theme.burn_style(rate))
        line.append(f" over {fmt_duration(span)}", style=theme.muted)
    return line


def _hints(pool: Pool) -> list[str]:
    """Explain an empty or probe-less board instead of leaving bare dashes."""
    hints = []
    empty = [acc for acc in pool.accounts if acc.age == float("inf")]
    if empty:
        names = ", ".join(acc.name for acc in empty)
        hints.append(f"no usage cache yet for {names}: start a session with "
                     f"{empty[0].name} (or claude-accounts launch {empty[0].slug}) "
                     "and its statusline fills it in")
    denied = [acc.name for acc in pool.accounts if acc.slug in pool.scope_denied]
    if denied:
        hints.append(f"usage API refuses {', '.join(denied)}: setup-token tokens "
                     "have no user:profile scope, so numbers come only from running sessions")
    return hints


def build_renderable(pool: Pool, prober: Prober | None, notice: str = "",
                     width: int = 120, theme: Theme | None = None):
    """Build the whole panel; every width uses the per-account block layout."""
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    theme = theme or LIGHT_THEME
    now = time.time()
    clock = datetime.fromtimestamp(now)
    panel_width = max(20, min(width, MAX_PANEL_WIDTH))
    compact = panel_width < MAX_PANEL_WIDTH

    header = Text()
    header.append(clock.strftime("%Y-%m-%d %H:%M:%S"), style=theme.clock)
    header.append(f"   {len(pool.accounts)} accounts", style=theme.secondary)
    live = [acc.name for acc in pool.accounts if acc.live]
    header.append("   ")
    header.append(f"live: {', '.join(live)}" if live else "live: none",
                  style=theme.secondary)
    if pool.offline:
        header.append("   offline", style=theme.notice)

    body = [header, Text("")]
    last = len(pool.accounts) - 1
    for index, account in enumerate(pool.accounts):
        body.extend(_account_line(pool, account, now, theme))
        if index < last:
            body.append(Text(""))
    body.append(Text(""))
    body.append(Text("5h history, last hour", style=f"bold {theme.secondary}"))
    for account in pool.accounts:
        body.append(_history_line(pool, account, now, theme))

    footer = Text()
    pick = pool.pick(now)
    footer.append("pick  ", style=f"bold {theme.secondary}")
    footer.append(pick.name if pick else "none available",
                  style=f"bold {theme.ready}" if pick else f"bold {theme.cool}")
    footer.append("      next probe  ", style=f"bold {theme.secondary}")
    upcoming = pool.next_probe(now)
    if upcoming is None and not pool.offline and pool.scope_denied:
        footer.append("n/a, statusline only", style=theme.secondary)
    elif upcoming is None:
        footer.append("off", style=theme.notice)
    elif upcoming[1] <= 1:
        footer.append("eligible now", style=theme.secondary)
    else:
        name, wait = upcoming
        footer.append(f"{name} in {fmt_duration(wait)}", style=theme.secondary)
    footer.append("      q quit · r probe · p pause · o offline" if not compact
                  else "      q · r · p · o", style=theme.muted)
    for hint in _hints(pool):
        footer.append("\n")
        footer.append(hint, style=theme.hint)
    if notice:
        footer.append("\n")
        footer.append(notice, style=theme.notice)
    if prober and prober.events:
        footer.append("\n")
        footer.append("  ".join(prober.events), style=theme.muted)

    return Panel(Group(*body, Text(""), footer),
                 border_style=theme.border,
                 title=Text("claude-route", style=theme.secondary), title_align="left",
                 width=panel_width)
