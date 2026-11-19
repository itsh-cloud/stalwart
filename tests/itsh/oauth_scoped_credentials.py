#!/usr/bin/env python3
"""App passwords and API keys must not authorize an OAuth client.

An OAuth authorization records only the account, so a token issued from an app
password login would carry the account's full permissions, whatever the app
password was restricted to, and would outlive its revocation.

Creates an account, an app password, an API key and a public OAuth client, then
checks that the app password and the API key sign in over HTTP but are refused
for an authorization code and for a device-code approval, while the account
password succeeds for both.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import urllib.parse

import common
from common import check

REDIRECT_URI = "https://client.example/cb"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


def auth(request: dict) -> str:
    status, body = common.post_json("/api/auth", request)
    if status != 200 or not isinstance(body, dict):
        raise SystemExit(f"/api/auth: HTTP {status}: {body}")
    return body.get("type", "")


def form(path: str, fields: dict[str, str]) -> dict:
    _, body = common.http("POST", path, urllib.parse.urlencode(fields).encode(),
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
    return json.loads(body)


def auth_code_query(client_id: str) -> str:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "state": secrets.token_hex(8),
        "scope": "openid",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })


def main() -> None:
    client_id = f"acceptance-{secrets.token_hex(4)}"
    common.create("OAuthClient", {
        "clientId": client_id,
        "redirectUris": {REDIRECT_URI: True},
        "grantTypes": {"authorizationCode": True, "deviceCode": True, "refreshToken": True},
    })
    address, password = common.new_account("example.org", "oauth")
    app_password = common.create("AppPassword", {"description": "acceptance test"},
                                 auth=f"{address}:{password}")["secret"]

    api_key = common.create("ApiKey", {"description": "acceptance test"},
                            auth=f"{address}:{password}")["secret"]

    status, _ = common.http("GET", "/jmap/session", auth=f"{address}:{app_password}")
    check(status == 200, f"the app password signs in over HTTP (HTTP {status})")
    status, _ = common.http("GET", "/jmap/session",
                            headers={"Authorization": f"Bearer {api_key}"})
    check(status == 200, f"the API key signs in over HTTP (HTTP {status})")

    # Authorization code
    def code_login(secret: str) -> str:
        return auth({"type": "authCode", "accountName": address, "accountSecret": secret,
                     "query": auth_code_query(client_id), "consent": True})

    result = code_login(app_password)
    check(result == "failure", f"authorization code with the app password: {result}")
    result = code_login(api_key)
    check(result == "failure", f"authorization code with the API key: {result}")
    result = code_login(password)
    check(result == "authenticated",
          f"authorization code with the account password: {result}")

    # Device code
    def device_login(secret: str) -> tuple[str, dict]:
        device = form("/auth/device", {"client_id": client_id, "scope": "openid"})
        result = auth({"type": "authDevice", "accountName": address, "accountSecret": secret,
                       "code": device["user_code"], "consent": True})
        token = form("/auth/token", {"grant_type": DEVICE_GRANT, "client_id": client_id,
                                     "device_code": device["device_code"]})
        return result, token

    result, token = device_login(app_password)
    check(result == "failure", f"device approval with the app password: {result}")
    check(token.get("error") in ("authorization_pending", "expired_token"),
          f"the refused device code stays unapproved: {token}")
    result, token = device_login(api_key)
    check(result == "failure", f"device approval with the API key: {result}")
    check(token.get("error") in ("authorization_pending", "expired_token"),
          f"the device code refused for the API key stays unapproved: {token}")
    result, token = device_login(password)
    check(result == "verified", f"device approval with the account password: {result}")
    check("access_token" in token, f"the approved device code yields a token: {sorted(token)}")
    common.finish()


if __name__ == "__main__":
    main()
