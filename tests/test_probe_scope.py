import io
import time
import urllib.error

from claude_route import core
from claude_route.core import SCOPE_BACKOFF, History, Pool, Store

SCOPE_BODY = (b'{"type":"error","error":{"type":"permission_error","message":'
              b'"OAuth token does not meet scope requirement user:profile",'
              b'"details":{"required_scopes":["user:profile"],'
              b'"error_code":"oauth_scope_insufficient"}}}')


def _pool(tmp_path, monkeypatch):
    base = tmp_path / "subs"
    base.mkdir()
    (base / "accounts.tsv").write_text(
        "00\t00\tClaude Code Subscription: claude-00\n"
        "01\t01\tClaude Code Subscription: claude-01\n", "utf-8")
    store = Store(base)
    monkeypatch.setattr(store, "token_for", lambda account: "sk-ant-oat-test")
    pool = Pool(store, History(tmp_path / "h.jsonl"), tmp_path / "state")
    pool.refresh()
    return pool


class _Opener:
    def __init__(self, code, body):
        self.code, self.body = code, body

    def open(self, request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, self.code, "denied", {},
                                     io.BytesIO(self.body))


def test_scope_error_marks_account_and_backs_off(tmp_path, monkeypatch):
    pool = _pool(tmp_path, monkeypatch)
    monkeypatch.setattr(core.urllib.request, "build_opener",
                        lambda *handlers: _Opener(403, SCOPE_BODY))
    account = pool.accounts[0]

    before = time.time()
    assert pool.probe(account) == "scope"
    assert account.slug in pool.scope_denied
    assert pool.next_allowed[account.slug] >= before + SCOPE_BACKOFF - 1
    assert not pool.should_probe(account, time.time())

    # The flag survives a restart, so the next run does not hammer the API.
    reloaded = Pool(pool.store, History(tmp_path / "h2.jsonl"), tmp_path / "state")
    assert account.slug in reloaded.scope_denied


def test_next_probe_skips_scope_denied_accounts(tmp_path, monkeypatch):
    pool = _pool(tmp_path, monkeypatch)
    pool.scope_denied = {"00"}
    upcoming = pool.next_probe(time.time())
    assert upcoming is not None and upcoming[0] == "claude-01"
    pool.scope_denied = {"00", "01"}
    assert pool.next_probe(time.time()) is None


def test_other_403_stays_a_generic_error(tmp_path, monkeypatch):
    pool = _pool(tmp_path, monkeypatch)
    monkeypatch.setattr(core.urllib.request, "build_opener",
                        lambda *handlers: _Opener(403, b'{"error":"forbidden"}'))
    assert pool.probe(pool.accounts[0]) == "error"
    assert not pool.scope_denied


def test_render_explains_empty_cache_and_scope(tmp_path, monkeypatch):
    from rich.console import Console

    from claude_route.render import build_renderable

    pool = _pool(tmp_path, monkeypatch)
    pool.scope_denied = {"00", "01"}
    console = Console(width=200, record=True)
    console.print(build_renderable(pool, None, width=200))
    text = console.export_text()
    assert "no usage cache yet for claude-00, claude-01" in text
    assert "no user:profile scope" in text
    assert "n/a, statusline only" in text
