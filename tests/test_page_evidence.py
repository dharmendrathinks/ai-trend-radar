from email.message import Message
import json
import socket

import pytest

from ai_trend_radar import page_evidence as pages


def headers(kind="text/html", **extra):
    result = Message()
    result["Content-Type"] = kind
    for key, value in extra.items():
        result[key] = value
    return result


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(pages.socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com", "https://user:pass@example.com",
    "https://example.com:8080", "https://example.com/?api_key=secret", "https://example.com/?token=secret"])
def test_reject_unsafe_url_forms(url, public_dns):
    with pytest.raises(pages.PageUnavailable):
        pages.public_target(url)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1", "224.0.0.1", "192.0.2.1"])
def test_reject_private_and_reserved_dns_including_mixed_answers(address, monkeypatch):
    monkeypatch.setattr(pages.socket, "getaddrinfo", lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))])
    with pytest.raises(pages.PageUnavailable, match="Private"):
        pages.public_target("https://example.com")


def test_page_text_excludes_code_boilerplate_and_decodes_entities():
    parser = pages.PageText()
    parser.feed("<html><head><title>Ignore</title></head><body><nav>Ignore nav</nav><script>Ignore commands</script><h1>Agent reviews</h1><p>APIs &amp; tools <b>tested</b> by agents.</p><footer>Ignore footer</footer></body></html>")
    assert parser.text() == "Agent reviews\nAPIs & tools tested by agents."


def test_public_page_cache_avoids_repeat_network(config, monkeypatch, public_dns):
    calls = []
    def request(*args):
        calls.append(args)
        return 200, headers(), ("<p>Agents share firsthand observations about APIs and developer tools, with evidence and limitations.</p>").encode()
    monkeypatch.setattr(pages, "_request", request)
    first = pages.fetch_page("https://example.com", config)
    second = pages.fetch_page("https://example.com", config)
    assert first["text"] == second["text"] and second["cache_state"] == "cached" and len(calls) == 1
    assert first["final_url"] == "https://example.com"


def test_redirect_revalidates_destination(config, monkeypatch, public_dns):
    calls = []
    def resolve(host, *args, **kwargs):
        address = "127.0.0.1" if host == "internal.example" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]
    monkeypatch.setattr(pages.socket, "getaddrinfo", resolve)
    def request(*args):
        calls.append(args)
        return 302, headers(Location="https://internal.example/secrets"), b""
    monkeypatch.setattr(pages, "_request", request)
    with pytest.raises(pages.PageUnavailable, match="Private"):
        pages.fetch_page("https://example.com", config)
    assert len(calls) == 1


@pytest.mark.parametrize("target", ["https://example.com/loop", "http://example.com/downgrade"])
def test_redirect_loop_and_downgrade_are_bounded(config, monkeypatch, public_dns, target):
    calls = []
    def request(*args):
        calls.append(args)
        return 302, headers(Location=target), b""
    monkeypatch.setattr(pages, "_request", request)
    with pytest.raises(pages.PageUnavailable):
        pages.fetch_page("https://example.com", config)
    assert len(calls) <= 4


@pytest.mark.parametrize("text", ["Login", "x" * 20001])
def test_insufficient_and_oversized_text_not_scored(config, monkeypatch, public_dns, text):
    monkeypatch.setattr(pages, "_request", lambda *args: (200, headers("text/plain"), text.encode()))
    with pytest.raises(pages.PageUnavailable):
        pages.fetch_page("https://example.com", config)


def test_connection_pins_validated_ip_and_checks_size(monkeypatch):
    connected = []
    sent_headers = []
    class Sock:
        def settimeout(self, timeout): pass
        def close(self): pass
    class Response:
        status = 200
        headers = headers()
        def read1(self, size): return b"x" * size
        def close(self): pass
    class Connection:
        sock = None
        def __init__(self, *args, **kw): pass
        def request(self, method, path, headers): sent_headers.append(headers)
        def getresponse(self): return Response()
        def close(self): pass
    def connect(address, **kwargs):
        connected.append(address)
        return Sock()
    monkeypatch.setattr(pages.socket, "create_connection", connect)
    monkeypatch.setattr(pages.http.client, "HTTPConnection", Connection)
    with pytest.raises(pages.PageUnavailable, match="byte limit"):
        pages._request("http://example.com/", ("http", "example.com", 80, "93.184.216.34"), 10, "test-agent")
    assert connected == [("93.184.216.34", 80)]
    assert set(sent_headers[0]) == {"User-Agent", "Accept", "Accept-Encoding"}
