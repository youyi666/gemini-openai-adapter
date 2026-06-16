#!/usr/bin/env python3
"""Refresh Gemini auth cookies from local browser profiles.

This helper intentionally prints only cookie names and lengths, never values.
It is optional: if browser profiles cannot be read, the caller can still use an
already prepared gemini_cookies.local.json file.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import browser_cookie3


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gemini_webapi.utils.load_browser_cookies import (  # noqa: E402
    HAS_BC3,
    load_browser_cookies,
)


COOKIE_NAMES = ("__Secure-1PSID", "__Secure-1PSIDTS", "__Secure-1PSIDCC")
DOMAIN_CANDIDATES = (
    "gemini.google.com",
    "accounts.google.com",
    ".google.com",
    "google.com",
)
SUPPORTED_COOKIE_BROWSERS = ("auto", "chrome", "edge")
DIRECT_BROWSER_CONFIGS = {
    "chrome": (
        "chrome-cookie-db",
        Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data",
        browser_cookie3.chrome,
    ),
    "edge": (
        "edge-cookie-db",
        Path.home() / "AppData" / "Local" / "Microsoft" / "Edge" / "User Data",
        browser_cookie3.edge,
    ),
}


def _configured_browser() -> str:
    raw = os.getenv("OPENAI_ADAPTER_COOKIE_BROWSER", "chrome").strip().lower()
    aliases = {
        "": "auto",
        "google-chrome": "chrome",
        "msedge": "edge",
        "microsoft-edge": "edge",
    }
    browser = aliases.get(raw, raw)
    if browser not in SUPPORTED_COOKIE_BROWSERS:
        supported = ", ".join(SUPPORTED_COOKIE_BROWSERS)
        raise RuntimeError(
            "Invalid OPENAI_ADAPTER_COOKIE_BROWSER="
            f"{raw!r}; supported values: {supported}."
        )
    return browser


def _configured_profile() -> str:
    return os.getenv("OPENAI_ADAPTER_COOKIE_PROFILE", "Default").strip() or "Default"


def _direct_cookie_db_candidates(
    browser_filter: str,
    profile: str,
) -> list[tuple[str, Path, Path, object]]:
    browser_keys = DIRECT_BROWSER_CONFIGS.keys()
    if browser_filter != "auto":
        browser_keys = (browser_filter,)

    candidates = []
    for browser_key in browser_keys:
        source, user_data_dir, reader = DIRECT_BROWSER_CONFIGS[browser_key]
        candidates.append(
            (
                f"{source}:{profile}",
                user_data_dir / profile / "Network" / "Cookies",
                user_data_dir / "Local State",
                reader,
            )
        )
    return candidates


def _browser_matches_filter(browser_name: str, browser_filter: str) -> bool:
    if browser_filter == "auto":
        return True
    lowered = browser_name.lower()
    if browser_filter == "chrome":
        return "chrome" in lowered
    if browser_filter == "edge":
        return "edge" in lowered
    return False


def _extract_auth(items: list[dict[str, object]]) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in items:
        name = item.get("name")
        value = item.get("value")
        if name in COOKIE_NAMES and isinstance(value, str) and value:
            values[name] = value
    return values


def _score(values: dict[str, str]) -> int:
    return sum(1 for name in COOKIE_NAMES if values.get(name))


def _read_direct_cookie_db(
    browser_filter: str,
    profile: str,
) -> tuple[str, str, dict[str, str]] | None:
    best: tuple[str, str, dict[str, str]] | None = None
    for source, cookie_file, key_file, reader in _direct_cookie_db_candidates(
        browser_filter,
        profile,
    ):
        if not cookie_file.exists() or not key_file.exists():
            continue
        for domain in DOMAIN_CANDIDATES:
            try:
                jar = reader(
                    cookie_file=str(cookie_file),
                    key_file=str(key_file),
                    domain_name=domain,
                )
            except Exception:
                continue
            values = {cookie.name: cookie.value for cookie in jar}
            auth_values = {
                name: value
                for name, value in values.items()
                if name in COOKIE_NAMES and isinstance(value, str) and value
            }
            if best is None or _score(auth_values) > _score(best[2]):
                best = (domain, source, auth_values)
    return best


def refresh_cookies_from_browser(output_path: Path | str) -> dict[str, object]:
    output_path = Path(output_path)
    if not HAS_BC3:
        raise RuntimeError("browser-cookie3 is not installed; cannot read browser cookies.")

    browser_filter = _configured_browser()
    profile = _configured_profile()
    best = _read_direct_cookie_db(browser_filter, profile)

    # browser_cookie3's generic loader does not expose Chromium profile
    # selection. Use it only for the default profile path.
    if profile == "Default":
        for domain in DOMAIN_CANDIDATES:
            cookies_by_browser = load_browser_cookies(domain_name=domain, verbose=False)
            for browser, items in cookies_by_browser.items():
                if not _browser_matches_filter(browser, browser_filter):
                    continue
                values = _extract_auth(items)
                if best is None or _score(values) > _score(best[2]):
                    best = (domain, browser, values)

    if best is None or _score(best[2]) == 0:
        raise RuntimeError(
            "No Gemini auth cookies found in readable browser profiles "
            f"(browser={browser_filter}, profile={profile})."
        )

    domain, browser, values = best
    if "__Secure-1PSID" not in values:
        raise RuntimeError(
            "Readable browser profiles did not include __Secure-1PSID; "
            "manual cookie JSON is still required."
        )

    existing: dict[str, str] = {}
    if output_path.exists():
        try:
            raw = json.loads(output_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and all(isinstance(v, str) for v in raw.values()):
                existing = {k: v for k, v in raw.items() if isinstance(v, str)}
            elif isinstance(raw, dict) and isinstance(raw.get("cookies"), dict):
                existing = {
                    k: v
                    for k, v in raw["cookies"].items()
                    if isinstance(k, str) and isinstance(v, str)
                }
        except Exception:
            existing = {}

    merged = dict(existing)
    merged.update(values)
    payload = {
        "updated_at": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": f"{browser}:{domain}",
        "browser_filter": browser_filter,
        "profile": profile,
        "cookies": {name: merged[name] for name in COOKIE_NAMES if merged.get(name)},
    }
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    return {
        "path": str(output_path),
        "source": f"{browser}:{domain}",
        "browser_filter": browser_filter,
        "profile": profile,
        "cookies": [
            {
                "name": name,
                "present": bool(merged.get(name)),
                "length": len(merged.get(name, "")),
            }
            for name in COOKIE_NAMES
        ],
    }


def main() -> int:
    output_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "gemini_cookies.local.json"
    try:
        result = refresh_cookies_from_browser(output_path)
    except RuntimeError as exc:
        print(str(exc))
        return 2

    print(f"Updated {output_path.name} from {result['source']}.")
    for name in COOKIE_NAMES:
        item = next(
            cookie for cookie in result["cookies"] if cookie["name"] == name
        )
        print(f"{name}: present={item['present']} length={item['length']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
