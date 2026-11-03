import json
import re
import time

from rich.console import Console

from claude_route import render
from claude_route.core import History, Pool, Store
from claude_route.render import build_renderable
from claude_route.theme import DARK_THEME, LIGHT_THEME


def write_pool(tmp_path):
    base = tmp_path / "base"
    (base / "usage").mkdir(parents=True)
    rows = "\n".join(f"{s}\t{s}\tClaude Code Subscription: claude-{s}"
                     for s in ("00", "01"))
    (base / "accounts.tsv").write_text(rows + "\n", "utf-8")
    now = time.time()
    for slug, five, seven in (("00", 42, 7), ("01", 3, 63)):
        payload = {"rate_limits": {
            "five_hour": {"used_percentage": five, "resets_at": int(now + 3 * 3600)},
            "seven_day": {"used_percentage": seven, "resets_at": int(now + 48 * 3600)},
        }}
        (base / "usage" / f"{slug}.json").write_text(json.dumps(payload), "utf-8")
    pool = Pool(Store(base), History(tmp_path / "history.jsonl"), tmp_path / "state")
    pool.offline = True
    pool.refresh()
    return pool


def render_text(pool, width, theme=None):
    console = Console(width=width, record=True, force_terminal=False)
    console.print(build_renderable(pool, None, width=width, theme=theme))
    return console.export_text()


def test_wide_terminal_keeps_the_block_layout(tmp_path):
    pool = write_pool(tmp_path)
    text = render_text(pool, 160, LIGHT_THEME)

    assert "5h window" not in text
    assert "5h history, last hour" in text
    assert text.count("     5h ") == 2
    assert text.count("     7d ") == 2
    for name in ("claude-00", "claude-01"):
        assert name in text
    assert max(len(line) for line in text.splitlines()) <= 100


def test_narrow_terminal_also_uses_the_block_layout(tmp_path):
    pool = write_pool(tmp_path)
    text = render_text(pool, 96, LIGHT_THEME)
    assert "5h window" not in text
    assert "5h history, last hour" in text
    assert max(len(line) for line in text.splitlines()) <= 96


def _named_sgr(ansi: str) -> list[int]:
    """SGR params that select one of the 16 named ANSI colours."""
    found = []
    for match in re.finditer(r"\x1b\[([0-9;]*)m", ansi):
        params = [int(p) if p else 0 for p in match.group(1).split(";")]
        index = 0
        while index < len(params):
            code = params[index]
            if code in (38, 48):
                mode = params[index + 1] if index + 1 < len(params) else None
                if mode == 2:
                    index += 5
                    continue
                if mode == 5:
                    index += 3
                    continue
            if 30 <= code <= 37 or 90 <= code <= 97:
                found.append(code)
            index += 1
    return found


def test_light_theme_emits_no_named_colours(tmp_path):
    pool = write_pool(tmp_path)
    console = Console(width=96, record=True, force_terminal=True,
                      color_system="truecolor")
    console.print(build_renderable(pool, None, width=96, theme=LIGHT_THEME))
    ansi = console.export_text(styles=True)

    assert "38;5;" not in ansi
    assert _named_sgr(ansi) == []
    assert "white" not in ansi
    assert "grey62" not in ansi


def test_bars_use_solid_blocks_for_both_tones(tmp_path):
    pool = write_pool(tmp_path)
    text = render_text(pool, 96, LIGHT_THEME)
    assert "\u2591" not in text
    assert text.count("\u2588") >= 4 * 12


def test_ascii_glyph_fallback_keeps_hash_and_dash(tmp_path, monkeypatch):
    pool = write_pool(tmp_path)
    monkeypatch.setattr(render, "BAR_FULL", "#")
    monkeypatch.setattr(render, "BAR_REST", "-")
    text = render_text(pool, 96, DARK_THEME)
    assert "\u2588" not in text
    assert "#" in text and "-" in text


def test_theme_is_an_optional_keyword(tmp_path):
    pool = write_pool(tmp_path)
    panel = build_renderable(pool, None, notice="", width=96)
    assert panel is not None
