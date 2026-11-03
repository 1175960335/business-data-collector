"""A tiny local HTTP server: a deterministic stand-in for a business's website.

Real sites change under a test, cannot produce a 500 on demand and cannot be
made to time out reliably. This server can do all three, so the enrichment tests
exercise the real HTTP path (real sockets, real redirects, real content types)
without depending on the network.

It binds to 127.0.0.1 on an ephemeral port, which is also why the tests construct
``UrlPolicy(allow_loopback=True, allow_private=True)``: production refuses
loopback addresses, and a separate test asserts that it does.
"""

from __future__ import annotations

import http.server
import threading
import time
import urllib.parse
from dataclasses import dataclass, field


@dataclass
class Page:
    """One canned response."""

    status: int = 200
    content_type: str = "text/html; charset=utf-8"
    body: str = ""
    location: str = ""
    delay: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)


class _Handler(http.server.BaseHTTPRequestHandler):
    pages: dict[str, Page] = {}
    hits: list[str] = []

    # HTTP/1.0 (the default) closes the connection after each response, so no
    # handler thread lingers waiting for a keep-alive request. That keeps
    # server shutdown immediate instead of costing half a second per test.

    def log_message(self, *args) -> None:  # keep the test output clean
        return

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        path = urllib.parse.urlsplit(self.path).path
        type(self).hits.append(path)

        page = type(self).pages.get(path)
        if page is None:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "9")
            self.end_headers()
            self.wfile.write(b"not found")
            return

        if page.delay:
            time.sleep(page.delay)

        if page.status in (301, 302, 303, 307, 308):
            self.send_response(page.status)
            self.send_header("Location", page.location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        payload = page.body.encode("utf-8")
        self.send_response(page.status)
        self.send_header("Content-Type", page.content_type)
        self.send_header("Content-Length", str(len(payload)))
        for name, value in page.headers.items():
            self.send_header(name, value)
        self.end_headers()
        if payload:
            try:
                self.wfile.write(payload)
            except (ConnectionError, OSError):
                # The client closed early — which is exactly what happens when
                # a test asserts on an HTTP error status and drops the body.
                # Not a server fault, so it must not print a traceback.
                pass


class SiteServer:
    """Serves a fixed set of pages for the duration of a ``with`` block."""

    def __init__(self, pages: dict[str, Page]):
        handler = type("_BoundHandler", (_Handler,), {"pages": pages, "hits": []})
        self.handler = handler
        self.pages = pages
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.02), daemon=True
        )

    # -- lifecycle -----------------------------------------------------------

    def __enter__(self) -> "SiteServer":
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    # -- helpers -------------------------------------------------------------

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def hits(self) -> list[str]:
        return list(self.handler.hits)

    def url(self, path: str = "/") -> str:
        return f"http://127.0.0.1:{self.port}{path}"
