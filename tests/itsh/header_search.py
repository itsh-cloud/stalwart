#!/usr/bin/env python3
"""IMAP header search must match the whole value, not any one of its tokens.

Draft clients re-append a draft on every autosave under a new Message-ID and
find the previous revision with `UID SEARCH HEADER Message-ID <id>`. Message-IDs
that share a domain share most of their tokens, so a search that unions tokens
returns every revision instead of one.

Enables header indexing, appends three messages to a new account's INBOX and
checks that each Message-ID finds exactly its own message.
"""

from __future__ import annotations

import imaplib
import time

import common
from common import check

MESSAGE_IDS = ["<alpha@aaa.example>", "<beta@aaa.example>", "<gamma@zzz.invalid>"]


def search(imap: imaplib.IMAP4, value: str) -> list[int]:
    status, data = imap.search(None, "HEADER", "Message-ID", f'"{value}"')
    assert status == "OK", data
    return sorted(int(n) for n in data[0].split())


def main() -> None:
    fields = common.jmap("x:Search/get", {"ids": ["singleton"]})["list"][0]["indexEmailFields"]
    if not fields.get("headers"):
        common.update_singleton("Search", {"indexEmailFields": {**fields, "headers": True}})
        common.reload_settings()

    address, password = common.new_account("example.org", "search")
    imap = imaplib.IMAP4_SSL(common.HOST, common.IMAPS_PORT, ssl_context=common.TLS)
    imap.login(address, password)
    imap.select("INBOX")
    for n, message_id in enumerate(MESSAGE_IDS, 1):
        message = (f"From: sender@example.org\r\nTo: {address}\r\n"
                   f"Subject: draft {n}\r\nMessage-ID: {message_id}\r\n\r\nrevision {n}\r\n")
        status, data = imap.append("INBOX", None, None, message.encode())
        assert status == "OK", data

    # Indexing is asynchronous; wait until the last message is searchable.
    deadline = time.monotonic() + 60
    while not search(imap, MESSAGE_IDS[-1]) and time.monotonic() < deadline:
        time.sleep(1)

    for n, message_id in enumerate(MESSAGE_IDS, 1):
        found = search(imap, message_id)
        check(found == [n], f"HEADER Message-ID {message_id} -> {found}, want [{n}]")
    found = search(imap, "<nonexistent@aaa.example>")
    check(found == [], f"HEADER Message-ID <nonexistent@aaa.example> -> {found}, want []")
    found = search(imap, "aaa")
    check(found == [1, 2], f"HEADER Message-ID aaa -> {found}, want [1, 2]")
    imap.logout()
    common.finish()


if __name__ == "__main__":
    main()
