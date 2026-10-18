"""Tests for prana.tools — registry gate, file tools, shell, web parsing.

Tool tests run against tmp sandboxes. No network is touched: web parsing is
tested via the pure `parse_ddg_html` function with canned HTML.
"""

import pytest

from prana.tools import (
    SideEffect,
    Tool,
    ToolCall,
    ToolRegistry,
    builtin_file_tools,
    builtin_shell_tools,
    builtin_web_tools,
)
from prana.tools.web import parse_ddg_html


@pytest.fixture()
def registry(tmp_path):
    reg = ToolRegistry(approver=lambda name, args: False)  # deny by default
    for tool in builtin_file_tools(str(tmp_path)):
        reg.register(tool)
    for tool in builtin_shell_tools(str(tmp_path)):
        reg.register(tool)
    for tool in builtin_web_tools():
        reg.register(tool)
    return reg


def _call(name, **args):
    return ToolCall(id="t1", name=name, arguments=args)


# ---- registry / approval gate ----

def test_unknown_tool_errors(registry):
    res = registry.dispatch(_call("nope"))
    assert res.is_error and "unknown tool" in res.output


def test_missing_required_arg_errors(registry):
    res = registry.dispatch(_call("read"))  # path required
    assert res.is_error and "missing required argument" in res.output


def test_unknown_arg_errors(registry):
    res = registry.dispatch(_call("read", path="x", bogus=1))
    assert res.is_error and "unknown argument" in res.output


def test_irreversible_denied_by_default(registry):
    ran = []

    def boom():
        ran.append(True)
        return "kaboom"

    registry.register(Tool(
        name="delete_everything", description="test", parameters={"type": "object", "properties": {}},
        side_effect=SideEffect.IRREVERSIBLE, func=boom,
    ))
    res = registry.dispatch(_call("delete_everything"))
    assert res.is_error and "denied" in res.output
    assert ran == []  # never executed


def test_irreversible_approved_runs(registry):
    registry.approver = lambda name, args: True
    registry.register(Tool(
        name="do_it", description="test", parameters={"type": "object", "properties": {}},
        side_effect=SideEffect.IRREVERSIBLE, func=lambda: "done",
    ))
    res = registry.dispatch(_call("do_it"))
    assert not res.is_error and res.output == "done"


def test_broken_approver_fails_closed(registry):
    def bad_approver(name, args):
        raise RuntimeError("approver crashed")

    registry.approver = bad_approver
    registry.register(Tool(
        name="do_it2", description="test", parameters={"type": "object", "properties": {}},
        side_effect=SideEffect.IRREVERSIBLE, func=lambda: "done",
    ))
    res = registry.dispatch(_call("do_it2"))
    assert res.is_error and "denied by default" in res.output


def test_duplicate_registration_rejected(registry):
    with pytest.raises(ValueError):
        registry.register(Tool(
            name="read", description="dup", parameters={"type": "object", "properties": {}},
            side_effect=SideEffect.READ, func=lambda: "x",
        ))


# ---- file tools ----

def test_write_read_roundtrip(registry, tmp_path):
    r1 = registry.dispatch(_call("write", path="a/b.txt", content="hello prana"))
    assert not r1.is_error
    r2 = registry.dispatch(_call("read", path="a/b.txt"))
    assert not r2.is_error and r2.output == "hello prana"


def test_edit_exact_once(registry):
    registry.dispatch(_call("write", path="f.txt", content="foo bar foo"))
    # ambiguous (2 matches) -> rejected
    r = registry.dispatch(_call("edit", path="f.txt", old_text="foo", new_text="baz"))
    assert r.is_error and "2 times" in r.output
    # exact single match -> ok
    r = registry.dispatch(_call("edit", path="f.txt", old_text="foo bar", new_text="baz"))
    assert not r.is_error
    r = registry.dispatch(_call("read", path="f.txt"))
    assert r.output == "baz foo"


def test_edit_no_match_errors(registry):
    registry.dispatch(_call("write", path="g.txt", content="abc"))
    r = registry.dispatch(_call("edit", path="g.txt", old_text="zzz", new_text="q"))
    assert r.is_error and "not found" in r.output


def test_path_escape_rejected(registry):
    r = registry.dispatch(_call("read", path="../../etc/passwd"))
    assert r.is_error and "escapes sandbox" in r.output
    r = registry.dispatch(_call("write", path="../evil.txt", content="x"))
    assert r.is_error and "escapes sandbox" in r.output


def test_list_dir(registry):
    registry.dispatch(_call("write", path="x.txt", content="1"))
    r = registry.dispatch(_call("list", path="."))
    assert not r.is_error and "x.txt" in r.output


# ---- shell ----

def test_exec_echo(registry):
    r = registry.dispatch(_call("exec", cmd="echo hello"))
    assert not r.is_error and "hello" in r.output and "exit 0" in r.output


def test_exec_failure_reported(registry):
    r = registry.dispatch(_call("exec", cmd="exit 3"))
    assert not r.is_error  # tool itself worked; the command failed
    assert "exit 3" in r.output


def test_exec_cwd_escape_rejected(registry):
    r = registry.dispatch(_call("exec", cmd="echo hi", cwd="../.."))
    assert r.is_error and "escapes sandbox" in r.output


# ---- web (pure parsing, no network) ----

def test_parse_ddg_html():
    page = """
    <a class="result__a" href="https://example.com/page">Example Title</a>
    <a class="result__snippet" href="x">A snippet here</a>
    <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Freal.com%2Fy&rut=x">Wrapped</a>
    <a class="result__snippet" href="x">Wrapped snippet</a>
    """
    out = parse_ddg_html(page)
    assert len(out) == 2
    assert out[0]["url"] == "https://example.com/page"
    assert out[0]["title"] == "Example Title"
    assert out[1]["url"] == "https://real.com/y"  # redirector unwrapped


def test_parse_ddg_html_empty():
    assert parse_ddg_html("<html>nothing</html>") == []


def test_fetch_url_rejects_non_http(registry):
    r = registry.dispatch(_call("fetch_url", url="ftp://x/y"))
    assert "only http(s)" in r.output


@pytest.mark.parametrize("host", [
    "127.0.0.1",          # loopback
    "10.0.0.1",           # private
    "192.168.1.1",        # private
    "169.254.169.254",    # link-local (cloud metadata)
    "224.0.0.1",          # multicast
])
def test_fetch_url_blocks_nonpublic_hosts(registry, host):
    # Literal IPs resolve without network; the block happens pre-fetch.
    r = registry.dispatch(_call("fetch_url", url=f"http://{host}/"))
    assert "blocked" in r.output


def test_fetch_url_blocks_localhost_name(registry, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "gethostbyname", lambda h: "127.0.0.1")
    r = registry.dispatch(_call("fetch_url", url="http://example-internal/"))
    assert "blocked" in r.output


def test_fetch_url_dns_failure_is_clean_error(registry, monkeypatch):
    import socket
    def boom(host):
        raise socket.gaierror("no such host")
    monkeypatch.setattr(socket, "gethostbyname", boom)
    r = registry.dispatch(_call("fetch_url", url="http://nonexistent.invalid/"))
    assert "DNS resolution failed" in r.output


def test_fetch_url_allows_public_host(registry, monkeypatch):
    import socket
    import urllib.request
    from prana.tools import web as web_mod

    monkeypatch.setattr(socket, "gethostbyname", lambda h: "93.184.216.0")

    class FakeResp:
        def geturl(self):
            return "http://example.com/"
        @property
        def headers(self):
            return {"Content-Type": "text/html"}
        def read(self, n=-1):
            return b"<html><body>hello</body></html>"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class FakeOpener:
        def open(self, req, timeout=None):
            assert isinstance(req, urllib.request.Request)
            return FakeResp()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: FakeOpener())
    out = web_mod.fetch_url("http://example.com/")
    assert "[http://example.com/]" in out and "hello" in out


def test_fetch_url_blocks_redirect_to_private(registry, monkeypatch):
    import socket
    import urllib.request
    from prana.tools import web as web_mod

    def fake_dns(host):
        return {"example.com": "93.184.216.0"}.get(host, "10.9.9.9")
    monkeypatch.setattr(socket, "gethostbyname", fake_dns)

    handler = web_mod._SSRFRedirectHandler()
    req = urllib.request.Request("http://example.com/")
    with pytest.raises(web_mod._RedirectBlocked):
        handler.redirect_request(req, None, 302, "Found", {}, "http://10.9.9.9/evil")



def test_config_dir_created_when_home_missing(tmp_path, monkeypatch):
    import os
    from prana import config
    fake_home = tmp_path / "nope" / "home"
    monkeypatch.setenv("HOME", str(fake_home))
    d = config.get_config_dir()
    assert d.is_dir()
    assert oct(d.stat().st_mode & 0o777) == "0o700"
