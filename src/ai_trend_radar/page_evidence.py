"""Bounded, unauthenticated public-page reads for selected community discoveries."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import socket
import ssl
import time
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

from ai_trend_radar.config import AppConfig
from ai_trend_radar.llm_adapter import digest
from ai_trend_radar.reports import _atomic_write

VERSION = "public-page-v1"
MAX_BYTES = 256_000


class PageUnavailable(ValueError):
    pass


class PageText(HTMLParser):
    OMIT = {"head", "script", "style", "noscript", "svg", "nav", "header", "footer", "form"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ignored = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.OMIT:
            self.ignored.append(tag)
        elif not self.ignored and tag in {"p", "div", "br", "li", "h1", "h2", "h3", "pre", "section"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.ignored and tag == self.ignored[-1]:
            self.ignored.pop()
        elif not self.ignored and tag in {"p", "div", "li", "h1", "h2", "h3", "pre", "section"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)

    def text(self):
        return "\n".join(line for part in "".join(self.parts).splitlines() if (line := " ".join(part.split())))


def public_target(url: str) -> tuple[str, str, int, str]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise PageUnavailable("Only unauthenticated public HTTP(S) pages are supported")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in {80, 443}:
        raise PageUnavailable("Nonstandard page ports are not allowed")
    if any(key.lower() in {"key", "token", "access_token", "api_key", "password", "secret"} for key, _ in parse_qsl(parsed.query)):
        raise PageUnavailable("Credential-bearing page URLs are not allowed")
    addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    ips = [record[4][0] for record in addresses]
    if not ips or any(not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast for ip in ips):
        raise PageUnavailable("Private, local, reserved, or mixed-address targets are not allowed")
    return parsed.scheme, parsed.hostname, port, ips[0]


def _request(url: str, target: tuple[str, str, int, str], timeout: float, user_agent: str):
    scheme, host, port, address = target
    # Connect to the validated address directly; no second hostname lookup/rebinding.
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    sock = socket.create_connection((address, port), timeout=timeout)
    response = None
    try:
        connection.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host) if scheme == "https" else sock
        parsed = urlsplit(url)
        path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        connection.request("GET", path, headers={"User-Agent": user_agent, "Accept": "text/html, text/plain, text/markdown", "Accept-Encoding": "identity"})
        response = connection.getresponse()
        headers = response.headers
        if response.status in {301, 302, 303, 307, 308}:
            return response.status, headers, b""
        if response.status != 200:
            raise PageUnavailable(f"Page returned HTTP {response.status}")
        if headers.get_content_type() not in {"text/html", "text/plain", "text/markdown"}:
            raise PageUnavailable("Unsupported page content type")
        if headers.get("Content-Encoding", "identity").lower() != "identity":
            raise PageUnavailable("Compressed page responses are not supported")
        body = bytearray()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PageUnavailable("Page read deadline exceeded")
            # HTTP/1.0 may detach the connection socket, but the response retains it.
            if connection.sock:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(16384, MAX_BYTES + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > MAX_BYTES:
                raise PageUnavailable("Page exceeds byte limit")
        return response.status, headers, bytes(body)
    finally:
        if response is not None:
            response.close()
        connection.close()
        sock.close()


def cached_page(url: str, config: AppConfig) -> dict | None:
    """Read an intact, unexpired document without DNS or network activity."""
    now = datetime.now(UTC)
    cache = config.database_path.with_suffix(".pages") / f"{digest([VERSION, url])}.json"
    try:
        saved = json.loads(cache.read_text())
        if (saved["version"] == VERSION and saved["requested_url"] == url
                and datetime.fromisoformat(saved["expires_at"]) > now
                and isinstance(saved["text"], str) and 80 <= len(saved["text"]) <= config.llm.max_input_chars
                and digest(saved["text"]) == saved["text_sha256"]):
            return {**saved, "cache_state": "cached"}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def fetch_page(url: str, config: AppConfig) -> dict:
    now = datetime.now(UTC)
    cache = config.database_path.with_suffix(".pages") / f"{digest([VERSION, url])}.json"
    saved = cached_page(url, config)
    if saved is not None:
        return saved
    current = url
    for hop in range(4):
        target = public_target(current)
        status, headers, body = _request(current, target, min(config.http.timeout_seconds, 10), config.http.user_agent)
        if status in {301, 302, 303, 307, 308}:
            if hop == 3 or not headers.get("Location"):
                raise PageUnavailable("Page redirect limit exceeded or missing destination")
            following = urljoin(current, headers["Location"])
            if urlsplit(current).scheme == "https" and urlsplit(following).scheme != "https":
                raise PageUnavailable("HTTPS downgrade redirect blocked")
            current = following
            continue
        raw = body.decode(headers.get_content_charset() or "utf-8", errors="replace")
        if headers.get_content_type() == "text/html":
            parser = PageText()
            parser.feed(raw)
            text = parser.text()
        else:
            text = raw.strip()
        if len(text) < 80:
            raise PageUnavailable("Page has insufficient readable evidence; no headline-only scoring")
        if len(text) > config.llm.max_input_chars:
            raise PageUnavailable("Page text exceeds model input limit; not truncated")
        saved = {"version": VERSION, "requested_url": url, "final_url": current, "text": text,
            "text_sha256": digest(text), "fetched_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=config.http.cache_ttl_minutes)).isoformat(), "cache_state": "live"}
        _atomic_write(cache, json.dumps(saved, ensure_ascii=False, indent=2) + "\n")
        return saved
    raise PageUnavailable("Page unavailable")
