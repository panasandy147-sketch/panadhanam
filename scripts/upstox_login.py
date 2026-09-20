#!/usr/bin/env python3
"""Upstox daily login helper.

    1. python -m scripts.upstox_login            → prints the authorisation URL
    2. log in, copy the `code` parameter from the redirect
    3. python -m scripts.upstox_login <code>
    4. paste the printed token into .env as UPSTOX_ACCESS_TOKEN
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlencode

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_config  # noqa: E402

AUTH_URL = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"


def main() -> int:
    cfg = get_config()
    creds = cfg.broker_credentials("upstox")
    api_key = creds.get("api_key")
    api_secret = creds.get("api_secret")
    redirect = creds.get("redirect_uri", "http://127.0.0.1:8000/broker/upstox/callback")

    if not api_key or not api_secret:
        print("Set UPSTOX_API_KEY and UPSTOX_API_SECRET in .env first.")
        return 1

    if len(sys.argv) < 2:
        params = {"client_id": api_key, "redirect_uri": redirect, "response_type": "code"}
        print("\nStep 1 — open this URL and log in:\n")
        print(f"  {AUTH_URL}?{urlencode(params)}\n")
        print("Step 2 — copy the `code` parameter from the redirect URL, then run:\n")
        print("  python -m scripts.upstox_login <code>\n")
        return 0

    code = sys.argv[1].strip()
    resp = httpx.post(TOKEN_URL, data={
        "code": code, "client_id": api_key, "client_secret": api_secret,
        "redirect_uri": redirect, "grant_type": "authorization_code",
    }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30.0)

    if resp.status_code != 200:
        print(f"Token exchange failed ({resp.status_code}): {resp.text[:400]}")
        return 1

    data = resp.json()
    print("\n✅ Logged in as", data.get("user_name", "?"))
    print("\nAdd this line to your .env (valid until ~3:30am tomorrow):\n")
    print(f"UPSTOX_ACCESS_TOKEN={data['access_token']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
