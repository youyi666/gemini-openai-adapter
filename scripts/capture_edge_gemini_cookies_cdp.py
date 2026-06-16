#!/usr/bin/env python3
"""Capture Gemini auth cookies from a Chromium browser with CDP enabled.

This script expects Edge to be running with --remote-debugging-port. It writes
only the auth cookies needed by the adapter and never prints cookie values.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
AUTH_COOKIE_NAMES = (
    "__Secure-1PSID",
    "__Secure-1PSIDTS",
    "__Secure-1PSIDCC",
)
COOKIE_URLS = [
    "https://gemini.google.com",
    "https://google.com",
    "https://accounts.google.com",
]


def main() -> int:
    endpoint = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9222"
    output_path = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "gemini_cookies.local.json"

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(endpoint)
        try:
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://gemini.google.com", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)

            cookies = context.cookies(COOKIE_URLS)
        finally:
            browser.close()

    values: dict[str, str] = {}
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if name in AUTH_COOKIE_NAMES and isinstance(value, str) and value:
            values[name] = value

    if "__Secure-1PSID" not in values:
        print("Edge CDP did not return __Secure-1PSID. Is Gemini logged in?")
        return 2

    if "__Secure-1PSIDTS" not in values:
        print("Warning: Edge CDP did not return __Secure-1PSIDTS; writing available auth cookies.")

    payload = {
        "updated_at": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
    "source": "chromium-cdp",
        "cookies": values,
    }
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"Wrote {output_path.name} from Chromium CDP.")
    for name in AUTH_COOKIE_NAMES:
        value = values.get(name, "")
        print(f"{name}: present={bool(value)} length={len(value)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
