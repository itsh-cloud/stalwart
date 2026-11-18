#!/usr/bin/env python3
"""Failed RCPT TO of an authenticated session is limited per login, not per IP.

Many authenticated clients can share one egress IP. If their recipient failures
count against that IP, one client sending to non-existent recipients gets the
IP banned and cuts off everyone behind it.

The SMTP client must reach the server from an address the server does not
allowlist, so not loopback, while the management API stays reachable after that
address is banned. Running the script in the server's network namespace does
both: the API on 127.0.0.1, which is always allowlisted, and SMTP on the
container's own address. README.md shows the command.

The script sets the abuse ban rate to 3 failures per hour, shortens the delay
after a failed recipient to 1s and raises the per-session failure limit, so
that only the rate decides. It then checks, in order:

1. An unauthenticated client over the rate gets its IP banned (unblocked again
   afterwards).
2. A login over the rate is refused with 451 and disconnected, and the IP is
   not banned.
3. That login is refused again on reconnecting, before any lookup.
4. A second login on the same IP is unaffected.
5. A login on an allowlisted IP is not limited.
"""

from __future__ import annotations

import ipaddress
import smtplib
import socket
import time

import common
from common import check

DOMAIN = "example.org"
RATE = 3


def blocked_ips() -> dict[str, str]:
    """Address -> id of every BlockedIp object."""
    ids = common.jmap("x:BlockedIp/query", {})["ids"]
    if not ids:
        return {}
    found = common.jmap("x:BlockedIp/get", {"ids": ids})["list"]
    return {entry["address"]: entry["id"] for entry in found}


def reload_ips() -> None:
    common.create("Action", {"@type": "ReloadBlockedIps"})


def unblock_all() -> None:
    ids = list(blocked_ips().values())
    if ids:
        common.jmap("x:BlockedIp/set", {"destroy": ids})
    reload_ips()


def greets() -> bool:
    """True if an unauthenticated connection to the SMTP port gets a greeting."""
    try:
        with smtplib.SMTP(common.HOST, common.SMTP_PORT, timeout=10) as smtp:
            return smtp.noop()[0] == 250
    except (OSError, smtplib.SMTPException):
        return False


def rcpt_codes(recipients: list[str], login: tuple[str, str] | None) -> list[int]:
    """Send RCPT TO for each recipient and return the reply codes.

    A disconnect ends the list early and is recorded as 0.
    """
    codes: list[int] = []
    try:
        if login:
            smtp = smtplib.SMTP_SSL(common.HOST, common.SMTPS_PORT, timeout=30,
                                    context=common.TLS)
            smtp.login(*login)
            sender = login[0]
        else:
            smtp = smtplib.SMTP(common.HOST, common.SMTP_PORT, timeout=30)
            smtp.ehlo("client.example.net")
            sender = ""
        smtp.mail(sender)
        for rcpt in recipients:
            code = smtp.rcpt(rcpt)[0]
            codes.append(code)
            if code == 451:
                break
        smtp.close()
    except (OSError, smtplib.SMTPException):
        codes.append(0)
    return codes


def missing(n: int) -> list[str]:
    stamp = time.monotonic_ns()
    return [f"missing-{stamp}-{i}@{DOMAIN}" for i in range(n)]


def main() -> None:
    common.update_singleton("Security", {"abuseBanRate": {"count": RATE, "period": 3600000}})
    common.update_singleton("MtaStageRcpt", {"waitOnFail": {"else": "1s"},
                                             "maxFailures": {"else": "20"}})
    common.reload_settings()
    unblock_all()
    first, first_pw = common.new_account(DOMAIN, "rcpt-a")
    second, second_pw = common.new_account(DOMAIN, "rcpt-b")
    third, third_pw = common.new_account(DOMAIN, "rcpt-c")

    # 1. Unauthenticated: the IP is banned, as upstream does.
    rcpt_codes(missing(RATE + 1), None)
    banned = blocked_ips()
    check(len(banned) == 1, f"unauthenticated failures ban the client IP: {sorted(banned)}")
    check(not greets(), "the banned IP is refused")
    if len(banned) != 1:
        common.finish()
    client_ip = next(iter(banned)).split("/")[0]
    if ipaddress.ip_address(client_ip).is_loopback:
        raise SystemExit("the server sees a loopback client, which is always allowlisted")
    unblock_all()
    check(greets(), f"{client_ip} is accepted again after unblocking")

    # 2. A login over the rate is refused and disconnected; the IP stays usable.
    codes = rcpt_codes(missing(RATE + 1), (first, first_pw))
    check(codes == [550] * RATE + [451],
          f"login over the rate: {codes}, want {[550] * RATE + [451]}")
    check(not blocked_ips(), "no IP is banned for an authenticated login's failures")
    check(greets(), "the shared IP still gets a greeting")

    # 3. The same login is refused on reconnecting, even for a valid recipient.
    codes = rcpt_codes([second], (first, first_pw))
    check(codes == [451], f"reconnecting login, valid recipient: {codes}, want [451]")

    # 4. Another login behind the same IP is unaffected.
    codes = rcpt_codes([first] + missing(1), (second, second_pw))
    check(codes == [250, 550], f"second login on the same IP: {codes}, want [250, 550]")

    # 5. An allowlisted IP is exempt.
    # The allowlist is part of the settings, not of the blocked IP list.
    allowed = common.create("AllowedIp", {"address": client_ip, "reason": "acceptance test"})
    common.reload_settings()
    codes = rcpt_codes(missing(RATE + 2), (third, third_pw))
    check(codes == [550] * (RATE + 2),
          f"login on an allowlisted IP: {codes}, want {[550] * (RATE + 2)}")
    codes = rcpt_codes([first], (third, third_pw))
    check(codes == [250], f"allowlisted login reconnecting: {codes}, want [250]")
    common.jmap("x:AllowedIp/set", {"destroy": [allowed["id"]]})
    common.reload_settings()
    check(not blocked_ips(), "no IP is banned at the end")
    unblock_all()
    common.finish()


if __name__ == "__main__":
    socket.setdefaulttimeout(30)
    main()
