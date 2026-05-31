import argparse
import asyncio
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
DEFAULT_DEVICE_PREFIX = "Codex Box"
DEFAULT_WORKSPACE = r"D:\code\ESP32S3\Claude_Watch_Buddy"
DEFAULT_SCAN_TIMEOUT = 8.0
DEFAULT_SCAN_RETRIES = 3
MAX_WATCH_MSG_BYTES = 96
SUBPROCESS_LINE_LIMIT = 16 * 1024 * 1024
NOISY_STDERR_MARKERS = (
    "Reading additional input from stdin",
    "failed to load plugin: missing or invalid plugin.json plugin=\"chrome@openai-bundled\"",
    "Failed to create shell snapshot for powershell",
    "ignoring interface.icon_small",
    "ignoring interface.icon_large",
)


@dataclass
class WatchState:
    total: int = 0
    running: int = 0
    waiting: int = 0
    tokens_today: int = 0
    msg: str = "Codex idle"
    done_sent: bool = False


class WatchSink:
    def __init__(self) -> None:
        self.closed = False

    async def send(self, payload: dict[str, Any]) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        self.closed = True


class DryRunSink(WatchSink):
    async def send(self, payload: dict[str, Any]) -> None:
        safe_print(f"WATCH {json.dumps(payload, ensure_ascii=False)}")


class BleSink(WatchSink):
    def __init__(self, name_prefix: str, scan_timeout: float, scan_retries: int) -> None:
        super().__init__()
        self.name_prefix = name_prefix
        self.scan_timeout = scan_timeout
        self.scan_retries = scan_retries
        self.client = None
        self.rx_char = None
        self.write_lock = asyncio.Lock()

    async def connect(self) -> None:
        try:
            from bleak import BleakClient, BleakScanner
        except ImportError as exc:
            raise SystemExit("Missing dependency: pip install -r requirements.txt") from exc

        safe_print(f"Scanning for BLE device prefix: {self.name_prefix}")
        device = None
        for attempt in range(1, self.scan_retries + 1):
            device = await BleakScanner.find_device_by_filter(
                lambda d, ad: self.matches_device(d, ad),
                timeout=self.scan_timeout,
            )
            if device:
                break
            if attempt < self.scan_retries:
                safe_print(f"BLE scan retry {attempt}/{self.scan_retries}")
                await asyncio.sleep(1.0)
        if not device:
            raise SystemExit(f"BLE device not found: {self.name_prefix}*")

        safe_print(f"Connecting to {device.name} ({device.address})")
        self.client = BleakClient(device, disconnected_callback=self.on_disconnected)
        await self.client.connect()
        self.rx_char = NUS_RX_UUID
        safe_print("BLE connected")

    def on_disconnected(self, client: Any) -> None:
        safe_print("BLE disconnected")

    def matches_device(self, device: Any, adv: Any) -> bool:
        names = [
            getattr(device, "name", None),
            getattr(adv, "local_name", None),
        ]
        if any(name and name.startswith(self.name_prefix) for name in names):
            return True

        service_uuids = [uuid.lower() for uuid in (getattr(adv, "service_uuids", None) or [])]
        has_nus = NUS_SERVICE_UUID.lower() in service_uuids
        return has_nus and any(name and self.name_prefix in name for name in names)

    async def send(self, payload: dict[str, Any]) -> None:
        if self.closed:
            return
        if not self.client or not self.client.is_connected:
            raise RuntimeError("BLE client is not connected")
        line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
        async with self.write_lock:
            await self.client.write_gatt_char(self.rx_char, line.encode("utf-8"), response=False)
        safe_print(f"BLE TX {line.strip()}")

    async def start_notify(self, callback: Any) -> None:
        if not self.client or not self.client.is_connected:
            raise RuntimeError("BLE client is not connected")
        await self.client.start_notify(NUS_TX_UUID, callback)
        safe_print("BLE notifications subscribed")

    async def stop_notify(self) -> None:
        if self.client and self.client.is_connected:
            try:
                await self.client.stop_notify(NUS_TX_UUID)
            except Exception:
                pass

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.client and self.client.is_connected:
            safe_print("BLE disconnecting")
            try:
                line = json.dumps({"cmd": "disconnect"}, separators=(",", ":")) + "\n"
                await asyncio.wait_for(
                    self.client.write_gatt_char(self.rx_char, line.encode("utf-8"), response=False),
                    timeout=2.0,
                )
                safe_print(f"BLE TX {line.strip()}")
                await asyncio.sleep(0.2)
            except Exception as exc:
                safe_print(f"BLE remote disconnect request failed: {exc}")
            try:
                await asyncio.wait_for(self.client.disconnect(), timeout=5.0)
            except TimeoutError:
                safe_print("BLE disconnect timed out")
            await asyncio.sleep(0.5)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bridge codex exec --json events to Codex Watch BLE.")
    parser.add_argument("--prompt", required=True, help="Prompt passed to codex exec.")
    parser.add_argument("--workspace", default=DEFAULT_WORKSPACE, help="Workspace passed to codex -C.")
    parser.add_argument("--model", default=None, help="Optional Codex model.")
    parser.add_argument("--device-prefix", default=DEFAULT_DEVICE_PREFIX, help="BLE device name prefix.")
    parser.add_argument("--scan-timeout", type=float, default=DEFAULT_SCAN_TIMEOUT, help="Seconds per BLE scan attempt.")
    parser.add_argument("--scan-retries", type=int, default=DEFAULT_SCAN_RETRIES, help="BLE scan attempts before failing.")
    parser.add_argument("--dry-run", action="store_true", help="Print watch JSON instead of using BLE.")
    parser.add_argument("--codex", default="codex", help="Codex CLI executable.")
    parser.add_argument(
        "--bypass-sandbox",
        action="store_true",
        help="Pass Codex --dangerously-bypass-approvals-and-sandbox. Use only for trusted prompts/workspaces.",
    )
    parser.add_argument(
        "--verbose-codex",
        action="store_true",
        help="Print full raw Codex JSONL events. Default prints compact event summaries.",
    )
    parser.add_argument(
        "--verbose-stderr",
        action="store_true",
        help="Print all Codex stderr lines, including known noisy warnings.",
    )
    return parser.parse_args()


def safe_print(text: str, *, stream: Any = sys.stdout) -> None:
    encoding = getattr(stream, "encoding", None) or "utf-8"
    safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    stream.write(safe_text + "\n")
    stream.flush()


def find_codex(executable: str) -> str:
    resolved = shutil.which(executable)
    if not resolved:
        raise SystemExit(f"Codex CLI not found in PATH: {executable}")
    return resolved


def watch_payload(state: WatchState, **updates: Any) -> dict[str, Any]:
    extras: dict[str, Any] = {}
    if "prompt" in updates:
        extras["prompt"] = updates.pop("prompt")
    for key, value in updates.items():
        setattr(state, key, value)
    payload = {
        "total": state.total,
        "running": state.running,
        "waiting": state.waiting,
        "tokens_today": state.tokens_today,
        "msg": fit_watch_msg(state.msg),
    }
    payload.update(extras)
    return payload


def fit_watch_msg(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_WATCH_MSG_BYTES:
        return text
    clipped = encoded[:MAX_WATCH_MSG_BYTES]
    return clipped.decode("utf-8", errors="ignore").rstrip()


def extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "message", "content", "name", "cmd", "command"):
            text = extract_text(value.get(key))
            if text:
                return text
    if isinstance(value, list):
        for item in value:
            text = extract_text(item)
            if text:
                return text
    return ""


def summarize_command(command: str) -> str:
    normalized = " ".join(command.replace("\\\\", "\\").split())
    lowered = normalized.lower()

    content_match = re.search(r"get-content\s+(?:-path\s+)?([^\s|]+)", normalized, re.IGNORECASE)
    if content_match:
        target = content_match.group(1).strip("'\"")
        return f"Reading {Path(target).name}"

    rg_match = re.search(r"\brg(?:\.exe)?\b\s+(.+)", normalized, re.IGNORECASE)
    if rg_match:
        return "Searching files"

    if "findstr" in lowered:
        return "Searching files"
    if "idf.py" in lowered and "build" in lowered:
        return "Building firmware"
    if "idf.py" in lowered and "flash" in lowered:
        return "Flashing firmware"
    if "python" in lowered:
        return "Running Python"
    if "git " in lowered:
        return "Inspecting git"
    if any(word in lowered for word in ("get-childitem", " dir ", " ls ")):
        return "Listing files"
    return "Running shell"


def summarize_item(item: dict[str, Any]) -> str:
    item_type = str(item.get("type") or "")
    if item_type == "command_execution":
        command = str(item.get("command") or "")
        return summarize_command(command)
    if "tool" in item_type:
        return "Using tool"
    if "command" in item_type:
        return "Running command"
    return "Codex working"


def summarize_codex_event(event: dict[str, Any]) -> str:
    event_type = str(event.get("type") or event.get("event") or event.get("msg") or "event")
    item = event.get("item")
    if isinstance(item, dict):
        item_type = str(item.get("type") or "")
        if item_type == "command_execution":
            status = str(item.get("status") or "event")
            return f"{event_type} command_execution {status}: {summarize_item(item)}"
        if item_type:
            return f"{event_type} {item_type}"
    return event_type


def should_print_stderr(line: str, verbose: bool) -> bool:
    if verbose:
        return True
    return not any(marker in line for marker in NOISY_STDERR_MARKERS)


def map_codex_event(event: dict[str, Any], state: WatchState) -> dict[str, Any] | None:
    event_type = str(event.get("type") or event.get("event") or event.get("msg", "event"))
    lower_type = event_type.lower()
    item = event.get("item")
    text = extract_text(event)
    if isinstance(item, dict):
        text = extract_text(item) or text

    usage = event.get("usage")
    if isinstance(usage, dict):
        total_tokens = usage.get("total_tokens") or usage.get("total")
        if total_tokens is None:
            input_tokens = usage.get("input_tokens") or 0
            output_tokens = usage.get("output_tokens") or 0
            reasoning_tokens = usage.get("reasoning_output_tokens") or 0
            if all(isinstance(v, int) for v in (input_tokens, output_tokens, reasoning_tokens)):
                total_tokens = input_tokens + output_tokens + reasoning_tokens
        if isinstance(total_tokens, int):
            state.tokens_today = total_tokens

    if "token" in lower_type and isinstance(event.get("tokens"), int):
        state.tokens_today = event["tokens"]

    if lower_type == "item.started" and isinstance(item, dict):
        item_type = str(item.get("type") or "")
        if item_type == "command_execution" or "tool" in item_type or "command" in item_type:
            return watch_payload(state, msg=summarize_item(item))

    if lower_type == "item.completed" and isinstance(item, dict):
        item_type = str(item.get("type") or "")
        if item_type == "agent_message" and text:
            trimmed = " ".join(text.split())
            return watch_payload(state, msg=trimmed[:96])
        if item_type == "command_execution" or "tool" in item_type or "command" in item_type:
            status = str(item.get("status") or "")
            exit_code = item.get("exit_code")
            if status == "failed" or (isinstance(exit_code, int) and exit_code != 0):
                return watch_payload(state, msg=f"Failed: {summarize_item(item)}")
            return None

    if any(word in lower_type for word in ("tool", "exec", "command", "shell")):
        state.msg = "Codex using tools"
        return watch_payload(state)

    if any(word in lower_type for word in ("error", "failed", "failure")):
        return watch_payload(state, running=0, waiting=0, msg="Codex failed")

    if lower_type in ("turn.completed", "thread.completed"):
        state.done_sent = True
        return watch_payload(state, running=0, waiting=0, msg="Codex done")

    if text and any(word in lower_type for word in ("assistant", "message", "delta", "response")):
        trimmed = " ".join(text.split())
        if trimmed:
            return watch_payload(state, msg=trimmed[:96])

    return None


async def run_codex(args: argparse.Namespace, sink: WatchSink) -> int:
    codex = find_codex(args.codex)
    workspace = str(Path(args.workspace))
    state = WatchState(total=1, running=1, waiting=0, msg="Codex starting")
    await sink.send(watch_payload(state))

    cmd = [
        codex,
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-C",
        workspace,
    ]
    if args.bypass_sandbox:
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    if args.model:
        cmd += ["-m", args.model]
    cmd.append(args.prompt)

    safe_print(f"RUN {' '.join(cmd)}")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=SUBPROCESS_LINE_LIMIT,
    )

    assert proc.stdout is not None
    assert proc.stderr is not None

    async def read_stderr() -> None:
        async for raw in proc.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line and should_print_stderr(line, args.verbose_stderr):
                safe_print(f"CODEX STDERR {line}", stream=sys.stderr)

    stderr_task = asyncio.create_task(read_stderr())

    watch_closed = False
    async for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            safe_print(f"CODEX {line[:240]}")
            continue
        if args.verbose_codex:
            safe_print(f"CODEX {line}")
        else:
            safe_print(f"CODEX {summarize_codex_event(event)}")
        payload = map_codex_event(event, state)
        if payload:
            await sink.send(payload)
            if state.done_sent and not watch_closed:
                watch_closed = True
                await sink.close()

    rc = await proc.wait()
    await stderr_task

    if rc == 0:
        if not state.done_sent:
            await sink.send(watch_payload(state, running=0, waiting=0, msg="Codex done"))
    else:
        await sink.send(watch_payload(state, running=0, waiting=0, msg=f"Codex failed rc={rc}"))
    return rc


async def main() -> int:
    args = parse_args()
    sink: WatchSink
    if args.dry_run:
        sink = DryRunSink()
    else:
        ble_sink = BleSink(args.device_prefix, args.scan_timeout, args.scan_retries)
        await ble_sink.connect()
        sink = ble_sink

    try:
        return await run_codex(args, sink)
    finally:
        await sink.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
