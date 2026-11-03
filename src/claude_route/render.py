"""Rich rendering for the pool dashboard."""

from __future__ import annotations

import time
from datetime import datetime

from .core import (
    BAR_EMPTY,
    BAR_FULL,
    HARD_STALE,
    LIVE_WINDOW,
    Account,
    Pool,
    fmt_duration,
    make_bar,
    severity,
    sparkline,
)


def _bar(pct: float | None, width: int, text):
    """A bar whose empty cells stay grey, so a full bar reads as one colour."""
    from rich.text import Text

    value = 0.0 if pct is None else max(0.0, min(100.0, pct))
    filled = int(round(value / 100.0 * width))
    text.append(BAR_FULL * filled, style=severity(pct))
    text.append(BAR_EMPTY * (width - filled), style="grey30")
    return text

def _account_line(pool: Pool, account: Account, now: float):
    """One text line per account for narrow terminals."""
    from rich.text import Text

    five, reset_five = account.five.effective(now)
    seven, reset_seven = account.seven.effective(now)
    state = account.state(now)

    head = Text()
    head.append(f"{account.slug:>2} ", style="grey62")
    head.append(f"{account.name:<10}", style="bold white")
    head.append(f" {state:<7}", style=Account.state_color(state))
    if account.live:
        head.append(" live", style="green")
    head.append(f"  data {fmt_duration(account.age) if account.age != float('inf') else '--'}",
                style="grey50")

    five_line = _bar(five, 12, Text("     5h "))
    five_line.append(" ")
    five_line.append("  --" if five is None else f"{five:3.0f}%", style=severity(five))
    if reset_five:
        five_line.append("  window reset", style="grey50")
    elif account.five.remaining(now) is not None:
        five_line.append(f"  reset {fmt_duration(account.five.remaining(now))}", style="grey62")

    seven_line = _bar(seven, 12, Text("     7d "))
    seven_line.append(" ")
    seven_line.append("  --" if seven is None else f"{seven:3.0f}%", style=severity(seven))
    if reset_seven:
        seven_line.append("  window reset", style="grey50")
    elif account.seven.remaining(now) is not None:
        seven_line.append(f"  reset {fmt_duration(account.seven.remaining(now))}",
                          style="grey62")
    if account.probe_error:
        seven_line.append(f"   probe: {account.probe_error}", style="yellow")

    return [head, five_line, seven_line]


def _history_line(pool: Pool, account: Account, now: float):
    from rich.text import Text

    series = pool.history.series(account.slug, now)
    estimate = pool.history.burn_rate(account.slug, now)
    line = Text()
    line.append(f"{account.slug:>2}  ", style="grey62")
    line.append(sparkline(series) or "(no history yet)", style="cyan")
    if estimate is None:
        line.append("    --", style="grey50")
    else:
        rate, span = estimate
        style = "red" if rate > 20 else ("yellow" if rate > 5 else "grey62")
        line.append(f"  {rate:+5.1f}%/h", style=style)
        line.append(f" over {fmt_duration(span)}", style="grey50")
    return line


def build_renderable(pool: Pool, prober: Prober | None, notice: str = "", width: int = 120):
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    now = time.time()
    clock = datetime.fromtimestamp(now)
    compact = width < 100

    header = Text()
    header.append(clock.strftime("%Y-%m-%d %H:%M:%S"), style="bright_cyan")
    header.append(f"   {len(pool.accounts)} accounts", style="grey62")
    live = [acc.name for acc in pool.accounts if acc.live]
    header.append("   ")
    header.append(f"live: {', '.join(live)}" if live else "live: none", style="grey62")
    if pool.offline:
        header.append("   offline", style="yellow")

    body = []
    if compact:
        for account in pool.accounts:
            body.extend(_account_line(pool, account, now))
            body.append(Text(""))
    else:
        table = Table(box=None, pad_edge=False, show_header=True,
                      header_style="bold grey62", expand=True, padding=(0, 1))
        table.add_column("#", width=2, justify="right")
        table.add_column("account", no_wrap=True, min_width=10)
        table.add_column("5h window", no_wrap=True, min_width=21)
        table.add_column("resets", no_wrap=True, min_width=8)
        table.add_column("7d window", no_wrap=True, min_width=19)
        table.add_column("resets", no_wrap=True, min_width=8)
        table.add_column("data", justify="right", no_wrap=True, min_width=5)
        table.add_column("state", no_wrap=True, min_width=7)

        for account in pool.accounts:
            five, reset_five = account.five.effective(now)
            seven, reset_seven = account.seven.effective(now)
            state = account.state(now)

            five_cell = _bar(five, 14, Text())
            five_cell.append(" ")
            five_cell.append("  --" if five is None else f"{five:3.0f}%", style=severity(five))
            if reset_five:
                five_cell.append(" ~", style="grey50")

            five_reset = "--"
            if reset_five:
                five_reset = "reset"
            elif account.five.remaining(now) is not None:
                five_reset = fmt_duration(account.five.remaining(now))

            seven_cell = _bar(seven, 12, Text())
            seven_cell.append(" ")
            seven_cell.append("  --" if seven is None else f"{seven:3.0f}%", style=severity(seven))
            if reset_seven:
                seven_cell.append(" ~", style="grey50")

            seven_reset = "--"
            if reset_seven:
                seven_reset = "reset"
            elif account.seven.remaining(now) is not None:
                seven_reset = fmt_duration(account.seven.remaining(now))

            if account.age == float("inf"):
                age_text, age_style = "--", "grey50"
            else:
                age_text = fmt_duration(account.age)
                age_style = "green" if account.age <= LIVE_WINDOW else (
                    "yellow" if account.age <= HARD_STALE else "red")

            table.add_row(
                Text(account.slug, style="grey62"),
                Text(account.name, style="bold white"),
                five_cell,
                Text(five_reset, style="grey62"),
                seven_cell,
                Text(seven_reset, style="grey62"),
                Text(age_text, style=age_style),
                Text(state, style=Account.state_color(state)),
            )
        body.append(table)

    hist_table = Table(box=None, pad_edge=False, show_header=True,
                       header_style="bold grey62", expand=not compact, padding=(0, 1))
    hist_table.add_column("5h history, last hour", no_wrap=True)
    for account in pool.accounts:
        hist_table.add_row(_history_line(pool, account, now))

    footer = Text()
    pick = pool.pick(now)
    footer.append("pick  ", style="bold grey62")
    footer.append(pick.name if pick else "none available",
                  style="bold green" if pick else "bold red")
    footer.append("      next probe  ", style="bold grey62")
    upcoming = pool.next_probe(now)
    if upcoming is None:
        footer.append("off", style="yellow")
    elif upcoming[1] <= 1:
        footer.append("eligible now", style="grey62")
    else:
        name, wait = upcoming
        footer.append(f"{name} in {fmt_duration(wait)}", style="grey62")
    footer.append("      q quit · r probe · p pause · o offline" if not compact
                  else "      q · r · p · o", style="grey50")
    if notice:
        footer.append("\n")
        footer.append(notice, style="yellow")
    if prober and prober.events:
        footer.append("\n")
        footer.append("  ".join(prober.events), style="grey50")

    return Panel(Group(header, Text(""), *body, Text(""), hist_table, Text(""), footer),
                 border_style="grey35", title="claude-route",
                 title_align="left")
