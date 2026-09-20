#!/usr/bin/env python3
"""Zerodha Kite Connect daily login helper.

Kite access tokens expire every morning, so this is a once-a-day ritual.

    1. python -m scripts.kite_login            → prints the login URL
    2. open it, log in, copy `request_token` from the redirect URL
    3. python -m scripts.kite_login <request_token>
    4. paste the printed access token into .env as KITE_ACCESS_TOKEN
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_config  # noqa: E402


def main() -> int:
    cfg = get_config()
    creds = cfg.broker_credentials("zerodha")
    api_key, api_secret = creds.get("api_key"), creds.get("api_secret")

    if not api_key or not api_secret:
        print("Set KITE_API_KEY and KITE_API_SECRET in .env first.")
        return 1

    try:
        from kiteconnect import KiteConnect
    except ImportError:
        print("pip install kiteconnect")
        return 1

    kite = KiteConnect(api_key=api_key)

    if len(sys.argv) < 2:
        print("\nStep 1 — open this URL and log in:\n")
        print(f"  {kite.login_url()}\n")
        print("Step 2 — copy the `request_token` query parameter from the URL you")
        print("land on, then run:\n")
        print("  python -m scripts.kite_login <request_token>\n")
        return 0

    request_token = sys.argv[1].strip()
    try:
        data = kite.generate_session(request_token, api_secret=api_secret)
    except Exception as exc:
        print(f"Token exchange failed: {exc}")
        print("Request tokens are single-use and expire in minutes — get a fresh one.")
        return 1

    print("\n✅ Logged in as", data.get("user_name"))
    print("\nAdd this line to your .env (it is valid until tomorrow morning):\n")
    print(f"KITE_ACCESS_TOKEN={data['access_token']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
