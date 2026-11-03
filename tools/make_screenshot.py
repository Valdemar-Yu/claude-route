#!/usr/bin/env python3
"""Render docs/assets/dashboard.png from a synthetic pool.

The screenshot has to be reproducible from the current code, otherwise it
drifts: the first hand-taken screenshot still showed the -496%/h bug that the
window filter fixed, and the pre-rename panel title.

    python tools/make_screenshot.py

No network, no claude-accounts state, no real account data: the pool below is
built in a temp directory and thrown away.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from rich.console import Console

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from claude_route.core import History, Pool, Store  # noqa: E402
from claude_route.render import build_renderable  # noqa: E402

WIDTH = 96
FONT_PATH = Path(r"C:\Windows\Fonts\DejaVuSansMono_0.ttf")
BOLD_PATH = Path(r"C:\Windows\Fonts\DejaVuSansMono-Bold_0.ttf")
FONT_SIZE = 16
LINE_HEIGHT = 23
PADDING = 20
RADIUS = 12
BACKGROUND = (27, 27, 27, 255)
FOREGROUND = (212, 212, 212)

NAMED = {
    30: (0, 0, 0), 31: (205, 49, 49), 32: (13, 188, 121), 33: (229, 229, 16),
    34: (36, 114, 200), 35: (188, 63, 188), 36: (17, 168, 205), 37: (229, 229, 229),
    90: (102, 102, 102), 91: (241, 76, 76), 92: (35, 209, 139), 93: (245, 245, 67),
    94: (59, 142, 234), 95: (214, 112, 214), 96: (41, 184, 219), 97: (255, 255, 255),
}


def palette_256(index: int) -> tuple[int, int, int]:
    if index < 16:
        basic = [(0, 0, 0), (128, 0, 0), (0, 128, 0), (128, 128, 0), (0, 0, 128),
                 (128, 0, 128), (0, 128, 128), (192, 192, 192), (128, 128, 128),
                 (255, 0, 0), (0, 255, 0), (255, 255, 0), (0, 0, 255),
                 (255, 0, 255), (0, 255, 255), (255, 255, 255)]
        return basic[index]
    if index < 232:
        index -= 16
        levels = [0, 95, 135, 175, 215, 255]
        return levels[index // 36], levels[(index // 6) % 6], levels[index % 6]
    grey = 8 + (index - 232) * 10
    return grey, grey, grey


def parse_ansi(line: str) -> list[tuple[str, tuple[int, int, int], bool]]:
    runs: list[tuple[str, tuple[int, int, int], bool]] = []
    color, bold = FOREGROUND, False
    for chunk in re.split(r"(\x1b\[[0-9;]*m)", line):
        if not chunk:
            continue
        if chunk.startswith("\x1b["):
            codes = [int(c) for c in chunk[2:-1].split(";") if c != ""] or [0]
            i = 0
            while i < len(codes):
                code = codes[i]
                if code == 0:
                    color, bold = FOREGROUND, False
                elif code == 1:
                    bold = True
                elif code in (21, 22):
                    bold = False
                elif code == 39:
                    color = FOREGROUND
                elif code in NAMED:
                    color = NAMED[code]
                elif code == 38 and i + 1 < len(codes):
                    if codes[i + 1] == 2 and i + 4 < len(codes):
                        color = tuple(codes[i + 2:i + 5])
                        i += 4
                    elif codes[i + 1] == 5 and i + 2 < len(codes):
                        color = palette_256(codes[i + 2])
                        i += 2
                i += 1
            continue
        runs.append((chunk, color, bold))
    return runs


def sample_pool(workdir: Path) -> Pool:
    now = time.time()
    base = workdir / "base"
    (base / "usage").mkdir(parents=True, exist_ok=True)
    (base / "accounts.tsv").write_text(
        "00\t00\tClaude Code Subscription: claude-00\n"
        "01\t01\tClaude Code Subscription: claude-01\n", "utf-8")

    five_reset = int(now + 3 * 3600 + 6 * 60)
    seven_reset = int(now + 49 * 3600)
    (base / "usage" / "00.json").write_text(json.dumps({"rate_limits": {
        "five_hour": {"used_percentage": 47, "resets_at": five_reset},
        "seven_day": {"used_percentage": 7, "resets_at": seven_reset}}}), "utf-8")

    # 01 is at 100% for the 5h window: the panel should flag it COOL and send
    # the "pick" line to the healthy account.
    (base / "usage" / "01.json").write_text(json.dumps({"rate_limits": {
        "five_hour": {"used_percentage": 100, "resets_at": int(now + 46 * 60)},
        "seven_day": {"used_percentage": 63, "resets_at": int(now + 47 * 3600)}}}),
        "utf-8")

    os.utime(base / "usage" / "00.json", (now, now))
    os.utime(base / "usage" / "01.json", (now - 5, now - 5))

    state = workdir / "state"
    state.mkdir(parents=True, exist_ok=True)
    samples = []
    for step in range(13):
        stamp = now - (12 - step) * 60
        samples.append({"t": round(stamp, 1), "slug": "00", "mtime": round(stamp, 3),
                        "win": five_reset, "five": 43 + step * 0.32, "seven": 7})
    for step in range(13):
        stamp = now - (12 - step) * 60
        samples.append({"t": round(stamp, 1), "slug": "01", "mtime": round(stamp, 3),
                        "win": int(now + 46 * 60), "five": 95 + step * 0.42, "seven": 63})
    (state / "history.jsonl").write_text(
        "\n".join(json.dumps(s) for s in samples) + "\n", "utf-8")

    pool = Pool(Store(base), History(state / "history.jsonl"), state)
    pool.offline = False
    pool.refresh()
    return pool


def render_text(pool: Pool) -> str:
    console = Console(width=WIDTH, record=True, force_terminal=True,
                      color_system="truecolor", highlight=False, file=io.StringIO())
    console.print(build_renderable(pool, None, width=WIDTH))
    return console.export_text(styles=True)


def draw(text: str, out: Path) -> None:
    lines = text.rstrip("\n").split("\n")
    regular = ImageFont.truetype(str(FONT_PATH), FONT_SIZE)
    bold_font = ImageFont.truetype(str(BOLD_PATH), FONT_SIZE)

    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    char_width = probe.textlength("M", font=regular)
    ascent, descent = regular.getmetrics()
    visible = [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in lines]
    width = int(len(max(visible, key=len)) * char_width) + PADDING * 2
    height = LINE_HEIGHT * len(lines) + PADDING * 2

    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw_ctx = ImageDraw.Draw(image)
    draw_ctx.rounded_rectangle([0, 0, width - 1, height - 1], RADIUS, fill=BACKGROUND)

    for row, line in enumerate(lines):
        x = PADDING
        y = PADDING + row * LINE_HEIGHT + (LINE_HEIGHT - (ascent + descent)) / 2
        for chunk, color, is_bold in parse_ansi(line):
            draw_ctx.text((x, y), chunk, font=bold_font if is_bold else regular,
                          fill=color + (255,))
            x += char_width * len(chunk)
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out)
    print(f"wrote {out} ({width}x{height})")


def main() -> int:
    if not FONT_PATH.is_file():
        print(f"font not found: {FONT_PATH}", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        pool = sample_pool(Path(tmp))
        text = render_text(pool)
    draw(text, ROOT / "docs" / "assets" / "dashboard.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
