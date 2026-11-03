"""Data model, on-disk readers, and the quota probe for claude-route.

Read this module first if you only want the facts:

1. ``~/.config/claude-subscriptions/usage/<slug>.json`` is written for free by
   claude-accounts' statusline hook whenever that account has a live session.
   A cache file younger than 90s also proves the account is currently in use.
2. The official OAuth usage endpoint is probed at most once every 45 minutes
   per account, one account at a time, always honouring ``Retry-After``.

Neither path triggers an inference request, so no 5h/7d quota is consumed.
claude-route only reads claude-accounts' files; it never writes back to them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

USAGE_API = "https://api.anthropic.com/api/oauth/usage"
ANTHROPIC_BETA = "oauth-2025-04-20"
SERVICE_PREFIX = "Claude Code Subscription: "

PROBE_INTERVAL = 45 * 60      # per account, seconds
PROBE_GAP = 90                # minimum gap between two probes, seconds
PROBE_STALE = 10 * 60         # cache older than this may be refreshed
PROBE_STALE_RESET = 2 * 60    # after a reset, refresh sooner than that
SCOPE_BACKOFF = 24 * 60 * 60  # token lacks user:profile; re-check once a day
HARD_STALE = 60 * 60          # display turns red
LIVE_WINDOW = 90              # cache age that proves a session is running
HISTORY_WINDOW = 60 * 60      # curve / burn-rate lookback
MIN_BURN_SPAN = 240           # shorter spans are too noisy to extrapolate

BAR_FULL = "\u2588"
BAR_EMPTY = "\u2591"
SPARKS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"


def configure_glyphs() -> None:
    """Fall back to ASCII when the console encoding cannot carry box glyphs."""
    global BAR_FULL, BAR_EMPTY, SPARKS
    encoding = (getattr(sys.stdout, "encoding", None) or "utf-8").lower()
    try:
        "\u2588\u2591\u2581\u2588".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        BAR_FULL, BAR_EMPTY, SPARKS = "#", "-", ".:-=+*@"


def enable_utf8_console() -> None:
    """Switch the Windows console to UTF-8 so rich can draw the UI."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
    except Exception:  # noqa: BLE001 - best effort, never fatal
        pass




def fmt_duration(seconds: float | None) -> str:
    """Compact human duration: 3h56m, 46m03s, 2d2h."""
    if seconds is None:
        return "--"
    total = int(abs(seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    sign = "-" if seconds < 0 else ""
    if days:
        return f"{sign}{days}d{hours}h"
    if hours:
        return f"{sign}{hours}h{minutes:02d}m"
    if minutes:
        return f"{sign}{minutes}m{secs:02d}s"
    return f"{sign}{secs}s"


def make_bar(pct: float | None, width: int = 14) -> str:
    value = 0.0 if pct is None else max(0.0, min(100.0, pct))
    filled = int(round(value / 100.0 * width))
    return BAR_FULL * filled + BAR_EMPTY * (width - filled)


def severity(pct: float | None) -> str:
    if pct is None:
        return "grey50"
    if pct >= 99.5:
        return "bold red"
    if pct >= 80:
        return "yellow"
    if pct >= 50:
        return "bright_green"
    return "green"


def parse_reset(value) -> datetime | None:
    """Accept epoch seconds (int/float/str) or an ISO-8601 timestamp."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value)).astimezone()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromtimestamp(float(text)).astimezone()
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone()
        except ValueError:
            return None
    return None


def read_json(path: Path) -> tuple[dict | None, float]:
    """Return (payload, mtime); (None, 0.0) when absent or unreadable."""
    try:
        stat = path.stat()
    except OSError:
        return None, 0.0
    try:
        return json.loads(path.read_text("utf-8", errors="replace")), stat.st_mtime
    except (OSError, ValueError):
        return None, stat.st_mtime




@dataclass
class Window:
    used: float | None = None
    resets_at: datetime | None = None
    source: str = "none"
    at: float = 0.0

    def effective(self, now: float) -> tuple[float | None, bool]:
        """Usage, with an already-ended fixed window counted as reset."""
        if self.used is None:
            return None, False
        if self.resets_at is not None:
            reset_ts = self.resets_at.timestamp()
            if now >= reset_ts and self.at < reset_ts:
                return 0.0, True
        return self.used, False

    def remaining(self, now: float) -> float | None:
        if self.resets_at is None:
            return None
        return self.resets_at.timestamp() - now


def _is_scope_error(exc: urllib.error.HTTPError) -> bool:
    """Whether a 401/403 from the usage endpoint means 'token scope too narrow'."""
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - an unreadable body is just not a scope error
        return False
    return "oauth_scope_insufficient" in body or "user:profile" in body


def windows_from(payload: dict | None, source: str, at: float) -> dict[str, Window]:
    """Normalise a claude-accounts cache file or an API response."""
    if not isinstance(payload, dict):
        return {}
    body = payload.get("rate_limits")
    if not isinstance(body, dict):
        body = payload
    out: dict[str, Window] = {}
    for key, name in (("five_hour", "five"), ("seven_day", "seven")):
        node = body.get(key)
        if not isinstance(node, dict):
            continue
        pct = node.get("used_percentage")
        if pct is None:
            pct = node.get("utilization")
            if isinstance(pct, (int, float)):
                pct = float(pct) * 100.0
        if not isinstance(pct, (int, float)):
            continue
        out[name] = Window(float(pct), parse_reset(node.get("resets_at")), source, at)
    return out


def keychain_token(service: str) -> str:
    """Read a claude-accounts token from the macOS login Keychain.

    Upstream stores one generic-password item per account on darwin, so there
    is no tokens.json to read there. Returns "" when the platform is not macOS,
    the item is missing (security exits 44), or the query fails.
    """
    if sys.platform != "darwin" or not service:
        return ""
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password",
             "-a", user, "-s", service, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


@dataclass
class Account:
    slug: str
    name: str
    label: str
    service: str = ""
    five: Window = field(default_factory=Window)
    seven: Window = field(default_factory=Window)
    age: float = float("inf")
    token: str = ""
    probe_error: str = ""

    @property
    def live(self) -> bool:
        return self.age <= LIVE_WINDOW

    def state(self, now: float) -> str:
        five, _ = self.five.effective(now)
        seven, _ = self.seven.effective(now)
        if five is None:
            return "UNKNOWN"
        if five >= 99.5:
            return "COOL"
        if seven is not None and seven >= 80:
            return "TIGHT"
        if five >= 80:
            return "TIGHT"
        return "READY"

    @staticmethod
    def state_color(state: str) -> str:
        return {"READY": "green", "TIGHT": "yellow",
                "COOL": "bold red", "UNKNOWN": "grey50"}.get(state, "white")


class Store:
    """Read-only view of claude-accounts' on-disk layout."""

    def __init__(self, base: Path):
        self.base = Path(base)
        accounts_file = os.environ.get("CLAUDE_SUBSCRIPTIONS_FILE")
        usage_dir = os.environ.get("CLAUDE_SUBSCRIPTIONS_USAGE_DIR")
        self.accounts_file = (Path(accounts_file).expanduser()
                              if accounts_file else self.base / "accounts.tsv")
        self.usage_dir = (Path(usage_dir).expanduser()
                          if usage_dir else self.base / "usage")

    def load_accounts(self) -> list[Account]:
        rows: list[tuple[str, str, str]] = []
        tsv = self.accounts_file
        if tsv.is_file():
            for line in tsv.read_text("utf-8", errors="replace").splitlines():
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                parts = [part.strip() for part in line.split("\t")]
                slug = parts[0] if parts else ""
                if not slug:
                    continue
                # claude-accounts writes slug<TAB>label<TAB>keychain-service.
                # Older layouts put the label in the third column instead, so
                # only treat column 3 as a service when it looks like one.
                label, service = "", ""
                if len(parts) > 2 and parts[2].startswith(SERVICE_PREFIX):
                    label, service = parts[1], parts[2]
                elif len(parts) > 2:
                    label = parts[2]
                elif len(parts) > 1:
                    label = parts[1]
                rows.append((slug, label, service))
        if not rows:  # fall back to the token store
            payload, _ = read_json(self.base / "tokens.json")
            for key in (payload or {}):
                slug = key.rsplit("claude-", 1)[-1].strip()
                if slug:
                    rows.append((slug, key, key))
        accounts = [
            Account(slug=slug, name=f"claude-{slug}", label=label or f"claude-{slug}",
                    service=service)
            for slug, label, service in rows
        ]
        accounts.sort(key=lambda acc: acc.slug)
        return accounts

    def usage_path(self, slug: str) -> Path:
        return self.usage_dir / f"{slug}.json"

    def token_for(self, account: Account) -> str:
        if account.token:
            return account.token
        service = account.service or f"{SERVICE_PREFIX}claude-{account.slug}"
        token = keychain_token(service)
        if token:
            account.token = token
            return token
        payload, _ = read_json(self.base / "tokens.json")
        if not isinstance(payload, dict):
            return ""
        for key in (account.label, service,
                    f"{SERVICE_PREFIX}claude-{account.slug}"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                account.token = value.strip()
                return account.token
        return ""


class History:
    """Append-only sample log used for the sparkline and the burn rate."""

    MAX_LINES = 8000

    def __init__(self, path: Path):
        self.path = path
        self.samples: list[dict] = []
        self._seen: set[tuple[str, float]] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        cutoff = time.time() - HISTORY_WINDOW * 2
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if item.get("t", 0) < cutoff:
                        continue
                    self.samples.append(item)
                    self._seen.add((item.get("slug", ""), item.get("mtime", 0.0)))
        except OSError:
            return

    def record(self, account: Account, now: float) -> None:
        stamp = round(account.five.at, 3)
        key = (account.slug, stamp)
        if not stamp or key in self._seen:
            return
        five, _ = account.five.effective(now)
        seven, _ = account.seven.effective(now)
        window = int(account.five.resets_at.timestamp()) if account.five.resets_at else 0
        item = {"t": round(now, 3), "slug": account.slug, "mtime": stamp,
                "win": window,
                "five": None if five is None else round(five, 3),
                "seven": None if seven is None else round(seven, 3)}
        self._seen.add(key)
        self.samples.append(item)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(item) + "\n")
        except OSError:
            pass

    def _points(self, slug: str, now: float) -> list[dict]:
        """Samples of the newest 5h window only.

        A window reset drops 100% to 0% in one step; mixing samples from both
        sides would produce nonsense burn rates like -500%/h.
        """
        cutoff = now - HISTORY_WINDOW
        points = [s for s in self.samples
                  if s.get("slug") == slug and s.get("t", 0) >= cutoff
                  and isinstance(s.get("five"), (int, float))]
        if not points:
            return []
        newest_window = points[-1].get("win", 0)
        return [s for s in points if s.get("win", 0) == newest_window]

    def series(self, slug: str, now: float) -> list[float]:
        return [float(s["five"]) for s in self._points(slug, now)]

    def burn_rate(self, slug: str, now: float) -> tuple[float, float] | None:
        """Return (percent per hour, span in seconds) over the lookback window.

        Short spans are skipped: three samples a minute apart can look like
        90%/h purely because of one 60-second cache update.
        """
        points = [(s["t"], float(s["five"])) for s in self._points(slug, now)]
        if len(points) < 2:
            return None
        span = points[-1][0] - points[0][0]
        if span < MIN_BURN_SPAN:
            return None
        delta = points[-1][1] - points[0][1]
        if delta < -1:
            # A window reset happened inside this span; a rate across it means
            # nothing. Older history files have no window marker, so this guard
            # is also what keeps them from showing -500%/h.
            return None
        return delta / span * 3600.0, span


def sparkline(values: list[float], width: int = 22) -> str:
    if not values:
        return ""
    points = values[-width:]
    chars = []
    for value in points:
        level = int(max(0.0, min(100.0, value)) / 100.0 * (len(SPARKS) - 1))
        chars.append(SPARKS[level])
    return "".join(chars)


class Pool:
    def __init__(self, store: Store, history: History, state_dir: Path):
        self.store = store
        self.history = history
        self.state_dir = state_dir
        self.accounts = store.load_accounts()
        self.lock = threading.RLock()
        self.offline = False
        self.next_allowed: dict[str, float] = {}
        self.last_global_probe = 0.0
        self.last_error: dict[str, str] = {}
        # Accounts whose token the usage endpoint refuses for missing
        # user:profile scope. `claude setup-token` tokens (what claude-accounts
        # stores) only carry inference scope, so for them the statusline cache
        # is the only data source.
        self.scope_denied: set[str] = set()
        self._load_state()

    # -- state ------------------------------------------------------------- #

    def _state_file(self) -> Path:
        return self.state_dir / "probe_state.json"

    def _load_state(self) -> None:
        payload, _ = read_json(self._state_file())
        if not isinstance(payload, dict):
            return
        for slug, value in (payload.get("next_allowed") or {}).items():
            if isinstance(value, (int, float)):
                self.next_allowed[str(slug)] = float(value)
        last = payload.get("last_global_probe")
        if isinstance(last, (int, float)):
            self.last_global_probe = float(last)
        denied = payload.get("scope_denied")
        if isinstance(denied, list):
            self.scope_denied = {str(slug) for slug in denied}

    def _save_state(self) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            payload = {"next_allowed": self.next_allowed,
                       "last_global_probe": self.last_global_probe,
                       "scope_denied": sorted(self.scope_denied)}
            self._state_file().write_text(json.dumps(payload), "utf-8")
        except OSError:
            pass

    # -- refresh ----------------------------------------------------------- #

    def refresh(self) -> None:
        now = time.time()
        with self.lock:
            for account in self.accounts:
                self._refresh_account(account, now)
                self.history.record(account, now)

    def _refresh_account(self, account: Account, now: float) -> None:
        cache_payload, cache_mtime = read_json(self.store.usage_path(account.slug))
        api_payload, api_mtime = read_json(self.state_dir / "api" / f"{account.slug}.json")

        windows = windows_from(cache_payload, "cache", cache_mtime)
        if api_mtime > cache_mtime:
            for name, window in windows_from(api_payload, "api", api_mtime).items():
                windows[name] = window

        account.five = windows.get("five", Window())
        account.seven = windows.get("seven", Window())
        stamp = max(account.five.at, account.seven.at)
        account.age = now - stamp if stamp else float("inf")

    # -- probing ----------------------------------------------------------- #

    def should_probe(self, account: Account, now: float) -> bool:
        if self.offline:
            return False
        if now < self.next_allowed.get(account.slug, 0.0):
            return False
        if now - self.last_global_probe < PROBE_GAP:
            return False
        _, reset_five = account.five.effective(now)
        _, reset_seven = account.seven.effective(now)
        resetted = reset_five or reset_seven
        if account.age <= (PROBE_STALE_RESET if resetted else PROBE_STALE):
            return False
        return bool(self.store.token_for(account))

    def probe(self, account: Account) -> str:
        """One GET against the usage endpoint. Returns 'ok' / '429' / 'scope' / 'error'."""
        token = self.store.token_for(account)
        if not token:
            return "error"
        request = urllib.request.Request(USAGE_API, headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": ANTHROPIC_BETA,
            "anthropic-version": "2023-06-01",
            "User-Agent": "claude-pool-monitor/1.0",
            "Accept": "application/json",
        })
        proxy = os.environ.get("CLAUDE_POOL_PROXY") or os.environ.get("HTTPS_PROXY")
        handlers = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        opener = urllib.request.build_opener(*handlers)

        now = time.time()
        with self.lock:
            self.last_global_probe = now

        try:
            with opener.open(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403) and _is_scope_error(exc):
                with self.lock:
                    self.scope_denied.add(account.slug)
                    self.next_allowed[account.slug] = now + SCOPE_BACKOFF
                    self._save_state()
                    self.last_error[account.slug] = "token lacks user:profile scope"
                return "scope"
            headers = exc.headers or {}
            raw_retry = headers.get("Retry-After") if headers else None
            try:
                delay = float(raw_retry) if raw_retry else PROBE_INTERVAL
            except (TypeError, ValueError):
                delay = PROBE_INTERVAL
            with self.lock:
                self.next_allowed[account.slug] = now + max(60.0, delay)
                self._save_state()
                self.last_error[account.slug] = f"HTTP {exc.code}, retry in {fmt_duration(delay)}"
            return "429" if exc.code == 429 else "error"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            with self.lock:
                self.next_allowed[account.slug] = now + 600
                self._save_state()
                self.last_error[account.slug] = str(getattr(exc, "reason", exc))[:48]
            return "error"

        windows = windows_from(payload, "api", time.time())
        if not windows:
            with self.lock:
                self.next_allowed[account.slug] = now + PROBE_INTERVAL
                self._save_state()
                self.last_error[account.slug] = "unexpected payload"
            return "error"

        self.scope_denied.discard(account.slug)
        target = self.state_dir / "api" / f"{account.slug}.json"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(payload), "utf-8")
        except OSError:
            pass
        with self.lock:
            self.next_allowed[account.slug] = now + PROBE_INTERVAL
            self.last_error.pop(account.slug, None)
            self._save_state()
        return "ok"

    def next_probe(self, now: float) -> tuple[str, float] | None:
        """Earliest moment some account becomes both budget- and staleness-eligible."""
        if self.offline or not self.accounts:
            return None
        best: tuple[str, float] | None = None
        for account in self.accounts:
            if account.slug in self.scope_denied:
                continue
            budget = max(self.next_allowed.get(account.slug, 0.0),
                         self.last_global_probe + PROBE_GAP)
            _, resetted = account.five.effective(now)
            _, reset_seven = account.seven.effective(now)
            threshold = PROBE_STALE_RESET if (resetted or reset_seven) else PROBE_STALE
            stale_at = account.five.at + threshold if account.five.at else 0.0
            when = max(budget, stale_at)
            if best is None or when < best[1]:
                best = (account.name, when)
        if best is None:
            return None
        return best[0], max(0.0, best[1] - now)

    def pick(self, now: float) -> Account | None:
        ready = [acc for acc in self.accounts if acc.state(now) == "READY"]
        pool = ready or [acc for acc in self.accounts if acc.state(now) == "TIGHT"]
        if not pool:
            return None
        return min(pool, key=lambda acc: (acc.five.effective(now)[0] or 0.0,
                                          acc.seven.effective(now)[0] or 0.0))


class Prober(threading.Thread):
    """Background probe scheduler; never blocks the render loop."""

    def __init__(self, pool: Pool):
        super().__init__(daemon=True, name="pool-prober")
        self.pool = pool
        self.stop_event = threading.Event()
        self.events: list[str] = []

    def run(self) -> None:
        while not self.stop_event.wait(5):
            for account in list(self.pool.accounts):
                if self.stop_event.is_set():
                    return
                if not self.pool.should_probe(account, time.time()):
                    continue
                result = self.pool.probe(account)
                stamp = datetime.now().strftime("%H:%M:%S")
                label = "no user:profile scope" if result == "scope" else result
                self.events.append(f"{stamp} {account.name} -> {label}")
                self.events[:] = self.events[-4:]
                if result == "429":
                    self.stop_event.wait(PROBE_GAP)
