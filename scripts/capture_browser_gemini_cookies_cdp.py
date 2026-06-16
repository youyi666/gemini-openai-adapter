#!/usr/bin/env python3
"""Capture Gemini auth cookies from a Chromium browser with CDP enabled.

The target browser can be Chrome or Edge as long as it is running with
--remote-debugging-port. This helper writes only the auth cookies required by
the adapter and never prints cookie values.
"""

from __future__ import annotations

import json
import os
import sys
import time
import http.client
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

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
LOCAL_BYPASS = "localhost,127.0.0.1,::1"


def _disable_process_proxy_for_local_cdp() -> None:
    # The user's shell may set HTTP_PROXY/ALL_PROXY for Clash. Playwright would
    # otherwise proxy http://127.0.0.1:<port>/json/version and receive 502.
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "SSLKEYLOGFILE",
    ):
        os.environ.pop(name, None)
    os.environ["NO_PROXY"] = LOCAL_BYPASS
    os.environ["no_proxy"] = LOCAL_BYPASS


def _resolve_cdp_endpoint(endpoint: str) -> str:
    if endpoint.startswith("ws://") or endpoint.startswith("wss://"):
        return endpoint

    parsed = urlparse(endpoint)
    if parsed.scheme != "http" or not parsed.hostname:
        raise RuntimeError(f"Unsupported CDP endpoint: {endpoint}")
    path = parsed.path.rstrip("/") + "/json/version"
    if not path.startswith("/"):
        path = "/" + path

    connection = http.client.HTTPConnection(
        parsed.hostname,
        parsed.port or 80,
        timeout=10,
    )
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"CDP /json/version returned HTTP {response.status}.")
        payload = json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()
    websocket_url = payload.get("webSocketDebuggerUrl")
    if not isinstance(websocket_url, str) or not websocket_url:
        raise RuntimeError("CDP /json/version did not include webSocketDebuggerUrl.")
    return websocket_url


def _load_existing_auth_values(output_path: Path) -> dict[str, str]:
    if not output_path.exists():
        return {}
    try:
        raw = json.loads(output_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(raw, dict) and isinstance(raw.get("cookies"), dict):
        return {
            name: value
            for name, value in raw["cookies"].items()
            if name in AUTH_COOKIE_NAMES and isinstance(value, str) and value
        }
    if isinstance(raw, dict):
        return {
            name: value
            for name, value in raw.items()
            if name in AUTH_COOKIE_NAMES and isinstance(value, str) and value
        }
    return {}


def _extract_auth_values(cookies: list[dict[str, object]]) -> dict[str, str]:
    values: dict[str, str] = {}
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if name in AUTH_COOKIE_NAMES and isinstance(value, str) and value:
            values[name] = value
    return values


def _changed_from_previous(values: dict[str, str], previous: dict[str, str]) -> bool:
    if not previous:
        return True
    return any(values.get(name) and values.get(name) != previous.get(name) for name in AUTH_COOKIE_NAMES)


def main() -> int:
    _disable_process_proxy_for_local_cdp()
    endpoint = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9222"
    output_path = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "gemini_cookies.local.json"
    wait_seconds = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    require_change = os.getenv("OPENAI_ADAPTER_COOKIE_REQUIRE_CHANGE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    previous_values = _load_existing_auth_values(output_path) if require_change else {}
    cdp_endpoint = _resolve_cdp_endpoint(endpoint)

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_endpoint)
        try:
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://gemini.google.com", wait_until="domcontentloaded", timeout=30000)
            deadline = time.monotonic() + max(0, wait_seconds)
            cookies = []
            if wait_seconds > 0:
                print(
                    "Waiting for Gemini login cookies. "
                    "If the browser shows a login page, sign in there."
                )
            while True:
                page.wait_for_timeout(3000)
                cookies = context.cookies(COOKIE_URLS)
                current_values = _extract_auth_values(cookies)
                if "__Secure-1PSID" in current_values and (
                    not require_change or _changed_from_previous(current_values, previous_values)
                ):
                    break
                if time.monotonic() >= deadline:
                    break
                if require_change and "__Secure-1PSID" in current_values:
                    print(
                        "Existing Gemini cookies are still unchanged. "
                        "Because the adapter is unauthenticated, sign in again or send a Gemini message in the opened browser."
                    )
                else:
                    print("Waiting for Gemini login cookies from browser CDP...")
        finally:
            browser.close()

    values = _extract_auth_values(cookies)

    if "__Secure-1PSID" not in values:
        print("Browser CDP did not return __Secure-1PSID. Is Gemini logged in?")
        return 2

    if require_change and not _changed_from_previous(values, previous_values):
        print(
            "Browser CDP only found the same cookies that were already unauthenticated. "
            "Please sign out/in or send a Gemini message in the opened browser, then try again."
        )
        return 3

    if "__Secure-1PSIDTS" not in values:
        print("Warning: Browser CDP did not return __Secure-1PSIDTS; writing available auth cookies.")

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
