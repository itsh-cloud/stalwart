"""Shared helpers for the acceptance scripts: management API calls and setup.

Python standard library only. Every script reads the same environment:

  STALWART_URL       HTTP base URL of the server, default http://127.0.0.1:8080
  STALWART_ADMIN     admin credential as user:password, default admin:admin-secret
  STALWART_HOST      host for the IMAP and SMTP ports, default 127.0.0.1
  STALWART_IMAPS     IMAPS port, default 993
  STALWART_SMTPS     SMTPS (implicit TLS submission) port, default 465
  STALWART_SMTP      plain SMTP port, default 25
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import ssl
import sys
import urllib.error
import urllib.request

URL = os.environ.get("STALWART_URL", "http://127.0.0.1:8080").rstrip("/")
ADMIN = os.environ.get("STALWART_ADMIN", "admin:admin-secret")
HOST = os.environ.get("STALWART_HOST", "127.0.0.1")
IMAPS_PORT = int(os.environ.get("STALWART_IMAPS", "993"))
SMTPS_PORT = int(os.environ.get("STALWART_SMTPS", "465"))
SMTP_PORT = int(os.environ.get("STALWART_SMTP", "25"))

USING = ["urn:ietf:params:jmap:core", "urn:stalwart:jmap"]

# The server's certificate is self-signed in a scratch deployment.
TLS = ssl.create_default_context()
TLS.check_hostname = False
TLS.verify_mode = ssl.CERT_NONE

failures: list[str] = []


def check(condition: bool, label: str) -> None:
    print(f"{'ok  ' if condition else 'FAIL'}  {label}")
    if not condition:
        failures.append(label)


def finish() -> None:
    if failures:
        print(f"\n{len(failures)} check(s) failed")
        sys.exit(1)
    print("\nPASS")


def http(method: str, path: str, body: bytes | None = None, *,
         auth: str | None = None, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(URL + path, data=body, method=method)
    if auth is not None:
        request.add_header("Authorization",
                           "Basic " + base64.b64encode(auth.encode()).decode())
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def post_json(path: str, payload: object, *, auth: str | None = None) -> tuple[int, object]:
    status, body = http("POST", path, json.dumps(payload).encode(), auth=auth,
                        headers={"Content-Type": "application/json"})
    try:
        return status, json.loads(body)
    except ValueError:
        return status, body.decode(errors="replace")


def jmap(method: str, args: dict, *, auth: str = ADMIN) -> dict:
    status, body = post_json("/jmap", {"using": USING, "methodCalls": [[method, args, "0"]]},
                             auth=auth)
    if status != 200 or not isinstance(body, dict):
        sys.exit(f"{method}: HTTP {status}: {body}")
    name, result, _ = body["methodResponses"][0]
    if name == "error":
        sys.exit(f"{method}: {result}")
    return result


def create(object_type: str, value: dict, *, auth: str = ADMIN, **args) -> dict:
    """Create one object with x:<object_type>/set and return the server's copy."""
    result = jmap(f"x:{object_type}/set", {"create": {"new": value}, **args}, auth=auth)
    if "new" not in (result.get("created") or {}):
        sys.exit(f"x:{object_type}/set: {result.get('notCreated')}")
    return result["created"]["new"]


def update_singleton(object_type: str, patch: dict) -> None:
    result = jmap(f"x:{object_type}/set", {"update": {"singleton": patch}})
    if "singleton" not in (result.get("updated") or {}):
        sys.exit(f"x:{object_type}/set: {result.get('notUpdated')}")


def reload_settings() -> None:
    create("Action", {"@type": "ReloadSettings"})


def domain_id(name: str) -> str:
    """The id of the domain called name, created when it does not exist yet."""
    found = jmap("x:Domain/query", {"filter": {"name": name}})["ids"]
    if found:
        return found[0]
    return create("Domain", {
        "name": name,
        "certificateManagement": {"@type": "Manual"},
        "dkimManagement": {"@type": "Manual"},
        "dnsManagement": {"@type": "Manual"},
        "subAddressing": {"@type": "Enabled"},
    })["id"]


def new_account(domain: str, prefix: str) -> tuple[str, str]:
    """Create a user account with a random name and password; return (address, password)."""
    name = f"{prefix}-{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(18)
    create("Account", {
        "@type": "User",
        "name": name,
        "domainId": domain_id(domain),
        "credentials": {"0": {"@type": "Password", "secret": password}},
    })
    return f"{name}@{domain}", password
