"""Web tools: web_search + fetch_url. Stdlib only (urllib).

Both are EXTERNAL side effects: they leave the machine. `web_search` uses
DuckDuckGo's HTML endpoint (no API key); result parsing is a pure function
(`parse_ddg_html`) so it is unit-testable without network. `fetch_url`
caps response size and timeout, follows the stdlib default redirect
behavior, and strips HTML tags to plain text.
"""

from __future__ import annotations

import html as html_lib
import ipaddress
import re
import socket
import urllib.parse
import urllib.request
from typing import List, Optional

from .base import SideEffect, Tool

SEARCH_TIMEOUT_S = 20
FETCH_TIMEOUT_S = 20
MAX_FETCH_BYTES = 200_000
USER_AGENT = "PRANA/0.1 (+https://github.com/Thegreatprabh/Prana)"


def parse_ddg_html(page: str, limit: int = 8) -> List[dict]:
    """Parse DuckDuckGo HTML results into [{title, url, snippet}]. Pure function."""
    results: List[dict] = []
    # DDG html endpoint wraps results in <a class="result__a" href="...">title</a>
    # followed by <a class="result__snippet">snippet</a>.
    pattern = re.compile(
        r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
        r".*?<a[^>]*class=\"result__snippet\"[^>]*>(.*?)</a>",
        re.DOTALL | re.IGNORECASE,
    )
    for m in pattern.finditer(page):
        raw_url, raw_title, raw_snip = m.group(1), m.group(2), m.group(3)
        url = html_lib.unescape(re.sub(r"<[^>]+>", "", raw_url)).strip()
        # DDG wraps outbound links in a redirector; unwrap it.
        if "duckduckgo.com/l/?" in url:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            url = qs.get("uddg", [url])[0]
        title = html_lib.unescape(re.sub(r"<[^>]+>", "", raw_title)).strip()
        snippet = html_lib.unescape(re.sub(r"<[^>]+>", "", raw_snip)).strip()
        if url.startswith("http"):
            results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= limit:
            break
    return results


def _request(url: str, timeout: int) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT})


# ---------------------------------------------------------------------------
# SSRF protection for fetch_url.
#
# The agent is LLM-driven, so a fetched page could instruct it to probe
# internal addresses (prompt injection → http://169.254.169.254/ on a VPS =
# cloud credential theft). We resolve the hostname and refuse anything that
# is not a public IP — before the first request AND on every redirect hop.
# ---------------------------------------------------------------------------

def _resolve_host(host: str) -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """DNS-resolve a hostname. Returns None on any resolution failure."""
    try:
        return ipaddress.ip_address(socket.gethostbyname(host))
    except Exception:
        return None


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _check_host_public(host: str) -> Optional[str]:
    """Return an ERROR string when `host` must not be fetched, else None."""
    if not host:
        return "ERROR: empty hostname"
    ip = _resolve_host(host)
    if ip is None:
        return "ERROR: DNS resolution failed"
    if _ip_is_blocked(ip):
        return "ERROR: private/local network addresses are blocked"
    return None


class _RedirectBlocked(Exception):
    """Raised when a redirect hops to a non-public address."""


class _SSRFRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that re-validates every hop against the SSRF blocklist."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        host = urllib.parse.urlparse(target).hostname or ""
        err = _check_host_public(host)
        if err is not None:
            raise _RedirectBlocked(f"redirect target blocked: {host}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def web_search(query: str, limit: int = 8) -> str:
    """Search the web (DuckDuckGo, no key needed). Returns title/url/snippet lines."""
    limit = max(1, min(int(limit), 10))
    qs = urllib.parse.urlencode({"q": query})
    url = f"https://html.duckduckgo.com/html/?{qs}"
    try:
        with urllib.request.urlopen(_request(url, SEARCH_TIMEOUT_S), timeout=SEARCH_TIMEOUT_S) as r:
            page = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        return f"ERROR: search failed ({type(e).__name__}: {e})"
    results = parse_ddg_html(page, limit=limit)
    if not results:
        return "no results"
    lines = []
    for i, res in enumerate(results, 1):
        lines.append(f"{i}. {res['title']}\n   {res['url']}\n   {res['snippet']}")
    return "\n".join(lines)


def _strip_html(page: str) -> str:
    page = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", page)
    page = re.sub(r"(?s)<[^>]*>", " ", page)
    text = html_lib.unescape(page)
    return re.sub(r"[ \t\xa0]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def fetch_url(url: str) -> str:
    """Fetch a URL and return plain text (HTML stripped), capped in size.

    SSRF-guarded: the hostname must resolve to a public IP, and every
    redirect hop is re-checked. Private/local targets are refused.
    """
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return "ERROR: only http(s) URLs are allowed"
    host = urllib.parse.urlparse(url).hostname or ""
    blocked = _check_host_public(host)
    if blocked is not None:
        return blocked
    # Per-call opener: redirect validation without touching the global opener.
    opener = urllib.request.build_opener(_SSRFRedirectHandler)
    try:
        with opener.open(_request(url, FETCH_TIMEOUT_S), timeout=FETCH_TIMEOUT_S) as r:
            final_url = r.geturl()
            ctype = (r.headers.get("Content-Type") or "").lower()
            raw = r.read(MAX_FETCH_BYTES + 1)
    except _RedirectBlocked as e:
        return f"ERROR: {e}"
    except Exception as e:
        return f"ERROR: fetch failed ({type(e).__name__}: {e})"
    truncated = len(raw) > MAX_FETCH_BYTES
    raw = raw[:MAX_FETCH_BYTES]
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return f"ERROR: could not decode response from {final_url}"
    if "html" in ctype:
        text = _strip_html(text)
    if truncated:
        text += f"\n… [response capped at {MAX_FETCH_BYTES} bytes]"
    return f"[{final_url}]\n{text}".strip()


def builtin_web_tools() -> List[Tool]:
    return [
        Tool(
            name="web_search",
            description="Search the web (no API key needed). Returns titles, URLs, snippets.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "description": "Max results (1-10, default 8)"},
                },
                "required": ["query"],
            },
            side_effect=SideEffect.EXTERNAL,
            func=web_search,
        ),
        Tool(
            name="fetch_url",
            description="Fetch a URL, return plain text (HTML stripped). http(s) only.",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string", "description": "http(s) URL to fetch"}},
                "required": ["url"],
            },
            side_effect=SideEffect.EXTERNAL,
            func=fetch_url,
        ),
    ]
