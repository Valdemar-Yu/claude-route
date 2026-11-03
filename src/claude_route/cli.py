"""Command line entry point: snapshot modes and the interactive loop."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from .core import History, Pool, Store, configure_glyphs, enable_utf8_console
from .render import build_renderable

def print_snapshot(pool: Pool, as_json: bool, width: int = 0) -> None:
    from rich.console import Console

    pool.refresh()
    now = time.time()
    if as_json:
        payload = []
        for account in pool.accounts:
            five, reset_five = account.five.effective(now)
            seven, reset_seven = account.seven.effective(now)
            payload.append({
                "slug": account.slug,
                "name": account.name,
                "five": None if five is None else round(five, 1),
                "five_resets_at": account.five.resets_at.isoformat()
                if account.five.resets_at else None,
                "five_reset_passed": reset_five,
                "seven": None if seven is None else round(seven, 1),
                "seven_resets_at": account.seven.resets_at.isoformat()
                if account.seven.resets_at else None,
                "seven_reset_passed": reset_seven,
                "age_seconds": None if account.age == float("inf") else round(account.age),
                "state": account.state(now),
                "live": account.live,
                "source": account.five.source,
            })
        pick = pool.pick(now)
        print(json.dumps({"generated_at": datetime.now().astimezone().isoformat(),
                          "accounts": payload,
                          "pick": pick.slug if pick else None},
                         indent=2, ensure_ascii=False))
        return
    console = Console(width=width or None)
    console.print(build_renderable(pool, None, width=width or console.width))

def read_keys() -> list[str]:
    if os.name != "nt":
        return []
    try:
        import msvcrt
    except ImportError:
        return []
    keys = []
    while msvcrt.kbhit():
        try:
            keys.append(msvcrt.getwch())
        except OSError:
            break
    return keys



def default_base() -> Path:
    for env in ("CLAUDE_SUBSCRIPTIONS_CONFIG_DIR", "CLAUDE_SUBSCRIPTIONS_DIR"):
        value = os.environ.get(env)
        if value:
            return Path(value).expanduser()
    return Path.home() / ".config" / "claude-subscriptions"


def default_state() -> Path:
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    base = Path(root).expanduser() if root else Path.home() / ".local" / "state"
    return base / "claude-pool-monitor"


def main(argv: list[str] | None = None) -> int:
    enable_utf8_console()
    configure_glyphs()

    parser = argparse.ArgumentParser(
        prog="pool_monitor.py",
        description="Live 5h/7d quota view for a claude-accounts pool.")
    parser.add_argument("--base", type=Path, default=None,
                        help="claude-subscriptions directory")
    parser.add_argument("--state-dir", type=Path, default=None,
                        help="where probe state and history are stored")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="redraw interval in seconds (default 1.0)")
    parser.add_argument("--offline", action="store_true",
                        help="never call the usage endpoint")
    parser.add_argument("--once", action="store_true", help="print one snapshot and exit")
    parser.add_argument("--json", action="store_true", help="print one JSON snapshot and exit")
    parser.add_argument("--probe", action="store_true",
                        help="run one probe round now, then a snapshot")
    parser.add_argument("--width", type=int, default=0, help="force layout width (testing)")
    parser.add_argument("--frames", type=int, default=0, help="stop after N frames (testing)")
    args = parser.parse_args(argv)

    try:
        import rich  # noqa: F401
    except ImportError:
        print("this tool needs the 'rich' package: python -m pip install rich",
              file=sys.stderr)
        return 2

    base = args.base or default_base()
    if not base.is_dir():
        print(f"claude-subscriptions directory not found: {base}", file=sys.stderr)
        return 2

    store = Store(base)
    state_dir = args.state_dir or default_state()
    history = History(state_dir / "history.jsonl")
    pool = Pool(store, history, state_dir)
    pool.offline = args.offline

    if not pool.accounts:
        print(f"no accounts found under {base}", file=sys.stderr)
        return 2

    if args.probe:
        pool.refresh()
        for account in pool.accounts:
            if pool.should_probe(account, time.time()):
                print(f"probe {account.name}: {pool.probe(account)}")
            else:
                print(f"probe {account.name}: skipped (budget or fresh cache)")

    if args.once or args.json:
        print_snapshot(pool, args.json, args.width)
        return 0

    from rich.console import Console
    from rich.live import Live

    console = Console()
    prober = None if args.offline else Prober(pool)
    if prober:
        prober.start()

    notice = ""
    paused = False
    frames = 0
    try:
        with Live(build_renderable(pool, prober, width=args.width or console.width),
                  console=console, refresh_per_second=4, screen=True) as live:
            while True:
                for key in read_keys():
                    if key in ("q", "Q", "\x03"):
                        raise KeyboardInterrupt
                    if key in ("p", "P"):
                        paused = not paused
                        notice = "paused" if paused else ""
                    elif key in ("o", "O"):
                        pool.offline = not pool.offline
                        notice = "offline: cache only" if pool.offline else "online"
                    elif key in ("r", "R"):
                        if pool.offline:
                            notice = "offline mode; press o first"
                        else:
                            pending = [a for a in pool.accounts
                                       if time.time() >= pool.next_allowed.get(a.slug, 0.0)]
                            if pending:
                                notice = f"probe {pending[0].name}: {pool.probe(pending[0])}"
                            else:
                                notice = "probe budget spent"

                if not paused:
                    pool.refresh()
                live.update(build_renderable(pool, prober, notice,
                                             width=args.width or console.width))
                frames += 1
                if args.frames and frames >= args.frames:
                    break
                time.sleep(max(0.2, args.interval))
    except KeyboardInterrupt:
        pass
    finally:
        if prober:
            prober.stop_event.set()
    return 0
