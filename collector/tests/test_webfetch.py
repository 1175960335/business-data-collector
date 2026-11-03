"""URL boundary checks and the policy-checked HTTP client."""

from __future__ import annotations

import unittest

from collector.webfetch import (
    REASON_CREDENTIALS_IN_URL,
    REASON_DNS_ERROR,
    REASON_EMPTY_URL,
    REASON_INVALID_PORT,
    REASON_LINK_LOCAL_ADDRESS,
    REASON_LOOPBACK_ADDRESS,
    REASON_MALFORMED_URL,
    REASON_MISSING_HOST,
    REASON_NON_HTTP_SCHEME,
    REASON_PRIVATE_ADDRESS,
    Fetcher,
    UrlPolicy,
    encode_url,
    normalize_url,
    registrable_domain,
    same_site,
)

from .stub_site import Page, SiteServer
from .support import local_fetcher

PUBLIC = lambda host: ("93.184.216.34",)  # noqa: E731 - a fixed public address


def policy(**kwargs) -> UrlPolicy:
    kwargs.setdefault("resolver", PUBLIC)
    return UrlPolicy(**kwargs)


class SchemeTest(unittest.TestCase):
    def test_http_and_https_are_allowed(self):
        self.assertTrue(policy().check("https://example.com/x").ok)
        self.assertTrue(policy().check("http://example.com/x").ok)

    def test_non_http_schemes_are_refused(self):
        for url in (
            "file:///etc/passwd",
            "ftp://example.com/x",
            "javascript:alert(1)",
            "data:text/html,<b>x</b>",
            "gopher://example.com/",
        ):
            with self.subTest(url=url):
                self.assertEqual(policy().check(url).reason, REASON_NON_HTTP_SCHEME)

    def test_empty_and_hostless_values_are_refused(self):
        self.assertEqual(policy().check("").reason, REASON_EMPTY_URL)
        self.assertEqual(policy().check(None).reason, REASON_EMPTY_URL)
        self.assertEqual(policy().check("https://").reason, REASON_MISSING_HOST)

    def test_credentials_in_the_url_are_refused(self):
        self.assertEqual(
            policy().check("https://user:pw@example.com/").reason, REASON_CREDENTIALS_IN_URL
        )

    def test_an_unparsable_port_is_refused(self):
        self.assertEqual(
            policy().check("http://example.com:99999/").reason, REASON_INVALID_PORT
        )

    def test_whitespace_in_the_host_is_refused(self):
        self.assertEqual(
            policy().check("https://exa mple.com/").reason, REASON_MALFORMED_URL
        )


class AddressBoundaryTest(unittest.TestCase):
    def test_loopback_is_refused(self):
        for url in ("http://127.0.0.1/", "http://127.1.2.3:8080/x", "https://[::1]/"):
            with self.subTest(url=url):
                self.assertEqual(policy().check(url).reason, REASON_LOOPBACK_ADDRESS)

    def test_local_host_names_are_refused_even_when_dns_lies(self):
        # The resolver returns a public address; the name itself is decisive.
        for url in ("http://localhost/x", "http://LOCALHOST:8080/", "https://db.local/x"):
            with self.subTest(url=url):
                self.assertEqual(policy().check(url).reason, REASON_LOOPBACK_ADDRESS)

    def test_private_ranges_are_refused(self):
        for url in ("http://10.0.0.5/", "http://192.168.1.1/", "http://172.16.9.9/"):
            with self.subTest(url=url):
                self.assertEqual(policy().check(url).reason, REASON_PRIVATE_ADDRESS)

    def test_link_local_metadata_endpoint_is_refused(self):
        self.assertEqual(
            policy().check("http://169.254.169.254/latest/meta-data/").reason,
            REASON_LINK_LOCAL_ADDRESS,
        )

    def test_a_host_name_resolving_to_a_private_address_is_refused(self):
        # DNS rebinding: the public-looking name points inside the network.
        checker = UrlPolicy(resolver=lambda host: ("10.1.2.3",))
        result = checker.check("https://internal.example.com/")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, REASON_PRIVATE_ADDRESS)
        self.assertIn("10.1.2.3", result.detail)

    def test_one_private_address_disqualifies_a_name(self):
        checker = UrlPolicy(resolver=lambda host: ("93.184.216.34", "192.168.0.9"))
        self.assertEqual(checker.check("https://mixed.example.com/").reason, REASON_PRIVATE_ADDRESS)

    def test_a_dns_failure_is_reported_as_such(self):
        import socket

        def failing(host):
            raise socket.gaierror("no such host")

        self.assertEqual(
            UrlPolicy(resolver=failing).check("https://nope.invalid/").reason, REASON_DNS_ERROR
        )

    def test_loopback_can_be_allowed_for_the_local_acceptance_server(self):
        self.assertTrue(
            UrlPolicy(allow_loopback=True, allow_private=True).check("http://127.0.0.1:8000/").ok
        )


class DomainTest(unittest.TestCase):
    def test_registrable_domain_collapses_subdomains(self):
        self.assertEqual(registrable_domain("shop.example.de"), "example.de")
        self.assertEqual(registrable_domain("www.example.de"), "example.de")
        self.assertEqual(registrable_domain("example.de"), "example.de")

    def test_multi_label_public_suffixes(self):
        self.assertEqual(registrable_domain("shop.example.co.uk"), "example.co.uk")
        self.assertEqual(registrable_domain("a.b.example.co.uk"), "example.co.uk")

    def test_ip_literals_are_not_collapsed(self):
        self.assertEqual(registrable_domain("127.0.0.1"), "127.0.0.1")
        self.assertEqual(registrable_domain("::1"), "::1")

    def test_same_site(self):
        self.assertTrue(same_site("www.example.de", "example.de"))
        self.assertFalse(same_site("example.de", "example.com"))
        self.assertFalse(same_site("example.de", ""))

    def test_normalize_url_adds_a_scheme_only_when_it_is_missing(self):
        self.assertEqual(normalize_url("www.example.de"), "https://www.example.de")
        self.assertEqual(normalize_url("example.de:8080/k"), "https://example.de:8080/k")
        self.assertEqual(normalize_url("//example.com/x"), "https://example.com/x")
        self.assertEqual(normalize_url("https://ok.de"), "https://ok.de")
        # A different scheme is left alone so the policy can reject it.
        self.assertEqual(normalize_url("javascript:alert(1)"), "javascript:alert(1)")
        self.assertEqual(normalize_url("mailto:a@b.de"), "mailto:a@b.de")


class EncodeUrlTest(unittest.TestCase):
    def test_non_ascii_is_percent_encoded(self):
        self.assertEqual(
            encode_url("https://d.example/Themen/Müller"),
            "https://d.example/Themen/M%C3%BCller",
        )

    def test_ascii_urls_are_returned_unchanged(self):
        url = "https://d.example/a?b=c&d=e#f"
        self.assertEqual(encode_url(url), url)

    def test_an_already_encoded_url_is_left_alone(self):
        self.assertEqual(encode_url("https://d.example/M%C3%BCller"), "https://d.example/M%C3%BCller")

    def test_encoded_and_raw_characters_can_be_mixed(self):
        # The percent sign stays in the safe set, so an existing escape survives
        # while the still-raw character is encoded — no double-encoding.
        self.assertEqual(
            encode_url("https://d.example/M%C3%BCller-ü"),
            "https://d.example/M%C3%BCller-%C3%BC",
        )

    def test_reserved_characters_are_not_touched(self):
        url = "https://d.example/Themen/A-Z?kw=Rechtsanwalt&ci=Berlin#treffer"
        self.assertEqual(encode_url(url), url)

    def test_a_space_is_encoded(self):
        self.assertEqual(encode_url("https://d.example/a b"), "https://d.example/a%20b")


class FetchTest(unittest.TestCase):
    def test_a_page_is_returned_with_its_content_type(self):
        pages = {"/": Page(body="<html>hallo</html>")}
        with SiteServer(pages) as site:
            result = local_fetcher().fetch(site.url("/"))
        self.assertTrue(result.ok, result.error_detail)
        self.assertEqual(result.status_code, 200)
        self.assertIn("hallo", result.body)
        self.assertEqual(result.redirect_chain, [site.url("/")])

    def test_a_url_with_non_ascii_characters_is_fetched(self):
        # A real German directory's detail URL carries umlauts. Without
        # encoding, http.client raises UnicodeEncodeError before sending.
        pages = {"/%C3%BCber-uns": Page(body="<html>ok</html>")}
        with SiteServer(pages) as site:
            result = local_fetcher().fetch(site.url("/über-uns"))
        self.assertTrue(result.ok, result.error_detail)
        self.assertEqual(result.status_code, 200)
        self.assertIn("ok", result.body)

    def test_a_same_site_redirect_is_followed_and_recorded(self):
        pages = {
            "/": Page(status=302, location="/home"),
            "/home": Page(body="<html>start</html>"),
        }
        with SiteServer(pages) as site:
            result = local_fetcher().fetch(site.url("/"), allowed_domains={"127.0.0.1"})
        self.assertTrue(result.ok, result.error_detail)
        self.assertEqual(result.redirect_chain, [site.url("/"), site.url("/home")])
        self.assertIn("start", result.body)

    def test_an_off_site_redirect_is_recorded_then_refused(self):
        pages = {"/": Page(status=302, location="http://example.com/elsewhere")}
        with SiteServer(pages) as site:
            result = local_fetcher().fetch(site.url("/"), allowed_domains={"127.0.0.1"})
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "offsite_redirect")
        # The target is kept as evidence even though it was not fetched.
        self.assertIn("example.com", result.redirect_chain[-1])

    def test_a_redirect_loop_stops(self):
        pages = {
            "/a": Page(status=302, location="/b"),
            "/b": Page(status=302, location="/a"),
        }
        with SiteServer(pages) as site:
            result = local_fetcher(max_redirects=3).fetch(site.url("/a"), allowed_domains={"127.0.0.1"})
        self.assertEqual(result.error_kind, "too_many_redirects")
        self.assertEqual(len(result.redirect_chain), 4)

    def test_an_http_error_is_reported_with_its_status(self):
        pages = {"/boom": Page(status=500, body="nope")}
        with SiteServer(pages) as site:
            result = local_fetcher().fetch(site.url("/boom"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "http_error")
        self.assertEqual(result.status_code, 500)

    def test_a_non_textual_response_is_not_parsed(self):
        pages = {"/file.pdf": Page(status=200, content_type="application/pdf", body="%PDF-1.4")}
        with SiteServer(pages) as site:
            result = local_fetcher().fetch(site.url("/file.pdf"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "unsupported_content_type")
        self.assertEqual(result.body, "")

    def test_a_slow_response_times_out(self):
        pages = {"/slow": Page(body="<html>later</html>", delay=2.0)}
        with SiteServer(pages) as site:
            result = local_fetcher(timeout_seconds=1).fetch(site.url("/slow"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "timeout")

    def test_a_large_page_is_truncated_and_flagged(self):
        pages = {"/big": Page(body="x" * 5000)}
        with SiteServer(pages) as site:
            result = local_fetcher(max_bytes=1000).fetch(site.url("/big"))
        self.assertTrue(result.ok, result.error_detail)
        self.assertTrue(result.truncated)
        self.assertEqual(result.body_bytes, 1000)

    def test_the_policy_is_applied_before_any_request_is_made(self):
        # No server needed: a refused URL must not even be attempted.
        result = Fetcher(policy=UrlPolicy(), trust_env_proxy=False).fetch("http://10.0.0.1/")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, REASON_PRIVATE_ADDRESS)

    def test_a_429_is_retried_only_when_the_wait_is_within_the_cap(self):
        pages = {
            "/limited": Page(status=429, headers={"Retry-After": "1"}),
        }
        with SiteServer(pages) as site:
            fetcher = local_fetcher(max_attempts=2, max_retry_wait_seconds=5)
            result = fetcher.fetch(site.url("/limited"))
        self.assertEqual(result.status_code, 429)
        self.assertEqual(result.retry_after_seconds, 1)
        # It was retried once, so the server saw the path twice.
        self.assertEqual(site.hits.count("/limited"), 2)

    def test_a_long_retry_after_is_not_waited_out(self):
        pages = {"/limited": Page(status=503, headers={"Retry-After": "600"})}
        with SiteServer(pages) as site:
            fetcher = local_fetcher(max_attempts=2, max_retry_wait_seconds=5)
            result = fetcher.fetch(site.url("/limited"))
        self.assertEqual(result.retry_after_seconds, 600)
        self.assertEqual(site.hits.count("/limited"), 1)


if __name__ == "__main__":
    unittest.main()
