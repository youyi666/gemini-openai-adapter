#!/usr/bin/env python3
"""OpenAI-compatible FastAPI adapter for HanaokaYuzu/Gemini-API.

This file is intentionally standalone. It imports the upstream Gemini client and
does not modify the cloned package internals.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import socket
import sys
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncGenerator

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, JSONResponse
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
from gemini_webapi.constants import Model  # noqa: E402
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


KNOWN_GEMINI_MODEL_NAMES = {model.model_name for model in Model}

CLINE_COMPACT_SYSTEM_PROMPT = """You are Cline, a concise software engineering assistant running inside an IDE.

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
    client = GeminiClient(
        secure_1psid=secure_1psid,
        secure_1psidts=secure_1psidts,
        proxy=os.getenv("GEMINI_PROXY"),
        verify=verify_ssl,
    )
    if cookie_values:
        client.cookies = cookie_values

    timeout = _env_float("GEMINI_REQUEST_TIMEOUT", 300)
    auto_refresh = _env_bool("GEMINI_AUTO_REFRESH", True)
    refresh_interval = _env_float("GEMINI_REFRESH_INTERVAL", 600)
    watchdog_timeout = _env_float("GEMINI_WATCHDOG_TIMEOUT", 120)
    verbose = _env_bool("GEMINI_VERBOSE", False)

    logger.info(
        "Initializing Gemini client: timeout=%s auto_refresh=%s proxy=%s verify_ssl=%s",
        timeout,
        auto_refresh,
        "set" if os.getenv("GEMINI_PROXY") else "not set",
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
            "Gemini authentication failed. Refresh __Secure-1PSID / "
            "__Secure-1PSIDTS or update GEMINI_COOKIES_JSON.",
            exc_info=True,
        )
        raise
    except Exception:
        logger.error("Gemini client initialization failed.", exc_info=True)
        raise

    logger.info("Gemini client initialized successfully.")
    return client


@asynccontextmanager
async def lifespan(app_: FastAPI):
    _backfill_shared_usage_log()
    client = await _create_gemini_client()
    app_.state.gemini_client = client
    try:
        yield
    finally:
        logger.info("Closing Gemini client.")
        await client.close()


app = FastAPI(title="Gemini WebAPI OpenAI Adapter", lifespan=lifespan)


def _get_client(request: Request) -> GeminiClient:
    client = getattr(request.app.state, "gemini_client", None)
    if client is None:
        raise RuntimeError("Gemini client is not initialized.")
    return client


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
        return raw_messages, False

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

    return prepared, True


def _messages_to_prompt(messages: list[ChatMessage]) -> str:
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


def _get_usage_log_path() -> Path:
    return Path(
        os.getenv(
            "OPENAI_ADAPTER_USAGE_LOG_PATH",
            str(ROOT / "adapter_usage.jsonl"),
        )
    )


def _usage_instance_id() -> str:
    raw = os.getenv("OPENAI_ADAPTER_INSTANCE_ID") or socket.gethostname() or "unknown"
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw.strip()).strip(".-")
    return cleaned or "unknown"


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
) -> None:
    record = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "id": completion_id,
        "instance_id": _usage_instance_id(),
        "host": socket.gethostname(),
        "requested_model": requested_model,
        "gemini_model": gemini_model,
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
        for line in path.read_text(encoding="utf-8").splitlines():
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
                record["_source_file"] = str(path)
                records.append(record)
    except OSError:
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

    upstream = client.generate_content_stream(prompt, model=gemini_model)
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
    except (AuthError, APIError, GeminiError, GeminiTimeoutError) as exc:
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


@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    client = _get_client(request)
    return {
        "status": "ok",
        "gemini_client": "initialized" if client else "missing",
    }


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
        instance_id = str(record.get("instance_id") or record.get("host") or "unknown")
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
            instance_id,
            {
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
        "usage_log_path": str(path),
        "shared_usage_dir": str(shared_dir) if shared_dir is not None else None,
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


@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(
    payload: ChatCompletionRequest,
    request: Request,
) -> JSONResponse | EventSourceResponse:
    completion_id = _completion_id()
    created = int(time.time())

    try:
        client = _get_client(request)
        prompt = _messages_to_prompt(payload.messages)
        _write_debug_prompt(prompt)
        gemini_model = _select_gemini_model(payload.model)

        logger.info(
            "Received chat completion: id=%s model=%s gemini_model=%s "
            "stream=%s messages=%s prompt_chars=%s",
            completion_id,
            payload.model,
            gemini_model,
            payload.stream,
            len(payload.messages),
            len(prompt),
        )

        if payload.temperature is not None:
            logger.info("temperature=%s is accepted but ignored.", payload.temperature)
        if payload.max_tokens is not None:
            logger.info("max_tokens=%s is accepted but ignored.", payload.max_tokens)

        if payload.stream:
            upstream, buffered_deltas = await _prime_gemini_stream(
                client,
                prompt,
                gemini_model,
            )
            logger.info(
                "Gemini streaming connection opened: id=%s buffered_chunks=%s",
                completion_id,
                len(buffered_deltas),
            )
            return EventSourceResponse(
                _openai_sse_events(
                    upstream,
                    buffered_deltas,
                    completion_id,
                    payload.model,
                    gemini_model,
                    prompt,
                    created,
                ),
                media_type="text/event-stream",
            )

        logger.info("Calling Gemini non-streaming generation: id=%s", completion_id)
        output = await client.generate_content(prompt, model=gemini_model)
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
