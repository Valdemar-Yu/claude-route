import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from claude_route.core import (
    Account,
    History,
    Pool,
    Store,
    Window,
    fmt_duration,
    parse_reset,
    sparkline,
    windows_from,
)


def make_account(slug="00", used=10.0, at=0.0, reset_in=3600):
    account = Account(slug=slug, name=f"claude-{slug}", label=f"claude-{slug}")
    account.five = Window(used=used,
                          resets_at=datetime.now() + timedelta(seconds=reset_in),
                          at=at)
    return account


def write_base(root: Path, slugs=("00", "01")) -> Path:
    (root / "usage").mkdir(parents=True, exist_ok=True)
    rows = "\n".join(f"{s}\t{s}\tClaude Code Subscription: claude-{s}" for s in slugs)
    (root / "accounts.tsv").write_text(rows + "\n", "utf-8")
    for slug in slugs:
        payload = {"rate_limits": {"five_hour": {"used_percentage": 42, "resets_at": 1793701200},
                                   "seven_day": {"used_percentage": 7, "resets_at": 1793869200}}}
        (root / "usage" / f"{slug}.json").write_text(json.dumps(payload), "utf-8")
    return root


def test_fmt_duration():
    assert fmt_duration(None) == "--"
    assert fmt_duration(45) == "45s"
    assert fmt_duration(125) == "2m05s"
    assert fmt_duration(3600 + 600) == "1h10m"
    assert fmt_duration(86400 * 2 + 7200) == "2d2h"


def test_parse_reset_shapes():
    assert parse_reset(1793701200).year == 2026
    assert parse_reset("1793701200").year == 2026
    assert parse_reset("2026-11-03T02:20:00.000Z") is not None
    assert parse_reset("nonsense") is None
    assert parse_reset(None) is None


def test_windows_from_cache_shape():
    payload = {"rate_limits": {"five_hour": {"used_percentage": 34, "resets_at": 1793701200}}}
    windows = windows_from(payload, "cache", 100.0)
    assert windows["five"].used == 34.0
    assert windows["five"].source == "cache"
    assert windows["five"].at == 100.0
    assert "seven" not in windows


def test_windows_from_api_shape_converts_utilization():
    payload = {"five_hour": {"utilization": 0.42, "resets_at": "2026-11-03T02:20:00Z"}}
    windows = windows_from(payload, "api", 1.0)
    assert windows["five"].used == pytest.approx(42.0)


def test_window_effective_after_reset():
    expired = Window(used=100.0, resets_at=datetime.now() - timedelta(minutes=1), at=1.0)
    used, resetted = expired.effective(time.time())
    assert used == 0.0 and resetted is True

    fresh = Window(used=12.0, resets_at=datetime.now() + timedelta(hours=4), at=1.0)
    used, resetted = fresh.effective(time.time())
    assert used == 12.0 and resetted is False


def test_sparkline_empty_and_range():
    assert sparkline([]) == ""
    line = sparkline([0.0, 50.0, 100.0])
    assert len(line) == 3
    assert line[0] != line[-1]


def test_account_state_and_pick(tmp_path):
    base = write_base(tmp_path / "base")
    pool = Pool(Store(base), History(tmp_path / "h.jsonl"), tmp_path / "state")
    pool.offline = True
    pool.refresh()

    assert [a.name for a in pool.accounts] == ["claude-00", "claude-01"]
    now = time.time()
    assert all(a.state(now) == "READY" for a in pool.accounts)
    assert pool.pick(now).slug == "00"


def test_missing_usage_degrades_to_unknown(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    (base / "accounts.tsv").write_text("00\t00\tclaude-00\n", "utf-8")
    pool = Pool(Store(base), History(tmp_path / "h.jsonl"), tmp_path / "state")
    pool.offline = True
    pool.refresh()
    assert pool.accounts[0].state(time.time()) == "UNKNOWN"
    assert pool.accounts[0].age == float("inf")
    assert pool.pick(time.time()) is None


def test_history_burn_rate(tmp_path):
    history = History(tmp_path / "h.jsonl")
    start = time.time()
    first = make_account(used=10.0, at=start)
    history.record(first, start)
    later = make_account(used=13.0, at=start + 300)
    history.record(later, start + 300)

    estimate = history.burn_rate("00", start + 300)
    assert estimate is not None
    rate, span = estimate
    assert span == pytest.approx(300, abs=1)
    assert rate == pytest.approx(36.0, rel=0.05)


def test_history_ignores_span_across_reset(tmp_path):
    history = History(tmp_path / "h.jsonl")
    start = time.time()
    history.record(make_account(used=100.0, at=start), start)
    after = make_account(used=0.0, at=start + 300,
                         reset_in=-600)  # window already rolled over
    history.record(after, start + 300)

    assert history.burn_rate("00", start + 300) is None


def test_history_requires_minimum_span(tmp_path):
    history = History(tmp_path / "h.jsonl")
    start = time.time()
    history.record(make_account(used=10.0, at=start), start)
    history.record(make_account(used=11.0, at=start + 60), start + 60)
    assert history.burn_rate("00", start + 60) is None


def test_render_smoke(tmp_path):
    from rich.console import Console

    from claude_route.render import build_renderable

    base = write_base(tmp_path / "base")
    pool = Pool(Store(base), History(tmp_path / "h.jsonl"), tmp_path / "state")
    pool.offline = True
    pool.refresh()

    for width in (84, 100, 140):
        console = Console(width=width, record=True, force_terminal=False)
        console.print(build_renderable(pool, None, width=width))
        text = console.export_text()
        assert "claude-00" in text
        assert "claude-01" in text
