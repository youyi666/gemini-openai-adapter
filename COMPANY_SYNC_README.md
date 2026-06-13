# Gemini OpenAI Adapter Sync Pack

This pack contains only the local OpenAI-compatible adapter files. It does not include your private Gemini cookies.

## Company PC setup

1. Clone or copy the original `HanaokaYuzu/Gemini-API` repository to the company PC.
2. Copy these adapter files into the repository root.
3. Run `install_adapter_dependencies.bat`.
4. Copy `adapter_env.example.ps1` to `adapter_env.local.ps1`.
5. Copy `gemini_cookies.example.json` to `gemini_cookies.local.json`.
6. Fill `gemini_cookies.local.json` with the company PC browser's Gemini cookies:
   - `__Secure-1PSID`
   - `__Secure-1PSIDTS`
7. Start the local API with `start_ai_server.bat`.
8. Open usage dashboard with `open_usage_dashboard.bat`.

## Cline configuration

- API Provider: OpenAI Compatible
- Base URL: `http://127.0.0.1:8000/v1`
- API Key: `dummy`
- Model ID: `gemini-3-pro`

## Cline file-reading compatibility

Keep these lines enabled in `adapter_env.local.ps1`:

```powershell
$env:OPENAI_ADAPTER_PROMPT_MODE = "auto"
$env:OPENAI_ADAPTER_LOCAL_FILE_CONTEXT = "1"
$env:OPENAI_ADAPTER_LOCAL_FILE_CONTEXT_MAX_FILES = "3"
$env:OPENAI_ADAPTER_LOCAL_FILE_CONTEXT_MAX_CHARS = "120000"
```

When Cline asks Gemini to inspect an explicitly named source file, the adapter can attach a read-only copy of that file to the prompt. This helps Gemini continue even when it misunderstands Cline's XML tool results. Secret-looking files such as cookies, local env files, usage logs, token/password files, and debug prompts are blocked from this local context.

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
