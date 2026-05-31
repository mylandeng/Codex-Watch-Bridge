# Codex Watch Bridge

Bridge `codex exec --json` events to the ESP32 Claude Watch BLE firmware.

The ESP32 firmware accepts newline-delimited JSON over Nordic UART Service:

```json
{"total":1,"running":1,"waiting":0,"tokens_today":0,"msg":"Codex running"}
```

## Setup

```powershell
cd D:\code\Codex_Watch_Bridge
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Send to watch

Make sure the watch is advertising as `Codex Box xxxx`, then run:

```powershell
python .\codex_watch_bridge.py --prompt "检查 D:\code\ESP32S3\Claude_Watch_Buddy 项目结构"
```

If Windows BLE is slow to release the previous session, increase scan retries:

```powershell
.\run-watch.ps1 -BypassSandbox -ScanRetries 5 -Prompt "检查 D:\code\ESP32S3\Claude_Watch_Buddy 项目结构"
```

If Codex CLI reports `windows sandbox: spawn setup refresh`, retry with:

```powershell
python .\codex_watch_bridge.py --bypass-sandbox --prompt "检查 D:\code\ESP32S3\Claude_Watch_Buddy 项目结构"
```

Only use `--bypass-sandbox` for trusted prompts and workspaces because it lets Codex execute commands without the Codex sandbox.

By default it runs Codex in:

```text
D:\code\ESP32S3\Claude_Watch_Buddy
```

Override workspace:

```powershell
python .\codex_watch_bridge.py --workspace D:\some\repo --prompt "review this project"
```

## Live Codex CLI sidecar

For normal long-running `codex` CLI sessions, keep the watch connected in a second terminal:

```powershell
cd D:\code\Codex_Watch_Bridge
.\run-live.ps1 -Workspace D:\code\ESP32S3\Claude_Watch_Buddy
```

Dry-run the event parser without BLE:

```powershell
.\.venv\Scripts\python.exe .\codex_watch_live.py --dry-run --once --from-start --workspace D:\code\ESP32S3\Claude_Watch_Buddy
```

Then use Codex normally in another terminal:

```powershell
cd D:\code\ESP32S3\Claude_Watch_Buddy
codex
```

The live sidecar watches Codex rollout files under `%USERPROFILE%\.codex\sessions`, mirrors new user/assistant/tool/token events to the watch, and keeps BLE connected until you press `Ctrl+C` in the sidecar terminal.

By default the live sidecar sends ASCII status labels because the current watch firmware uses LVGL Montserrat fonts, which do not include Chinese glyphs. Add `--allow-cjk` to `codex_watch_live.py` only after adding a CJK font to the firmware.

The live sidecar also starts a local permission IPC server on `127.0.0.1:8766`. Codex hooks use that server to ask the already-connected watch for `Allow once` / `Deny`, so the BLE connection stays persistent.

## Codex PermissionRequest hook

This repository installs a project-local hook at:

```text
D:\code\ESP32S3\Claude_Watch_Buddy\.codex\hooks.json
```

The hook calls:

```powershell
python D:\code\Codex_Watch_Bridge\watch_permission_hook.py
```

Use it like this:

```powershell
# Terminal 1: keep BLE connected and expose hook IPC
cd D:\code\Codex_Watch_Bridge
.\run-live.ps1 -Workspace D:\code\ESP32S3\Claude_Watch_Buddy

# Terminal 2: start Codex normally inside the firmware project
cd D:\code\ESP32S3\Claude_Watch_Buddy
codex
```

The first time Codex sees this hook, run `/hooks` in the Codex CLI and trust the project hook. After that, when Codex is about to ask for approval for `Bash`, `apply_patch`, `Edit`, `Write`, or MCP tools, the watch should show the permission popup. The config also uses `PreToolUse` for `apply_patch/Edit/Write`, because Codex's edit-diff confirmation can be separate from the normal `PermissionRequest` approval path.

If the live sidecar is not running, `watch_permission_hook.py` exits without returning a decision, so Codex falls back to its normal approval prompt.

## Notes

- The default BLE scan prefix is `Codex Box`. Override it with `--device-prefix` if you rename the watch again.
- The scanner checks both the device name and scan-response local name, because Windows may omit `device.name` after a previous connection.
- This currently maps Codex state to watch status, token counters when present, and tool-ish events when visible in Codex JSONL.
- Long Codex messages are clipped before sending to the watch so the ESP32 UI does not receive oversized status strings.
- The bridge prints compact Codex event summaries by default and suppresses known noisy Codex warnings. Add `--verbose-codex` for full raw JSONL and `--verbose-stderr` for all Codex stderr.
- Approval interception uses Codex's official `PermissionRequest` hook. The hook only decides when Codex was already going to ask for approval; it does not wrap or replace the Codex terminal.
