#!/usr/bin/env python3
"""OpenAI-compatible FastAPI adapter for the vendored Gemini WebAPI client.

This file is intentionally standalone. It imports the upstream Gemini client and
does not modify the vendored package internals.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import http.client
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncGenerator
from urllib.parse import urlsplit, urlunsplit

try:
    from fastapi import FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
    from pydantic import BaseModel, ConfigDict, Field
    from sse_starlette.sse import EventSourceResponse
except ImportError as exc:  # pragma: no cover - startup dependency guard
    raise RuntimeError(
        "Missing adapter dependencies. Install them with: "
        "pip install fastapi uvicorn sse-starlette"
    ) from exc


ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gemini_webapi import GeminiClient, set_log_level  # noqa: E402
from gemini_webapi.constants import AccountStatus, Model  # noqa: E402
from gemini_webapi.exceptions import (  # noqa: E402
    APIError,
    AuthError,
    GeminiError,
    TimeoutError as GeminiTimeoutError,
)


LOG_LEVEL = os.getenv("OPENAI_ADAPTER_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("openai_adapter")
set_log_level(os.getenv("GEMINI_WEBAPI_LOG_LEVEL", "WARNING").upper())


class ChatMessage(BaseModel):
    """Subset of OpenAI chat message shape accepted by the adapter."""

    model_config = ConfigDict(extra="allow")

    role: str
    content: Any = ""
    name: str | None = None


class ChatCompletionRequest(BaseModel):
    """Subset of OpenAI chat completion request shape accepted by the adapter."""

    model_config = ConfigDict(extra="allow")

    model: str = Field(default="gemini-3-flash")
    messages: list[ChatMessage]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None


class RateLimitTestRequest(BaseModel):
    """Conservative local probe for adapter/upstream rate-limit behavior."""

    model_config = ConfigDict(extra="allow")

    model: str = Field(default="gemini-3-flash")
    prompt: str = Field(default="hi")
    auto_mode: bool = False
    auto_profile: str = Field(default="balanced")
    start_parallel: int = Field(default=1, ge=1, le=3)
    max_parallel: int = Field(default=4, ge=1, le=8)
    max_rounds: int = Field(default=4, ge=1, le=6)
    max_total_requests: int = Field(default=12, ge=1, le=30)
    base_delay_seconds: float = Field(default=2.0, ge=0.25, le=10.0)
    min_delay_seconds: float = Field(default=0.5, ge=0.1, le=5.0)
    per_request_timeout_seconds: float = Field(default=45.0, ge=5.0, le=120.0)
    stop_on_first_error: bool = True


class PromptSizeTestRequest(BaseModel):
    """Conservative probe for practical prompt-size limits."""

    model_config = ConfigDict(extra="allow")

    model: str = Field(default="gemini-3-flash")
    auto_profile: str = Field(default="balanced")
    max_target_tokens: int = Field(default=32_000, ge=100, le=128_000)
    per_request_timeout_seconds: float = Field(default=180.0, ge=15.0, le=300.0)
    stop_on_first_error: bool = True


KNOWN_GEMINI_MODEL_NAMES = {model.model_name for model in Model}

CLINE_COMPACT_SYSTEM_PROMPT = f"""You are Cline, a concise software engineering assistant running inside an IDE.

Follow the user's task directly. You may use the host IDE's XML-style tools when a tool is truly needed; otherwise answer normally.
For coding tasks, inspect the available context, propose focused changes, and prefer small, verifiable steps.
Ask before destructive filesystem operations or external actions.
Do not refuse just because you are receiving IDE/tool instructions as text. The host IDE is responsible for executing approved tool calls.
If current information is requested and no browsing tool result is present, answer from available context and say when live verification is needed.

Critical execution contract:
- You are operating through Cline. You do not directly access the local file system; Cline executes your XML tool calls and returns the results as conversation messages.
- Never say "I cannot access your local file system", "I cannot execute XML tools", "I am Gemini, not Cline", or ask the user to paste a file when a Cline tool can be used.
- If Cline returns a tool result, treat it as real evidence from the user's machine. If it contains file contents, analyze those contents directly.
- If a tool result is an error, fix the tool call and try the next best tool. For a missing file, list or search the workspace instead of giving up.
- Relative paths are resolved from the Current Working Directory shown in the environment summary. If a bare filename is not found, search/list likely subdirectories and retry with the discovered relative path.
- If Cline says "You did not use a tool", your next response must be exactly one XML tool call.

Important Cline tool-use reminders:
- If the user asks you to view, inspect, read, summarize, or modify a local file, use the relevant file tool instead of asking the user to paste the file.
- Use one tool call at a time and wait for the tool result before continuing.
- Do not wrap tool calls in Markdown fences.
- Do not use placeholder values like "relative/or/absolute/path"; put the actual path from the task or from search/list results.
- To read a file, emit exactly:
<read_file>
<path>actual/file/path</path>
</read_file>
- To list files, emit exactly:
<list_files>
<path>actual/directory/path</path>
<recursive>false</recursive>
</list_files>
- To search files, emit exactly:
<search_files>
<path>actual/directory/path</path>
<regex>search pattern</regex>
<file_pattern>*</file_pattern>
</search_files>
- To inspect source definitions in a directory, emit exactly:
<list_code_definition_names>
<path>actual/directory/path</path>
</list_code_definition_names>
- To create a new file or replace an entire small file, emit exactly:
<write_to_file>
<path>actual/file/path</path>
<content>
Complete final file content goes here.
</content>
</write_to_file>
- To make targeted edits to an existing file, prefer replace_in_file and emit exactly:
<replace_in_file>
<path>actual/file/path</path>
<diff>
------- SEARCH
Exact existing complete lines to find.
{"=" * 7}
Replacement complete lines.
{"+" * 7} REPLACE
</diff>
</replace_in_file>
- You may include multiple SEARCH/REPLACE blocks inside one replace_in_file diff. Each SEARCH block must match the current file exactly, including indentation and whitespace.
- For deleting code with replace_in_file, leave the REPLACE section empty. For moving code, use one block to delete and another block to insert.
- Only use write_to_file or replace_in_file when the current user task explicitly asks you to create or modify a file. If the task is to run a command, inspect Git status, explain code, or answer a question, do not edit files.
- Never write task instructions, command text, tool names, XML tags, or surrounding conversation text into a file unless the user explicitly says that exact text is the desired file content.
- In replace_in_file, the REPLACE section must contain only the intended final file content for the matched region. Do not mix content from separate user requests into the replacement.
- To run a terminal command, emit exactly:
<execute_command>
<command>non-interactive shell command</command>
<requires_approval>false</requires_approval>
</execute_command>
- On Windows PowerShell, do not chain commands with `&&`; it fails in Windows PowerShell 5.x. Use `;` or separate tool calls, and prefer `Set-Location "actual/path"; command`.
- When checking command success in PowerShell, inspect `$LASTEXITCODE`. For example:
<execute_command>
<command>$root = git rev-parse --show-toplevel; Set-Location $root; python -m py_compile openai_adapter_server.py; if ($LASTEXITCODE -eq 0) {{ Write-Host "Syntax check passed" }} else {{ exit $LASTEXITCODE }}</command>
<requires_approval>false</requires_approval>
</execute_command>
- If command output cannot be captured, the terminal shows `^C`, or visible output contains `fatal:`, `ParserError`, or `InvalidEndOfLine`, do not claim success. Retry with a simpler command, correct the working directory, or ask the user for the visible output.
- If Git says `fatal: not a git repository`, you are probably in the workspace parent directory. List or search for the real repository folder, then rerun Git commands from that directory.
- Before editing an existing file, read it first or use adapter-provided local file context. After editing, use the tool result as the new source of truth and verify with read_file, search_files, or execute_command when useful.
- If a verification read shows that a file accidentally contains user instructions or unrelated command text, immediately correct the file or ask before proceeding.
- Prefer replace_in_file for small localized changes. Use write_to_file for new files, generated files, or when a file is small and most of it changes.
- For destructive, broad, or externally visible actions, ask the user first instead of executing them.
- When the task is complete, emit exactly:
<attempt_completion>
<result>
Your final answer to the user.
</result>
</attempt_completion>
- If you need more information from the user and no tool can discover it, emit exactly:
<ask_followup_question>
<question>Your question.</question>
</ask_followup_question>
- After receiving file contents or search results from the tool, continue the task using that result and finish with attempt_completion."""


@dataclass(frozen=True)
class PriceSpec:
    official_model: str
    tier: str
    input_usd_per_1m: float
    output_usd_per_1m: float
    input_over_threshold_usd_per_1m: float | None = None
    output_over_threshold_usd_per_1m: float | None = None
    threshold_tokens: int | None = None


# Local API-equivalent estimates based on Google Gemini API paid Standard prices.
# The reverse-engineered Gemini web endpoint does not return official billing data.
PRICING_SOURCE_URL = "https://ai.google.dev/gemini-api/docs/pricing"
PRICE_SPECS: dict[str, PriceSpec] = {
    "gemini-3.5-flash": PriceSpec("gemini-3.5-flash", "standard", 1.50, 9.00),
    "gemini-3.1-pro-preview": PriceSpec(
        "gemini-3.1-pro-preview",
        "standard",
        2.00,
        12.00,
        input_over_threshold_usd_per_1m=4.00,
        output_over_threshold_usd_per_1m=18.00,
        threshold_tokens=200_000,
    ),
    "gemini-3.1-flash-lite": PriceSpec(
        "gemini-3.1-flash-lite",
        "standard",
        0.25,
        1.50,
    ),
    "gemini-2.5-flash": PriceSpec("gemini-2.5-flash", "standard", 0.30, 2.50),
    "gemini-2.5-flash-lite": PriceSpec(
        "gemini-2.5-flash-lite",
        "standard",
        0.10,
        0.40,
    ),
}
MODEL_PRICE_ALIASES = {
    "gemini-3-pro": "gemini-3.1-pro-preview",
    "gemini-3-pro-plus": "gemini-3.1-pro-preview",
    "gemini-3-pro-advanced": "gemini-3.1-pro-preview",
    "gemini-3-flash": "gemini-3.5-flash",
    "gemini-3-flash-plus": "gemini-3.5-flash",
    "gemini-3-flash-advanced": "gemini-3.5-flash",
    "gemini-3-flash-thinking": "gemini-3.5-flash",
    "gemini-3-flash-thinking-plus": "gemini-3.5-flash",
    "gemini-3-flash-thinking-advanced": "gemini-3.5-flash",
    "unspecified": "gemini-3.5-flash",
}
COOKIE_WRITEBACK_NAMES = ("__Secure-1PSID", "__Secure-1PSIDTS")
COOKIE_REFRESH_NAMES = ("__Secure-1PSID", "__Secure-1PSIDTS", "__Secure-1PSIDCC")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; falling back to %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; falling back to %s", name, raw, default)
        return default


def _normalize_proxy_url(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = f"http://{text}"
    return text


def _proxy_for_log(proxy: str | None) -> str:
    if not proxy:
        return "direct"
    try:
        parts = urlsplit(proxy)
        if parts.username or parts.password:
            host = parts.hostname or ""
            if parts.port:
                host = f"{host}:{parts.port}"
            return urlunsplit((parts.scheme, f"***:***@{host}", parts.path, parts.query, parts.fragment))
    except ValueError:
        return "<invalid>"
    return proxy


def _split_proxy_candidates(raw: str | None) -> list[str]:
    if not raw:
        return []
    candidates: list[str] = []
    for item in re.split(r"[\r\n,]+", raw):
        normalized = _normalize_proxy_url(item)
        if normalized:
            candidates.append(normalized)
    return candidates


G4F_MODEL_PREFIXES = ("g4f:", "gpt4free:")
DEFAULT_G4F_MODELS = (
    "g4f:gpt-4o-mini",
    "g4f:gpt-4.1-mini",
    "g4f:gpt-4",
    "g4f:deepseek-v3",
)


class G4FUpstreamError(RuntimeError):
    """Raised when the optional gpt4free sidecar is unreachable or invalid."""


def _split_csv_env(name: str, default: tuple[str, ...]) -> list[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return list(default)
    values = [item.strip() for item in re.split(r"[\r\n,]+", raw) if item.strip()]
    return values or list(default)


def _g4f_base_url() -> str | None:
    raw = os.getenv("OPENAI_ADAPTER_G4F_BASE_URL", "").strip()
    if not raw:
        return None
    return raw.rstrip("/")


def _g4f_timeout_seconds() -> float:
    return max(5.0, _env_float("OPENAI_ADAPTER_G4F_TIMEOUT_SECONDS", 180.0))


def _g4f_provider() -> str | None:
    provider = os.getenv("OPENAI_ADAPTER_G4F_PROVIDER", "").strip()
    return provider or None


def _g4f_models() -> list[str]:
    return _split_csv_env("OPENAI_ADAPTER_G4F_MODELS", DEFAULT_G4F_MODELS)


def _strip_g4f_model_prefix(model: str | None) -> tuple[str, bool]:
    text = (model or "").strip()
    lowered = text.lower()
    for prefix in G4F_MODEL_PREFIXES:
        if lowered.startswith(prefix):
            return text[len(prefix) :].strip() or "gpt-4o-mini", True
    return text, False


def _looks_like_g4f_model(model: str | None) -> bool:
    text = (model or "").strip().lower()
    if not text:
        return False
    return bool(
        text.startswith(("gpt-", "o1", "o3", "o4", "deepseek", "kimi", "claude"))
        or text in {"chatgpt", "llama-3.1", "llama-3.2", "llama-3.3"}
    )


def _should_route_to_g4f(model: str | None) -> bool:
    if _g4f_base_url() is None:
        return False
    stripped_model, explicit = _strip_g4f_model_prefix(model)
    if explicit:
        return True
    if stripped_model.startswith("gemini-") or stripped_model in KNOWN_GEMINI_MODEL_NAMES:
        return False
    return _env_bool("OPENAI_ADAPTER_G4F_ROUTE_OPENAI_MODELS", True) and _looks_like_g4f_model(
        stripped_model
    )


def _g4f_status_info() -> dict[str, Any]:
    base_url = _g4f_base_url()
    return {
        "enabled": base_url is not None,
        "base_url": base_url,
        "route_openai_models": _env_bool("OPENAI_ADAPTER_G4F_ROUTE_OPENAI_MODELS", True),
        "expose_unprefixed_models": _env_bool("OPENAI_ADAPTER_G4F_EXPOSE_UNPREFIXED", True),
        "provider": _g4f_provider(),
        "models": _g4f_models(),
        "timeout_seconds": _g4f_timeout_seconds(),
    }


def _gemini_proxy_candidates() -> list[str | None]:
    candidates: list[str | None] = []
    explicit_proxy = _normalize_proxy_url(os.getenv("GEMINI_PROXY"))
    configured_candidates = _split_proxy_candidates(
        os.getenv("OPENAI_ADAPTER_GEMINI_PROXY_CANDIDATES")
        or os.getenv("OPENAI_ADAPTER_PROXY_CANDIDATES")
    )
    if configured_candidates:
        candidates.extend(configured_candidates)
        if explicit_proxy:
            candidates.append(explicit_proxy)
    else:
        if explicit_proxy:
            candidates.append(explicit_proxy)
        candidates.extend(
            [
                "http://127.0.0.1:17997",
                "http://127.0.0.1:7897",
            ]
        )

    if _env_bool("OPENAI_ADAPTER_GEMINI_DIRECT_FALLBACK", False):
        candidates.append(None)

    deduped: list[str | None] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = candidate or "<direct>"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)

    return deduped or [None]


def _gemini_proxy_config() -> dict[str, Any]:
    return {
        "selected": _proxy_for_log(os.getenv("GEMINI_PROXY")),
        "candidates": [_proxy_for_log(candidate) for candidate in _gemini_proxy_candidates()],
        "direct_fallback": _env_bool("OPENAI_ADAPTER_GEMINI_DIRECT_FALLBACK", False),
    }


def _load_cookies_json(path: str | os.PathLike[str]) -> dict[str, str]:
    """Load common cookie export JSON formats into a flat cookie dictionary."""

    cookie_path = Path(path)
    data = json.loads(cookie_path.read_text(encoding="utf-8"))
    cookies: dict[str, str] = {}

    def upsert(name: Any, value: Any) -> None:
        if isinstance(name, str) and name and isinstance(value, str) and value:
            cookies[name] = value

    def handle_cookie_object(item: Any) -> None:
        if isinstance(item, dict):
            upsert(item.get("name"), item.get("value"))

    if isinstance(data, dict) and all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in data.items()
    ):
        for key, value in data.items():
            upsert(key, value)
    elif isinstance(data, dict) and isinstance(data.get("cookies"), dict):
        for key, value in data["cookies"].items():
            upsert(key, value)
    elif isinstance(data, dict) and isinstance(data.get("cookies"), list):
        for item in data["cookies"]:
            handle_cookie_object(item)
    elif isinstance(data, list):
        for item in data:
            handle_cookie_object(item)

    if not cookies:
        raise ValueError(f"Unsupported or empty cookie JSON format: {cookie_path}")

    return cookies


def _cookie_writeback_enabled() -> bool:
    return _env_bool("OPENAI_ADAPTER_COOKIE_WRITEBACK", True)


def _cookie_writeback_interval_seconds() -> int:
    return max(30, _env_int("OPENAI_ADAPTER_COOKIE_WRITEBACK_INTERVAL_SECONDS", 60))


def _cookie_refresh_config() -> dict[str, str]:
    browser = os.getenv("OPENAI_ADAPTER_COOKIE_BROWSER", "chrome").strip() or "chrome"
    profile = os.getenv("OPENAI_ADAPTER_COOKIE_PROFILE", "Default").strip() or "Default"
    return {
        "browser": browser,
        "profile": profile,
    }


def _cookie_writeback_path() -> Path | None:
    if not _cookie_writeback_enabled():
        return None

    raw_path = os.getenv("GEMINI_COOKIES_JSON", "").strip()
    if not raw_path:
        return None

    return Path(raw_path)


def _extract_client_cookie_values(client: GeminiClient) -> dict[str, str]:
    """Read refreshed auth cookie values from the live Gemini client."""

    values: dict[str, str] = {}
    cookies = getattr(client, "cookies", None)
    cookie_jar = getattr(cookies, "jar", None)

    if cookie_jar is not None:
        for cookie in cookie_jar:
            name = getattr(cookie, "name", "")
            value = getattr(cookie, "value", "")
            if name in COOKIE_WRITEBACK_NAMES and isinstance(value, str) and value:
                values[name] = value

    for name in COOKIE_WRITEBACK_NAMES:
        if name in values or cookies is None:
            continue
        try:
            value = cookies.get(name)
        except Exception:
            value = None
        if isinstance(value, str) and value:
            values[name] = value

    return values


def _cookie_object(name: str, value: str) -> dict[str, Any]:
    return {
        "name": name,
        "value": value,
        "domain": ".google.com",
        "path": "/",
    }


def _merge_cookie_dict(target: dict[str, Any], values: dict[str, str]) -> bool:
    changed = False
    for name, value in values.items():
        if target.get(name) != value:
            target[name] = value
            changed = True
    return changed


def _merge_cookie_list(target: list[Any], values: dict[str, str]) -> bool:
    changed = False
    seen: set[str] = set()

    for item in target:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if name not in values:
            continue
        seen.add(name)
        if item.get("value") != values[name]:
            item["value"] = values[name]
            changed = True

    for name, value in values.items():
        if name not in seen:
            target.append(_cookie_object(name, value))
            changed = True

    return changed


def _merge_cookie_values_into_json(
    data: Any,
    values: dict[str, str],
) -> tuple[Any, bool]:
    if isinstance(data, dict) and isinstance(data.get("cookies"), dict):
        return data, _merge_cookie_dict(data["cookies"], values)

    if isinstance(data, dict) and isinstance(data.get("cookies"), list):
        return data, _merge_cookie_list(data["cookies"], values)

    if isinstance(data, list):
        return data, _merge_cookie_list(data, values)

    if isinstance(data, dict):
        return data, _merge_cookie_dict(data, values)

    return dict(values), True


def _write_cookie_values_to_json(path: Path, values: dict[str, str]) -> bool:
    if not values:
        return False

    if not path.exists() and "__Secure-1PSID" not in values:
        logger.warning(
            "Skipping Gemini cookie writeback: %s does not exist and "
            "__Secure-1PSID is unavailable.",
            path,
        )
        return False

    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        data = {}

    merged, changed = _merge_cookie_values_into_json(data, values)
    if not changed:
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp")
    temp_path.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temp_path, path)
    return True


async def _write_client_cookies_back(
    client: GeminiClient,
    reason: str,
) -> bool:
    path = _cookie_writeback_path()
    if path is None:
        return False

    status = _account_status_info(client)
    if not status["authenticated"]:
        logger.info(
            "Skipping Gemini cookie writeback because account is not "
            "authenticated: %s (%s). reason=%s",
            status["name"],
            status["value"],
            reason,
        )
        return False

    values = _extract_client_cookie_values(client)
    try:
        changed = _write_cookie_values_to_json(path, values)
    except Exception:
        logger.error(
            "Failed to write refreshed Gemini cookies to %s (%s).",
            path,
            reason,
            exc_info=True,
        )
        return False

    if changed:
        logger.info(
            "Wrote refreshed Gemini cookies to %s (%s): %s",
            path,
            reason,
            ", ".join(sorted(values)),
        )
    return changed


async def _cookie_writeback_loop(client: GeminiClient) -> None:
    interval = _cookie_writeback_interval_seconds()
    logger.info("Gemini cookie writeback loop enabled: interval=%ss", interval)
    while True:
        await asyncio.sleep(interval)
        await _write_client_cookies_back(client, "periodic")


async def _start_cookie_writeback_task(app_: FastAPI, client: GeminiClient) -> None:
    if _cookie_writeback_path() is None:
        app_.state.cookie_writeback_task = None
        return
    app_.state.cookie_writeback_task = asyncio.create_task(
        _cookie_writeback_loop(client)
    )


async def _cancel_cookie_writeback_task(app_: FastAPI) -> None:
    task = getattr(app_.state, "cookie_writeback_task", None)
    app_.state.cookie_writeback_task = None
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _create_gemini_client() -> GeminiClient:
    cookie_file = os.getenv("GEMINI_COOKIES_JSON")
    cookie_values: dict[str, str] = {}
    if cookie_file:
        cookie_values = _load_cookies_json(cookie_file)
        logger.info("Loaded Gemini cookies from %s", cookie_file)

    secure_1psid = (
        os.getenv("GEMINI_SECURE_1PSID")
        or cookie_values.get("__Secure-1PSID")
        or None
    )
    secure_1psidts = (
        os.getenv("GEMINI_SECURE_1PSIDTS")
        or cookie_values.get("__Secure-1PSIDTS")
        or None
    )

    if not secure_1psid:
        logger.warning(
            "GEMINI_SECURE_1PSID was not provided; GeminiClient will rely on "
            "cached/browser cookies if the upstream package can find them."
        )
    if secure_1psid and not secure_1psidts:
        logger.warning("__Secure-1PSIDTS was not provided; auth may fail.")

    verify_ssl = _env_bool("GEMINI_VERIFY_SSL", True) and not _env_bool(
        "GEMINI_SKIP_VERIFY", False
    )
    timeout = _env_float("GEMINI_REQUEST_TIMEOUT", 300)
    auto_refresh = _env_bool("GEMINI_AUTO_REFRESH", True)
    refresh_interval = _env_float("GEMINI_REFRESH_INTERVAL", 600)
    watchdog_timeout = _env_float("GEMINI_WATCHDOG_TIMEOUT", 120)
    verbose = _env_bool("GEMINI_VERBOSE", False)
    proxy_candidates = _gemini_proxy_candidates()
    last_error: BaseException | None = None

    for index, proxy in enumerate(proxy_candidates, start=1):
        client = GeminiClient(
            secure_1psid=secure_1psid,
            secure_1psidts=secure_1psidts,
            proxy=proxy,
            verify=verify_ssl,
        )
        if cookie_values:
            client.cookies = cookie_values

        logger.info(
            "Initializing Gemini client: timeout=%s auto_refresh=%s proxy=%s "
            "candidate=%s/%s verify_ssl=%s",
            timeout,
            auto_refresh,
            _proxy_for_log(proxy),
            index,
            len(proxy_candidates),
            verify_ssl,
        )
        try:
            await client.init(
                timeout=timeout,
                auto_close=False,
                auto_refresh=auto_refresh,
                refresh_interval=refresh_interval,
                watchdog_timeout=watchdog_timeout,
                verbose=verbose,
            )
        except AuthError:
            logger.error(
                "Gemini authentication failed via proxy=%s. Refresh "
                "__Secure-1PSID / __Secure-1PSIDTS or update GEMINI_COOKIES_JSON.",
                _proxy_for_log(proxy),
                exc_info=True,
            )
            with suppress(Exception):
                await client.close()
            raise
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Gemini client initialization failed via proxy=%s; trying next "
                "candidate if available.",
                _proxy_for_log(proxy),
                exc_info=True,
            )
            with suppress(Exception):
                await client.close()
            continue

        setattr(client, "_adapter_proxy", proxy)
        logger.info(
            "Gemini client initialized successfully via proxy=%s.",
            _proxy_for_log(proxy),
        )
        return client

    logger.error("Gemini client initialization failed for all proxy candidates.")
    if last_error is not None:
        raise last_error
    raise RuntimeError("Gemini client initialization failed for all proxy candidates.")


@asynccontextmanager
async def lifespan(app_: FastAPI):
    _backfill_shared_usage_log()
    app_.state.client_reload_lock = asyncio.Lock()
    client = await _create_gemini_client()
    app_.state.gemini_client = client

    await _write_client_cookies_back(client, "startup")
    await _start_cookie_writeback_task(app_, client)

    try:
        yield
    finally:
        await _cancel_cookie_writeback_task(app_)
        await _write_client_cookies_back(client, "shutdown-before-close")
        logger.info("Closing Gemini client.")
        await client.close()


app = FastAPI(title="Gemini WebAPI OpenAI Adapter", lifespan=lifespan)


def _cors_allow_origins() -> list[str]:
    raw = os.getenv("OPENAI_ADAPTER_CORS_ORIGINS", "*").strip()
    if not raw:
        return ["*"]
    return [item.strip() for item in raw.split(",") if item.strip()]


_cors_origins = _cors_allow_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials="*" not in _cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _get_client(request: Request) -> GeminiClient:
    client = getattr(request.app.state, "gemini_client", None)
    if client is None:
        raise RuntimeError("Gemini client is not initialized.")
    return client


def _account_status_info(client: GeminiClient) -> dict[str, Any]:
    status = getattr(client, "account_status", None)
    name = getattr(status, "name", str(status) if status is not None else "UNKNOWN")
    value = int(status) if isinstance(status, int) else None
    description = getattr(status, "description", "")
    return {
        "name": name,
        "value": value,
        "description": description,
        "authenticated": status in {None, AccountStatus.AVAILABLE},
    }


def _auth_failure_message(client: GeminiClient | None = None) -> str:
    if client is not None:
        status = _account_status_info(client)
        if not status["authenticated"]:
            return (
                "Gemini session is not authenticated. "
                f"account_status={status['name']} ({status['value']}): "
                f"{status['description']} "
                "Please refresh gemini_cookies.local.json and restart the adapter."
            )
    return (
        "Gemini session is not authenticated or cookies have expired. "
        "Please refresh gemini_cookies.local.json and restart the adapter."
    )


def _ensure_client_authenticated(client: GeminiClient) -> None:
    if not _account_status_info(client)["authenticated"]:
        raise AuthError(_auth_failure_message(client))


def _looks_like_auth_failure(exc: BaseException, client: GeminiClient | None = None) -> bool:
    if client is not None and not _account_status_info(client)["authenticated"]:
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "status: 405",
            "unauthenticated",
            "not authenticated",
            "cookies have expired",
            "authentication",
        )
    )


def _require_local_request(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("Admin endpoints are local-only.")


def _server_log_path() -> Path:
    raw_path = os.getenv("OPENAI_ADAPTER_SERVER_LOG_PATH", "").strip()
    if raw_path:
        return Path(raw_path)
    return ROOT / "runtime" / "server.log"


def _tail_text_file(path: Path, *, lines: int = 160, max_bytes: int = 120_000) -> str:
    if not path.exists():
        return ""
    data = path.read_bytes()
    if len(data) > max_bytes:
        data = data[-max_bytes:]
    text = data.decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-max(1, lines):])


def _reset_gemini_cookie_cache() -> dict[str, Any]:
    raw_path = os.getenv("GEMINI_COOKIE_PATH", "").strip()
    if not raw_path:
        return {"enabled": False, "path": None, "cleared": False}

    cache_path = Path(raw_path)
    try:
        if cache_path.exists():
            for item in cache_path.iterdir():
                if item.is_dir():
                    import shutil

                    shutil.rmtree(item)
                else:
                    item.unlink()
        cache_path.mkdir(parents=True, exist_ok=True)
        logger.info("Cleared Gemini cookie cache: %s", cache_path)
        return {"enabled": True, "path": str(cache_path), "cleared": True}
    except OSError as exc:
        logger.error("Failed to clear Gemini cookie cache: %s", cache_path, exc_info=True)
        return {
            "enabled": True,
            "path": str(cache_path),
            "cleared": False,
            "error": str(exc),
        }


def _cookie_refresh_report_from_file(path: Path, source_hint: str | None = None) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    source = source_hint
    cookies: dict[str, str] = {}

    if isinstance(data, dict):
        if isinstance(data.get("source"), str):
            source = data["source"]
        if isinstance(data.get("cookies"), dict):
            cookies = {
                name: value
                for name, value in data["cookies"].items()
                if isinstance(name, str) and isinstance(value, str)
            }
        elif all(isinstance(key, str) and isinstance(value, str) for key, value in data.items()):
            cookies = data

    return {
        "path": str(path),
        "source": source or "unknown",
        "cookies": [
            {
                "name": name,
                "present": bool(cookies.get(name)),
                "length": len(cookies.get(name, "")),
            }
            for name in COOKIE_REFRESH_NAMES
        ],
    }


def _run_cookie_refresh_script(
    cookie_path: Path,
    require_cookie_change: bool = False,
) -> dict[str, Any]:
    script_path = ROOT / "scripts" / "refresh_gemini_cookies_from_browser.py"
    env = os.environ.copy()
    if require_cookie_change:
        env["OPENAI_ADAPTER_COOKIE_REQUIRE_CHANGE"] = "1"
    result = subprocess.run(
        [sys.executable, str(script_path), str(cookie_path)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=90,
        env=env,
    )
    if result.returncode != 0:
        detail = (result.stdout + "\n" + result.stderr).strip()
        raise RuntimeError(detail or f"Cookie refresh script exited with {result.returncode}.")
    logger.info("Cookie refresh script completed: %s", result.stdout.strip())
    return _cookie_refresh_report_from_file(cookie_path)


def _configured_cookie_browser() -> str:
    browser = os.getenv("OPENAI_ADAPTER_COOKIE_BROWSER", "chrome").strip().lower()
    aliases = {
        "": "chrome",
        "auto": "chrome",
        "google-chrome": "chrome",
        "msedge": "edge",
        "microsoft-edge": "edge",
    }
    browser = aliases.get(browser, browser)
    if browser not in {"chrome", "edge"}:
        raise RuntimeError(
            f"Unsupported OPENAI_ADAPTER_COOKIE_BROWSER={browser!r}; use chrome or edge."
        )
    return browser


def _configured_cookie_profile() -> str:
    return os.getenv("OPENAI_ADAPTER_COOKIE_PROFILE", "Default").strip() or "Default"


def _find_chromium_browser_exe(browser: str) -> Path:
    candidates: list[Path] = []
    executable_names: list[str] = []

    if browser == "chrome":
        if sys.platform == "darwin":
            candidates = [
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                Path.home() / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome",
                Path("/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary"),
            ]
            executable_names = ["google-chrome", "chrome", "chromium", "chromium-browser"]
        elif os.name == "nt":
            executable_names = ["chrome.exe"]
            candidates = [
                Path(os.getenv("ProgramFiles", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
                Path(os.getenv("ProgramFiles(x86)", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
                Path(os.getenv("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
            ]
        else:
            executable_names = ["google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser"]
    else:
        if sys.platform == "darwin":
            candidates = [
                Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
                Path.home() / "Applications" / "Microsoft Edge.app" / "Contents" / "MacOS" / "Microsoft Edge",
            ]
            executable_names = ["microsoft-edge", "microsoft-edge-stable", "msedge"]
        elif os.name == "nt":
            executable_names = ["msedge.exe"]
            candidates = [
                Path(os.getenv("ProgramFiles(x86)", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                Path(os.getenv("ProgramFiles", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            ]
        else:
            executable_names = ["microsoft-edge", "microsoft-edge-stable", "msedge"]

    for candidate in candidates:
        if candidate.exists():
            return candidate

    for executable_name in executable_names:
        path = shutil.which(executable_name)
        if path:
            return Path(path)

    raise RuntimeError(f"{browser} executable not found.")


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for_cdp(port: int, timeout_seconds: int = 30) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request("GET", "/json/version")
            response = connection.getresponse()
            response.read()
            if response.status == 200:
                return
        except OSError as exc:
            last_error = exc
        finally:
            connection.close()
        time.sleep(1)
    if last_error is not None:
        raise RuntimeError(f"Browser CDP did not become ready: {last_error}")
    raise RuntimeError("Browser CDP did not become ready.")


def _stop_dedicated_chrome_profile(user_data_dir: Path) -> None:
    if sys.platform == "darwin":
        with suppress(Exception):
            subprocess.run(
                ["pkill", "-f", str(user_data_dir)],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=8,
            )
        return

    if os.name != "nt":
        return
    escaped = str(user_data_dir).replace("'", "''")
    command = (
        f"$dir = '{escaped}'; "
        "$pattern = [regex]::Escape($dir); "
        "Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
        "Where-Object { $_.CommandLine -match $pattern } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    with suppress(Exception):
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=12,
        )


def _run_cookie_refresh_cdp_script(
    cookie_path: Path,
    *,
    require_cookie_change: bool = False,
) -> dict[str, Any]:
    browser = _configured_cookie_browser()
    profile = _configured_cookie_profile()
    browser_exe = _find_chromium_browser_exe(browser)
    debug_port = _free_local_port()
    wait_seconds = max(30, _env_int("OPENAI_ADAPTER_COOKIE_CDP_WAIT_SECONDS", 300))

    browser_args = [
        str(browser_exe),
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={debug_port}",
        f"--profile-directory={profile}",
    ]
    user_data_dir: Path | None = None
    if browser == "chrome":
        raw_user_data_dir = os.getenv("OPENAI_ADAPTER_CHROME_USER_DATA_DIR", "").strip()
        user_data_dir = Path(raw_user_data_dir) if raw_user_data_dir else ROOT / "runtime" / "chrome-gemini-profile"
        user_data_dir.mkdir(parents=True, exist_ok=True)
        _stop_dedicated_chrome_profile(user_data_dir)
        browser_args.append(f"--user-data-dir={user_data_dir}")

    browser_args.append("https://gemini.google.com")
    logger.info(
        "Starting %s CDP cookie refresh: port=%s profile=%s user_data_dir=%s",
        browser,
        debug_port,
        profile,
        user_data_dir,
    )
    subprocess.Popen(browser_args, cwd=str(ROOT))
    _wait_for_cdp(debug_port)

    env = os.environ.copy()
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "SSLKEYLOGFILE",
    ):
        env.pop(name, None)
    env["NO_PROXY"] = "localhost,127.0.0.1,::1"
    env["no_proxy"] = "localhost,127.0.0.1,::1"
    if require_cookie_change:
        env["OPENAI_ADAPTER_COOKIE_REQUIRE_CHANGE"] = "1"

    script_path = ROOT / "scripts" / "capture_browser_gemini_cookies_cdp.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script_path),
            f"http://127.0.0.1:{debug_port}",
            str(cookie_path),
            str(wait_seconds),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=wait_seconds + 60,
        env=env,
    )
    output = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(output or f"CDP cookie refresh exited with {result.returncode}.")

    logger.info("CDP cookie refresh completed: %s", result.stdout.strip())
    report = _cookie_refresh_report_from_file(cookie_path, "chromium-cdp")
    report["method"] = "chromium-cdp"
    report["browser"] = browser
    report["profile"] = profile
    report["debug_port"] = debug_port
    report["required_cookie_change"] = require_cookie_change
    report["stdout"] = result.stdout.strip()
    return report


def _run_cookie_refresh(
    cookie_path: Path,
    require_cookie_change: bool = False,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    try:
        report = _run_cookie_refresh_script(
            cookie_path,
            require_cookie_change=require_cookie_change,
        )
        report["method"] = "browser-cookie-db"
        attempts.append({"method": "browser-cookie-db", "ok": True})
        report["attempts"] = attempts
        return report
    except RuntimeError as exc:
        attempts.append(
            {
                "method": "browser-cookie-db",
                "ok": False,
                "error": str(exc),
            }
        )
        logger.warning("Browser cookie DB refresh failed; trying CDP fallback: %s", exc)
        if not _env_bool("OPENAI_ADAPTER_COOKIE_CDP_FALLBACK", True):
            raise

    try:
        report = _run_cookie_refresh_cdp_script(
            cookie_path,
            require_cookie_change=require_cookie_change,
        )
        attempts.append({"method": "chromium-cdp", "ok": True})
        report["attempts"] = attempts
        return report
    except RuntimeError as exc:
        attempts.append(
            {
                "method": "chromium-cdp",
                "ok": False,
                "error": str(exc),
            }
        )
        detail = "Cookie refresh failed after all methods:\n" + json.dumps(
            attempts,
            ensure_ascii=False,
            indent=2,
        )
        raise RuntimeError(detail) from exc


async def _refresh_cookies_and_reload_client(request: Request) -> dict[str, Any]:
    _require_local_request(request)

    lock = getattr(request.app.state, "client_reload_lock", None)
    if lock is None:
        request.app.state.client_reload_lock = asyncio.Lock()
        lock = request.app.state.client_reload_lock

    async with lock:
        cookie_path = _cookie_writeback_path()
        if cookie_path is None:
            raise RuntimeError("GEMINI_COOKIES_JSON is not configured.")

        old_client = _get_client(request)
        old_status = _account_status_info(old_client)

        await _cancel_cookie_writeback_task(request.app)
        refresh_report = await asyncio.to_thread(
            _run_cookie_refresh,
            cookie_path,
            not old_status["authenticated"],
        )
        cache_report = _reset_gemini_cookie_cache()

        new_client = await _create_gemini_client()
        new_status = _account_status_info(new_client)
        if not new_status["authenticated"]:
            await new_client.close()
            await _start_cookie_writeback_task(request.app, old_client)
            raise AuthError(_auth_failure_message(new_client))

        request.app.state.gemini_client = new_client
        await _write_client_cookies_back(new_client, "manual-refresh")
        await _start_cookie_writeback_task(request.app, new_client)

        logger.info(
            "Reloaded Gemini client after browser cookie refresh: old_status=%s "
            "new_status=%s source=%s",
            old_status["name"],
            new_status["name"],
            refresh_report.get("source"),
        )
        with suppress(Exception):
            await old_client.close()

        return {
            "ok": True,
            "old_account_status": old_status,
            "account_status": new_status,
            "cookie_refresh": refresh_report,
            "cookie_cache": cache_report,
        }


def _extract_http_status_from_error(exc: BaseException) -> int | None:
    text = str(exc)
    patterns = (
        r"status:\s*(\d{3})",
        r"\[(\d{3})\]",
        r"status_code\s*[=:]\s*(\d{3})",
        r"\b(\d{3})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            try:
                return int(match.group(1))
            except (TypeError, ValueError):
                continue
    return None


def _classify_probe_error(exc: BaseException, client: GeminiClient | None = None) -> dict[str, Any]:
    status_code = _extract_http_status_from_error(exc)
    text = str(exc)
    lowered = text.lower()
    if _looks_like_auth_failure(exc, client):
        category = "auth"
    elif status_code == 413 or any(
        marker in lowered
        for marker in (
            "prompt too large",
            "context length",
            "too many tokens",
            "token limit",
            "exceeds",
            "payload too large",
            "request entity too large",
        )
    ):
        category = "prompt_too_large"
    elif status_code == 429 or any(marker in lowered for marker in ("rate limit", "too many", "quota")):
        category = "rate_limit"
    elif isinstance(exc, (asyncio.TimeoutError, GeminiTimeoutError)) or "timeout" in lowered:
        category = "timeout"
    else:
        category = "upstream_error"

    return {
        "ok": False,
        "category": category,
        "status_code": status_code,
        "error_type": exc.__class__.__name__,
        "message": text[:500],
    }


async def _rate_limit_probe_once(
    client: GeminiClient,
    openai_model: str,
    gemini_model: str,
    prompt: str,
    round_index: int,
    request_index: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    completion_id = _completion_id()
    probe_prompt = (
        f"{prompt}\n\n"
        f"Probe id: {completion_id}. Round: {round_index}. Request: {request_index}."
    )
    started = time.perf_counter()
    try:
        output = await asyncio.wait_for(
            client.generate_content(
                probe_prompt,
                model=gemini_model,
                current_retry=_upstream_retry_count(),
            ),
            timeout=timeout_seconds,
        )
        elapsed = time.perf_counter() - started
        text = getattr(output, "text", "") or ""
        usage, cost_estimate = _build_usage(gemini_model, probe_prompt, text)
        _record_usage(
            completion_id,
            openai_model,
            gemini_model,
            False,
            usage,
            cost_estimate,
        )
        return {
            "ok": True,
            "id": completion_id,
            "latency_seconds": round(elapsed, 3),
            "response_chars": len(text),
            "usage": usage,
            "cost_estimate": cost_estimate,
        }
    except Exception as exc:
        elapsed = time.perf_counter() - started
        error = _classify_probe_error(exc, client)
        error["id"] = completion_id
        error["latency_seconds"] = round(elapsed, 3)
        return error


async def _run_rate_limit_probe(
    client: GeminiClient,
    payload: RateLimitTestRequest,
) -> dict[str, Any]:
    _ensure_client_authenticated(client)
    openai_model = payload.model
    gemini_model = _select_gemini_model(payload.model)
    auto_profile = (payload.auto_profile or "balanced").strip().lower()
    auto_plans: dict[str, dict[str, Any]] = {
        "safe": {
            "label": "稳妥自动",
            "parallel_plan": [1, 2, 4, 4],
            "base_delay_seconds": 2.0,
            "min_delay_seconds": 0.5,
            "per_request_timeout_seconds": 60.0,
        },
        "balanced": {
            "label": "标准自动",
            "parallel_plan": [1, 2, 4, 6, 8],
            "base_delay_seconds": 1.5,
            "min_delay_seconds": 0.3,
            "per_request_timeout_seconds": 75.0,
        },
        "edge": {
            "label": "摸边界自动",
            "parallel_plan": [1, 2, 4, 6, 8, 8],
            "base_delay_seconds": 1.0,
            "min_delay_seconds": 0.2,
            "per_request_timeout_seconds": 90.0,
        },
    }
    if auto_profile not in auto_plans:
        auto_profile = "balanced"

    if payload.auto_mode:
        selected_plan = auto_plans[auto_profile]
        parallel_plan = [int(item) for item in selected_plan["parallel_plan"]]
        max_total_requests = min(sum(parallel_plan), 30)
        max_rounds = len(parallel_plan)
        start_parallel = parallel_plan[0]
        max_parallel = max(parallel_plan)
        base_delay = float(selected_plan["base_delay_seconds"])
        min_delay = float(selected_plan["min_delay_seconds"])
        timeout_seconds = float(selected_plan["per_request_timeout_seconds"])
    else:
        max_total_requests = min(max(1, payload.max_total_requests), 30)
        max_rounds = min(max(1, payload.max_rounds), 6)
        start_parallel = min(max(1, payload.start_parallel), 3)
        max_parallel = min(max(start_parallel, payload.max_parallel), 8)
        parallel_plan = [
            min(start_parallel * (2**round_index), max_parallel)
            for round_index in range(max_rounds)
        ]
        base_delay = max(0.25, min(payload.base_delay_seconds, 10.0))
        min_delay = max(0.1, min(payload.min_delay_seconds, base_delay))
        timeout_seconds = max(5.0, min(payload.per_request_timeout_seconds, 120.0))

    logger.info(
        "Starting rate-limit probe: model=%s gemini_model=%s auto_mode=%s "
        "auto_profile=%s parallel_plan=%s max_total_requests=%s",
        openai_model,
        gemini_model,
        payload.auto_mode,
        auto_profile,
        parallel_plan,
        max_total_requests,
    )

    report: dict[str, Any] = {
        "ok": True,
        "model": openai_model,
        "gemini_model": gemini_model,
        "started_at": datetime.now(tz=timezone.utc).isoformat(),
        "safe_caps": {
            "max_parallel": 8,
            "max_rounds": 6,
            "max_total_requests": 30,
        },
        "settings": {
            "auto_mode": payload.auto_mode,
            "auto_profile": auto_profile if payload.auto_mode else None,
            "auto_profile_label": auto_plans[auto_profile]["label"] if payload.auto_mode else None,
            "parallel_plan": parallel_plan,
            "start_parallel": start_parallel,
            "max_parallel": max_parallel,
            "max_rounds": max_rounds,
            "max_total_requests": max_total_requests,
            "base_delay_seconds": base_delay,
            "min_delay_seconds": min_delay,
            "per_request_timeout_seconds": timeout_seconds,
            "stop_on_first_error": payload.stop_on_first_error,
        },
        "rounds": [],
        "summary": {
            "total_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "fastest_successful_rps": 0.0,
            "highest_successful_parallel": 0,
            "last_fully_successful_round": None,
            "first_failed_round": None,
            "limit_signal": None,
        },
    }

    remaining = max_total_requests
    stop_reason = "completed"
    for zero_based_round in range(max_rounds):
        if remaining <= 0:
            stop_reason = "max_total_requests_reached"
            break

        round_number = zero_based_round + 1
        parallel = min(parallel_plan[zero_based_round], max_parallel, remaining)
        delay_after_round = max(min_delay, round(base_delay * (0.65**zero_based_round), 3))
        round_started = time.perf_counter()

        logger.info(
            "Rate-limit probe round %s: parallel=%s remaining_before=%s",
            round_number,
            parallel,
            remaining,
        )
        results = await asyncio.gather(
            *(
                _rate_limit_probe_once(
                    client,
                    openai_model,
                    gemini_model,
                    payload.prompt.strip() or "hi",
                    round_number,
                    request_index + 1,
                    timeout_seconds,
                )
                for request_index in range(parallel)
            )
        )
        elapsed = max(time.perf_counter() - round_started, 0.001)
        remaining -= parallel

        successes = [item for item in results if item.get("ok")]
        failures = [item for item in results if not item.get("ok")]
        categories: dict[str, int] = {}
        for item in failures:
            category = str(item.get("category") or "unknown")
            categories[category] = categories.get(category, 0) + 1

        estimated_rps = round(parallel / elapsed, 3)
        round_report = {
            "round": round_number,
            "parallel": parallel,
            "elapsed_seconds": round(elapsed, 3),
            "estimated_rps": estimated_rps,
            "successes": len(successes),
            "failures": len(failures),
            "error_categories": categories,
            "first_failure": failures[0] if failures else None,
            "results": results,
            "delay_after_round_seconds": delay_after_round if remaining > 0 else 0,
        }
        report["rounds"].append(round_report)

        summary = report["summary"]
        summary["total_requests"] += parallel
        summary["successful_requests"] += len(successes)
        summary["failed_requests"] += len(failures)
        if successes:
            summary["fastest_successful_rps"] = max(
                float(summary["fastest_successful_rps"]),
                estimated_rps,
            )
            summary["highest_successful_parallel"] = max(
                int(summary["highest_successful_parallel"]),
                parallel,
            )
        if len(successes) == parallel and not failures:
            summary["last_fully_successful_round"] = {
                "round": round_number,
                "parallel": parallel,
                "estimated_rps": estimated_rps,
                "elapsed_seconds": round(elapsed, 3),
            }

        logger.info(
            "Rate-limit probe round %s complete: successes=%s failures=%s rps=%s",
            round_number,
            len(successes),
            len(failures),
            estimated_rps,
        )

        if failures:
            summary["first_failed_round"] = {
                "round": round_number,
                "parallel": parallel,
                "estimated_rps": estimated_rps,
                "elapsed_seconds": round(elapsed, 3),
                "error_categories": categories,
            }
            summary["limit_signal"] = failures[0]
            if payload.stop_on_first_error:
                stop_reason = "first_error"
                break

        if remaining > 0 and round_number < max_rounds:
            await asyncio.sleep(delay_after_round)

    report["finished_at"] = datetime.now(tz=timezone.utc).isoformat()
    report["stop_reason"] = stop_reason
    report["account_status"] = _account_status_info(client)
    await _write_client_cookies_back(client, "rate-limit-probe")
    return report


def _prompt_size_target_plan(profile: str, max_target_tokens: int) -> tuple[str, list[int]]:
    profile = (profile or "balanced").strip().lower()
    plans: dict[str, tuple[str, list[int]]] = {
        "safe": ("稳妥体积", [1_000, 4_000, 8_000, 16_000]),
        "balanced": ("标准体积", [1_000, 4_000, 8_000, 16_000, 32_000]),
        "edge": ("摸边界体积", [1_000, 8_000, 16_000, 32_000, 64_000]),
        "expert": ("专家体积", [1_000, 8_000, 16_000, 32_000, 64_000, 128_000]),
    }
    if profile not in plans:
        profile = "balanced"
    label, plan = plans[profile]
    capped = [tokens for tokens in plan if tokens <= max_target_tokens]
    if not capped:
        capped = [min(max_target_tokens, plan[0])]
    return label, capped


def _build_prompt_size_probe_prompt(target_tokens: int) -> str:
    header = (
        "You are running a prompt-size boundary probe. "
        "Ignore the padding content below and reply with exactly: OK\n\n"
    )
    chunk = (
        "padding line: alpha beta gamma delta epsilon zeta eta theta iota kappa "
        "lambda mu nu xi omicron pi rho sigma tau upsilon phi chi psi omega. "
        "This sentence exists only to increase the input prompt size.\n"
    )
    target_tokens = max(100, min(int(target_tokens), 128_000))
    header_tokens = _estimate_tokens(header)
    chunk_tokens = max(1, _estimate_tokens(chunk))
    repeat_count = max(1, int((target_tokens - header_tokens + chunk_tokens - 1) / chunk_tokens))
    prompt = header + chunk * repeat_count

    # Trim near the target according to the adapter's own estimator.
    while _estimate_tokens(prompt) > target_tokens + 64 and len(prompt) > len(header):
        prompt = prompt[:-256]
    while _estimate_tokens(prompt) < target_tokens - 64:
        prompt += chunk
    return prompt


async def _prompt_size_probe_once(
    client: GeminiClient,
    openai_model: str,
    gemini_model: str,
    target_tokens: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    completion_id = _completion_id()
    probe_prompt = _build_prompt_size_probe_prompt(target_tokens)
    estimated_prompt_tokens = _estimate_tokens(probe_prompt)
    started = time.perf_counter()
    try:
        output = await asyncio.wait_for(
            client.generate_content(
                probe_prompt,
                model=gemini_model,
                current_retry=_upstream_retry_count(),
            ),
            timeout=timeout_seconds,
        )
        elapsed = time.perf_counter() - started
        text = getattr(output, "text", "") or ""
        usage, cost_estimate = _build_usage(gemini_model, probe_prompt, text)
        _record_usage(
            completion_id,
            openai_model,
            gemini_model,
            False,
            usage,
            cost_estimate,
        )
        return {
            "ok": True,
            "id": completion_id,
            "target_prompt_tokens": target_tokens,
            "estimated_prompt_tokens": estimated_prompt_tokens,
            "prompt_chars": len(probe_prompt),
            "latency_seconds": round(elapsed, 3),
            "response_chars": len(text),
            "usage": usage,
            "cost_estimate": cost_estimate,
        }
    except Exception as exc:
        elapsed = time.perf_counter() - started
        error = _classify_probe_error(exc, client)
        error.update(
            {
                "id": completion_id,
                "target_prompt_tokens": target_tokens,
                "estimated_prompt_tokens": estimated_prompt_tokens,
                "prompt_chars": len(probe_prompt),
                "latency_seconds": round(elapsed, 3),
            }
        )
        return error


async def _run_prompt_size_probe(
    client: GeminiClient,
    payload: PromptSizeTestRequest,
) -> dict[str, Any]:
    _ensure_client_authenticated(client)
    openai_model = payload.model
    gemini_model = _select_gemini_model(payload.model)
    max_target_tokens = min(max(100, payload.max_target_tokens), 128_000)
    timeout_seconds = max(15.0, min(payload.per_request_timeout_seconds, 300.0))
    profile_label, target_plan = _prompt_size_target_plan(
        payload.auto_profile,
        max_target_tokens,
    )

    logger.info(
        "Starting prompt-size probe: model=%s gemini_model=%s profile=%s "
        "target_plan=%s timeout=%s",
        openai_model,
        gemini_model,
        payload.auto_profile,
        target_plan,
        timeout_seconds,
    )

    report: dict[str, Any] = {
        "ok": True,
        "model": openai_model,
        "gemini_model": gemini_model,
        "started_at": datetime.now(tz=timezone.utc).isoformat(),
        "safe_caps": {
            "max_target_tokens": 128_000,
            "max_steps": 6,
        },
        "settings": {
            "auto_profile": payload.auto_profile,
            "auto_profile_label": profile_label,
            "target_plan": target_plan,
            "max_target_tokens": max_target_tokens,
            "per_request_timeout_seconds": timeout_seconds,
            "stop_on_first_error": payload.stop_on_first_error,
        },
        "steps": [],
        "summary": {
            "successful_steps": 0,
            "failed_steps": 0,
            "largest_successful_prompt_tokens": 0,
            "largest_successful_prompt_chars": 0,
            "first_failed_target_tokens": None,
            "limit_signal": None,
        },
    }

    stop_reason = "completed"
    for index, target_tokens in enumerate(target_plan, start=1):
        result = await _prompt_size_probe_once(
            client,
            openai_model,
            gemini_model,
            target_tokens,
            timeout_seconds,
        )
        step = {
            "step": index,
            "target_prompt_tokens": target_tokens,
            **result,
        }
        report["steps"].append(step)

        summary = report["summary"]
        if result.get("ok"):
            summary["successful_steps"] += 1
            summary["largest_successful_prompt_tokens"] = max(
                int(summary["largest_successful_prompt_tokens"]),
                int(result.get("estimated_prompt_tokens") or 0),
            )
            summary["largest_successful_prompt_chars"] = max(
                int(summary["largest_successful_prompt_chars"]),
                int(result.get("prompt_chars") or 0),
            )
        else:
            summary["failed_steps"] += 1
            summary["first_failed_target_tokens"] = target_tokens
            summary["limit_signal"] = result
            if payload.stop_on_first_error:
                stop_reason = "first_error"
                break

    report["finished_at"] = datetime.now(tz=timezone.utc).isoformat()
    report["stop_reason"] = stop_reason
    report["account_status"] = _account_status_info(client)
    await _write_client_cookies_back(client, "prompt-size-probe")
    return report


def _message_content_to_text(content: Any) -> str:
    """Convert OpenAI text or multimodal content into plain prompt text."""

    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type == "text":
                    text = item.get("text", "")
                    if text:
                        parts.append(str(text))
                elif item_type == "image_url":
                    image_url = item.get("image_url")
                    if isinstance(image_url, dict):
                        image_url = image_url.get("url")
                    if image_url:
                        parts.append(f"[image_url: {image_url}]")
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    return json.dumps(content, ensure_ascii=False)


def _is_cline_system_prompt(text: str) -> bool:
    markers = [
        "You are Cline",
        "TOOL USE",
        "ACT MODE V.S. PLAN MODE",
        "attempt_completion",
    ]
    return all(marker in text for marker in markers)


def _is_cline_self_refusal(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    if not normalized:
        return False
    refusal_markers = (
        "i cannot execute the xml-style tools",
        "i can't execute the xml-style tools",
        "i cannot execute those commands",
        "i cannot run commands",
        "i cannot modify files",
        "i cannot edit files",
        "i cannot write files",
        "i cannot access your local file system",
        "i don't actually have access to your local file system",
        "i do not have access to your local file system",
        "i am gemini, your ai collaborator",
        "but i am gemini",
        "please copy and paste the contents",
        "please paste the contents",
        "无法执行",
        "无法直接访问",
        "无法直接读取",
        "无法修改",
        "无法写入",
        "无法运行命令",
        "请直接将该文件",
        "复制并粘贴",
    )
    return any(marker in normalized for marker in refusal_markers)


def _extract_tag(text: str, tag: str) -> str | None:
    match = re.search(
        rf"<{re.escape(tag)}>\s*(.*?)\s*</{re.escape(tag)}>",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not match:
        return None
    return match.group(1).strip()


def _extract_environment_summary(text: str) -> str:
    env = _extract_tag(text, "environment_details")
    if not env:
        return ""

    wanted_prefixes = (
        "# Current Time",
        "# Current Working Directory",
        "# Current Mode",
        "# Visual Studio Code Visible Files",
        "# Visual Studio Code Open Tabs",
    )
    summary_lines: list[str] = []
    file_list_limit = int(_env_float("OPENAI_ADAPTER_ENV_FILE_LIST_LIMIT", 60))
    lines = env.splitlines()
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if line.startswith(wanted_prefixes):
            is_cwd_line = line.startswith("# Current Working Directory")
            if is_cwd_line and " Files" in line:
                line = line.split(" Files", 1)[0]
            summary_lines.append(line)
            if is_cwd_line:
                listed_files: list[str] = []
                for next_raw_line in lines[index + 1 :]:
                    next_line = next_raw_line.rstrip()
                    if next_line.strip().startswith("#"):
                        break
                    if not next_line.strip():
                        continue
                    listed_files.append(next_line)
                    if len(listed_files) >= file_list_limit:
                        listed_files.append("... (file list truncated)")
                        break
                if listed_files:
                    summary_lines.append("Current working directory files:")
                    summary_lines.extend(listed_files)
                continue
            if index + 1 < len(lines):
                next_line = lines[index + 1].strip()
                if next_line and not next_line.startswith("#"):
                    summary_lines.append(next_line)

    if not summary_lines:
        return ""

    return "Environment summary:\n" + "\n".join(summary_lines)


def _compact_cline_user_message(text: str) -> str:
    task = _extract_tag(text, "task")
    env_summary = _extract_environment_summary(text)

    if task:
        parts = [f"<task>\n{task}\n</task>"]
        if env_summary:
            parts.append(env_summary)
        return "\n\n".join(parts)

    if "<environment_details>" in text:
        without_env = re.sub(
            r"<environment_details>.*?</environment_details>",
            "",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        ).strip()
        parts = [without_env] if without_env else []
        if env_summary:
            parts.append(env_summary)
        return "\n\n".join(parts) if parts else env_summary

    return text


def _truncate_middle(text: str, max_chars: int, reason: str) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text

    marker = (
        f"\n\n[adapter truncated {len(text) - max_chars:,} chars from the "
        f"middle: {reason}]\n\n"
    )
    if max_chars <= len(marker) + 200:
        return text[:max_chars] + "\n\n[adapter truncated long content]"

    head_chars = max(100, int(max_chars * 0.62))
    tail_chars = max(100, max_chars - head_chars - len(marker))
    return text[:head_chars].rstrip() + marker + text[-tail_chars:].lstrip()


def _compact_cline_history_messages(
    prepared: list[tuple[str, str]],
    *,
    label: str = "Cline",
) -> list[tuple[str, str]]:
    max_chars = max(
        2_000,
        _env_int("OPENAI_ADAPTER_CLINE_MESSAGE_MAX_CHARS", 18_000),
    )
    older_max_chars = max(
        1_000,
        _env_int("OPENAI_ADAPTER_CLINE_OLDER_MESSAGE_MAX_CHARS", 7_000),
    )
    keep_recent = max(1, _env_int("OPENAI_ADAPTER_CLINE_KEEP_RECENT_MESSAGES", 8))
    recent_start = max(0, len(prepared) - keep_recent)
    compacted: list[tuple[str, str]] = []
    truncated_count = 0

    for index, (role, text) in enumerate(prepared):
        role_key = (role or "").lower()
        limit = max_chars
        if index < recent_start and role_key in {"assistant", "tool", "function"}:
            limit = older_max_chars
        if len(text) > limit:
            truncated_count += 1
            text = _truncate_middle(
                text,
                limit,
                f"long {label} {role_key or 'message'} history",
            )
        compacted.append((role, text))

    if truncated_count:
        logger.info(
            "Compacted long %s history messages: truncated=%s max_chars=%s "
            "older_max_chars=%s keep_recent=%s",
            label,
            truncated_count,
            max_chars,
            older_max_chars,
            keep_recent,
        )
    return compacted


LOCAL_CONTEXT_FILE_RE = re.compile(
    r"(?:[A-Za-z]:[\\/])?(?:[\w .()\-\u4e00-\u9fff]+[\\/])*"
    r"[\w .()\-\u4e00-\u9fff]+\."
    r"(?:py|js|jsx|ts|tsx|json|md|txt|yml|yaml|toml|ps1|bat|cmd|html|css|scss|sql|cjs|mjs)",
    flags=re.IGNORECASE,
)
LOCAL_CONTEXT_ALLOWED_EXTENSIONS = {
    ".bat",
    ".cjs",
    ".cmd",
    ".css",
    ".html",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".mjs",
    ".ps1",
    ".py",
    ".scss",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}
LOCAL_CONTEXT_DENY_SUBSTRINGS = (
    ".env",
    "cookie",
    "cookies",
    "gemini_cookies.local",
    "adapter_env.local",
    "adapter_usage",
    "adapter_forwarded_prompt",
    "secret",
    "token",
    "password",
)
LOCAL_CONTEXT_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "__pycache__",
    "dist",
    "env",
    "node_modules",
    "venv",
}


def _extract_cwd_from_text(text: str) -> Path | None:
    match = re.search(r"# Current Working Directory\s*\(([^)]+)\)", text)
    if match:
        return Path(match.group(1).strip())

    lines = text.splitlines()
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if line.startswith("# Current Working Directory") and index + 1 < len(lines):
            next_line = lines[index + 1].strip()
            if next_line and not next_line.startswith("#"):
                return Path(next_line)
    return None


def _extract_local_file_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for match in LOCAL_CONTEXT_FILE_RE.finditer(text):
        candidate = match.group(0).strip("`'\"<>[](){}，。；;：:,")
        normalized = candidate.replace("\\", "/")
        if normalized.lower() in seen:
            continue
        seen.add(normalized.lower())
        candidates.append(candidate)
    return candidates


def _is_allowed_local_context_path(path: Path) -> bool:
    lowered = str(path).lower()
    if path.suffix.lower() not in LOCAL_CONTEXT_ALLOWED_EXTENSIONS:
        return False
    return not any(marker in lowered for marker in LOCAL_CONTEXT_DENY_SUBSTRINGS)


def _walk_find_file(base: Path, filename: str) -> Path | None:
    max_files = _env_int("OPENAI_ADAPTER_LOCAL_FILE_CONTEXT_SEARCH_LIMIT", 20000)
    checked = 0
    try:
        for root, dirs, files in os.walk(base):
            dirs[:] = [
                dirname
                for dirname in dirs
                if dirname not in LOCAL_CONTEXT_SKIP_DIRS
                and not dirname.startswith(".")
            ]
            checked += len(files)
            if checked > max_files:
                logger.info(
                    "Stopped local file context search after %s files under %s",
                    checked,
                    base,
                )
                return None
            if filename in files:
                return Path(root) / filename
    except OSError:
        logger.info("Failed to search local context path under %s", base, exc_info=True)
    return None


def _resolve_local_context_path(candidate: str, cwd: Path | None) -> Path | None:
    raw = Path(candidate.replace("/", os.sep))
    base_paths = [path for path in (cwd, ROOT, ROOT.parent) if path is not None]

    possible_paths: list[Path] = []
    if raw.is_absolute():
        possible_paths.append(raw)
    else:
        possible_paths.extend(base / raw for base in base_paths)

    for possible in possible_paths:
        try:
            resolved = possible.resolve()
        except OSError:
            resolved = possible.absolute()
        if resolved.is_file() and _is_allowed_local_context_path(resolved):
            return resolved

    if raw.name and cwd is not None:
        found = _walk_find_file(cwd, raw.name)
        if found is not None and _is_allowed_local_context_path(found):
            return found.resolve()
    return None


def _format_local_context_file(path: Path, remaining_chars: int) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    truncated = len(text) > remaining_chars
    if truncated:
        text = text[:remaining_chars]
    suffix = "\n\n[truncated by adapter local file context limit]" if truncated else ""
    return (
        f"--- FILE: {path} ---\n"
        "```text\n"
        f"{text}{suffix}\n"
        "```"
    )


def _build_local_file_context(
    prepared_messages: list[tuple[str, str]],
    max_chars_override: int | None = None,
) -> str:
    if not _env_bool("OPENAI_ADAPTER_LOCAL_FILE_CONTEXT", True):
        return ""

    combined_text = "\n\n".join(text for _, text in prepared_messages)
    candidates = _extract_local_file_candidates(combined_text)
    if not candidates:
        return ""

    cwd = _extract_cwd_from_text(combined_text)
    max_files = _env_int("OPENAI_ADAPTER_LOCAL_FILE_CONTEXT_MAX_FILES", 3)
    max_chars = _env_int("OPENAI_ADAPTER_LOCAL_FILE_CONTEXT_MAX_CHARS", 120000)
    if max_chars_override is not None:
        max_chars = min(max_chars, max(0, max_chars_override))
    if max_chars < 1_000:
        logger.info(
            "Skipped local file context because remaining prompt budget is small: "
            "max_chars=%s",
            max_chars,
        )
        return ""
    resolved_paths: list[Path] = []
    seen: set[str] = set()

    for candidate in candidates:
        path = _resolve_local_context_path(candidate, cwd)
        if path is None:
            continue
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        resolved_paths.append(path)
        if len(resolved_paths) >= max_files:
            break

    if not resolved_paths:
        return ""

    sections: list[str] = []
    remaining = max_chars
    for path in resolved_paths:
        if remaining <= 0:
            break
        try:
            section = _format_local_context_file(path, remaining)
        except OSError:
            logger.info("Failed to read local file context: %s", path, exc_info=True)
            continue
        if len(section) > remaining:
            section = section[:remaining] + "\n\n[truncated by adapter prompt budget]"
        sections.append(section)
        remaining -= len(section)

    if not sections:
        return ""

    logger.info(
        "Injected local file context: files=%s chars=%s",
        len(sections),
        sum(len(section) for section in sections),
    )
    return (
        "Adapter local file context:\n"
        "The local adapter read these files because the Cline task explicitly "
        "referenced them. Treat this as real file content from the user's machine. "
        "Use it directly; do not ask the user to paste the same file again.\n\n"
        + "\n\n".join(sections)
    )


def _prepare_messages_for_gemini(
    messages: list[ChatMessage],
) -> tuple[list[tuple[str, str]], bool]:
    prompt_mode = os.getenv("OPENAI_ADAPTER_PROMPT_MODE", "auto").strip().lower()
    if prompt_mode in {"raw", "pass", "passthrough", "off", "none"}:
        return [
            ((message.role or "").lower(), _message_content_to_text(message.content))
            for message in messages
        ], False

    raw_messages = [
        ((message.role or "").lower(), _message_content_to_text(message.content))
        for message in messages
    ]
    cline_detected = any(_is_cline_system_prompt(text) for _, text in raw_messages)

    if not cline_detected:
        return _compact_cline_history_messages(raw_messages, label="OpenAI"), False

    prepared: list[tuple[str, str]] = []
    compact_system_added = False

    for role, text in raw_messages:
        if _is_cline_system_prompt(text):
            if not compact_system_added:
                prepared.append(("system", CLINE_COMPACT_SYSTEM_PROMPT))
                compact_system_added = True
            if "<task>" in text or "<environment_details>" in text:
                embedded_user_text = _compact_cline_user_message(text)
                if embedded_user_text and embedded_user_text != text:
                    prepared.append(("user", embedded_user_text))
            continue
        if role == "user":
            prepared.append((role, _compact_cline_user_message(text)))
        elif role == "assistant" and _is_cline_self_refusal(text):
            logger.info("Dropped prior Cline self-refusal from compact prompt.")
        else:
            prepared.append((role, text))

    return _compact_cline_history_messages(prepared, label="Cline"), True


def _messages_to_prompt(
    messages: list[ChatMessage],
    *,
    include_local_context: bool = True,
    local_context_max_chars: int | None = None,
) -> str:
    if not messages:
        raise ValueError("messages must contain at least one item.")

    system_parts: list[str] = []
    conversation_parts: list[str] = []
    prepared_messages, compacted = _prepare_messages_for_gemini(messages)
    if compacted:
        logger.info("Applied compact Cline prompt compatibility mode.")

    for role, text in prepared_messages:
        role = (role or "").lower()
        text = text.strip()
        if not text:
            continue

        if role in {"system", "developer"}:
            system_parts.append(text)
            continue

        label = {
            "user": "User",
            "assistant": "Assistant",
            "tool": "Tool",
            "function": "Tool",
        }.get(role, role.title() or "Message")
        conversation_parts.append(f"{label}:\n{text}")

    if not system_parts and not conversation_parts:
        raise ValueError("messages did not contain any text content.")

    prompt_parts: list[str] = []
    if system_parts:
        prompt_parts.append("System instructions:\n" + "\n\n".join(system_parts))
    if conversation_parts:
        prompt_parts.append("Conversation:\n" + "\n\n".join(conversation_parts))
    if compacted and include_local_context:
        local_file_context = _build_local_file_context(
            prepared_messages,
            local_context_max_chars,
        )
        if local_file_context:
            prompt_parts.append(local_file_context)

    return "\n\n".join(prompt_parts)


def _write_debug_prompt(prompt: str) -> None:
    debug_path = os.getenv("OPENAI_ADAPTER_DEBUG_PROMPT_PATH")
    if not debug_path:
        return
    try:
        Path(debug_path).write_text(prompt, encoding="utf-8")
        logger.info(
            "Wrote forwarded Gemini prompt debug file: %s chars=%s",
            debug_path,
            len(prompt),
        )
    except OSError:
        logger.error("Failed to write debug prompt file: %s", debug_path, exc_info=True)


def _select_gemini_model(requested_model: str | None) -> str:
    fallback = os.getenv("GEMINI_DEFAULT_MODEL", "unspecified")
    if not requested_model:
        return fallback
    if requested_model in KNOWN_GEMINI_MODEL_NAMES or requested_model.startswith(
        "gemini-"
    ):
        return requested_model
    logger.info(
        "Mapping OpenAI-facing model %r to Gemini model %r.",
        requested_model,
        fallback,
    )
    return fallback


def _completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex}"


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk_chars = len(re.findall(r"[\u3400-\u9fff\uf900-\ufaff]", text))
    non_cjk_chars = max(0, len(text) - cjk_chars)
    return max(1, int(round(cjk_chars + non_cjk_chars / 4)))


def _max_prompt_tokens() -> int:
    # 彻底调大到 120,000，防止因历史上下文积累导致的任何溢出报错
    return max(0, _env_int("OPENAI_ADAPTER_MAX_PROMPT_TOKENS", 120_000))


def _ensure_prompt_budget(prompt: str) -> int:
    prompt_tokens = _estimate_tokens(prompt)
    max_prompt_tokens = _max_prompt_tokens()
    if max_prompt_tokens and prompt_tokens > max_prompt_tokens:
        raise ValueError(
            "Prompt is too large for the configured Gemini adapter budget: "
            f"estimated {prompt_tokens:,} tokens > limit {max_prompt_tokens:,}. "
            "Split the task into smaller phases or reduce the attached context."
        )
    return prompt_tokens


def _build_prompt_with_budget(messages: list[ChatMessage]) -> tuple[str, int]:
    max_prompt_tokens = _max_prompt_tokens()
    prompt = _messages_to_prompt(messages)
    prompt_tokens = _estimate_tokens(prompt)
    if not max_prompt_tokens or prompt_tokens <= max_prompt_tokens:
        return prompt, prompt_tokens

    base_prompt = _messages_to_prompt(messages, include_local_context=False)
    base_tokens = _estimate_tokens(base_prompt)
    if base_tokens > max_prompt_tokens:
        raise ValueError(
            "Prompt is too large for the configured Gemini adapter budget even "
            "after dropping adapter-added file context: "
            f"estimated {base_tokens:,} tokens > limit {max_prompt_tokens:,}. "
            "Start a fresh Cline task or split the task into smaller phases."
        )

    remaining_tokens = max_prompt_tokens - base_tokens
    if remaining_tokens < 800:
        logger.info(
            "Dropped local file context to stay within prompt budget: "
            "base_tokens=%s max_prompt_tokens=%s",
            base_tokens,
            max_prompt_tokens,
        )
        return base_prompt, base_tokens

    char_budget = min(
        _env_int("OPENAI_ADAPTER_LOCAL_FILE_CONTEXT_MAX_CHARS", 120000),
        max(1_000, int((remaining_tokens - 256) * 3)),
    )
    for attempt in range(6):
        candidate = _messages_to_prompt(
            messages,
            include_local_context=True,
            local_context_max_chars=char_budget,
        )
        candidate_tokens = _estimate_tokens(candidate)
        if candidate_tokens <= max_prompt_tokens:
            logger.info(
                "Shrank local file context to fit prompt budget: "
                "attempt=%s prompt_tokens=%s max_prompt_tokens=%s "
                "local_context_max_chars=%s",
                attempt + 1,
                candidate_tokens,
                max_prompt_tokens,
                char_budget,
            )
            return candidate, candidate_tokens
        ratio = max_prompt_tokens / max(candidate_tokens, 1)
        char_budget = int(char_budget * ratio * 0.82)
        if char_budget < 1_000:
            break

    logger.info(
        "Dropped local file context after shrink attempts: base_tokens=%s "
        "max_prompt_tokens=%s",
        base_tokens,
        max_prompt_tokens,
    )
    return base_prompt, base_tokens


def _get_usage_log_path() -> Path:
    return Path(
        os.getenv(
            "OPENAI_ADAPTER_USAGE_LOG_PATH",
            str(ROOT / "runtime" / "adapter_usage.jsonl"),
        )
    )


def _usage_instance_id() -> str:
    raw = os.getenv("OPENAI_ADAPTER_INSTANCE_ID") or socket.gethostname() or "unknown"
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw.strip()).strip(".-")
    if cleaned:
        return cleaned
    digest = hashlib.sha1(raw.strip().encode("utf-8", errors="ignore")).hexdigest()[:10]
    return f"pc-{digest}" if digest else "unknown"


def _usage_instance_name() -> str:
    raw = (
        os.getenv("OPENAI_ADAPTER_INSTANCE_NAME")
        or os.getenv("OPENAI_ADAPTER_INSTANCE_ID")
        or socket.gethostname()
        or "unknown"
    )
    return raw.strip() or _usage_instance_id()


def _get_shared_usage_dir() -> Path | None:
    raw = os.getenv("OPENAI_ADAPTER_USAGE_SHARED_DIR", "").strip()
    if not raw:
        return None
    return Path(raw)


def _get_shared_usage_log_path() -> Path | None:
    shared_dir = _get_shared_usage_dir()
    if shared_dir is None:
        return None
    return shared_dir / f"adapter_usage.{_usage_instance_id()}.jsonl"


def _same_path(left: Path, right: Path) -> bool:
    try:
        return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))
    except OSError:
        return os.path.normcase(str(left.absolute())) == os.path.normcase(str(right.absolute()))


def _resolve_price_spec(model: str | None) -> PriceSpec | None:
    requested = (model or "").strip().lower()
    canonical = MODEL_PRICE_ALIASES.get(requested, requested)
    return PRICE_SPECS.get(canonical)


def _estimate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> dict[str, Any]:
    usd_to_cny = _env_float("OPENAI_ADAPTER_USD_TO_CNY", 7.25)
    spec = _resolve_price_spec(model)
    if spec is None:
        return {
            "estimated": True,
            "available": False,
            "reason": f"No local pricing table entry for model {model!r}.",
            "source": PRICING_SOURCE_URL,
        }

    extended = bool(
        spec.threshold_tokens is not None and prompt_tokens > spec.threshold_tokens
    )
    input_rate = (
        spec.input_over_threshold_usd_per_1m
        if extended and spec.input_over_threshold_usd_per_1m is not None
        else spec.input_usd_per_1m
    )
    output_rate = (
        spec.output_over_threshold_usd_per_1m
        if extended and spec.output_over_threshold_usd_per_1m is not None
        else spec.output_usd_per_1m
    )
    input_cost_usd = prompt_tokens * input_rate / 1_000_000
    output_cost_usd = completion_tokens * output_rate / 1_000_000
    total_cost_usd = input_cost_usd + output_cost_usd

    return {
        "estimated": True,
        "available": True,
        "billing_basis": "Gemini API paid-tier equivalent; not a Gemini web bill",
        "model_for_pricing": model,
        "official_pricing_model": spec.official_model,
        "tier": spec.tier,
        "source": PRICING_SOURCE_URL,
        "prompt_threshold_tokens": spec.threshold_tokens,
        "extended_context_rate": extended,
        "input_usd_per_1m_tokens": input_rate,
        "output_usd_per_1m_tokens": output_rate,
        "input_cost_usd": round(input_cost_usd, 8),
        "output_cost_usd": round(output_cost_usd, 8),
        "total_cost_usd": round(total_cost_usd, 8),
        "usd_to_cny": usd_to_cny,
        "total_cost_cny": round(total_cost_usd * usd_to_cny, 8),
    }


def _build_usage(
    model: str,
    prompt: str,
    completion_text: str,
) -> tuple[dict[str, int], dict[str, Any]]:
    prompt_tokens = _estimate_tokens(prompt)
    completion_tokens = _estimate_tokens(completion_text)
    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    return usage, _estimate_cost(model, prompt_tokens, completion_tokens)


def _append_jsonl_record(path: Path, record: dict[str, Any], label: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
    except OSError:
        logger.error("Failed to append %s usage record: %s", label, path, exc_info=True)


def _append_usage_record(record: dict[str, Any]) -> None:
    local_path = _get_usage_log_path()
    _append_jsonl_record(local_path, record, "local")

    shared_path = _get_shared_usage_log_path()
    if shared_path is not None and not _same_path(local_path, shared_path):
        _append_jsonl_record(shared_path, record, "shared")


def _record_usage(
    completion_id: str,
    requested_model: str,
    gemini_model: str,
    stream: bool,
    usage: dict[str, int],
    cost_estimate: dict[str, Any],
    upstream_provider: str = "gemini",
) -> None:
    record = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "id": completion_id,
        "instance_id": _usage_instance_id(),
        "instance_name": _usage_instance_name(),
        "host": socket.gethostname(),
        "requested_model": requested_model,
        "gemini_model": gemini_model,
        "upstream_provider": upstream_provider,
        "upstream_model": gemini_model,
        "stream": stream,
        "usage": usage,
        "cost_estimate": cost_estimate,
    }
    _append_usage_record(record)
    logger.info(
        "Usage estimate: id=%s model=%s prompt_tokens=%s completion_tokens=%s "
        "cost_usd=%s cost_cny=%s",
        completion_id,
        requested_model,
        usage["prompt_tokens"],
        usage["completion_tokens"],
        cost_estimate.get("total_cost_usd"),
        cost_estimate.get("total_cost_cny"),
    )


def _infer_instance_id_from_path(path: Path) -> str:
    stem = path.stem
    for prefix in ("adapter_usage.", "adapter_usage_"):
        if stem.startswith(prefix):
            return stem[len(prefix) :] or "unknown"
    return _usage_instance_id()


def _record_instance_id(record: dict[str, Any]) -> str:
    return str(record.get("instance_id") or record.get("host") or "unknown")


def _record_instance_label(record: dict[str, Any]) -> str:
    name = str(record.get("instance_name") or "").strip()
    if name:
        return name
    instance_id = str(record.get("instance_id") or "").strip()
    host = str(record.get("host") or "").strip()
    if host and (not instance_id or instance_id == "unknown" or instance_id.startswith("pc-")):
        return host
    return instance_id or host or "unknown"


def _record_looks_local(record: dict[str, Any], source_path: Path) -> bool:
    if _same_path(source_path, _get_usage_log_path()):
        return True
    current_shared_path = _get_shared_usage_log_path()
    if current_shared_path is not None and _same_path(source_path, current_shared_path):
        return True
    host = str(record.get("host") or "")
    return host in {socket.gethostname(), _usage_instance_name()}


def _usage_log_paths() -> list[Path]:
    paths = [_get_usage_log_path()]
    shared_dir = _get_shared_usage_dir()
    if shared_dir is not None and shared_dir.exists():
        paths.extend(sorted(shared_dir.glob("adapter_usage*.jsonl")))

    unique: list[Path] = []
    for path in paths:
        if any(_same_path(path, existing) for existing in unique):
            continue
        unique.append(path)
    return unique


def _read_usage_records_from_path(path: Path) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    invalid_lines = 0
    if not path.exists():
        return records, invalid_lines

    fallback_instance_id = _infer_instance_id_from_path(path)
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                invalid_lines += 1
                logger.warning("Skipping invalid usage log line in %s", path)
                continue
            if isinstance(record, dict):
                record.setdefault("instance_id", fallback_instance_id)
                record.setdefault("host", record.get("instance_id") or fallback_instance_id)
                if str(record.get("instance_id") or "") == "unknown" and _record_looks_local(record, path):
                    record["instance_id"] = _usage_instance_id()
                record.setdefault("instance_name", _record_instance_label(record))
                record["_source_file"] = str(path)
                records.append(record)
    except (OSError, UnicodeError):
        logger.error("Failed to read usage log: %s", path, exc_info=True)

    return records, invalid_lines


def _collect_usage_records() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records_by_key: dict[str, dict[str, Any]] = {}
    anonymous_index = 0
    sources: list[dict[str, Any]] = []

    for path in _usage_log_paths():
        records, invalid_lines = _read_usage_records_from_path(path)
        sources.append(
            {
                "path": str(path),
                "exists": path.exists(),
                "records": len(records),
                "invalid_lines": invalid_lines,
            }
        )
        for record in records:
            key = str(record.get("id") or "")
            if not key:
                anonymous_index += 1
                key = (
                    f"anonymous:{record.get('timestamp', '')}:"
                    f"{record.get('instance_id', '')}:{anonymous_index}"
                )
            existing = records_by_key.setdefault(key, record)
            if existing is not record:
                source_files = existing.setdefault("_source_files", [existing.get("_source_file")])
                source_file = record.get("_source_file")
                if source_file and source_file not in source_files:
                    source_files.append(source_file)

    records = list(records_by_key.values())
    records.sort(key=lambda item: str(item.get("timestamp") or ""))
    return records, sources


def _strip_usage_reader_fields(record: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(record)
    cleaned.pop("_source_file", None)
    cleaned.pop("_source_files", None)
    return cleaned


def _backfill_shared_usage_log() -> None:
    local_path = _get_usage_log_path()
    shared_path = _get_shared_usage_log_path()
    if shared_path is None or _same_path(local_path, shared_path) or not local_path.exists():
        return

    local_records, _ = _read_usage_records_from_path(local_path)
    shared_records, _ = _read_usage_records_from_path(shared_path)
    shared_ids = {str(record.get("id")) for record in shared_records if record.get("id")}
    missing = [
        _strip_usage_reader_fields(record)
        for record in local_records
        if record.get("id") and str(record.get("id")) not in shared_ids
    ]
    for record in missing:
        _append_jsonl_record(shared_path, record, "shared backfill")
    if missing:
        logger.info(
            "Backfilled %s usage records to shared usage log: %s",
            len(missing),
            shared_path,
        )


def _usage_timezone() -> timezone:
    offset_hours = _env_float("OPENAI_ADAPTER_USAGE_TZ_OFFSET_HOURS", 8)
    return timezone(timedelta(hours=offset_hours))


def _record_local_date_key(record: dict[str, Any]) -> str:
    raw_timestamp = str(record.get("timestamp") or "")
    if not raw_timestamp:
        return datetime.now(tz=_usage_timezone()).date().isoformat()
    try:
        parsed = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(_usage_timezone()).date().isoformat()
    except ValueError:
        return raw_timestamp[:10]


def _build_daily_usage(records: list[dict[str, Any]], year: int | None = None) -> dict[str, Any]:
    tz = _usage_timezone()
    today = datetime.now(tz=tz).date()
    target_year = year or today.year
    start = date(target_year, 1, 1)
    end = date(target_year, 12, 31)
    days_since_sunday = (start.weekday() + 1) % 7
    grid_start = start - timedelta(days=days_since_sunday)
    days_until_saturday = (5 - end.weekday()) % 7
    grid_end = end + timedelta(days=days_until_saturday)

    daily: dict[str, dict[str, Any]] = {}
    for record in records:
        key = _record_local_date_key(record)
        bucket = daily.setdefault(
            key,
            {
                "date": key,
                "requests": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "cost_cny": 0.0,
            },
        )
        usage = record.get("usage") or {}
        cost = record.get("cost_estimate") or {}
        bucket["requests"] += 1
        bucket["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        bucket["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        bucket["total_tokens"] += int(usage.get("total_tokens") or 0)
        bucket["cost_usd"] += float(cost.get("total_cost_usd") or 0)
        bucket["cost_cny"] += float(cost.get("total_cost_cny") or 0)

    max_tokens = max(
        (
            int(item["total_tokens"])
            for key, item in daily.items()
            if start.isoformat() <= key <= end.isoformat()
        ),
        default=0,
    )
    cells: list[dict[str, Any]] = []
    cursor = grid_start
    while cursor <= grid_end:
        key = cursor.isoformat()
        item = daily.get(
            key,
            {
                "date": key,
                "requests": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "cost_cny": 0.0,
            },
        )
        tokens = int(item["total_tokens"])
        if tokens == 0 or max_tokens == 0:
            level = 0
        elif tokens <= max_tokens * 0.25:
            level = 1
        elif tokens <= max_tokens * 0.50:
            level = 2
        elif tokens <= max_tokens * 0.75:
            level = 3
        else:
            level = 4
        cells.append(
            {
                **item,
                "cost_usd": round(float(item["cost_usd"]), 8),
                "cost_cny": round(float(item["cost_cny"]), 8),
                "weekday": (cursor.weekday() + 1) % 7,
                "level": level,
                "in_range": start <= cursor <= end,
                "is_future": cursor > today,
                "is_today": cursor == today,
            }
        )
        cursor += timedelta(days=1)

    return {
        "timezone_offset_hours": _env_float("OPENAI_ADAPTER_USAGE_TZ_OFFSET_HOURS", 8),
        "display_mode": "calendar_year",
        "year": target_year,
        "today": today.isoformat(),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "max_daily_tokens": max_tokens,
        "cells": cells,
    }


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _openai_error_response(
    status_code: int,
    message: str,
    error_type: str = "server_error",
    code: str | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "param": None,
                "code": code,
            }
        },
    )


def _join_g4f_url(base_url: str, path: str) -> str:
    clean_base = base_url.rstrip("/")
    clean_path = path if path.startswith("/") else f"/{path}"
    return f"{clean_base}{clean_path}"


def _extract_openai_response_text(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    first = choices[0] or {}
    message = first.get("message") or {}
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts)
    text = first.get("text")
    if isinstance(text, str):
        return text
    delta = first.get("delta") or {}
    if isinstance(delta, dict) and isinstance(delta.get("content"), str):
        return delta["content"]
    return ""


def _g4f_request_payload(payload: ChatCompletionRequest) -> tuple[str, dict[str, Any]]:
    upstream_model, _ = _strip_g4f_model_prefix(payload.model)
    body = payload.model_dump(exclude_none=True)
    body["model"] = upstream_model or "gpt-4o-mini"
    body["stream"] = False
    provider = _g4f_provider()
    if provider and not body.get("provider"):
        body["provider"] = provider
    return body["model"], body


def _post_g4f_chat_completion(
    payload: ChatCompletionRequest,
) -> tuple[str, dict[str, Any]]:
    base_url = _g4f_base_url()
    if base_url is None:
        raise G4FUpstreamError(
            "gpt4free upstream is not configured. Set OPENAI_ADAPTER_G4F_BASE_URL, "
            "for example http://127.0.0.1:1337/v1."
        )

    upstream_model, body = _g4f_request_payload(payload)
    url = _join_g4f_url(base_url, "/chat/completions")
    request_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    api_key = os.getenv("OPENAI_ADAPTER_G4F_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    request = urllib.request.Request(
        url,
        data=request_body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_g4f_timeout_seconds()) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise G4FUpstreamError(
            f"gpt4free upstream returned HTTP {exc.code}: {error_body[:800]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise G4FUpstreamError(f"gpt4free upstream is unreachable: {exc.reason}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise G4FUpstreamError(
            f"gpt4free upstream returned non-JSON response: {raw[:800]}"
        ) from exc
    if not isinstance(data, dict):
        raise G4FUpstreamError("gpt4free upstream returned an unexpected response shape.")
    return upstream_model, data


async def _call_g4f_chat_completion(
    payload: ChatCompletionRequest,
) -> tuple[str, dict[str, Any]]:
    return await asyncio.to_thread(_post_g4f_chat_completion, payload)


def _normalize_g4f_completion(
    upstream_data: dict[str, Any],
    *,
    completion_id: str,
    created: int,
    requested_model: str,
    upstream_model: str,
    prompt: str,
    stream: bool,
) -> dict[str, Any]:
    text = _extract_openai_response_text(upstream_data)
    usage = upstream_data.get("usage")
    if not isinstance(usage, dict):
        usage, cost_estimate = _build_usage(upstream_model, prompt, text)
    else:
        usage = {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        }
        cost_estimate = _estimate_cost(upstream_model, usage["prompt_tokens"], usage["completion_tokens"])

    if not usage["total_tokens"]:
        usage, cost_estimate = _build_usage(upstream_model, prompt, text)

    _record_usage(
        completion_id,
        requested_model,
        upstream_model,
        stream,
        usage,
        cost_estimate,
        upstream_provider="gpt4free",
    )

    return {
        "id": upstream_data.get("id") or completion_id,
        "object": "chat.completion",
        "created": int(upstream_data.get("created") or created),
        "model": requested_model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": text,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
        "cost_estimate": cost_estimate,
        "upstream_provider": "gpt4free",
        "upstream_model": upstream_model,
    }


async def _g4f_sse_events(
    completion: dict[str, Any],
    completion_id: str,
    requested_model: str,
    created: int,
) -> AsyncGenerator[dict[str, str], None]:
    text = _extract_openai_response_text(completion)
    yield {
        "data": _json_dumps(
            _stream_chunk_payload(
                completion_id,
                requested_model,
                created,
                {"role": "assistant"},
            )
        )
    }
    if text:
        yield {
            "data": _json_dumps(
                _stream_chunk_payload(
                    completion_id,
                    requested_model,
                    created,
                    {"content": text},
                )
            )
        }
    finish_payload = _stream_chunk_payload(
        completion_id,
        requested_model,
        created,
        {},
        finish_reason="stop",
    )
    if "usage" in completion:
        finish_payload["usage"] = completion["usage"]
    if "cost_estimate" in completion:
        finish_payload["cost_estimate"] = completion["cost_estimate"]
    yield {"data": _json_dumps(finish_payload)}
    yield {"data": "[DONE]"}


async def _handle_g4f_chat_completion(
    payload: ChatCompletionRequest,
    completion_id: str,
    created: int,
) -> JSONResponse | EventSourceResponse:
    prompt = _messages_to_prompt(payload.messages, include_local_context=False)
    upstream_model, upstream_data = await _call_g4f_chat_completion(payload)
    completion = _normalize_g4f_completion(
        upstream_data,
        completion_id=completion_id,
        created=created,
        requested_model=payload.model,
        upstream_model=upstream_model,
        prompt=prompt,
        stream=payload.stream,
    )
    logger.info(
        "Completed gpt4free response: id=%s requested_model=%s upstream_model=%s "
        "stream=%s response_chars=%s",
        completion_id,
        payload.model,
        upstream_model,
        payload.stream,
        len(_extract_openai_response_text(completion)),
    )
    if payload.stream:
        return EventSourceResponse(
            _g4f_sse_events(completion, completion_id, payload.model, created),
            media_type="text/event-stream",
            ping=_stream_ping_seconds(),
        )
    return JSONResponse(completion)


def _stream_chunk_payload(
    completion_id: str,
    model: str,
    created: int,
    delta: dict[str, Any],
    finish_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }


async def _prime_gemini_stream(
    client: GeminiClient,
    prompt: str,
    gemini_model: str,
) -> tuple[AsyncGenerator[Any, None], list[str]]:
    """Start Gemini streaming and buffer the first text delta.

    This lets startup/auth/request errors surface before FastAPI sends SSE
    headers, so clients can still receive an HTTP 500 JSON error.
    """

    upstream = client.generate_content_stream(
        prompt,
        model=gemini_model,
        current_retry=_upstream_retry_count(),
    )
    buffered: list[str] = []

    while True:
        try:
            output = await upstream.__anext__()
        except StopAsyncIteration:
            return upstream, buffered

        delta = getattr(output, "text_delta", "") or ""
        if delta:
            buffered.append(delta)
            return upstream, buffered


async def _openai_sse_events(
    client: GeminiClient,
    upstream: AsyncGenerator[Any, None],
    buffered_deltas: list[str],
    completion_id: str,
    openai_model: str,
    gemini_model: str,
    prompt: str,
    created: int,
) -> AsyncGenerator[dict[str, str], None]:
    chunk_count = 0
    completion_parts: list[str] = []

    try:
        yield {
            "data": _json_dumps(
                _stream_chunk_payload(
                    completion_id,
                    openai_model,
                    created,
                    {"role": "assistant"},
                )
            )
        }

        for delta in buffered_deltas:
            chunk_count += 1
            completion_parts.append(delta)
            yield {
                "data": _json_dumps(
                    _stream_chunk_payload(
                        completion_id,
                        openai_model,
                        created,
                        {"content": delta},
                    )
                )
            }

        async for output in upstream:
            delta = getattr(output, "text_delta", "") or ""
            if not delta:
                continue
            chunk_count += 1
            completion_parts.append(delta)
            yield {
                "data": _json_dumps(
                    _stream_chunk_payload(
                        completion_id,
                        openai_model,
                        created,
                        {"content": delta},
                    )
                )
            }

        usage, cost_estimate = _build_usage(
            gemini_model,
            prompt,
            "".join(completion_parts),
        )
        _record_usage(
            completion_id,
            openai_model,
            gemini_model,
            True,
            usage,
            cost_estimate,
        )
        await _write_client_cookies_back(client, "stream-response")
        finish_payload = _stream_chunk_payload(
            completion_id,
            openai_model,
            created,
            {},
            finish_reason="stop",
        )
        finish_payload["usage"] = usage
        finish_payload["cost_estimate"] = cost_estimate
        yield {
            "data": _json_dumps(finish_payload)
        }
        logger.info(
            "Completed streaming response: id=%s chunks=%s",
            completion_id,
            chunk_count,
        )
        yield {"data": "[DONE]"}

    except asyncio.CancelledError:
        logger.info("Streaming response cancelled by client: id=%s", completion_id)
        raise
    except AuthError as exc:
        logger.error("Gemini streaming error: id=%s", completion_id, exc_info=True)
        yield {
            "data": _json_dumps(
                {
                    "error": {
                        "message": str(exc),
                        "type": exc.__class__.__name__,
                        "param": None,
                        "code": "gemini_stream_error",
                    }
                }
            )
        }
        yield {"data": "[DONE]"}
    except (APIError, GeminiError, GeminiTimeoutError) as exc:
        logger.error("Gemini streaming error: id=%s", completion_id, exc_info=True)
        if _looks_like_auth_failure(exc, client):
            yield {
                "data": _json_dumps(
                    {
                        "error": {
                            "message": _auth_failure_message(client),
                            "type": "authentication_error",
                            "param": None,
                            "code": "gemini_auth_failed",
                        }
                    }
                )
            }
            yield {"data": "[DONE]"}
            return

        fallback_model = _stream_fallback_model(gemini_model)
        if _stream_fallback_enabled() and not completion_parts and fallback_model:
            logger.info(
                "Trying fallback generation after stream failure: id=%s "
                "fallback_model=%s",
                completion_id,
                fallback_model,
            )
            try:
                fallback_output = await client.generate_content(
                    prompt,
                    model=fallback_model,
                    current_retry=_upstream_retry_count(),
                )
                fallback_text = getattr(fallback_output, "text", "") or ""
                if fallback_text:
                    usage, cost_estimate = _build_usage(
                        fallback_model,
                        prompt,
                        fallback_text,
                    )
                    _record_usage(
                        completion_id,
                        openai_model,
                        fallback_model,
                        True,
                        usage,
                        cost_estimate,
                    )
                    await _write_client_cookies_back(
                        client,
                        "stream-fallback-response",
                    )
                    yield {
                        "data": _json_dumps(
                            _stream_chunk_payload(
                                completion_id,
                                openai_model,
                                created,
                                {"content": fallback_text},
                            )
                        )
                    }
                    finish_payload = _stream_chunk_payload(
                        completion_id,
                        openai_model,
                        created,
                        {},
                        finish_reason="stop",
                    )
                    finish_payload["usage"] = usage
                    finish_payload["cost_estimate"] = cost_estimate
                    finish_payload["fallback"] = {
                        "reason": "stream_error_before_text",
                        "original_gemini_model": gemini_model,
                        "fallback_gemini_model": fallback_model,
                    }
                    yield {"data": _json_dumps(finish_payload)}
                    yield {"data": "[DONE]"}
                    logger.info(
                        "Completed fallback response: id=%s fallback_model=%s "
                        "chars=%s",
                        completion_id,
                        fallback_model,
                        len(fallback_text),
                    )
                    return
            except Exception as fallback_exc:
                logger.error(
                    "Fallback generation failed after stream error: id=%s "
                    "fallback_model=%s",
                    completion_id,
                    fallback_model,
                    exc_info=True,
                )
                if _looks_like_auth_failure(fallback_exc, client):
                    yield {
                        "data": _json_dumps(
                            {
                                "error": {
                                    "message": _auth_failure_message(client),
                                    "type": "authentication_error",
                                    "param": None,
                                    "code": "gemini_auth_failed",
                                }
                            }
                        )
                    }
                    yield {"data": "[DONE]"}
                    return
                exc = GeminiError(
                    f"{exc} Fallback model {fallback_model!r} also failed: "
                    f"{fallback_exc}"
                )
        yield {
            "data": _json_dumps(
                {
                    "error": {
                        "message": str(exc),
                        "type": exc.__class__.__name__,
                        "param": None,
                        "code": "gemini_stream_error",
                    }
                }
            )
        }
        yield {"data": "[DONE]"}
    except Exception as exc:
        logger.error("Unexpected streaming error: id=%s", completion_id, exc_info=True)
        yield {
            "data": _json_dumps(
                {
                    "error": {
                        "message": str(exc),
                        "type": exc.__class__.__name__,
                        "param": None,
                        "code": "unexpected_stream_error",
                    }
                }
            )
        }
        yield {"data": "[DONE]"}
    finally:
        close = getattr(upstream, "aclose", None)
        if close is not None:
            await close()


def _stream_eager_enabled() -> bool:
    return _env_bool("OPENAI_ADAPTER_STREAM_EAGER", True)


def _stream_fallback_enabled() -> bool:
    return _env_bool("OPENAI_ADAPTER_STREAM_FALLBACK_NON_STREAM", True)


def _stream_fallback_model(current_model: str) -> str | None:
    fallback = os.getenv("OPENAI_ADAPTER_STREAM_FALLBACK_MODEL", "gemini-3-flash")
    fallback = fallback.strip()
    if not fallback or fallback.lower() in {"0", "false", "none", "off"}:
        return None
    if fallback == current_model:
        return None
    return fallback


def _stream_ping_seconds() -> int:
    return max(1, _env_int("OPENAI_ADAPTER_STREAM_PING_SECONDS", 15))


def _upstream_retry_count() -> int:
    return max(0, _env_int("OPENAI_ADAPTER_UPSTREAM_RETRIES", 0))


def _adapter_console_html() -> str:
    start_entry = "START_HERE.command" if sys.platform == "darwin" else "START_HERE.bat"
    body = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Gemini OpenAI 适配器控制台</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f7fb;
      --panel: #ffffff;
      --panel-soft: #f8fafc;
      --text: #172033;
      --muted: #64748b;
      --line: #d7dde8;
      --blue: #155eef;
      --blue-soft: #eaf1ff;
      --green: #16803c;
      --red: #b42318;
      --amber: #a15c07;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
      color: var(--text);
      background: var(--bg);
    }
    main {
      max-width: 1180px;
      margin: 0 auto;
      padding: 22px 16px 42px;
    }
    header {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 16px;
    }
    h1 {
      margin: 0 0 6px;
      font-size: 26px;
      line-height: 1.25;
    }
    h2 {
      margin: 0 0 12px;
      font-size: 18px;
      line-height: 1.3;
    }
    h3 {
      margin: 16px 0 8px;
      font-size: 15px;
    }
    p { margin: 0; }
    a { color: var(--blue); text-decoration: none; }
    code {
      font-family: Consolas, "Courier New", monospace;
      word-break: break-all;
    }
    .muted {
      color: var(--muted);
      line-height: 1.65;
    }
    .top-actions {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
      justify-content: flex-end;
    }
    .btn {
      appearance: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border: 1px solid var(--blue);
      background: var(--blue);
      color: #fff;
      border-radius: 7px;
      padding: 9px 12px;
      font-size: 14px;
      font-weight: 700;
      cursor: pointer;
      min-height: 38px;
      text-decoration: none;
    }
    .btn.secondary {
      background: #fff;
      color: var(--blue);
    }
    .btn:disabled {
      opacity: 0.62;
      cursor: wait;
    }
    .nav {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin: 14px 0 18px;
    }
    .nav a {
      display: inline-flex;
      align-items: center;
      min-height: 34px;
      padding: 6px 10px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--panel);
      color: var(--text);
      font-size: 13px;
    }
    .section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      margin: 14px 0;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
      gap: 10px;
    }
    .metric {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: var(--panel-soft);
      min-width: 0;
    }
    .label {
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 5px;
    }
    .value {
      font-weight: 800;
      font-size: 20px;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }
    .value.small { font-size: 14px; font-weight: 700; }
    .ok { color: var(--green); }
    .bad { color: var(--red); }
    .warn { color: var(--amber); }
    .split {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(280px, 360px);
      gap: 12px;
    }
    .status-line {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      margin-top: 10px;
    }
    .guide-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 12px;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      padding: 4px 9px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--panel-soft);
      font-size: 13px;
    }
    .pill.ok { border-color: #a7e0b8; background: #edfdf3; }
    .pill.bad { border-color: #f3b1aa; background: #fff1f0; }
    .output {
      width: 100%;
      min-height: 150px;
      margin: 0;
      padding: 12px;
      border-radius: 8px;
      overflow: auto;
      white-space: pre-wrap;
      background: #101828;
      color: #eef4ff;
      font-family: Consolas, "Courier New", monospace;
      font-size: 12px;
      line-height: 1.55;
    }
    .terminal-output {
      min-height: 260px;
      max-height: 520px;
    }
    .quick-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }
    .quick-table th,
    .quick-table td {
      border-bottom: 1px solid var(--line);
      padding: 8px 4px;
      text-align: left;
      vertical-align: top;
    }
    .quick-table th {
      color: var(--muted);
      font-weight: 700;
      width: 120px;
    }
    .calendar-wrap {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      margin-top: 12px;
      overflow-x: auto;
      background: var(--panel-soft);
    }
    .heatmap {
      width: max-content;
      min-width: 100%;
    }
    .month-row {
      display: grid;
      gap: 3px;
      margin-left: 24px;
      margin-bottom: 5px;
      min-height: 16px;
      font-size: 11px;
      color: var(--muted);
      white-space: nowrap;
    }
    .month-label {
      min-width: 30px;
      overflow: visible;
    }
    .heatmap-body {
      display: flex;
      align-items: flex-start;
      gap: 6px;
    }
    .weekday-labels {
      display: grid;
      grid-template-rows: repeat(7, 12px);
      gap: 3px;
      width: 18px;
      font-size: 11px;
      line-height: 12px;
      color: var(--muted);
      text-align: right;
    }
    .calendar {
      display: grid;
      grid-auto-flow: column;
      grid-template-rows: repeat(7, 12px);
      gap: 3px;
      min-height: 102px;
      width: max-content;
    }
    .day {
      width: 12px;
      height: 12px;
      border-radius: 3px;
      background: #ebedf0;
      box-shadow: inset 0 0 0 1px rgba(27, 31, 35, 0.06);
    }
    .day.out { visibility: hidden; }
    .day.today {
      outline: 2px solid #111827;
      outline-offset: 1px;
    }
    .level-1 { background: #9be9a8; }
    .level-2 { background: #40c463; }
    .level-3 { background: #30a14e; }
    .level-4 { background: #216e39; }
    .legend {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 10px;
      font-size: 12px;
      color: var(--muted);
    }
    .legend .day { display: inline-block; }
    .table-wrap {
      overflow-x: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }
    table.data {
      width: 100%;
      border-collapse: collapse;
      min-width: 680px;
      font-size: 13px;
    }
    table.data th,
    table.data td {
      border-bottom: 1px solid var(--line);
      padding: 8px;
      text-align: left;
      vertical-align: top;
    }
    table.data th {
      background: var(--panel-soft);
      color: #334155;
      font-weight: 800;
    }
    table.data tr:last-child td { border-bottom: 0; }
    .models {
      display: flex;
      flex-wrap: wrap;
      gap: 7px;
    }
    .form-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 10px;
      margin: 12px 0;
    }
    .field {
      display: grid;
      gap: 5px;
      font-size: 13px;
      color: var(--muted);
    }
    .field input,
    .field select {
      width: 100%;
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 7px 9px;
      background: #fff;
      color: var(--text);
      font: inherit;
    }
    .hint {
      margin-top: 8px;
      color: var(--amber);
      line-height: 1.6;
      font-size: 13px;
    }
    @media (max-width: 780px) {
      header,
      .split {
        grid-template-columns: 1fr;
        display: block;
      }
      .top-actions {
        justify-content: flex-start;
        margin-top: 12px;
      }
      main { padding: 18px 12px 34px; }
      .section { padding: 13px; }
    }
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>Gemini OpenAI 适配器控制台</h1>
      <p class="muted">以后只记一个入口：双击 <code>__START_ENTRY__</code>。它会启动服务并打开这个控制台。</p>
    </div>
    <div class="top-actions">
      <a class="btn secondary" href="/guide.html" target="_blank" rel="noreferrer">入门引导</a>
      <button id="refreshPageBtn" class="btn secondary" type="button">刷新状态</button>
      <button id="refreshCookieBtn" class="btn" type="button">刷新登录凭据</button>
    </div>
  </header>

  <nav class="nav" aria-label="控制台导航">
    <a href="#guide">入门引导</a>
    <a href="#status">服务状态</a>
    <a href="#terminal">终端输出</a>
    <a href="#cookies">登录凭据</a>
    <a href="#usage">用量热力图</a>
    <a href="#models">模型</a>
    <a href="#test">快速测试</a>
    <a href="#limit">限流探测</a>
    <a href="#prompt-size">提示词体积</a>
    <a href="#config">Cline 配置</a>
  </nav>

  <section id="guide" class="section">
    <div class="split">
      <div>
        <h2>入门引导</h2>
        <p class="muted">日常只双击根目录的 <code>__START_ENTRY__</code>。第一次使用时，先确认浏览器里的 Gemini 能正常发消息，再刷新登录凭据，然后运行快速测试。</p>
        <div class="guide-actions">
          <a class="btn" href="/guide.html" target="_blank" rel="noreferrer">打开完整入门说明</a>
          <a class="btn secondary" href="#config">查看 Cline 配置</a>
          <a class="btn secondary" href="#test">运行快速测试</a>
          <a class="btn secondary" href="#terminal">查看终端输出</a>
        </div>
      </div>
      <table class="quick-table">
        <tr><th>第一步</th><td>双击 <code>__START_ENTRY__</code></td></tr>
        <tr><th>控制台</th><td><code>http://127.0.0.1:8000/</code></td></tr>
        <tr><th>Cline 地址</th><td><code>http://127.0.0.1:8000/v1</code></td></tr>
        <tr><th>模型</th><td><code>gemini-3-flash</code> 或 <code>gemini-3-pro</code></td></tr>
      </table>
    </div>
  </section>

  <section id="status" class="section">
    <h2>服务状态</h2>
    <div id="statusCards" class="grid"></div>
    <div id="statusLine" class="status-line"></div>
  </section>

  <section id="terminal" class="section">
    <div class="split">
      <div>
        <h2>终端输出</h2>
        <p class="muted">这里显示后台服务日志，相当于把启动窗口的关键信息搬到面板里。服务启动失败、Cookie 失效、端口占用、上游错误通常都会出现在这里。</p>
        <div class="status-line">
          <button id="terminalRefreshBtn" class="btn secondary" type="button">刷新终端输出</button>
          <span id="terminalMeta" class="pill">读取中...</span>
        </div>
      </div>
      <pre id="terminalOutput" class="output terminal-output">读取中...</pre>
    </div>
  </section>

  <section id="cookies" class="section">
    <div class="split">
      <div>
        <h2>登录凭据管理</h2>
        <p class="muted">点击按钮会按当前配置从本机 Chrome / Edge 读取 Gemini 登录凭据，清理本项目登录缓存，并热重载 Gemini 客户端。页面只显示凭据是否存在和长度，不显示真实值。</p>
        <div class="status-line">
          <button id="refreshCookieBtn2" class="btn" type="button">刷新登录凭据并重载客户端</button>
          <a class="pill" href="/health" target="_blank" rel="noreferrer">查看 /health JSON</a>
        </div>
      </div>
      <pre id="cookieOutput" class="output">等待操作。</pre>
    </div>
  </section>

  <section id="usage" class="section">
    <h2>用量统计</h2>
    <p class="muted" id="usageNote">读取中...</p>
    <div id="usageCards" class="grid"></div>
    <div class="calendar-wrap">
      <div class="heatmap">
        <div id="monthRow" class="month-row" aria-hidden="true"></div>
        <div class="heatmap-body">
          <div class="weekday-labels" aria-hidden="true">
            <span>日</span><span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span>
          </div>
          <div id="calendar" class="calendar" aria-label="每日用量热力图"></div>
        </div>
      </div>
      <div id="legend" class="legend"></div>
    </div>

    <h3>按模型统计</h3>
    <div class="table-wrap"><table class="data"><thead><tr><th>模型</th><th>请求</th><th>输入 Token</th><th>输出 Token</th><th>总 Token</th><th>美元</th><th>人民币</th></tr></thead><tbody id="modelRows"></tbody></table></div>

    <h3>按电脑统计</h3>
    <div class="table-wrap"><table class="data"><thead><tr><th>电脑</th><th>请求</th><th>输入 Token</th><th>输出 Token</th><th>总 Token</th><th>美元</th><th>人民币</th></tr></thead><tbody id="instanceRows"></tbody></table></div>

    <h3>最近请求</h3>
    <div class="table-wrap"><table class="data"><thead><tr><th>时间</th><th>电脑</th><th>请求模型</th><th>实际模型</th><th>流式</th><th>Tokens</th><th>人民币</th></tr></thead><tbody id="recentRows"></tbody></table></div>

    <h3>数据来源</h3>
    <div class="table-wrap"><table class="data"><thead><tr><th>文件</th><th>存在</th><th>有效记录</th><th>无效行</th></tr></thead><tbody id="sourceRows"></tbody></table></div>
  </section>

  <section id="models" class="section">
    <h2>可用模型</h2>
    <div id="modelList" class="models"></div>
  </section>

  <section id="test" class="section">
    <div class="split">
      <div>
        <h2>快速测试</h2>
        <p class="muted">只在你点击时发送一次最小请求，用来确认 Cline/Continue 使用同一个 OpenAI 兼容接口能够收到回答。</p>
        <div class="status-line">
          <button id="probeBtn" class="btn secondary" type="button">发送一句测试</button>
        </div>
      </div>
      <pre id="probeOutput" class="output">尚未测试。</pre>
    </div>
  </section>

  <section id="limit" class="section">
    <div class="split">
      <div>
        <h2>限流探测</h2>
        <p class="muted">自动挡会逐轮提高并发数量，并缩短轮次间隔，用来观察当前账号、浏览器 Cookie 和模型在本地适配器下的可承受边界。</p>
        <p class="hint">这会真实调用 Gemini。默认“标准自动”会按 1、2、4、6、8 并发逐步测试，最多 21 次请求，遇到 429/鉴权/超时/上游错误会停止。不要频繁重复跑。</p>
        <div class="form-grid">
          <label class="field">测试模式
            <select id="limitMode">
              <option value="auto" selected>自动挡（推荐）</option>
              <option value="manual">手动挡</option>
            </select>
          </label>
          <label class="field">自动强度
            <select id="limitProfile">
              <option value="balanced" selected>标准自动：1,2,4,6,8</option>
              <option value="safe">稳妥自动：1,2,4,4</option>
              <option value="edge">摸边界：1,2,4,6,8,8</option>
            </select>
          </label>
          <label class="field">模型
            <select id="limitModel">
              <option value="gemini-3-flash">gemini-3-flash</option>
              <option value="gemini-3-pro">gemini-3-pro</option>
            </select>
          </label>
          <label class="field manual-field">最大轮数
            <input id="limitRounds" type="number" min="1" max="6" value="4">
          </label>
          <label class="field manual-field">总请求上限
            <input id="limitTotal" type="number" min="1" max="30" value="12">
          </label>
          <label class="field manual-field">最高并发
            <input id="limitParallel" type="number" min="1" max="8" value="4">
          </label>
          <label class="field manual-field">初始轮间隔秒
            <input id="limitDelay" type="number" min="0.25" max="10" step="0.25" value="2">
          </label>
        </div>
        <div class="status-line">
          <span id="autoPlanHint" class="pill">自动计划：1 -> 2 -> 4 -> 6 -> 8，最多 21 次</span>
          <button id="rateLimitBtn" class="btn secondary" type="button">开始自动探测</button>
        </div>
      </div>
      <pre id="rateLimitOutput" class="output">尚未探测。</pre>
    </div>
  </section>

  <section id="prompt-size" class="section">
    <div class="split">
      <div>
        <h2>提示词体积探测</h2>
        <p class="muted">逐档增加单次输入 prompt 的估算 tokens，用来观察“一个任务最多能塞多少上下文”。每档只发 1 次请求，并要求模型只回复 OK。</p>
        <p class="hint">这会真实消耗输入 tokens。建议先跑“标准体积”到 32k；如果全成功，再跑“摸边界体积”到 64k，最后才考虑专家体积到 128k。</p>
        <div class="form-grid">
          <label class="field">模型
            <select id="promptSizeModel">
              <option value="gemini-3-flash">gemini-3-flash</option>
              <option value="gemini-3-pro">gemini-3-pro</option>
            </select>
          </label>
          <label class="field">测试强度
            <select id="promptSizeProfile">
              <option value="balanced" selected>标准体积：1k,4k,8k,16k,32k</option>
              <option value="safe">稳妥体积：1k,4k,8k,16k</option>
              <option value="edge">摸边界体积：1k,8k,16k,32k,64k</option>
              <option value="expert">专家体积：1k,8k,16k,32k,64k,128k</option>
            </select>
          </label>
          <label class="field">最高目标 tokens
            <input id="promptSizeMax" type="number" min="1000" max="128000" step="1000" value="32000">
          </label>
        </div>
        <div class="status-line">
          <span id="promptSizePlanHint" class="pill">计划：1k -> 4k -> 8k -> 16k -> 32k</span>
          <button id="promptSizeBtn" class="btn secondary" type="button">开始体积探测</button>
        </div>
      </div>
      <pre id="promptSizeOutput" class="output">尚未探测。</pre>
    </div>
  </section>

  <section id="config" class="section">
    <h2>Cline / Continue 配置速查</h2>
    <table class="quick-table">
      <tbody>
        <tr><th>接口地址</th><td><code>http://127.0.0.1:8000/v1</code></td></tr>
        <tr><th>接口密钥</th><td><code>dummy</code> 或任意非空字符串</td></tr>
        <tr><th>稳妥模型</th><td><code>gemini-3-flash</code></td></tr>
        <tr><th>更强模型</th><td><code>gemini-3-pro</code>，复杂任务建议拆小一点，遇到 429 先切回 Flash</td></tr>
        <tr><th>健康检查</th><td><code>Invoke-RestMethod http://127.0.0.1:8000/health | ConvertTo-Json -Depth 5</code></td></tr>
      </tbody>
    </table>
  </section>
</main>

<script>
const pricingSourceUrl = "__PRICING_SOURCE_URL__";
const $ = (id) => document.getElementById(id);
const fmt = new Intl.NumberFormat("zh-CN");
const dateTimeFmt = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function money(value, prefix) {
  const num = Number(value || 0);
  return `${prefix}${num.toFixed(6)}`;
}

function localDateTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const parts = Object.fromEntries(
    dateTimeFmt.formatToParts(date).map((part) => [part.type, part.value])
  );
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
}

function displayValue(value) {
  const text = String(value ?? "");
  const map = {
    ok: "正常",
    initialized: "已初始化",
    UNKNOWN: "未知",
    unknown: "未知",
    AVAILABLE: "可用",
    UNAUTHENTICATED: "未认证",
    auto: "自动",
    chrome: "Chrome",
    edge: "Edge",
  };
  return map[text] || text;
}

function setButtonsDisabled(ids, isDisabled) {
  ids.forEach((id) => {
    const el = $(id);
    if (el) el.disabled = isDisabled;
  });
}

function setBusy(isBusy) {
  setButtonsDisabled(["refreshPageBtn", "probeBtn", "rateLimitBtn", "promptSizeBtn"], isBusy);
}

function setCookieBusy(isBusy) {
  ["refreshCookieBtn", "refreshCookieBtn2"].forEach((id) => {
    const el = $(id);
    if (!el) return;
    if (!el.dataset.originalText) el.dataset.originalText = el.textContent;
    el.disabled = isBusy;
    el.textContent = isBusy ? "刷新中..." : el.dataset.originalText;
  });
}

async function fetchJson(url, options = {}, timeoutMs = 15000) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  let response;
  try {
    response = await fetch(url, { ...options, signal: controller.signal });
  } catch (error) {
    if (error?.name === "AbortError") {
      throw new Error(`请求超时：${url}`);
    }
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
  const text = await response.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { raw: text };
  }
  if (!response.ok) {
    const message = data?.error?.message || data?.detail || text || response.statusText;
    throw new Error(`${response.status} ${message}`);
  }
  return data;
}

function metric(label, value, className = "") {
  return `<div class="metric"><div class="label">${escapeHtml(label)}</div><div class="value ${className}">${escapeHtml(value)}</div></div>`;
}

function metricHtml(label, valueHtml, className = "") {
  return `<div class="metric"><div class="label">${escapeHtml(label)}</div><div class="value ${className}">${valueHtml}</div></div>`;
}

async function loadHealth() {
  const data = await fetchJson("/health");
  const status = data.account_status || {};
  const authClass = status.authenticated ? "ok" : "bad";
  $("statusCards").innerHTML = [
    metric("服务", displayValue(data.status || "unknown"), data.status === "ok" ? "ok" : "bad"),
    metric("Gemini 客户端", displayValue(data.gemini_client || "unknown"), data.gemini_client === "initialized" ? "ok" : "bad"),
    metric("账号状态", displayValue(status.name || "UNKNOWN"), authClass),
    metric("已认证", status.authenticated ? "是" : "否", authClass),
    metric("登录凭据写回", data.cookie_writeback?.enabled ? "已启用" : "未启用", data.cookie_writeback?.enabled ? "ok" : "warn"),
    metric("写回间隔", `${data.cookie_writeback?.interval_seconds ?? "-"} 秒`),
    metric("登录浏览器", displayValue(data.cookie_refresh?.browser || "auto")),
    metric("浏览器配置档", data.cookie_refresh?.profile || "默认配置"),
  ].join("");
  $("statusLine").innerHTML = `
    <span class="pill ${status.authenticated ? "ok" : "bad"}">${escapeHtml(status.description || "无状态说明")}</span>
    <span class="pill">页面每 30 秒自动刷新数据</span>
  `;
}

function renderRows(targetId, rows, emptyText) {
  $(targetId).innerHTML = rows.length ? rows.join("") : `<tr><td colspan="7" class="muted">${escapeHtml(emptyText)}</td></tr>`;
}

function summaryRow(name, values) {
  return `<tr>
    <td>${escapeHtml(name)}</td>
    <td>${fmt.format(values.requests || 0)}</td>
    <td>${fmt.format(values.prompt_tokens || 0)}</td>
    <td>${fmt.format(values.completion_tokens || 0)}</td>
    <td>${fmt.format(values.total_tokens || 0)}</td>
    <td>${money(values.cost_usd, "$")}</td>
    <td>${money(values.cost_cny, "¥")}</td>
  </tr>`;
}

function renderHeatmap(daily) {
  const cells = daily?.cells || [];
  const weekCount = Math.max(1, Math.ceil(cells.length / 7));
  const labels = [];
  for (let weekIndex = 0; weekIndex < weekCount; weekIndex += 1) {
    const week = cells.slice(weekIndex * 7, weekIndex * 7 + 7);
    const monthStart = week.find((cell) => cell.in_range && String(cell.date || "").endsWith("-01"));
    labels.push(monthStart ? `${Number(String(monthStart.date).slice(5, 7))}月` : "");
  }
  $("monthRow").style.gridTemplateColumns = `repeat(${weekCount}, 12px)`;
  $("monthRow").innerHTML = labels.map((label) => `<span class="month-label">${escapeHtml(label)}</span>`).join("");
  $("calendar").innerHTML = cells.map((cell) => {
    const title = `${cell.date}: ${cell.requests || 0} 次请求，${fmt.format(cell.total_tokens || 0)} tokens，约 ¥${Number(cell.cost_cny || 0).toFixed(6)}`;
    const classes = ["day", `level-${cell.level || 0}`];
    if (!cell.in_range) classes.push("out");
    if (cell.is_today) classes.push("today");
    return `<span class="${classes.join(" ")}" title="${escapeHtml(title)}"></span>`;
  }).join("");
  $("legend").innerHTML = `
    <span>少</span>
    <span class="day level-0"></span>
    <span class="day level-1"></span>
    <span class="day level-2"></span>
    <span class="day level-3"></span>
    <span class="day level-4"></span>
    <span>多</span>
    <span>最高单日 ${fmt.format(daily?.max_daily_tokens || 0)} tokens</span>
    <span>黑框是今天 ${escapeHtml(daily?.today || "")}</span>
  `;
}

async function loadUsage() {
  const data = await fetchJson("/usage?limit=20");
  const totals = data.totals || {};
  $("usageNote").innerHTML = `本页按 Google Gemini API 官方付费档做本地估算，不是 Gemini 网页版真实账单。价格来源：<a href="${escapeHtml(data.pricing_source || pricingSourceUrl)}" target="_blank" rel="noreferrer">官方价格页</a>`;
  $("usageCards").innerHTML = [
    metric("请求次数", fmt.format(totals.requests || 0)),
    metric("总 Token", fmt.format(totals.total_tokens || 0)),
    metric("估算美元", money(totals.cost_usd, "$")),
    metric("估算人民币", money(totals.cost_cny, "¥")),
    metric("电脑数量", fmt.format(Object.keys(data.by_instance || {}).length)),
    metricHtml("共享目录", `<code>${escapeHtml(data.shared_usage_dir || "未启用")}</code>`, "small"),
  ].join("");
  renderHeatmap(data.daily || {});

  renderRows(
    "modelRows",
    Object.entries(data.by_model || {}).map(([name, values]) => summaryRow(name, values)),
    "暂无模型用量记录。"
  );
  renderRows(
    "instanceRows",
    Object.entries(data.by_instance || {}).map(([name, values]) => summaryRow(name, values)),
    "暂无电脑用量记录。"
  );
  $("recentRows").innerHTML = (data.recent || []).slice().reverse().map((record) => {
    const usage = record.usage || {};
    const cost = record.cost_estimate || {};
    return `<tr>
      <td title="${escapeHtml(record.timestamp || "")}">${escapeHtml(localDateTime(record.timestamp))}</td>
      <td>${escapeHtml(record.instance_name || record.host || record.instance_id || "未知")}</td>
      <td>${escapeHtml(record.requested_model || "")}</td>
      <td>${escapeHtml(record.gemini_model || "")}</td>
      <td>${record.stream ? "是" : "否"}</td>
      <td>${fmt.format(usage.total_tokens || 0)}</td>
      <td>${money(cost.total_cost_cny, "¥")}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="7" class="muted">暂无最近请求。</td></tr>`;
  $("sourceRows").innerHTML = (data.usage_sources || []).map((source) => `<tr>
    <td><code>${escapeHtml(source.path || "")}</code></td>
    <td>${source.exists ? "是" : "否"}</td>
    <td>${fmt.format(source.records || 0)}</td>
    <td>${fmt.format(source.invalid_lines || 0)}</td>
  </tr>`).join("") || `<tr><td colspan="4" class="muted">暂无数据来源。</td></tr>`;
}

async function loadModels() {
  const data = await fetchJson("/v1/models");
  const models = data.data || [];
  $("modelList").innerHTML = models.length
    ? models.map((model) => `<span class="pill">${escapeHtml(model.id)}</span>`).join("")
    : `<span class="muted">暂无模型列表。</span>`;
}

async function loadTerminalLog() {
  try {
    const data = await fetchJson("/admin/server-log?lines=180", {}, 8000);
    $("terminalOutput").textContent = data.text || "暂无终端输出。";
    $("terminalMeta").textContent = data.exists
      ? `日志：${data.path}`
      : `日志文件尚未生成：${data.path}`;
  } catch (error) {
    $("terminalOutput").textContent = String(error);
    $("terminalMeta").textContent = "读取失败";
  }
}

async function refreshAll() {
  await Promise.allSettled([loadHealth(), loadUsage(), loadModels(), loadTerminalLog()]);
}

async function refreshCookies() {
  setCookieBusy(true);
  $("cookieOutput").textContent = `${localDateTime(new Date().toISOString())} 已开始刷新登录凭据。\n\n如果普通读取 Chrome Cookie 失败，系统会自动打开专用 Chrome 登录窗口。\n如果看到 Gemini 登录页，请在那个窗口完成登录。\n如果窗口里看起来已登录但本面板仍在等待，请在那个窗口退出重登，或发送一条 Gemini 消息，让 Google 刷新 Cookie。\n\n这个过程可能需要几分钟；面板其他按钮现在仍然可以使用。`;
  try {
    const data = await fetchJson("/admin/refresh-cookies", { method: "POST" }, 360000);
    $("cookieOutput").textContent = JSON.stringify(data, null, 2);
    await refreshAll();
  } catch (error) {
    $("cookieOutput").textContent = String(error);
  } finally {
    setCookieBusy(false);
  }
}

async function runProbe() {
  setBusy(true);
  $("probeOutput").textContent = "正在发送测试请求...";
  try {
    const data = await fetchJson("/v1/chat/completions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model: "gemini-3-flash",
        stream: false,
        messages: [{ role: "user", content: "用一句话回复：adapter 正常。" }],
      }),
    }, 120000);
    $("probeOutput").textContent = JSON.stringify(data, null, 2);
    await loadUsage();
  } catch (error) {
    $("probeOutput").textContent = String(error);
  } finally {
    setBusy(false);
  }
}

function limitInputNumber(id, fallback) {
  const value = Number($(id)?.value);
  return Number.isFinite(value) ? value : fallback;
}

const autoRateLimitPlans = {
  safe: { label: "稳妥自动", plan: [1, 2, 4, 4], total: 11 },
  balanced: { label: "标准自动", plan: [1, 2, 4, 6, 8], total: 21 },
  edge: { label: "摸边界自动", plan: [1, 2, 4, 6, 8, 8], total: 29 },
};

const promptSizePlans = {
  safe: { label: "稳妥体积", plan: [1000, 4000, 8000, 16000] },
  balanced: { label: "标准体积", plan: [1000, 4000, 8000, 16000, 32000] },
  edge: { label: "摸边界体积", plan: [1000, 8000, 16000, 32000, 64000] },
  expert: { label: "专家体积", plan: [1000, 8000, 16000, 32000, 64000, 128000] },
};

function formatCompactTokens(tokens) {
  const num = Number(tokens || 0);
  if (num >= 1000) return `${Math.round(num / 1000)}k`;
  return String(num);
}

function updateRateLimitMode() {
  const autoMode = ($("limitMode")?.value || "auto") === "auto";
  document.querySelectorAll(".manual-field").forEach((el) => {
    el.style.display = autoMode ? "none" : "grid";
  });
  if ($("limitProfile")) {
    $("limitProfile").disabled = !autoMode;
  }
  const profile = $("limitProfile")?.value || "balanced";
  const plan = autoRateLimitPlans[profile] || autoRateLimitPlans.balanced;
  $("autoPlanHint").textContent = autoMode
    ? `自动计划：${plan.plan.join(" -> ")}，最多 ${plan.total} 次`
    : "手动计划：按你填写的轮数、总请求和最高并发执行";
  $("rateLimitBtn").textContent = autoMode ? "开始自动探测" : "开始手动探测";
}

function updatePromptSizePlan() {
  const profile = $("promptSizeProfile")?.value || "balanced";
  const maxTokens = limitInputNumber("promptSizeMax", 32000);
  const plan = promptSizePlans[profile] || promptSizePlans.balanced;
  const capped = plan.plan.filter((tokens) => tokens <= maxTokens);
  const effective = capped.length ? capped : [Math.min(maxTokens, plan.plan[0])];
  $("promptSizePlanHint").textContent = `计划：${effective.map(formatCompactTokens).join(" -> ")}`;
}

function formatRateLimitReport(data) {
  const summary = data.summary || {};
  const settings = data.settings || {};
  const rounds = data.rounds || [];
  const lastGood = summary.last_fully_successful_round;
  const firstBad = summary.first_failed_round;
  let conclusion = "未触顶：本次计划全部成功，可以下次选择更高自动强度继续摸边界。";
  if (firstBad) {
    conclusion = `触到边界：上一轮可参考 ${lastGood ? `并发 ${lastGood.parallel}` : "无完整成功轮"}，失败轮是并发 ${firstBad.parallel}。`;
  }
  const lines = [
    `结论: ${conclusion}`,
    `停止原因: ${data.stop_reason || "unknown"}`,
    `模型: ${data.model || ""} -> ${data.gemini_model || ""}`,
    `模式: ${settings.auto_mode ? settings.auto_profile_label || settings.auto_profile : "手动挡"}`,
    `计划: ${(settings.parallel_plan || []).join(" -> ")}`,
    `总请求: ${summary.total_requests || 0}`,
    `成功/失败: ${summary.successful_requests || 0}/${summary.failed_requests || 0}`,
    `最高成功并发: ${summary.highest_successful_parallel || 0}`,
    `最快成功 RPS: ${Number(summary.fastest_successful_rps || 0).toFixed(3)}`,
    "",
    "轮次明细:",
  ];
  for (const round of rounds) {
    lines.push(
      `第 ${round.round} 轮 | 并发 ${round.parallel} | 成功 ${round.successes} | 失败 ${round.failures} | 耗时 ${round.elapsed_seconds}s | 约 ${round.estimated_rps} req/s`
    );
    if (round.first_failure) {
      lines.push(
        `  首个错误: ${round.first_failure.category || "unknown"} ` +
        `${round.first_failure.status_code || ""} ${round.first_failure.message || ""}`.trim()
      );
    }
  }
  if (summary.limit_signal) {
    lines.push("", "边界信号:");
    lines.push(JSON.stringify(summary.limit_signal, null, 2));
  }
  lines.push("", "完整 JSON:");
  lines.push(JSON.stringify(data, null, 2));
  return lines.join("\\n");
}

function formatPromptSizeReport(data) {
  const summary = data.summary || {};
  const settings = data.settings || {};
  const steps = data.steps || [];
  let conclusion = `未触顶：本次最大成功约 ${fmt.format(summary.largest_successful_prompt_tokens || 0)} tokens，约 ${fmt.format(summary.largest_successful_prompt_chars || 0)} 字符。`;
  if (summary.limit_signal) {
    conclusion = `触到边界：上一档最大成功约 ${fmt.format(summary.largest_successful_prompt_tokens || 0)} tokens；失败目标是 ${fmt.format(summary.first_failed_target_tokens || 0)} tokens。`;
  }
  const lines = [
    `结论: ${conclusion}`,
    `停止原因: ${data.stop_reason || "unknown"}`,
    `模型: ${data.model || ""} -> ${data.gemini_model || ""}`,
    `模式: ${settings.auto_profile_label || settings.auto_profile || ""}`,
    `计划: ${(settings.target_plan || []).map(formatCompactTokens).join(" -> ")}`,
    `成功/失败档位: ${summary.successful_steps || 0}/${summary.failed_steps || 0}`,
    `最大成功 prompt: ${fmt.format(summary.largest_successful_prompt_tokens || 0)} tokens, ${fmt.format(summary.largest_successful_prompt_chars || 0)} 字符`,
    "",
    "档位明细:",
  ];
  for (const step of steps) {
    lines.push(
      `第 ${step.step} 档 | 目标 ${fmt.format(step.target_prompt_tokens || 0)} tokens | 估算 ${fmt.format(step.estimated_prompt_tokens || 0)} tokens | 字符 ${fmt.format(step.prompt_chars || 0)} | ${step.ok ? "成功" : "失败"} | 耗时 ${step.latency_seconds}s`
    );
    if (!step.ok) {
      lines.push(
        `  错误: ${step.category || "unknown"} ${step.status_code || ""} ${step.message || ""}`.trim()
      );
    }
  }
  if (summary.limit_signal) {
    lines.push("", "边界信号:");
    lines.push(JSON.stringify(summary.limit_signal, null, 2));
  }
  lines.push("", "完整 JSON:");
  lines.push(JSON.stringify(data, null, 2));
  return lines.join("\\n");
}

async function runRateLimitTest() {
  const autoMode = ($("limitMode")?.value || "auto") === "auto";
  const profile = $("limitProfile")?.value || "balanced";
  const selectedPlan = autoRateLimitPlans[profile] || autoRateLimitPlans.balanced;
  const confirmed = window.confirm(
    autoMode
      ? `自动限流探测会真实调用 Gemini，并计入本地用量统计。${selectedPlan.label} 将按 ${selectedPlan.plan.join(" -> ")} 并发逐步测试，最多 ${selectedPlan.total} 次请求，遇到错误会停止。确定开始吗？`
      : "手动限流探测会真实调用 Gemini，并计入本地用量统计，遇到错误会停止。确定开始吗？"
  );
  if (!confirmed) return;

  setBusy(true);
  $("rateLimitOutput").textContent = autoMode
    ? "正在进行自动限流探测，请不要重复点击..."
    : "正在进行手动限流探测，请不要重复点击...";
  try {
    const payload = {
      model: $("limitModel")?.value || "gemini-3-flash",
      auto_mode: autoMode,
      auto_profile: profile,
      max_rounds: limitInputNumber("limitRounds", 4),
      max_total_requests: limitInputNumber("limitTotal", 12),
      max_parallel: limitInputNumber("limitParallel", 4),
      base_delay_seconds: limitInputNumber("limitDelay", 2),
      start_parallel: 1,
      min_delay_seconds: 0.5,
      per_request_timeout_seconds: 45,
      stop_on_first_error: true,
      prompt: "hi",
    };
    const data = await fetchJson("/admin/rate-limit-test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }, 900000);
    $("rateLimitOutput").textContent = formatRateLimitReport(data);
    await loadUsage();
    await loadHealth();
  } catch (error) {
    $("rateLimitOutput").textContent = String(error);
  } finally {
    setBusy(false);
  }
}

async function runPromptSizeTest() {
  const profile = $("promptSizeProfile")?.value || "balanced";
  const plan = promptSizePlans[profile] || promptSizePlans.balanced;
  const maxTokens = limitInputNumber("promptSizeMax", 32000);
  const capped = plan.plan.filter((tokens) => tokens <= maxTokens);
  const effective = capped.length ? capped : [Math.min(maxTokens, plan.plan[0])];
  const confirmed = window.confirm(
    `提示词体积探测会真实调用 Gemini，并消耗大量输入 token。${plan.label} 将测试 ${effective.map(formatCompactTokens).join(" -> ")}。确定开始吗？`
  );
  if (!confirmed) return;

  setBusy(true);
  $("promptSizeOutput").textContent = "正在进行提示词体积探测，请不要重复点击...";
  try {
    const payload = {
      model: $("promptSizeModel")?.value || "gemini-3-flash",
      auto_profile: profile,
      max_target_tokens: maxTokens,
      per_request_timeout_seconds: 180,
      stop_on_first_error: true,
    };
    const data = await fetchJson("/admin/prompt-size-test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }, 1800000);
    $("promptSizeOutput").textContent = formatPromptSizeReport(data);
    await loadUsage();
    await loadHealth();
  } catch (error) {
    $("promptSizeOutput").textContent = String(error);
  } finally {
    setBusy(false);
  }
}

$("refreshPageBtn").addEventListener("click", refreshAll);
$("refreshCookieBtn").addEventListener("click", refreshCookies);
$("refreshCookieBtn2").addEventListener("click", refreshCookies);
$("probeBtn").addEventListener("click", runProbe);
$("terminalRefreshBtn").addEventListener("click", loadTerminalLog);
$("rateLimitBtn").addEventListener("click", runRateLimitTest);
$("limitMode").addEventListener("change", updateRateLimitMode);
$("limitProfile").addEventListener("change", updateRateLimitMode);
$("promptSizeBtn").addEventListener("click", runPromptSizeTest);
$("promptSizeProfile").addEventListener("change", updatePromptSizePlan);
$("promptSizeMax").addEventListener("input", updatePromptSizePlan);

updateRateLimitMode();
updatePromptSizePlan();
refreshAll();
setInterval(refreshAll, 30000);
</script>
</body>
</html>"""
    return (
        body.replace("__PRICING_SOURCE_URL__", html.escape(PRICING_SOURCE_URL, quote=True))
        .replace("__START_ENTRY__", html.escape(start_entry, quote=True))
    )


@app.get("/", response_class=HTMLResponse)
async def adapter_console() -> HTMLResponse:
    return HTMLResponse(_adapter_console_html())


@app.get("/dashboard.html", response_class=HTMLResponse)
async def adapter_console_alias() -> HTMLResponse:
    return HTMLResponse(_adapter_console_html())


@app.get("/guide.html", response_class=HTMLResponse)
async def quickstart_guide() -> HTMLResponse:
    guide_path = ROOT / "docs" / "TEAM_QUICK_START.md"
    if guide_path.exists():
        guide_text = guide_path.read_text(encoding="utf-8", errors="replace")
    else:
        guide_text = (ROOT / "README.md").read_text(encoding="utf-8", errors="replace")
    body = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Gemini Adapter 入门引导</title>
  <style>
    body {{
      margin: 0;
      font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
      color: #172033;
      background: #f5f7fb;
    }}
    main {{
      max-width: 920px;
      margin: 0 auto;
      padding: 24px 16px 48px;
    }}
    a {{ color: #155eef; text-decoration: none; }}
    .top {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      margin-bottom: 16px;
    }}
    .btn {{
      display: inline-flex;
      align-items: center;
      min-height: 36px;
      padding: 8px 12px;
      border: 1px solid #155eef;
      border-radius: 7px;
      background: #155eef;
      color: #fff;
      font-weight: 700;
    }}
    pre {{
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      border: 1px solid #d7dde8;
      border-radius: 8px;
      background: #fff;
      padding: 16px;
      line-height: 1.7;
      font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
      font-size: 14px;
    }}
  </style>
</head>
<body>
<main>
  <div class="top">
    <h1>入门引导</h1>
    <a class="btn" href="/">返回控制台</a>
  </div>
  <pre>{html.escape(guide_text)}</pre>
</main>
</body>
</html>"""
    return HTMLResponse(body)


@app.get("/admin/server-log")
async def server_log_tail(request: Request, lines: int = 160) -> JSONResponse:
    try:
        _require_local_request(request)
        safe_lines = min(max(int(lines), 20), 500)
        path = _server_log_path()
        text = _tail_text_file(path, lines=safe_lines)
        return JSONResponse(
            {
                "path": str(path),
                "exists": path.exists(),
                "lines": safe_lines,
                "text": text,
            }
        )
    except ValueError as exc:
        return _openai_error_response(
            403,
            str(exc),
            error_type="forbidden",
            code="local_only",
        )
    except Exception as exc:
        logger.error("Failed to read server log.", exc_info=True)
        return _openai_error_response(
            500,
            f"Failed to read server log: {exc}",
            error_type="server_error",
            code="server_log_error",
        )


@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    client = _get_client(request)
    return {
        "status": "ok",
        "gemini_client": "initialized" if client else "missing",
        "account_status": _account_status_info(client),
        "cookie_writeback": {
            "enabled": _cookie_writeback_path() is not None,
            "interval_seconds": _cookie_writeback_interval_seconds(),
        },
        "cookie_refresh": _cookie_refresh_config(),
        "upstream_proxy": _proxy_for_log(getattr(client, "_adapter_proxy", None)),
        "upstream_proxy_config": _gemini_proxy_config(),
        "gpt4free": _g4f_status_info(),
        "prompt_budget": {
            "max_prompt_tokens": _max_prompt_tokens(),
        },
    }


@app.post("/admin/refresh-cookies")
async def refresh_cookies_from_browser_endpoint(request: Request) -> JSONResponse:
    try:
        result = await _refresh_cookies_and_reload_client(request)
        return JSONResponse(result)
    except ValueError as exc:
        logger.error("Invalid cookie refresh request: %s", exc)
        return _openai_error_response(
            403,
            str(exc),
            error_type="forbidden",
            code="local_only",
        )
    except AuthError as exc:
        logger.error("Cookie refresh produced unauthenticated Gemini client.", exc_info=True)
        return _openai_error_response(
            500,
            f"Cookie refresh failed authentication: {exc}",
            error_type="authentication_error",
            code="gemini_auth_failed",
        )
    except Exception as exc:
        logger.error("Cookie refresh failed.", exc_info=True)
        return _openai_error_response(
            500,
            f"Cookie refresh failed: {exc}",
            error_type=exc.__class__.__name__,
            code="cookie_refresh_failed",
        )


@app.post("/admin/rate-limit-test")
async def rate_limit_test_endpoint(
    payload: RateLimitTestRequest,
    request: Request,
) -> JSONResponse:
    try:
        _require_local_request(request)
        client = _get_client(request)
        result = await _run_rate_limit_probe(client, payload)
        return JSONResponse(result)
    except ValueError as exc:
        logger.error("Invalid rate-limit probe request: %s", exc)
        return _openai_error_response(
            403,
            str(exc),
            error_type="forbidden",
            code="local_only",
        )
    except AuthError as exc:
        logger.error("Rate-limit probe blocked by Gemini auth failure.", exc_info=True)
        return _openai_error_response(
            500,
            f"Gemini authentication failed: {exc}",
            error_type="authentication_error",
            code="gemini_auth_failed",
        )
    except Exception as exc:
        logger.error("Rate-limit probe failed.", exc_info=True)
        return _openai_error_response(
            500,
            f"Rate-limit probe failed: {exc}",
            error_type=exc.__class__.__name__,
            code="rate_limit_probe_failed",
        )


@app.post("/admin/prompt-size-test")
async def prompt_size_test_endpoint(
    payload: PromptSizeTestRequest,
    request: Request,
) -> JSONResponse:
    try:
        _require_local_request(request)
        client = _get_client(request)
        result = await _run_prompt_size_probe(client, payload)
        return JSONResponse(result)
    except ValueError as exc:
        logger.error("Invalid prompt-size probe request: %s", exc)
        return _openai_error_response(
            403,
            str(exc),
            error_type="forbidden",
            code="local_only",
        )
    except AuthError as exc:
        logger.error("Prompt-size probe blocked by Gemini auth failure.", exc_info=True)
        return _openai_error_response(
            500,
            f"Gemini authentication failed: {exc}",
            error_type="authentication_error",
            code="gemini_auth_failed",
        )
    except Exception as exc:
        logger.error("Prompt-size probe failed.", exc_info=True)
        return _openai_error_response(
            500,
            f"Prompt-size probe failed: {exc}",
            error_type=exc.__class__.__name__,
            code="prompt_size_probe_failed",
        )


@app.get("/cookie.html", response_class=HTMLResponse)
async def cookie_dashboard(request: Request) -> HTMLResponse:
    return RedirectResponse(url="/#cookies", status_code=307)
    client = _get_client(request)
    account_status = _account_status_info(client)
    cookie_path = _cookie_writeback_path()
    cache_path = os.getenv("GEMINI_COOKIE_PATH", "").strip()
    body = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Gemini 登录凭据管理</title>
  <style>
    :root {{
      color-scheme: light;
      --ok: #16803c;
      --bad: #b42318;
      --line: #d7dde5;
      --soft: #f6f8fb;
      --text: #172033;
      --muted: #5d6b82;
    }}
    body {{
      margin: 0;
      font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
      color: var(--text);
      background: #ffffff;
    }}
    main {{
      max-width: 980px;
      margin: 0 auto;
      padding: 28px 18px 48px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 26px;
    }}
    h2 {{
      margin-top: 26px;
      font-size: 18px;
    }}
    .muted {{
      color: var(--muted);
      line-height: 1.7;
    }}
    .panel {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      background: var(--soft);
      margin-top: 16px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 12px;
    }}
    .metric {{
      background: #ffffff;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
    }}
    .label {{
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 6px;
    }}
    .value {{
      font-size: 16px;
      font-weight: 700;
      word-break: break-word;
    }}
    .ok {{ color: var(--ok); }}
    .bad {{ color: var(--bad); }}
    button {{
      appearance: none;
      border: 1px solid #0f5bd7;
      background: #0f5bd7;
      color: #fff;
      border-radius: 7px;
      font-size: 15px;
      font-weight: 700;
      padding: 10px 14px;
      cursor: pointer;
    }}
    button:disabled {{
      opacity: 0.65;
      cursor: wait;
    }}
    pre {{
      background: #101828;
      color: #eef4ff;
      padding: 14px;
      border-radius: 8px;
      overflow: auto;
      min-height: 80px;
      white-space: pre-wrap;
    }}
    code {{
      font-family: Consolas, "Courier New", monospace;
    }}
    a {{
      color: #0f5bd7;
      text-decoration: none;
    }}
  </style>
</head>
<body>
<main>
  <h1>Gemini 登录凭据管理</h1>
  <p class="muted">从本机可读取的 Chrome / Edge 登录凭据数据库刷新 Gemini 登录态，并在不中断 Web 服务的情况下重载 Gemini 客户端。页面不会显示真实凭据值。</p>

  <section class="panel">
    <div class="grid">
      <div class="metric">
        <div class="label">账号状态</div>
        <div id="accountName" class="value {'ok' if account_status['authenticated'] else 'bad'}">{html.escape(str(account_status['name']))}</div>
      </div>
      <div class="metric">
        <div class="label">已认证</div>
        <div id="authenticated" class="value {'ok' if account_status['authenticated'] else 'bad'}">{str(account_status['authenticated']).lower()}</div>
      </div>
      <div class="metric">
        <div class="label">凭据文件</div>
        <div class="value"><code>{html.escape(str(cookie_path or '未配置'))}</code></div>
      </div>
      <div class="metric">
        <div class="label">缓存目录</div>
        <div class="value"><code>{html.escape(cache_path or '未配置')}</code></div>
      </div>
    </div>
    <p>
      <button id="refreshBtn" type="button">刷新登录凭据并重载客户端</button>
      <a href="/usage.html" style="margin-left: 12px;">查看用量</a>
    </p>
  </section>

  <h2>执行结果</h2>
  <pre id="output">等待操作。</pre>
</main>
<script>
const output = document.getElementById("output");
const button = document.getElementById("refreshBtn");
const accountName = document.getElementById("accountName");
const authenticated = document.getElementById("authenticated");

function show(data) {{
  output.textContent = JSON.stringify(data, null, 2);
}}

async function loadHealth() {{
  const response = await fetch("/health");
  const data = await response.json();
  const status = data.account_status || {{}};
  accountName.textContent = status.name || "UNKNOWN";
  authenticated.textContent = String(Boolean(status.authenticated));
  accountName.className = "value " + (status.authenticated ? "ok" : "bad");
  authenticated.className = "value " + (status.authenticated ? "ok" : "bad");
}}

button.addEventListener("click", async () => {{
  button.disabled = true;
  output.textContent = "正在刷新登录凭据、清理缓存并重载 Gemini 客户端...";
  try {{
    const response = await fetch("/admin/refresh-cookies", {{ method: "POST" }});
    const data = await response.json();
    show(data);
    await loadHealth();
  }} catch (error) {{
    output.textContent = String(error);
  }} finally {{
    button.disabled = false;
  }}
}});

loadHealth().catch(error => {{
  output.textContent = String(error);
}});
</script>
</body>
</html>"""
    return HTMLResponse(body)


@app.get("/models")
@app.get("/v1/models")
async def list_models(request: Request) -> dict[str, Any]:
    client = _get_client(request)
    models = client.list_models() or []
    data = []
    for model in models:
        model_id = getattr(model, "model_name", "") or getattr(model, "model_id", "")
        if not model_id:
            continue
        data.append(
            {
                "id": model_id,
                "object": "model",
                "created": 0,
                "owned_by": "google-gemini-web",
            }
        )

    if not data:
        data = [
            {
                "id": model.model_name,
                "object": "model",
                "created": 0,
                "owned_by": "google-gemini-web",
            }
            for model in Model
        ]

    if _g4f_base_url() is not None:
        g4f_model_ids: list[str] = []
        for model_id in _g4f_models():
            if model_id not in g4f_model_ids:
                g4f_model_ids.append(model_id)
            stripped_model, explicit = _strip_g4f_model_prefix(model_id)
            if (
                explicit
                and _env_bool("OPENAI_ADAPTER_G4F_EXPOSE_UNPREFIXED", True)
                and stripped_model not in g4f_model_ids
            ):
                g4f_model_ids.append(stripped_model)

        existing_ids = {item["id"] for item in data}
        for model_id in g4f_model_ids:
            if model_id in existing_ids:
                continue
            data.append(
                {
                    "id": model_id,
                    "object": "model",
                    "created": 0,
                    "owned_by": "gpt4free",
                }
            )

    return {"object": "list", "data": data}


@app.get("/usage")
async def usage_summary(limit: int = 20) -> dict[str, Any]:
    path = _get_usage_log_path()
    shared_dir = _get_shared_usage_dir()
    records, usage_sources = _collect_usage_records()

    totals = {
        "requests": len(records),
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "cost_cny": 0.0,
    }
    by_model: dict[str, dict[str, Any]] = {}
    by_instance: dict[str, dict[str, Any]] = {}

    for record in records:
        usage = record.get("usage") or {}
        cost = record.get("cost_estimate") or {}
        model = record.get("requested_model") or "unknown"
        instance_id = _record_instance_id(record)
        instance_label = _record_instance_label(record)
        model_totals = by_model.setdefault(
            model,
            {
                "requests": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "cost_cny": 0.0,
            },
        )
        instance_totals = by_instance.setdefault(
            instance_label,
            {
                "instance_id": instance_id,
                "display_name": instance_label,
                "requests": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "cost_cny": 0.0,
            },
        )
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or 0)
        cost_usd = float(cost.get("total_cost_usd") or 0)
        cost_cny = float(cost.get("total_cost_cny") or 0)

        totals["prompt_tokens"] += prompt_tokens
        totals["completion_tokens"] += completion_tokens
        totals["total_tokens"] += total_tokens
        totals["cost_usd"] += cost_usd
        totals["cost_cny"] += cost_cny

        model_totals["requests"] += 1
        model_totals["prompt_tokens"] += prompt_tokens
        model_totals["completion_tokens"] += completion_tokens
        model_totals["total_tokens"] += total_tokens
        model_totals["cost_usd"] += cost_usd
        model_totals["cost_cny"] += cost_cny

        instance_totals["requests"] += 1
        instance_totals["prompt_tokens"] += prompt_tokens
        instance_totals["completion_tokens"] += completion_tokens
        instance_totals["total_tokens"] += total_tokens
        instance_totals["cost_usd"] += cost_usd
        instance_totals["cost_cny"] += cost_cny

    totals["cost_usd"] = round(float(totals["cost_usd"]), 8)
    totals["cost_cny"] = round(float(totals["cost_cny"]), 8)
    for model_totals in by_model.values():
        model_totals["cost_usd"] = round(float(model_totals["cost_usd"]), 8)
        model_totals["cost_cny"] = round(float(model_totals["cost_cny"]), 8)
    for instance_totals in by_instance.values():
        instance_totals["cost_usd"] = round(float(instance_totals["cost_usd"]), 8)
        instance_totals["cost_cny"] = round(float(instance_totals["cost_cny"]), 8)

    return {
        "estimated": True,
        "note": "Gemini API paid-tier equivalent estimate; not actual Gemini web billing.",
        "pricing_source": PRICING_SOURCE_URL,
        "instance_id": _usage_instance_id(),
        "instance_name": _usage_instance_name(),
        "usage_log_path": str(path),
        "shared_usage_dir": str(shared_dir) if shared_dir is not None else None,
        "shared_usage_log_path": str(_get_shared_usage_log_path() or ""),
        "usage_sources": usage_sources,
        "usd_to_cny": _env_float("OPENAI_ADAPTER_USD_TO_CNY", 7.25),
        "totals": totals,
        "by_model": by_model,
        "by_instance": by_instance,
        "daily": _build_daily_usage(records),
        "recent": records[-max(0, limit) :],
    }


@app.get("/usage.html", response_class=HTMLResponse)
async def usage_dashboard(limit: int = 20) -> HTMLResponse:
    return RedirectResponse(url="/#usage", status_code=307)
    data = await usage_summary(limit=limit)
    totals = data["totals"]
    by_model = data["by_model"]
    by_instance = data["by_instance"]
    usage_sources = data["usage_sources"]
    daily = data["daily"]
    recent = data["recent"]
    cells = daily["cells"]
    machine_count = len(by_instance)
    week_count = max(1, (len(cells) + 6) // 7)
    month_labels: list[str] = []
    for week_index in range(week_count):
        week_cells = cells[week_index * 7 : week_index * 7 + 7]
        month_start = next(
            (
                cell
                for cell in week_cells
                if cell["in_range"] and str(cell["date"]).endswith("-01")
            ),
            None,
        )
        if month_start is None:
            month_labels.append("")
            continue
        month_key = str(month_start["date"])[:7]
        month_labels.append(f"{int(month_key[5:7])}月")

    month_cells = "\n".join(
        f'<span class="month-label">{html.escape(label)}</span>'
        if label
        else '<span class="month-label"></span>'
        for label in month_labels
    )

    heat_cells = "\n".join(
        (
            f'<span class="day level-{cell["level"]} '
            f'{"out" if not cell["in_range"] else ""} '
            f'{"today" if cell["is_today"] else ""}" '
            f'title="{html.escape(cell["date"])}: '
            f'{cell["requests"]} 次请求, '
            f'{cell["total_tokens"]:,} tokens, '
            f'约 ¥{cell["cost_cny"]:.6f}"></span>'
        )
        for cell in cells
    )

    model_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(model)}</td>"
        f"<td>{values['requests']}</td>"
        f"<td>{values['prompt_tokens']:,}</td>"
        f"<td>{values['completion_tokens']:,}</td>"
        f"<td>{values['total_tokens']:,}</td>"
        f"<td>${values['cost_usd']:.6f}</td>"
        f"<td>&yen;{values['cost_cny']:.6f}</td>"
        "</tr>"
        for model, values in by_model.items()
    ) or '<tr><td colspan="7" class="muted">暂无用量记录。</td></tr>'

    instance_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(instance_id)}</td>"
        f"<td>{values['requests']}</td>"
        f"<td>{values['prompt_tokens']:,}</td>"
        f"<td>{values['completion_tokens']:,}</td>"
        f"<td>{values['total_tokens']:,}</td>"
        f"<td>${values['cost_usd']:.6f}</td>"
        f"<td>&yen;{values['cost_cny']:.6f}</td>"
        "</tr>"
        for instance_id, values in by_instance.items()
    ) or '<tr><td colspan="7" class="muted">暂无电脑用量记录。</td></tr>'

    source_rows = "\n".join(
        "<tr>"
        f"<td><code>{html.escape(str(source.get('path', '')))}</code></td>"
        f"<td>{'是' if source.get('exists') else '否'}</td>"
        f"<td>{int(source.get('records') or 0):,}</td>"
        f"<td>{int(source.get('invalid_lines') or 0):,}</td>"
        "</tr>"
        for source in usage_sources
    ) or '<tr><td colspan="4" class="muted">暂无数据来源。</td></tr>'

    recent_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(str(record.get('timestamp', '')))}</td>"
        f"<td>{html.escape(str(record.get('instance_id', 'unknown')))}</td>"
        f"<td>{html.escape(str(record.get('requested_model', '')))}</td>"
        f"<td>{html.escape(str(record.get('gemini_model', '')))}</td>"
        f"<td>{'是' if record.get('stream') else '否'}</td>"
        f"<td>{int((record.get('usage') or {}).get('total_tokens') or 0):,}</td>"
        f"<td>&yen;{float((record.get('cost_estimate') or {}).get('total_cost_cny') or 0):.6f}</td>"
        "</tr>"
        for record in reversed(recent)
    ) or '<tr><td colspan="7" class="muted">暂无最近请求。</td></tr>'

    body = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="30">
  <title>Gemini 适配器用量</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin: 24px;
      line-height: 1.45;
    }}
    h1 {{ margin: 0 0 4px; font-size: 24px; }}
    h2 {{ margin-top: 26px; font-size: 18px; }}
    .muted {{ color: #6b7280; }}
    .cards {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin: 18px 0;
    }}
    .card {{
      border: 1px solid #d1d5db;
      border-radius: 8px;
      padding: 14px;
      background: rgba(127, 127, 127, 0.06);
    }}
    .label {{ color: #6b7280; font-size: 13px; }}
    .value {{ font-size: 24px; font-weight: 700; margin-top: 4px; }}
    .calendar-wrap {{
      border: 1px solid #d1d5db;
      border-radius: 8px;
      padding: 14px;
      margin-top: 12px;
      overflow-x: auto;
      background: rgba(127, 127, 127, 0.04);
    }}
    .heatmap {{
      width: max-content;
      min-width: 100%;
    }}
    .month-row {{
      display: grid;
      grid-template-columns: repeat({week_count}, 12px);
      gap: 3px;
      margin-left: 24px;
      margin-bottom: 5px;
      width: max-content;
      min-height: 16px;
      font-size: 11px;
      color: #6b7280;
      white-space: nowrap;
    }}
    .month-label {{
      min-width: 32px;
      overflow: visible;
    }}
    .heatmap-body {{
      display: flex;
      align-items: flex-start;
      gap: 6px;
    }}
    .weekday-labels {{
      display: grid;
      grid-template-rows: repeat(7, 12px);
      gap: 3px;
      width: 18px;
      font-size: 11px;
      line-height: 12px;
      color: #6b7280;
      text-align: right;
    }}
    .calendar {{
      display: grid;
      grid-auto-flow: column;
      grid-template-rows: repeat(7, 12px);
      gap: 3px;
      width: max-content;
      min-height: 102px;
    }}
    .day {{
      width: 12px;
      height: 12px;
      border-radius: 3px;
      background: #ebedf0;
      box-shadow: inset 0 0 0 1px rgba(27, 31, 35, 0.06);
    }}
    .day.out {{ visibility: hidden; }}
    .day.today {{
      outline: 2px solid #111827;
      outline-offset: 1px;
    }}
    .level-1 {{ background: #9be9a8; }}
    .level-2 {{ background: #40c463; }}
    .level-3 {{ background: #30a14e; }}
    .level-4 {{ background: #216e39; }}
    .legend {{
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 10px;
      font-size: 12px;
      color: #6b7280;
    }}
    .legend .day {{ display: inline-block; }}
    table {{
      border-collapse: collapse;
      width: 100%;
      margin: 14px 0 24px;
      font-size: 14px;
    }}
    th, td {{
      border-bottom: 1px solid #d1d5db;
      padding: 8px;
      text-align: left;
      vertical-align: top;
    }}
    th {{ background: rgba(127, 127, 127, 0.10); }}
    code {{ font-family: Consolas, monospace; }}
  </style>
</head>
<body>
  <h1>Gemini 适配器用量</h1>
  <div class="muted">这是按 Gemini API 官方付费档做的本地估算，不是 Gemini 网页版真实账单。页面每 30 秒自动刷新。</div>

  <section class="cards">
    <div class="card"><div class="label">请求次数</div><div class="value">{totals['requests']}</div></div>
    <div class="card"><div class="label">总 Tokens</div><div class="value">{totals['total_tokens']:,}</div></div>
    <div class="card"><div class="label">估算美元</div><div class="value">${totals['cost_usd']:.6f}</div></div>
    <div class="card"><div class="label">估算人民币</div><div class="value">&yen;{totals['cost_cny']:.6f}</div></div>
    <div class="card"><div class="label">电脑数量</div><div class="value">{machine_count}</div></div>
  </section>

  <h2>每日用量热力图</h2>
  <div class="muted">{daily['year']} 年（{html.escape(daily['start_date'])} 至 {html.escape(daily['end_date'])}），按自然年显示；每列是一周，最上面是周日，最下面是周六。黑框表示今天 {html.escape(daily['today'])}。</div>
  <div class="calendar-wrap">
    <div class="heatmap">
      <div class="month-row" aria-hidden="true">{month_cells}</div>
      <div class="heatmap-body">
        <div class="weekday-labels" aria-hidden="true">
          <span>日</span><span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span>
        </div>
        <div class="calendar" aria-label="每日用量热力图">{heat_cells}</div>
      </div>
    </div>
    <div class="legend">
      <span>少</span>
      <span class="day level-0"></span>
      <span class="day level-1"></span>
      <span class="day level-2"></span>
      <span class="day level-3"></span>
      <span class="day level-4"></span>
      <span>多</span>
      <span>最高单日 {daily['max_daily_tokens']:,} tokens</span>
    </div>
  </div>

  <h2>按模型统计</h2>
  <table>
    <thead><tr><th>模型</th><th>请求次数</th><th>输入 Tokens</th><th>输出 Tokens</th><th>总 Tokens</th><th>美元</th><th>人民币</th></tr></thead>
    <tbody>{model_rows}</tbody>
  </table>

  <h2>按电脑统计</h2>
  <table>
    <thead><tr><th>电脑</th><th>请求次数</th><th>输入 Tokens</th><th>输出 Tokens</th><th>总 Tokens</th><th>美元</th><th>人民币</th></tr></thead>
    <tbody>{instance_rows}</tbody>
  </table>

  <h2>最近请求</h2>
  <table>
    <thead><tr><th>时间</th><th>电脑</th><th>请求模型</th><th>实际 Gemini 模型</th><th>流式</th><th>Tokens</th><th>人民币</th></tr></thead>
    <tbody>{recent_rows}</tbody>
  </table>

  <h2>数据来源</h2>
  <table>
    <thead><tr><th>文件</th><th>存在</th><th>有效记录</th><th>无效行</th></tr></thead>
    <tbody>{source_rows}</tbody>
  </table>

  <p class="muted">用量日志：<code>{html.escape(data['usage_log_path'])}</code></p>
  <p class="muted">共享目录：<code>{html.escape(str(data.get('shared_usage_dir') or '未启用'))}</code></p>
  <p class="muted">价格来源：<a href="{PRICING_SOURCE_URL}">{PRICING_SOURCE_URL}</a></p>
</body>
</html>"""
    return HTMLResponse(body)


@app.post("/chat/completions", response_model=None)
@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(
    payload: ChatCompletionRequest,
    request: Request,
) -> JSONResponse | EventSourceResponse:
    completion_id = _completion_id()
    created = int(time.time())

    try:
        if _should_route_to_g4f(payload.model):
            logger.info(
                "Routing chat completion to gpt4free: id=%s model=%s stream=%s",
                completion_id,
                payload.model,
                payload.stream,
            )
            return await _handle_g4f_chat_completion(payload, completion_id, created)

        client = _get_client(request)
        _ensure_client_authenticated(client)
        prompt, prompt_tokens = _build_prompt_with_budget(payload.messages)
        _write_debug_prompt(prompt)
        gemini_model = _select_gemini_model(payload.model)

        logger.info(
            "Received chat completion: id=%s model=%s gemini_model=%s "
            "stream=%s messages=%s prompt_chars=%s prompt_tokens=%s "
            "max_prompt_tokens=%s",
            completion_id,
            payload.model,
            gemini_model,
            payload.stream,
            len(payload.messages),
            len(prompt),
            prompt_tokens,
            _max_prompt_tokens(),
        )

        if payload.temperature is not None:
            logger.info("temperature=%s is accepted but ignored.", payload.temperature)
        if payload.max_tokens is not None:
            logger.info("max_tokens=%s is accepted but ignored.", payload.max_tokens)

        if payload.stream:
            buffered_deltas: list[str] = []
            if _stream_eager_enabled():
                upstream = client.generate_content_stream(
                    prompt,
                    model=gemini_model,
                    current_retry=_upstream_retry_count(),
                )
                logger.info(
                    "Gemini streaming connection opened eagerly: id=%s",
                    completion_id,
                )
            else:
                upstream, buffered_deltas = await _prime_gemini_stream(
                    client,
                    prompt,
                    gemini_model,
                )
                logger.info(
                    "Gemini streaming connection opened after priming: "
                    "id=%s buffered_chunks=%s",
                    completion_id,
                    len(buffered_deltas),
                )
            return EventSourceResponse(
                _openai_sse_events(
                    client,
                    upstream,
                    buffered_deltas,
                    completion_id,
                    payload.model,
                    gemini_model,
                    prompt,
                    created,
                ),
                media_type="text/event-stream",
                ping=_stream_ping_seconds(),
            )

        logger.info("Calling Gemini non-streaming generation: id=%s", completion_id)
        output = await client.generate_content(
            prompt,
            model=gemini_model,
            current_retry=_upstream_retry_count(),
        )
        text = getattr(output, "text", "") or ""
        usage, cost_estimate = _build_usage(gemini_model, prompt, text)
        _record_usage(
            completion_id,
            payload.model,
            gemini_model,
            False,
            usage,
            cost_estimate,
        )
        await _write_client_cookies_back(client, "non-stream-response")

        logger.info(
            "Completed non-streaming response: id=%s response_chars=%s",
            completion_id,
            len(text),
        )
        return JSONResponse(
            {
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": payload.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": text,
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage,
                "cost_estimate": cost_estimate,
            }
        )

    except ValueError as exc:
        logger.error("Invalid OpenAI-compatible request: %s", exc)
        return _openai_error_response(
            400,
            str(exc),
            error_type="invalid_request_error",
            code="invalid_request",
        )
    except G4FUpstreamError as exc:
        logger.error("gpt4free upstream error.", exc_info=True)
        return _openai_error_response(
            502,
            f"gpt4free upstream error: {exc}",
            error_type="upstream_error",
            code="gpt4free_upstream_error",
        )
    except AuthError as exc:
        logger.error("Gemini Cookie/authentication failure.", exc_info=True)
        return _openai_error_response(
            500,
            f"Gemini authentication failed: {exc}",
            error_type="authentication_error",
            code="gemini_auth_failed",
        )
    except (APIError, GeminiError, GeminiTimeoutError) as exc:
        logger.error("Gemini upstream error.", exc_info=True)
        return _openai_error_response(
            500,
            f"Gemini upstream error: {exc}",
            error_type=exc.__class__.__name__,
            code="gemini_upstream_error",
        )
    except Exception as exc:
        logger.error("Unexpected adapter error.", exc_info=True)
        return _openai_error_response(
            500,
            f"Unexpected adapter error: {exc}",
            error_type=exc.__class__.__name__,
            code="unexpected_adapter_error",
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "openai_adapter_server:app",
        host=os.getenv("OPENAI_ADAPTER_HOST", "127.0.0.1"),
        port=int(os.getenv("OPENAI_ADAPTER_PORT", "8000")),
        reload=_env_bool("OPENAI_ADAPTER_RELOAD", False),
    )
