"""Email validation: syntax, then the domain's mail setup.

Every DNS outcome is scripted, so the states the task book asks to be stored
separately are asserted separately rather than inferred from whatever the
network happened to answer.
"""

from __future__ import annotations

import unittest

from collector.emailcheck import (
    DNS_NXDOMAIN,
    DNS_OK,
    DNS_TEMP_ERROR,
    DNS_TIMEOUT,
    DNS_UNAVAILABLE,
    STATE_DNS_TEMP_ERROR,
    STATE_DNS_TIMEOUT,
    STATE_DNS_UNAVAILABLE,
    STATE_DOMAIN_NOT_FOUND,
    STATE_MX_PRESENT,
    STATE_NO_MX_IMPLICIT_A,
    STATE_NOT_CHECKED,
    STATE_NULL_MX,
    STATE_PLACEHOLDER,
    STATE_SYNTAX_INVALID,
    check_email,
    check_emails,
)

from .support import FakeResolver


class SyntaxTest(unittest.TestCase):
    def test_a_malformed_address_is_invalid_without_any_lookup(self):
        resolver = FakeResolver()
        verdict = check_email("kaputt@@example.de", resolver=resolver)
        self.assertEqual(verdict.status, "invalid")
        self.assertEqual(verdict.state, STATE_SYNTAX_INVALID)
        self.assertEqual(resolver.mx_calls, [])  # a typo must not cost a lookup

    def test_an_empty_value_is_invalid(self):
        self.assertEqual(check_email("").status, "invalid")
        self.assertEqual(check_email(None).state, STATE_SYNTAX_INVALID)

    def test_a_placeholder_is_kept_but_marked_uncertain(self):
        resolver = FakeResolver()
        verdict = check_email("user@domain.com", resolver=resolver)
        self.assertEqual(verdict.status, "uncertain")
        self.assertEqual(verdict.state, STATE_PLACEHOLDER)
        self.assertEqual(resolver.mx_calls, [])

    def test_dns_can_be_switched_off(self):
        verdict = check_email("a@b.de", check_dns=False)
        self.assertEqual(verdict.state, STATE_NOT_CHECKED)
        self.assertEqual(verdict.status, "unverified")


class DomainTest(unittest.TestCase):
    def test_a_domain_with_mx_records_is_valid_but_never_called_deliverable(self):
        resolver = FakeResolver(mx={"praxis.de": (DNS_OK, ("mx1.praxis.de", "mx2.praxis.de"))})
        verdict = check_email("info@praxis.de", resolver=resolver)
        self.assertEqual(verdict.state, STATE_MX_PRESENT)
        self.assertEqual(verdict.status, "valid")
        self.assertEqual(verdict.mx_hosts, ("mx1.praxis.de", "mx2.praxis.de"))
        self.assertIn("never sends mail", verdict.note)
        self.assertIn("not established", verdict.note)

    def test_a_null_mx_is_invalid_and_says_why(self):
        # RFC 7505: a single MX pointing at the root means "no mail here".
        resolver = FakeResolver(mx={"example.com": (DNS_OK, ("",))})
        verdict = check_email("x@example.com", resolver=resolver)
        self.assertEqual(verdict.state, STATE_NULL_MX)
        self.assertEqual(verdict.status, "invalid")
        self.assertIn("RFC 7505", verdict.note)
        self.assertTrue(verdict.checks["null_mx"])

    def test_no_mx_but_an_address_record_falls_back_and_stays_uncertain(self):
        resolver = FakeResolver(
            mx={"alt.example": ("noanswer", ())},
            addresses={"alt.example": (DNS_OK, ("93.184.216.34",))},
        )
        verdict = check_email("info@alt.example", resolver=resolver)
        self.assertEqual(verdict.state, STATE_NO_MX_IMPLICIT_A)
        self.assertEqual(verdict.status, "uncertain")
        self.assertIn("RFC 5321", verdict.note)

    def test_no_mx_and_no_address_record_means_the_domain_does_not_exist(self):
        resolver = FakeResolver(mx={"weg.example": ("noanswer", ())},
                                addresses={"weg.example": (DNS_NXDOMAIN, ())})
        verdict = check_email("a@weg.example", resolver=resolver)
        self.assertEqual(verdict.state, STATE_DOMAIN_NOT_FOUND)
        self.assertEqual(verdict.status, "invalid")

    def test_nxdomain_on_the_mx_lookup_is_reported_as_such(self):
        resolver = FakeResolver(mx={"weg.example": (DNS_NXDOMAIN, ())})
        verdict = check_email("a@weg.example", resolver=resolver)
        self.assertEqual(verdict.state, STATE_DOMAIN_NOT_FOUND)
        self.assertEqual(verdict.dns_status, DNS_NXDOMAIN)

    def test_a_dns_timeout_leaves_the_address_unjudged(self):
        resolver = FakeResolver(mx={"langsam.example": (DNS_TIMEOUT, ())})
        verdict = check_email("a@langsam.example", resolver=resolver)
        self.assertEqual(verdict.state, STATE_DNS_TIMEOUT)
        self.assertEqual(verdict.status, "uncertain")
        self.assertIn("left unjudged", verdict.note)

    def test_a_timeout_on_the_fallback_lookup_is_also_reported(self):
        resolver = FakeResolver(
            mx={"halb.example": ("noanswer", ())},
            addresses={"halb.example": (DNS_TIMEOUT, ())},
        )
        verdict = check_email("a@halb.example", resolver=resolver)
        self.assertEqual(verdict.state, STATE_DNS_TIMEOUT)

    def test_a_temporary_dns_failure_is_distinct_from_a_timeout(self):
        resolver = FakeResolver(mx={"wackel.example": (DNS_TEMP_ERROR, ())})
        verdict = check_email("a@wackel.example", resolver=resolver)
        self.assertEqual(verdict.state, STATE_DNS_TEMP_ERROR)
        self.assertEqual(verdict.status, "uncertain")

    def test_an_unavailable_resolver_is_distinct_from_a_failure(self):
        resolver = FakeResolver(mx={"x.example": (DNS_UNAVAILABLE, ())})
        verdict = check_email("a@x.example", resolver=resolver)
        self.assertEqual(verdict.state, STATE_DNS_UNAVAILABLE)
        self.assertEqual(verdict.status, "uncertain")


class BatchTest(unittest.TestCase):
    def test_each_domain_is_looked_up_only_once(self):
        resolver = FakeResolver(mx={"praxis.de": (DNS_OK, ("mx.praxis.de",))})
        verdicts = check_emails(
            ["a@praxis.de", "b@praxis.de", "c@praxis.de"], resolver=resolver
        )
        self.assertEqual(len(verdicts), 3)
        self.assertEqual(resolver.mx_calls, ["praxis.de"])
        self.assertEqual({v.status for v in verdicts}, {"valid"})

    def test_the_normalized_form_is_none_for_an_invalid_address(self):
        resolver = FakeResolver()
        self.assertIsNone(check_email("kaputt@@x.de", resolver=resolver).normalized)
        self.assertEqual(
            check_email("Info@Praxis.de", resolver=FakeResolver(mx={"praxis.de": (DNS_OK, ("mx.praxis.de",))})).normalized,
            "info@praxis.de",
        )


if __name__ == "__main__":
    unittest.main()
