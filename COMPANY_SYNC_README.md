# Gemini OpenAI Adapter Sync Pack

This repository contains the local OpenAI-compatible adapter plus the vendored Gemini WebAPI client it imports at runtime. It does not include your private Gemini cookies.

## Company PC setup

1. Clone or copy `youyi666/gemini-openai-adapter` to the company PC.
2. Run `install_adapter_dependencies.bat`.
3. Copy `adapter_env.example.ps1` to `adapter_env.local.ps1`.
4. Copy `gemini_cookies.example.json` to `gemini_cookies.local.json`.
5. Fill `gemini_cookies.local.json` with the company PC browser's Gemini cookies:
   - `__Secure-1PSID`
   - `__Secure-1PSIDTS`
6. Start the local API with `start_ai_server.bat`.
7. Open usage dashboard with `open_usage_dashboard.bat`.

## Cline configuration

- API Provider: OpenAI Compatible
- Base URL: `http://127.0.0.1:8000/v1`
- API Key: `dummy`
- Model ID: `gemini-3-pro`

If a client expects the provider root instead of an OpenAI `/v1` root, use `http://127.0.0.1:8000`. The adapter exposes both `/v1/chat/completions` and `/chat/completions`.

## Cline file-reading compatibility

Keep these lines enabled in `adapter_env.local.ps1`:

```powershell
$env:OPENAI_ADAPTER_PROMPT_MODE = "auto"
$env:OPENAI_ADAPTER_LOCAL_FILE_CONTEXT = "1"
$env:OPENAI_ADAPTER_LOCAL_FILE_CONTEXT_MAX_FILES = "3"
$env:OPENAI_ADAPTER_LOCAL_FILE_CONTEXT_MAX_CHARS = "120000"
```

When Cline asks Gemini to inspect an explicitly named source file, the adapter can attach a read-only copy of that file to the prompt. This helps Gemini continue even when it misunderstands Cline's XML tool results. Secret-looking files such as cookies, local env files, usage logs, token/password files, and debug prompts are blocked from this local context.

For Cline responsiveness, streaming requests default to eager SSE mode:

- `OPENAI_ADAPTER_STREAM_EAGER=1` returns the SSE connection immediately instead of waiting for Gemini's first text chunk.
- `OPENAI_ADAPTER_STREAM_PING_SECONDS=15` keeps the stream alive while Gemini is thinking.
- `OPENAI_ADAPTER_STREAM_FALLBACK_NON_STREAM=1` retries once with non-streaming generation when Gemini's stream fails before any answer text is emitted.
- `OPENAI_ADAPTER_STREAM_FALLBACK_MODEL=gemini-3-flash` uses Flash as the fallback model because Pro can be more fragile with long Cline prompts on the Gemini web stream endpoint.
- Set `OPENAI_ADAPTER_STREAM_EAGER=0` only if you prefer the older behavior where upstream startup errors can still become HTTP 500 JSON before SSE starts.

## Cookie writeback

Keep these lines enabled in `adapter_env.local.ps1`:

```powershell
$env:OPENAI_ADAPTER_COOKIE_WRITEBACK = "1"
$env:OPENAI_ADAPTER_COOKIE_WRITEBACK_INTERVAL_SECONDS = "60"
```

When the upstream Gemini client refreshes or rotates `__Secure-1PSID` / `__Secure-1PSIDTS`, the adapter writes the refreshed values back to `gemini_cookies.local.json`. The writeback runs on startup, periodically while the server is running, after successful responses, and before shutdown. It only writes when Gemini account status is authenticated.

This does not recover an already invalid browser session. If `/health` reports `UNAUTHENTICATED`, copy fresh cookies from the browser once, restart the adapter, and then writeback can keep later rotations in the local JSON file.

This repository also includes:

- `.clineignore` to keep secrets, logs, caches, usage files, and prompt dumps out of Cline context.
- `.clinerules` to keep per-task guidance short: PowerShell syntax, safe file edits, protected local files, and Git confirmation rules.

The compact prompt also preserves the core Cline action tools:

- `read_file`, `list_files`, `search_files`
- `write_to_file` for creating or rewriting files
- `replace_in_file` for targeted edits
- `execute_command` for verification commands
- `attempt_completion` and `ask_followup_question`

For existing files, ask Cline to prefer `replace_in_file`. Use a small disposable file first when validating a new machine setup.

The compact prompt now also tells Cline not to mix tasks:

- Only use write/edit tools when the current task explicitly asks for file changes.
- Do not write command text, task instructions, XML tags, or surrounding chat text into files.
- If a verification read shows unrelated instructions inside a file, correct the file before continuing.

## Windows PowerShell command tips

The compact prompt tells Cline to avoid Unix-style command chaining in Windows PowerShell:

- Do not use `&&` in Windows PowerShell 5.x.
- Use `;` or separate command calls instead.
- Prefer `Set-Location "Gemini-API"; git status --short` over `cd Gemini-API && git status --short`.
- For verification commands, check `$LASTEXITCODE` before claiming success.
- If visible terminal output contains `fatal:`, `ParserError`, `InvalidEndOfLine`, or `^C`, treat the command as failed even if Cline says the output was not captured.

Example:

```powershell
Set-Location "Gemini-API"; python -m py_compile openai_adapter_server.py; if ($LASTEXITCODE -eq 0) { Write-Host "Syntax check passed" } else { exit $LASTEXITCODE }
```

## Useful URLs

- Health: `http://127.0.0.1:8000/health`
- Usage JSON: `http://127.0.0.1:8000/usage`
- Usage Dashboard: `http://127.0.0.1:8000/usage.html`

`/health` also reports Gemini account status. If it shows `UNAUTHENTICATED`, refresh `gemini_cookies.local.json` from the browser and restart the adapter before testing Cline again.

## Multi-PC usage dashboard

The adapter can aggregate usage from multiple computers without a database.
Each computer writes its own shared usage file:

```text
usage-sync/adapter_usage.<computer-name>.jsonl
```

Recommended setup:

1. Keep this project in a private Git repository, OneDrive folder, or SMB shared folder.
2. Keep these lines in `adapter_env.local.ps1` on every computer:

   ```powershell
   $env:OPENAI_ADAPTER_INSTANCE_ID = $env:COMPUTERNAME
   $env:OPENAI_ADAPTER_USAGE_SHARED_DIR = Join-Path $PSScriptRoot "usage-sync"
   ```

3. If you use Git, commit and pull the `usage-sync/adapter_usage.<computer-name>.jsonl` files when you want the dashboard to include another computer.
4. If you use OneDrive or an SMB shared folder, the dashboard will update after the files sync.
5. Open `http://127.0.0.1:8000/usage.html` on any computer to see the combined total and the per-computer breakdown.

The usage files contain timestamps, model names, token estimates, and estimated costs. They do not store prompt or answer text.

## Security note

Do not commit or share these local files:

- `gemini_cookies.local.json`
- `adapter_env.local.ps1`
- `adapter_usage.jsonl`
- `adapter_forwarded_prompt.debug.txt`

Only commit `usage-sync/adapter_usage.<computer-name>.jsonl` to a private repository if you explicitly want Git-based usage aggregation.
