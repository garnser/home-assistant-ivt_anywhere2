#!/usr/bin/env python3
"""
IVT Anywhere II / Bosch Pointt OAuth Helper (SingleKey ID + PKCE)

Purpose:
- Perform the initial interactive login once to obtain a refresh_token
- Save tokens.json for use with the Home Assistant integration

How it works:
- Bosch uses Authorization Code + PKCE with an app redirect URI:
    com.bosch.tt.dashtt.pointt://app/login
- Desktop browsers may not open this redirect.
- You can still capture the authorization code by:
  - Copying the final redirect URL that contains ?code=...
  - OR capturing the Location header of the final 302 redirect in devtools/network
  - OR logging in on your phone where the app is installed and copying the code

Usage:
  python ivt_anywhere2_auth.py
  python ivt_anywhere2_auth.py --out tokens.json --verify

Outputs:
  tokens.json containing: access_token, refresh_token, expires_at
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlencode, urlparse, parse_qs

import httpx


# --- Bosch SingleKey / Pointt settings (matching the IVT Anywhere II app flow) ---
AUTH_BASE = "https://singlekey-id.com/auth/connect"
AUTHORIZE_URL = f"{AUTH_BASE}/authorize"
TOKEN_URL = f"{AUTH_BASE}/token"

CLIENT_ID = "762162C0-FA2D-4540-AE66-6489F189FADC"
REDIRECT_URI = "com.bosch.tt.dashtt.pointt://app/login"

SCOPE = (
    "openid email profile offline_access "
    "pointt.gateway.claiming pointt.gateway.removal pointt.gateway.list "
    "pointt.gateway.users pointt.gateway.resource.dashapp "
    "pointt.castt.flow.token-exchange bacon hcc.tariff.read"
)

POINTT_API_BASE = "https://pointt-api.bosch-thermotechnology.com/pointt-api/api/v1"


def _b64url_no_pad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def pkce_verifier(length: int = 64) -> str:
    """
    RFC 7636: 43..128 chars.
    We'll create urlsafe chars and trim.
    """
    if not (43 <= length <= 128):
        raise ValueError("PKCE verifier length must be 43..128")
    raw = os.urandom(96)
    v = _b64url_no_pad(raw)
    return v[:length]


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return _b64url_no_pad(digest)


def extract_code(pasted: str) -> str:
    """
    Allow pasting either:
    - raw code
    - full URL containing ?code=...
    """
    pasted = pasted.strip()
    if "://" in pasted and "code=" in pasted:
        q = parse_qs(urlparse(pasted).query)
        if "code" in q and q["code"]:
            return q["code"][0]
    return pasted


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str
    expires_at: float  # epoch seconds

    @classmethod
    def from_oauth(cls, data: Dict[str, Any]) -> "TokenSet":
        expires_in = float(data.get("expires_in", 3600))
        # refresh a bit early
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=time.time() + expires_in - 30,
        )


def build_authorization_url(*, code_verifier: str, state: Optional[str] = None, nonce: Optional[str] = None) -> str:
    state = state or _b64url_no_pad(os.urandom(18))
    nonce = nonce or _b64url_no_pad(os.urandom(18))
    challenge = pkce_challenge(code_verifier)

    params = {
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "response_type": "code",
        "prompt": "login",
        "scope": SCOPE,
        "style_id": "tt_bsch",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code_for_token(http: httpx.Client, *, code: str, code_verifier: str) -> TokenSet:
    data = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code": code,
        "code_verifier": code_verifier,
    }
    r = http.post(TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    if r.status_code >= 400:
        raise RuntimeError(f"Token exchange failed {r.status_code}: {r.text}")
    return TokenSet.from_oauth(r.json())


def refresh_access_token(http: httpx.Client, *, refresh_token: str) -> TokenSet:
    data = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "refresh_token": refresh_token,
    }
    r = http.post(TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    if r.status_code >= 400:
        raise RuntimeError(f"Token refresh failed {r.status_code}: {r.text}")
    return TokenSet.from_oauth(r.json())


def save_tokens(path: str, tokens: TokenSet) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "access_token": tokens.access_token,
                "refresh_token": tokens.refresh_token,
                "expires_at": int(tokens.expires_at),
            },
            f,
            indent=2,
        )


def verify_gateways(http: httpx.Client, access_token: str) -> Any:
    url = f"{POINTT_API_BASE}/gateways/"
    r = http.get(url, headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"})
    if r.status_code >= 400:
        raise RuntimeError(f"Gateway verify failed {r.status_code}: {r.text}")
    return r.json()


def main() -> int:
    ap = argparse.ArgumentParser(description="IVT Anywhere II initial auth helper (SingleKey + PKCE)")
    ap.add_argument("--out", default="tokens.json", help="Output tokens JSON file (default: tokens.json)")
    ap.add_argument("--verify", action="store_true", help="After auth, call /gateways/ to verify tokens")
    ap.add_argument("--refresh-only", action="store_true", help="Use existing tokens.json refresh_token to refresh access")
    args = ap.parse_args()

    with httpx.Client(timeout=30.0, headers={"Accept": "application/json"}) as http:
        if args.refresh_only:
            if not os.path.exists(args.out):
                raise SystemExit(f"--refresh-only: file not found: {args.out}")
            data = json.load(open(args.out, "r", encoding="utf-8"))
            rt = data.get("refresh_token")
            if not rt:
                raise SystemExit(f"--refresh-only: no refresh_token in {args.out}")
            tokens = refresh_access_token(http, refresh_token=rt)
            save_tokens(args.out, tokens)
            print("Refreshed tokens saved to:", args.out)
            print("Token expires at:", time.ctime(tokens.expires_at))
            if args.verify:
                gws = verify_gateways(http, tokens.access_token)
                print("Gateways:", json.dumps(gws, indent=2))
            return 0

        verifier = pkce_verifier()
        auth_url = build_authorization_url(code_verifier=verifier)

        print("\nOpen this in a browser and log in:\n")
        print(auth_url)
        print(
            "\nAfter login, you need the OAuth 'code'. You can paste either:\n"
            "  - the full redirect URL containing ?code=...\n"
            "  - OR just the code value\n"
        )

        pasted = input("Paste the redirect URL (or just the code): ").strip()
        code = extract_code(pasted)

        tokens = exchange_code_for_token(http, code=code, code_verifier=verifier)
        save_tokens(args.out, tokens)

        print("\nAuthenticated ✅")
        print("Saved:", args.out)
        print("Token expires at:", time.ctime(tokens.expires_at))
        print("\nCopy this into Home Assistant integration setup as REFRESH TOKEN:\n")
        print(tokens.refresh_token)

        if args.verify:
            gws = verify_gateways(http, tokens.access_token)
            print("\nGateways:\n", json.dumps(gws, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
