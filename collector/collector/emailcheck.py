"""Email validation: syntax, then the domain's mail setup.

Task book 004, stage D:

    "邮箱：语法、域名 DNS、MX、null-MX、必要的无 MX 回退语义、DNS 超时/暂时错误
     分别存状态；域有 MX 不等于具体邮箱可投递。不要主动发信或进行批量 SMTP
     探测。"

What this module will and will not claim
----------------------------------------
It will report, separately and by machine-readable code, that an address is
syntactically wrong, that its domain does not exist, that the domain publishes a
*null MX* (RFC 7505 — an explicit "this domain accepts no mail"), that the domain
publishes mail exchangers, that it publishes none but does have an address record
(the RFC 5321 implicit-MX fallback), or that DNS could not answer in time.

It will never claim that a mailbox exists. A domain with a valid MX says the
domain accepts mail, nothing about a particular local part. The collector does
not open SMTP connections and does not send probe messages — the task book
forbids both, and either would be indistinguishable from abuse.

Because DNS is involved, every verdict here is a snapshot: a transient NXDOMAIN
or timeout is recorded as such, and a later run re-checks rather than inheriting
a stale "invalid".
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from typing import Any, Callable

from .identity import is_placeholder_email, is_valid_email_syntax

#: Machine-readable states, stored in ``field_facts.validation_state``. The
#: task book asks for these to be stored separately, so they are constants
#: rather than prose inside a note.
STATE_SYNTAX_INVALID = "syntax_invalid"
STATE_PLACEHOLDER = "placeholder_address"
STATE_MX_PRESENT = "mx_present"
STATE_NULL_MX = "null_mx"
STATE_NO_MX_IMPLICIT_A = "no_mx_implicit_a"
STATE_DOMAIN_NOT_FOUND = "domain_not_found"
STATE_DNS_TIMEOUT = "dns_timeout"
STATE_DNS_TEMP_ERROR = "dns_temp_error"
STATE_DNS_UNAVAILABLE = "dns_unavailable"
STATE_NOT_CHECKED = "dns_not_checked"

#: Coarse verdicts, the vocabulary ``field_facts.validation_status`` already
#: uses (stage C). ``valid`` here means "the domain publishes mail servers",
#: never "this mailbox is deliverable".
STATUS_VALID = "valid"
STATUS_INVALID = "invalid"
STATUS_UNCERTAIN = "uncertain"
STATUS_UNVERIFIED = "unverified"

#: State -> coarse status.
_STATE_TO_STATUS = {
    STATE_SYNTAX_INVALID: STATUS_INVALID,
    STATE_PLACEHOLDER: STATUS_UNCERTAIN,
    STATE_MX_PRESENT: STATUS_VALID,
    STATE_NULL_MX: STATUS_INVALID,
    STATE_NO_MX_IMPLICIT_A: STATUS_UNCERTAIN,
    STATE_DOMAIN_NOT_FOUND: STATUS_INVALID,
    STATE_DNS_TIMEOUT: STATUS_UNCERTAIN,
    STATE_DNS_TEMP_ERROR: STATUS_UNCERTAIN,
    STATE_DNS_UNAVAILABLE: STATUS_UNCERTAIN,
    STATE_NOT_CHECKED: STATUS_UNVERIFIED,
}

#: Every note repeats this, because a report reader must not upgrade
#: "domain has MX" into "the address works".
NOT_DELIVERABILITY = (
    "domain-level check only: the collector never sends mail or probes SMTP, so "
    "mailbox deliverability is not established"
)

# --- DNS --------------------------------------------------------------------

DNS_OK = "ok"
DNS_NXDOMAIN = "nxdomain"
DNS_NOANSWER = "noanswer"
DNS_TIMEOUT = "timeout"
DNS_TEMP_ERROR = "temp_error"
DNS_UNAVAILABLE = "unavailable"


class DnsResolver:
    """Thin wrapper over dnspython with bounded timeouts.

    ``query`` is the single seam the tests replace, so every DNS outcome
    (including a timeout) can be exercised deterministically.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 5.0,
        lifetime_seconds: float = 8.0,
        nameservers: list[str] | None = None,
    ):
        self.timeout_seconds = timeout_seconds
        self.lifetime_seconds = lifetime_seconds
        self._resolver = None
        self._configure_error = ""
        try:
            import dns.resolver  # noqa: F401

            resolver = dns.resolver.Resolver(configure=True)
            resolver.timeout = timeout_seconds
            resolver.lifetime = lifetime_seconds
            if nameservers:
                resolver.nameservers = list(nameservers)
            self._resolver = resolver
        except Exception as exc:  # noqa: BLE001 - reported as "dns unavailable"
            self._configure_error = f"{type(exc).__name__}: {exc}"

    @property
    def available(self) -> bool:
        return self._resolver is not None

    def query(self, name: str, rdtype: str) -> tuple[str, tuple[str, ...]]:
        """Return (dns_status, record_texts). Never raises."""
        if self._resolver is None:
            return DNS_UNAVAILABLE, ()

        import dns.exception
        import dns.resolver

        try:
            answer = self._resolver.resolve(name, rdtype, raise_on_no_answer=False)
        except dns.resolver.NXDOMAIN:
            return DNS_NXDOMAIN, ()
        except dns.resolver.NoAnswer:
            return DNS_NOANSWER, ()
        except dns.resolver.NoNameservers:
            # Every configured server failed or refused: transient by nature.
            return DNS_TEMP_ERROR, ()
        except dns.exception.Timeout:
            return DNS_TIMEOUT, ()
        except dns.exception.DNSException:
            return DNS_TEMP_ERROR, ()
        except (OSError, socket.error):
            return DNS_UNAVAILABLE, ()

        rrset = getattr(answer, "rrset", None)
        if rrset is None:
            return DNS_NOANSWER, ()
        return DNS_OK, tuple(rdata.to_text() for rdata in rrset)

    def mx(self, domain: str) -> tuple[str, tuple[str, ...]]:
        """Mail exchangers for a domain, as host names.

        A null MX (``0 .``, RFC 7505) comes back as a single empty host, which
        is exactly how it must be distinguished from "no MX at all".
        """
        status, records = self.query(domain, "MX")
        if status != DNS_OK:
            return status, ()
        hosts: list[str] = []
        for record in records:
            parts = record.split()
            host = parts[-1].rstrip(".").lower() if parts else ""
            hosts.append(host)
        return DNS_OK, tuple(hosts)

    def addresses(self, domain: str) -> tuple[str, tuple[str, ...]]:
        """A/AAAA records for a domain, used for the implicit-MX fallback."""
        status, records = self.query(domain, "A")
        if status == DNS_OK and records:
            return DNS_OK, records
        status_v6, records_v6 = self.query(domain, "AAAA")
        if status_v6 == DNS_OK and records_v6:
            return DNS_OK, records_v6
        # Prefer to report the more specific failure of the two lookups.
        for candidate in (status, status_v6):
            if candidate in (DNS_TIMEOUT, DNS_TEMP_ERROR, DNS_NXDOMAIN):
                return candidate, ()
        return status, ()


# --- verdicts ---------------------------------------------------------------


@dataclass
class EmailVerdict:
    address: str
    domain: str = ""
    state: str = STATE_NOT_CHECKED
    status: str = STATUS_UNVERIFIED
    note: str = ""
    mx_hosts: tuple[str, ...] = ()
    dns_status: str = ""
    checks: dict[str, Any] = field(default_factory=dict)

    @property
    def normalized(self) -> str | None:
        """The form used for matching: casefolded address, or None if invalid."""
        if self.status == STATUS_INVALID:
            return None
        return self.address.strip().casefold() or None

    def as_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "domain": self.domain,
            "state": self.state,
            "status": self.status,
            "note": self.note,
            "mx_hosts": list(self.mx_hosts),
            "dns_status": self.dns_status,
            "checks": dict(self.checks),
        }


def _domain_of(address: str) -> str:
    _, _, domain = address.strip().rpartition("@")
    return domain.strip().lower()


def check_email(
    address: object,
    *,
    resolver: DnsResolver | None = None,
    check_dns: bool = True,
    rules: dict | None = None,
) -> EmailVerdict:
    """Validate one address as far as the task book allows.

    Order matters: a syntactically impossible address is never looked up, so a
    typo in a page footer cannot generate DNS traffic.
    """
    rules = rules or {}
    text = "" if address is None else str(address).strip()
    verdict = EmailVerdict(address=text)

    if not text:
        verdict.state = STATE_SYNTAX_INVALID
        verdict.status = STATUS_INVALID
        verdict.note = "empty address"
        return verdict

    if rules.get("email_syntax", True) and not is_valid_email_syntax(text):
        verdict.state = STATE_SYNTAX_INVALID
        verdict.status = STATUS_INVALID
        verdict.note = "fails email syntax check"
        return verdict

    verdict.domain = _domain_of(text)

    if is_placeholder_email(text):
        # A template value from the page ("user@domain.com"). It is
        # syntactically fine, so it is kept, but never presented as a contact.
        verdict.state = STATE_PLACEHOLDER
        verdict.status = STATUS_UNCERTAIN
        verdict.note = "placeholder address, not a real contact"
        return verdict

    if not verdict.domain:
        verdict.state = STATE_SYNTAX_INVALID
        verdict.status = STATUS_INVALID
        verdict.note = "no domain part"
        return verdict

    if not check_dns:
        verdict.state = STATE_NOT_CHECKED
        verdict.status = STATUS_UNVERIFIED
        verdict.note = f"dns lookup disabled; {NOT_DELIVERABILITY}"
        return verdict

    resolver = resolver or DnsResolver()
    _apply_dns(verdict, resolver)
    return verdict


def _apply_dns(verdict: EmailVerdict, resolver: DnsResolver) -> None:
    mx_status, mx_hosts = resolver.mx(verdict.domain)
    verdict.dns_status = mx_status
    verdict.mx_hosts = tuple(host for host in mx_hosts if host)
    verdict.checks["mx_query"] = mx_status

    if mx_status == DNS_OK:
        if mx_hosts and all(host == "" for host in mx_hosts):
            verdict.state = STATE_NULL_MX
            verdict.status = STATUS_INVALID
            verdict.note = (
                f"{verdict.domain} publishes a null MX (RFC 7505): the domain "
                "states that it accepts no mail"
            )
            verdict.checks["null_mx"] = True
            return
        if verdict.mx_hosts:
            verdict.state = STATE_MX_PRESENT
            verdict.status = STATUS_VALID
            verdict.note = (
                f"{verdict.domain} publishes {len(verdict.mx_hosts)} mail "
                f"exchanger(s); {NOT_DELIVERABILITY}"
            )
            return
        # MX records existed but all were blank in an unexpected way.
        mx_status = DNS_NOANSWER

    if mx_status == DNS_NOANSWER:
        # RFC 5321 §5.1: with no MX record, the domain's A/AAAA record is the
        # implicit mail exchanger. It is weaker evidence than a published MX,
        # so it is reported as uncertain rather than valid.
        addr_status, addresses = resolver.addresses(verdict.domain)
        verdict.checks["address_query"] = addr_status
        if addr_status == DNS_OK and addresses:
            verdict.state = STATE_NO_MX_IMPLICIT_A
            verdict.status = STATUS_UNCERTAIN
            verdict.note = (
                f"{verdict.domain} publishes no MX record; mail may fall back to "
                f"its address record (RFC 5321 implicit MX, {len(addresses)} "
                f"address(es)) — weaker evidence than a published MX"
            )
            return
        if addr_status == DNS_NXDOMAIN:
            verdict.state = STATE_DOMAIN_NOT_FOUND
            verdict.status = STATUS_INVALID
            verdict.note = f"{verdict.domain} does not exist (NXDOMAIN)"
            return
        if addr_status == DNS_TIMEOUT:
            verdict.state = STATE_DNS_TIMEOUT
            verdict.status = STATUS_UNCERTAIN
            verdict.note = f"DNS timed out while checking {verdict.domain}"
            return
        if addr_status == DNS_UNAVAILABLE:
            verdict.state = STATE_DNS_UNAVAILABLE
            verdict.status = STATUS_UNCERTAIN
            verdict.note = f"no DNS resolver is available; {verdict.domain} was not checked"
            return
        verdict.state = STATE_DNS_TEMP_ERROR
        verdict.status = STATUS_UNCERTAIN
        verdict.note = (
            f"{verdict.domain} has no MX record and its address lookup did not "
            f"succeed ({addr_status}); this is a temporary or inconclusive answer"
        )
        return

    if mx_status == DNS_NXDOMAIN:
        verdict.state = STATE_DOMAIN_NOT_FOUND
        verdict.status = STATUS_INVALID
        verdict.note = f"{verdict.domain} does not exist (NXDOMAIN)"
        return

    if mx_status == DNS_TIMEOUT:
        verdict.state = STATE_DNS_TIMEOUT
        verdict.status = STATUS_UNCERTAIN
        verdict.note = (
            f"DNS timed out while looking up MX for {verdict.domain}; the address "
            "is left unjudged rather than marked invalid"
        )
        return

    if mx_status == DNS_UNAVAILABLE:
        verdict.state = STATE_DNS_UNAVAILABLE
        verdict.status = STATUS_UNCERTAIN
        verdict.note = f"no DNS resolver is available; {verdict.domain} was not checked"
        return

    verdict.state = STATE_DNS_TEMP_ERROR
    verdict.status = STATUS_UNCERTAIN
    verdict.note = (
        f"DNS returned a temporary failure ({mx_status}) for {verdict.domain}; "
        "re-checked on a later run"
    )


def check_emails(
    addresses: list[str],
    *,
    resolver: DnsResolver | None = None,
    check_dns: bool = True,
    rules: dict | None = None,
    cache: dict[str, EmailVerdict] | None = None,
) -> list[EmailVerdict]:
    """Validate several addresses, looking up each domain only once.

    A page often lists the same domain in three addresses; the per-domain cache
    keeps the DNS budget proportional to the number of *domains*, not addresses.
    """
    resolver = resolver or DnsResolver()
    cache = cache if cache is not None else {}
    out: list[EmailVerdict] = []
    for address in addresses:
        domain = _domain_of(str(address))
        if domain and domain in cache:
            template = cache[domain]
            verdict = EmailVerdict(
                address=str(address),
                domain=domain,
                state=template.state,
                status=template.status,
                note=template.note,
                mx_hosts=template.mx_hosts,
                dns_status=template.dns_status,
                checks=dict(template.checks, cached_from=template.address),
            )
            out.append(verdict)
            continue
        verdict = check_email(address, resolver=resolver, check_dns=check_dns, rules=rules)
        if verdict.domain:
            cache[verdict.domain] = verdict
        out.append(verdict)
    return out


#: Injected into the fetcher's DNS-failure path for symmetry with the tests.
ResolverFactory = Callable[[], DnsResolver]
