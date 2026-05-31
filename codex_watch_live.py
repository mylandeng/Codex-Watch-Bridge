import argparse
import asyncio
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from codex_watch_bridge import (
    BleSink,
    DEFAULT_DEVICE_PREFIX,
    DEFAULT_SCAN_RETRIES,
    DEFAULT_SCAN_TIMEOUT,
    DryRunSink,
    WatchSink,
    WatchState,
    fit_watch_msg,
    safe_print,
    summarize_command,
    watch_payload,
)

CODEX_HOME = Path.home() / ".codex"
STATE_DB = CODEX_HOME / "state_5.sqlite"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Keep Codex Box connected and mirror live Codex CLI sessions.")
    parser.add_argument("--workspace", default=None, help="Only watch the latest Codex thread for this workspace.")
    parser.add_argument("--device-prefix", default=DEFAULT_DEVICE_PREFIX, help="BLE device name prefix.")
    parser.add_argument("--scan-timeout", type=float, default=DEFAULT_SCAN_TIMEOUT, help="Seconds per BLE scan attempt.")
    parser.add_argument("--scan-retries", type=int, default=DEFAULT_SCAN_RETRIES, help="BLE scan attempts before failing.")
    parser.add_argument("--poll", type=float, default=0.5, help="Polling interval in seconds.")
    parser.add_argument("--from-start", action="store_true", help="Replay the selected rollout from the beginning.")
    parser.add_argument("--dry-run", action="store_true", help="Print watch JSON instead of using BLE.")
    parser.add_argument("--once", action="store_true", help="Process currently available rollout events once, then exit.")
    parser.add_argument("--allow-cjk", action="store_true", help="Send non-ASCII text to the watch. Default uses ASCII status labels.")
    parser.add_argument("--ipc-host", default="127.0.0.1", help="Local host for Codex hook IPC.")
    parser.add_argument("--ipc-port", type=int, default=8766, help="Local port for Codex hook IPC.")
    parser.add_argument("--no-ipc", action="store_true", help="Disable PermissionRequest hook IPC server.")
    parser.add_argument("--permission-timeout", type=float, default=60.0, help="Seconds to wait for a watch approval tap.")
    return parser.parse_args()


def normalize_path(value: str | None) -> str:
    if not value:
        return ""
    if value.startswith("\\\\?\\"):
        value = value[4:]
    return str(Path(value)).rstrip("\\/").lower()


def latest_thread(workspace: str | None) -> dict[str, Any] | None:
    if not STATE_DB.exists():
        return None

    workspace_norm = normalize_path(workspace)
    con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "select id, title, cwd, source, tokens_used, updated_at, rollout_path "
            "from threads order by updated_at desc limit 50"
        ).fetchall()
    finally:
        con.close()

    for row in rows:
        if workspace_norm and normalize_path(row["cwd"]) != workspace_norm:
            continue
        rollout = Path(row["rollout_path"])
        if rollout.exists():
            return dict(row)
    return None


def extract_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("input_text") or item.get("output_text")
                if isinstance(text, str):
                    chunks.append(text)
        return " ".join(chunks)
    return ""


def ascii_watch_msg(kind: str, text: str = "") -> str:
    if kind == "user":
        return "User message received"
    if kind == "assistant":
        return "Codex replied"
    if kind == "complete":
        return "Codex idle"
    if kind == "session":
        return "Codex session active"
    if text and text.isascii():
        return text[:96]
    return kind


def watch_msg(kind: str, text: str, allow_cjk: bool) -> str:
    if allow_cjk:
        return text[:96] if text else ascii_watch_msg(kind)
    return ascii_watch_msg(kind, text)


def map_rollout_event(event: dict[str, Any], state: WatchState, allow_cjk: bool) -> dict[str, Any] | None:
    outer_type = event.get("type")
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None

    payload_type = payload.get("type")

    if outer_type == "event_msg":
        if payload_type == "task_started":
            state.total += 1
            return watch_payload(state, running=1, waiting=0, msg="Codex thinking")

        if payload_type == "user_message":
            message = payload.get("message")
            if isinstance(message, str) and message:
                return watch_payload(state, running=1, waiting=0, msg=watch_msg("user", f"User: {message[:80]}", allow_cjk))

        if payload_type == "agent_message":
            message = payload.get("message")
            if isinstance(message, str) and message:
                return watch_payload(state, running=1, waiting=0, msg=watch_msg("assistant", message, allow_cjk))

        if payload_type == "token_count":
            info = payload.get("info")
            usage = info.get("total_token_usage") if isinstance(info, dict) else None
            total = usage.get("total_tokens") if isinstance(usage, dict) else None
            if isinstance(total, int):
                return watch_payload(state, tokens_today=total, msg=f"Tokens {total}")

        if payload_type == "task_complete":
            message = payload.get("last_agent_message")
            msg = watch_msg("complete", message if isinstance(message, str) else "", allow_cjk)
            return watch_payload(state, running=0, waiting=0, msg=msg)

    if outer_type == "response_item":
        if payload_type == "function_call":
            name = str(payload.get("name") or "tool")
            args = payload.get("arguments")
            if name == "shell_command" and isinstance(args, str):
                try:
                    command = json.loads(args).get("command", "")
                except json.JSONDecodeError:
                    command = args
                return watch_payload(state, running=1, msg=summarize_command(str(command)))
            return watch_payload(state, running=1, msg=f"Using {name}")

    return None


class RolloutTail:
    def __init__(self, path: Path, from_start: bool) -> None:
        self.path = path
        self.pos = 0 if from_start else path.stat().st_size
        self.buffer = ""

    def read_new_events(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if not self.path.exists():
            return events

        with self.path.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(self.pos)
            chunk = f.read()
            self.pos = f.tell()

        if not chunk:
            return events

        self.buffer += chunk
        lines = self.buffer.splitlines(keepends=True)
        self.buffer = ""
        for line in lines:
            if not line.endswith("\n"):
                self.buffer = line
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events


class PermissionBroker:
    def __init__(self, sink: WatchSink, state: WatchState, host: str, port: int, default_timeout: float) -> None:
        self.sink = sink
        self.state = state
        self.host = host
        self.port = port
        self.default_timeout = default_timeout
        self.server: asyncio.AbstractServer | None = None
        self.pending: dict[str, asyncio.Future[str]] = {}
        self.notify_buffer = ""

    async def start(self) -> None:
        self.server = await asyncio.start_server(self.handle_client, self.host, self.port)
        safe_print(f"Permission IPC listening on {self.host}:{self.port}")
        if isinstance(self.sink, BleSink):
            await self.sink.start_notify(self.on_notify)

    async def close(self) -> None:
        if isinstance(self.sink, BleSink):
            await self.sink.stop_notify()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
            request = json.loads(raw.decode("utf-8"))
            if request.get("type") != "permission":
                await self.write_response(writer, {"ok": False, "error": "unsupported request"})
                return

            timeout = float(request.get("timeout") or self.default_timeout)
            decision = await self.request_permission(
                tool=str(request.get("tool") or "Codex tool"),
                details=str(request.get("details") or ""),
                timeout=timeout,
            )
            await self.write_response(writer, {"ok": True, "decision": decision})
        except TimeoutError:
            await self.write_response(writer, {"ok": False, "error": "ipc timeout"})
        except Exception as exc:
            await self.write_response(writer, {"ok": False, "error": str(exc)})
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def write_response(self, writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
        writer.write((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))
        await writer.drain()

    async def request_permission(self, tool: str, details: str, timeout: float) -> str:
        prompt_id = f"codex-{uuid.uuid4().hex[:12]}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self.pending[prompt_id] = future

        label = tool[:48] if tool else "Codex tool"
        msg = f"Allow {label}?"
        if details:
            msg = details[:80]

        await self.sink.send(
            watch_payload(
                self.state,
                running=1,
                waiting=1,
                msg=msg,
                prompt={"id": prompt_id, "tool": label},
            )
        )

        try:
            decision = await asyncio.wait_for(future, timeout=timeout)
            await self.sink.send(watch_payload(self.state, waiting=0, msg=f"Decision: {decision}", prompt=None))
            return decision
        except TimeoutError:
            await self.sink.send(watch_payload(self.state, waiting=0, msg="Permission timeout", prompt=None))
            return "deny"
        finally:
            self.pending.pop(prompt_id, None)

    def on_notify(self, sender: Any, data: bytearray) -> None:
        text = data.decode("utf-8", errors="replace")
        self.notify_buffer += text
        lines = self.notify_buffer.splitlines(keepends=True)
        self.notify_buffer = ""
        for line in lines:
            if not line.endswith("\n"):
                self.notify_buffer = line
                continue
            self.handle_notify_line(line.strip())

    def handle_notify_line(self, line: str) -> None:
        if not line:
            return
        safe_print(f"BLE RX {line}")
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return
        if payload.get("cmd") != "permission":
            return
        prompt_id = str(payload.get("id") or "")
        decision = str(payload.get("decision") or "deny")
        future = self.pending.get(prompt_id)
        if future and not future.done():
            future.set_result(decision)


async def run_live(args: argparse.Namespace) -> int:
    if args.dry_run:
        sink = DryRunSink()
    else:
        sink = BleSink(args.device_prefix, args.scan_timeout, args.scan_retries)
        await sink.connect()
    state = WatchState(total=0, running=0, waiting=0, msg="Watching Codex CLI")
    await sink.send(watch_payload(state))
    broker: PermissionBroker | None = None
    if not args.no_ipc:
        broker = PermissionBroker(sink, state, args.ipc_host, args.ipc_port, args.permission_timeout)
        await broker.start()

    current_thread_id: str | None = None
    tail: RolloutTail | None = None
    try:
        while True:
            thread = latest_thread(args.workspace)
            if thread and thread["id"] != current_thread_id:
                current_thread_id = thread["id"]
                rollout_path = Path(thread["rollout_path"])
                tail = RolloutTail(rollout_path, args.from_start)
                title = str(thread["title"] or "Codex session")
                safe_print(f"Watching thread {current_thread_id}: {title}")
                await sink.send(watch_payload(state, running=0, msg="Codex session active"))

            if tail:
                for event in tail.read_new_events():
                    payload = map_rollout_event(event, state, args.allow_cjk)
                    if payload:
                        await sink.send(payload)

            if args.once:
                return 0

            await asyncio.sleep(args.poll)
    except KeyboardInterrupt:
        safe_print("Stopping live watch")
        return 0
    finally:
        if broker:
            await broker.close()
        await sink.close()


def main() -> int:
    args = parse_args()
    return asyncio.run(run_live(args))


if __name__ == "__main__":
    raise SystemExit(main())
