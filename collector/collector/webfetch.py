"""HTTP fetching behind a URL boundary check.

Task book 004, stage D:

    "站外跳转、非 HTTP URL、私网/回环地址及重定向链需要边界检查，避免把不可信
     网址当本地资源访问。"

The scraper output is untrusted input: a business "website" field is whatever a
stranger put on a Google Maps listing. Handing such a string straight to an HTTP
client lets a listing point the collector at ``http://127.0.0.1:8080/admin``,
``http://169.254.169.254/`` (cloud metadata) or an internal host name, and the
collector would happily fetch it from inside whatever network it runs on.

So every URL is checked *before* it is opened, and every redirect hop is checked
again, because a perfectly public URL can redirect into private space.

Checks
------
* scheme must be ``http`` or ``https`` — no ``file:``, ``ftp:``, ``gopher:``,
  ``data:``, ``javascript:``;
* no ``user:password@`` in the URL;
* a host must be present and the port, if written, must parse;
* the host must not be, or resolve to, a non-global address: private (RFC 1918),
  loopback, link-local, unique-local, reserved, multicast or unspecified;
* a redirect to a *different registrable domain* is recorded and then refused
  rather than followed.

The last point is a policy choice, not a safety one: "同域" in the task book
means enrichment stays on the business's own site. The off-site URL is kept in
the redirect chain as evidence.

``allow_loopback`` / ``allow_private`` exist so the test suite and the local
acceptance server can be reached; production callers leave them at ``False``.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

DEFAULT_USER_AGENT = (
    "osint-collector/0.1 (public business website enrichment; contact pages only)"
)
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 5

#: Content types worth parsing. Anything else (PDF, images, binaries) is
#: recorded and skipped rather than decoded as HTML.
TEXTUAL_CONTENT_TYPES = (
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "application/xml",
    "text/xml",
)

#: Two-label public suffixes that are common enough to be worth knowing. This is
#: deliberately a short list, not a copy of the Public Suffix List: it only has
#: to keep ``example.co.uk`` and ``shop.example.co.uk`` on the same site. The
#: limitation is documented in the README.
_MULTI_LABEL_SUFFIXES = frozenset(
    {
        "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk",
        "com.au", "net.au", "org.au", "edu.au", "gov.au", "id.au",
        "co.nz", "net.nz", "org.nz", "govt.nz",
        "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp",
        "com.br", "net.br", "org.br", "gov.br",
        "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn",
        "com.hk", "org.hk", "edu.hk", "gov.hk", "net.hk",
        "com.tw", "org.tw", "edu.tw", "gov.tw", "net.tw",
        "co.kr", "or.kr", "ne.kr", "go.kr", "re.kr",
        "co.in", "net.in", "org.in", "gov.in", "ac.in", "firm.in",
        "com.mx", "com.tr", "com.sg", "com.my", "com.ph", "com.vn",
        "com.pk", "com.sa", "com.eg", "com.ng", "com.pe", "com.ar",
        "com.co", "com.pl", "com.ua", "com.ru", "com.es", "com.pt",
        "com.gr", "com.hr", "com.ro", "com.bg", "com.cy",
        "co.za", "co.ke", "co.tz", "co.ug", "co.zw", "co.bw", "co.mz",
        "co.il", "co.th", "co.id", "co.at", "co.hu", "co.cr", "co.ve",
        "co.jp", "co.nz", "co.uk",
    }
)


#: Host names that never leave the machine, whatever DNS says. Relying on
#: resolution alone is not enough: ``localhost`` may be answered from a hosts
#: file or an intercepting resolver, and ``*.local`` is mDNS.
_LOCAL_HOST_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})
_LOCAL_HOST_SUFFIXES = (".localhost", ".local", ".localdomain", ".internal", ".home.arpa")

#: Characters that cannot appear in a host and that indicate a value was never
#: a URL at all (a sentence, a stray note from the listing).
_INVALID_HOST_CHARS = re.compile(r'[\s<>"{}|\\^`\[\]]')


def registrable_domain(host: str) -> str:
    """The site a host belongs to: ``shop.example.co.uk`` -> ``example.co.uk``.

    IP literals are returned unchanged, because they have no registrable domain
    and must never be treated as "the same site" as anything else.
    """
    host = (host or "").strip().strip(".").lower()
    if not host:
        return ""
    if _is_ip_literal(host):
        return host
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    last_two = ".".join(labels[-2:])
    if last_two in _MULTI_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return last_two


def same_site(host_a: str, host_b: str) -> bool:
    """True when two hosts belong to the same registrable domain."""
    left = registrable_domain(host_a)
    right = registrable_domain(host_b)
    return bool(left) and left == right


def normalize_url(value: object) -> str:
    """Give a scheme-less listing value a scheme, and change nothing else.

    Google Maps listings sometimes carry ``www.example.de`` or
    ``example.de:8080/kontakt`` without a scheme. Those are real websites and
    must not be thrown away, so ``https://`` is prepended.

    A value that already names a *different* scheme (``javascript:``,
    ``mailto:``, ``data:``, a Windows drive letter) is returned untouched, so
    the policy can reject it as a non-HTTP URL instead of it being silently
    rewritten into something fetchable.
    """
    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    if "://" in text:
        return text
    if text.startswith("//"):
        return "https:" + text

    head, sep, _rest = text.partition(":")
    if sep and "/" not in head:
        # ``example.com:8080/x`` has a dot in the head, a scheme does not.
        if "." not in head and head.lower() not in _LOCAL_HOST_NAMES:
            return text
    return "https://" + text


#: Characters that carry meaning in a URL and must survive percent-encoding
#: untouched. Everything outside this set — above all non-ASCII letters — is
#: encoded, because ``http.client`` writes the request line as ASCII.
_URL_SAFE = ":/?#[]@!$&'()*+,;=%~"


def encode_url(url: str) -> str:
    """Percent-encode the parts of a URL ``http.client`` cannot send as ASCII.

    Real directories carry business names with umlauts and accents in their
    detail URLs (``.../Rechtsanwaltsgesellschaft-Berlin-Kurfürstendamm``). The
    link is correct, but ``http.client`` writes the request line as ASCII, so an
    un-encoded value raises ``UnicodeEncodeError`` before any bytes leave the
    process — a source would fail on every accented entry, not on the one page
    that happened to contain one. Encoding here keeps the whole pipeline
    working, and is idempotent: an already-encoded ``%C3%BC`` is left alone,
    because ``%`` is in the safe set. A URL that is already pure ASCII and
    reserved-character clean comes back byte-for-byte identical.
    """
    text = "" if url is None else str(url)
    if not text:
        return ""
    return urllib.parse.quote(text, safe=_URL_SAFE)


def _is_ip_literal(host: str) -> bool:
    candidate = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return True


#: Reason codes returned by :meth:`UrlPolicy.check`. Kept as constants so tests
#: and reports can assert on them instead of on prose.
REASON_EMPTY_URL = "empty_url"
REASON_MALFORMED_URL = "malformed_url"
REASON_NON_HTTP_SCHEME = "non_http_scheme"
REASON_CREDENTIALS_IN_URL = "credentials_in_url"
REASON_MISSING_HOST = "missing_host"
REASON_INVALID_PORT = "invalid_port"
REASON_LOOPBACK_ADDRESS = "loopback_address"
REASON_PRIVATE_ADDRESS = "private_address"
REASON_LINK_LOCAL_ADDRESS = "link_local_address"
REASON_RESERVED_ADDRESS = "reserved_address"
REASON_NON_GLOBAL_ADDRESS = "non_global_address"
REASON_DNS_ERROR = "dns_error"


@dataclass
class UrlCheck:
    """The verdict for one URL."""

    ok: bool
    url: str
    host: str = ""
    scheme: str = ""
    port: int | None = None
    registrable: str = ""
    reason: str = ""
    detail: str = ""
    resolved: tuple[str, ...] = ()

    @property
    def code(self) -> str:
        return "" if self.ok else self.reason


def _address_verdict(address: ipaddress._BaseAddress) -> str:
    """Why an address must not be fetched, or '' when it is fine."""
    if address.is_loopback:
        return REASON_LOOPBACK_ADDRESS
    if address.is_link_local:
        return REASON_LINK_LOCAL_ADDRESS
    if address.is_private:
        return REASON_PRIVATE_ADDRESS
    if address.is_multicast or address.is_reserved or address.is_unspecified:
        return REASON_RESERVED_ADDRESS
    if not address.is_global:
        # TEST-NET, benchmarking ranges, CGNAT (100.64/10), documentation
        # prefixes and the like are not reachable on the public internet.
        return REASON_NON_GLOBAL_ADDRESS
    return ""


def default_resolver(host: str) -> tuple[str, ...]:
    """Resolve a host to its address strings. Raises ``socket.gaierror``."""
    infos = socket.getaddrinfo(host, None)
    out: list[str] = []
    for info in infos:
        sockaddr = info[4]
        address = str(sockaddr[0])
        if address not in out:
            out.append(address)
    return tuple(out)


class UrlPolicy:
    """Decides whether a URL may be fetched.

    ``resolver`` is injectable so tests never touch the network, and
    ``allow_loopback`` / ``allow_private`` exist for the local acceptance
    server. Both default to ``False``: an untrusted listing must not be able to
    aim the collector at the machine it runs on.
    """

    def __init__(
        self,
        *,
        allow_loopback: bool = False,
        allow_private: bool = False,
        resolver: Callable[[str], Iterable[str]] | None = None,
        resolve_hosts: bool = True,
    ):
        self.allow_loopback = allow_loopback
        self.allow_private = allow_private
        self._resolver = resolver or default_resolver
        self.resolve_hosts = resolve_hosts

    def check(self, url: object) -> UrlCheck:
        text = "" if url is None else str(url).strip()
        if not text:
            return UrlCheck(False, text, reason=REASON_EMPTY_URL)

        try:
            parts = urllib.parse.urlsplit(text)
        except ValueError as exc:
            return UrlCheck(False, text, reason=REASON_MALFORMED_URL, detail=str(exc))

        scheme = (parts.scheme or "").lower()
        if scheme not in ("http", "https"):
            return UrlCheck(
                False,
                text,
                scheme=scheme,
                reason=REASON_NON_HTTP_SCHEME,
                detail=f"scheme {scheme or '(none)'!r} is not http/https",
            )

        if parts.username or parts.password:
            return UrlCheck(
                False,
                text,
                scheme=scheme,
                reason=REASON_CREDENTIALS_IN_URL,
                detail="the URL embeds credentials",
            )

        host = (parts.hostname or "").lower()
        if not host:
            return UrlCheck(False, text, scheme=scheme, reason=REASON_MISSING_HOST)

        if _INVALID_HOST_CHARS.search(host):
            return UrlCheck(
                False,
                text,
                scheme=scheme,
                host=host,
                reason=REASON_MALFORMED_URL,
                detail=f"host {host!r} contains characters that cannot appear in a host",
            )

        if host in _LOCAL_HOST_NAMES or host.endswith(_LOCAL_HOST_SUFFIXES):
            if not (self.allow_loopback and self.allow_private):
                return UrlCheck(
                    False,
                    text,
                    scheme=scheme,
                    host=host,
                    reason=REASON_LOOPBACK_ADDRESS,
                    detail=f"{host} always refers to the local machine",
                )

        try:
            port = parts.port
        except ValueError as exc:
            return UrlCheck(
                False, text, scheme=scheme, host=host, reason=REASON_INVALID_PORT, detail=str(exc)
            )

        base = {
            "url": text,
            "host": host,
            "scheme": scheme,
            "port": port,
            "registrable": registrable_domain(host),
        }

        if self.allow_private and self.allow_loopback:
            return UrlCheck(True, **base)

        if _is_ip_literal(host):
            literal = host[1:-1] if host.startswith("[") and host.endswith("]") else host
            address = ipaddress.ip_address(literal)
            if self._allowed_special(address):
                return UrlCheck(True, resolved=(literal,), **base)
            reason = _address_verdict(address)
            if reason:
                return UrlCheck(
                    False,
                    reason=reason,
                    detail=f"{host} is {reason.replace('_', ' ')}",
                    resolved=(literal,),
                    **base,
                )
            return UrlCheck(True, resolved=(literal,), **base)

        if not self.resolve_hosts:
            return UrlCheck(True, **base)

        try:
            resolved = tuple(self._resolver(host))
        except socket.gaierror as exc:
            return UrlCheck(
                False, reason=REASON_DNS_ERROR, detail=f"could not resolve {host}: {exc}", **base
            )
        except OSError as exc:
            return UrlCheck(
                False, reason=REASON_DNS_ERROR, detail=f"could not resolve {host}: {exc}", **base
            )

        if not resolved:
            return UrlCheck(
                False, reason=REASON_DNS_ERROR, detail=f"{host} resolved to no address", **base
            )

        for item in resolved:
            try:
                address = ipaddress.ip_address(str(item).split("%")[0])
            except ValueError:
                return UrlCheck(
                    False,
                    reason=REASON_DNS_ERROR,
                    detail=f"{host} resolved to unparsable address {item!r}",
                    resolved=resolved,
                    **base,
                )
            if self._allowed_special(address):
                continue
            reason = _address_verdict(address)
            if reason:
                # Any non-global address behind the name disqualifies it: a
                # public A record must not be able to hide a private one.
                return UrlCheck(
                    False,
                    reason=reason,
                    detail=f"{host} resolves to {item} ({reason.replace('_', ' ')})",
                    resolved=resolved,
                    **base,
                )

        return UrlCheck(True, resolved=resolved, **base)

    def _allowed_special(self, address: ipaddress._BaseAddress) -> bool:
        if self.allow_private:
            return True
        if self.allow_loopback and address.is_loopback:
            return True
        return False


# --- fetching ---------------------------------------------------------------


class _Redirect(Exception):
    """Raised by the no-follow redirect handler to hand a hop back to the loop."""

    def __init__(self, url: str, code: int):
        super().__init__(url)
        self.url = url
        self.code = code


class _NoAutoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise _Redirect(newurl, code)


@dataclass
class FetchResult:
    """Outcome of one fetch, including why it did not happen."""

    requested_url: str
    final_url: str = ""
    ok: bool = False
    status_code: int | None = None
    content_type: str = ""
    charset: str = ""
    body: str = ""
    body_bytes: int = 0
    truncated: bool = False
    redirect_chain: list[str] = field(default_factory=list)
    error_kind: str = ""
    error_detail: str = ""
    retry_after_seconds: int | None = None
    elapsed_seconds: float = 0.0
    attempts: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "ok": self.ok,
            "status_code": self.status_code,
            "content_type": self.content_type,
            "body_bytes": self.body_bytes,
            "truncated": self.truncated,
            "redirect_chain": list(self.redirect_chain),
            "error_kind": self.error_kind,
            "error_detail": self.error_detail,
            "retry_after_seconds": self.retry_after_seconds,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "attempts": self.attempts,
        }


class PageCache:
    """Page bodies this run already obtained, keyed by requested URL.

    Candidate-site discovery reads a site's contact/Impressum page to decide
    whether the site belongs to the business; the enrichment stage then reads
    what, on a German site, is very often the same page. Both happen inside one
    run and minutes apart, so the second read spends a request against the
    site for an answer already in hand (045.3).

    Only *successful* reads are stored: a failure is not an answer, and
    re-asking is governed by the retry/cooldown rules, not by this cache. A
    hit charges no request, so a caller must not count one.
    """

    def __init__(self) -> None:
        self._results: dict[str, FetchResult] = {}
        #: How many lookups were answered without a request. Reported so "the
        #: budget went further" is visible rather than looking like a miscount.
        self.hits = 0

    @staticmethod
    def key(url: str) -> str:
        normalized = normalize_url(url or "")
        return normalized or (url or "").strip()

    def get(self, url: str) -> FetchResult | None:
        found = self._results.get(self.key(url))
        if found is not None:
            self.hits += 1
        return found

    def put(self, result: FetchResult) -> None:
        if not result.ok or not result.body:
            return
        for url in (result.requested_url, result.final_url):
            if url:
                self._results.setdefault(self.key(url), result)

    def __len__(self) -> int:
        return len(self._results)


def _parse_retry_after(value: str | None) -> int | None:
    if not value:
        return None
    text = value.strip()
    if text.isdigit():
        return int(text)
    # An HTTP-date form is accepted but only its presence is reported.
    return None


def _classify_url_error(exc: urllib.error.URLError) -> tuple[str, str]:
    reason = exc.reason
    if isinstance(reason, socket.timeout) or isinstance(reason, TimeoutError):
        return "timeout", f"timed out: {reason}"
    if isinstance(reason, socket.gaierror):
        return "dns_error", f"dns error: {reason}"
    if isinstance(reason, OSError) and "ssl" in type(reason).__name__.lower():
        return "tls_error", str(reason)
    text = str(reason)
    if "certificate" in text.lower() or "ssl" in text.lower():
        return "tls_error", text
    return "connection_error", text


class Fetcher:
    """A small, policy-checked HTTP client.

    Deliberately synchronous and sequential: enrichment spends a small, explicit
    page budget per site, so a thread pool would buy nothing and would make the
    politeness delay and the budget harder to reason about.
    """

    def __init__(
        self,
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        user_agent: str = DEFAULT_USER_AGENT,
        proxy: str = "",
        policy: UrlPolicy | None = None,
        trust_env_proxy: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        max_retry_wait_seconds: int = 5,
        max_attempts: int = 2,
    ):
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.user_agent = user_agent
        self.proxy = proxy
        self.policy = policy or UrlPolicy()
        self.sleep = sleep
        self.max_retry_wait_seconds = max_retry_wait_seconds
        self.max_attempts = max(1, max_attempts)
        self._opener = self._build_opener(trust_env_proxy)

    def _build_opener(self, trust_env_proxy: bool) -> urllib.request.OpenerDirector:
        handlers: list[urllib.request.BaseHandler] = [_NoAutoRedirect()]
        if self.proxy:
            handlers.append(
                urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy})
            )
        elif not trust_env_proxy:
            handlers.append(urllib.request.ProxyHandler({}))
        return urllib.request.build_opener(*handlers)

    # -- public API ----------------------------------------------------------

    def fetch(
        self,
        url: str,
        *,
        allowed_domains: Iterable[str] | None = None,
        accept_types: tuple[str, ...] = TEXTUAL_CONTENT_TYPES,
    ) -> FetchResult:
        """Fetch one URL, following only same-site redirects.

        ``allowed_domains`` is a set of registrable domains; a redirect landing
        outside it is recorded and refused. Pass ``None`` to allow any *public*
        redirect target (the boundary checks still apply).
        """
        allowed = {d.lower() for d in allowed_domains} if allowed_domains is not None else None
        started = time.monotonic()
        last: FetchResult | None = None

        # A link read off a real page may carry non-ASCII characters (umlauts in
        # a German business name, accents in a French one). Encode once here so
        # every hop, and the recorded URL, is something http.client can send.
        url = encode_url(url)

        for attempt in range(1, self.max_attempts + 1):
            result = self._fetch_once(url, allowed=allowed, accept_types=accept_types)
            result.attempts = attempt
            result.elapsed_seconds = time.monotonic() - started
            last = result

            retryable = result.status_code in (429, 503) and attempt < self.max_attempts
            if not retryable:
                return result

            wait = result.retry_after_seconds
            if wait is None or wait > self.max_retry_wait_seconds:
                # Waiting longer than the cap is the caller's decision, not the
                # fetcher's: report the server's wish and stop.
                return result
            self.sleep(wait)

        assert last is not None  # pragma: no cover - loop always runs
        return last

    # -- internals -----------------------------------------------------------

    def _fetch_once(
        self,
        url: str,
        *,
        allowed: set[str] | None,
        accept_types: tuple[str, ...],
    ) -> FetchResult:
        chain: list[str] = []
        current = url

        for hop in range(self.max_redirects + 1):
            check = self.policy.check(current)
            if not check.ok:
                if hop > 0 and chain:
                    # The redirect target itself is kept as evidence.
                    chain.append(current)
                return FetchResult(
                    requested_url=url,
                    redirect_chain=chain,
                    error_kind=check.reason,
                    error_detail=check.detail or f"{check.reason} for {current}",
                )

            if hop > 0 and allowed is not None and check.registrable not in allowed:
                chain.append(current)
                return FetchResult(
                    requested_url=url,
                    redirect_chain=chain,
                    error_kind="offsite_redirect",
                    error_detail=(
                        f"redirect left the site: {check.host} is not in "
                        f"{sorted(allowed)}"
                    ),
                )

            chain.append(current)

            request = urllib.request.Request(
                current,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
                    "Accept-Language": "*",
                    # No compressed transfer: the body is read and decoded here,
                    # and an identity response keeps that path small.
                    "Accept-Encoding": "identity",
                },
                method="GET",
            )

            try:
                response = self._opener.open(request, timeout=self.timeout_seconds)
            except _Redirect as redirect:
                current = encode_url(redirect.url)
                continue
            except urllib.error.HTTPError as exc:
                retry_after = _parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
                exc.close()
                return FetchResult(
                    requested_url=url,
                    final_url=current,
                    status_code=exc.code,
                    redirect_chain=chain,
                    error_kind="http_error",
                    error_detail=f"HTTP {exc.code} {exc.reason}",
                    retry_after_seconds=retry_after,
                )
            except urllib.error.URLError as exc:
                kind, detail = _classify_url_error(exc)
                return FetchResult(
                    requested_url=url,
                    final_url=current,
                    redirect_chain=chain,
                    error_kind=kind,
                    error_detail=detail,
                )
            except (socket.timeout, TimeoutError):
                return FetchResult(
                    requested_url=url,
                    final_url=current,
                    redirect_chain=chain,
                    error_kind="timeout",
                    error_detail=f"no response within {self.timeout_seconds}s",
                )
            except OSError as exc:
                return FetchResult(
                    requested_url=url,
                    final_url=current,
                    redirect_chain=chain,
                    error_kind="connection_error",
                    error_detail=str(exc),
                )

            with response:
                status = int(getattr(response, "status", 0) or 0)
                headers = response.headers
                content_type = (headers.get_content_type() or "").lower()
                charset = headers.get_content_charset() or ""

                if status >= 400:
                    return FetchResult(
                        requested_url=url,
                        final_url=current,
                        status_code=status,
                        redirect_chain=chain,
                        error_kind="http_error",
                        error_detail=f"HTTP {status}",
                    )

                if content_type and not _is_textual(content_type, accept_types):
                    return FetchResult(
                        requested_url=url,
                        final_url=current,
                        status_code=status,
                        content_type=content_type,
                        redirect_chain=chain,
                        error_kind="unsupported_content_type",
                        error_detail=f"content type {content_type!r} is not parsed as HTML",
                    )

                raw, truncated = self._read_limited(response)
                body = _decode(raw, charset)

                return FetchResult(
                    requested_url=url,
                    final_url=current,
                    ok=True,
                    status_code=status,
                    content_type=content_type or "text/html",
                    charset=charset or "utf-8",
                    body=body,
                    body_bytes=len(raw),
                    truncated=truncated,
                    redirect_chain=chain,
                )

        return FetchResult(
            requested_url=url,
            final_url=chain[-1] if chain else url,
            redirect_chain=chain,
            error_kind="too_many_redirects",
            error_detail=f"more than {self.max_redirects} redirects",
        )

    def _read_limited(self, response) -> tuple[bytes, bool]:
        limit = self.max_bytes
        chunks: list[bytes] = []
        total = 0
        while total <= limit:
            chunk = response.read(min(65536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        data = b"".join(chunks)
        if len(data) > limit:
            return data[:limit], True
        return data, False


def _is_textual(content_type: str, accept_types: tuple[str, ...]) -> bool:
    if content_type in accept_types:
        return True
    return content_type.startswith("text/")


def _decode(raw: bytes, charset: str) -> str:
    for encoding in (charset, "utf-8", "cp1252", "latin-1"):
        if not encoding:
            continue
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")
