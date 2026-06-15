# Shared Usage Sync

This folder is used by the OpenAI adapter dashboard to aggregate usage from
multiple computers.

Runtime files are named like:

```text
adapter_usage.<computer-name>.jsonl
```

If the computer name contains non-ASCII characters, the adapter writes a stable
safe file name such as `adapter_usage.pc-bf8048e9b4.jsonl` and keeps the readable
computer name inside each record.

They contain timestamps, model names, token estimates, and estimated costs.
They do not contain prompt or response text.

By default, real usage logs are ignored so the repository can be pushed safely.
If this repository is private and you want Git-based aggregation, explicitly add
the usage files:

```powershell
git add usage-sync/adapter_usage*.jsonl
```
