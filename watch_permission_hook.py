import argparse
from datetime import datetime
import json
import os
import re
import socket
import sys
from pathlib import Path
from typing import Any


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
DEFAULT_TIMEOUT = 60.0
LOG_PATH = Path(__file__).with_name("watch_permission_hook.log")
INPUT_DIR = Path(__file__).with_name("hook_inputs")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Codex PermissionRequest hook for Codex Box watch approval.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    return parser.parse_args()


def read_hook_input() -> dict[str, Any]:
    text = sys.stdin.read()
    dump_hook_input(text)
    log_hook(f"stdin chars={len(text)}")
    if text.strip():
        log_hook(f"stdin preview={compact(text, 300)}")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log_hook("stdin json=invalid")
        return {}
    return data if isinstance(data, dict) else {}


def dump_hook_input(text: str) -> None:
    try:
        INPUT_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = INPUT_DIR / f"{stamp}-{os.getpid()}.json"
        path.write_bytes(text.encode("utf-8", errors="backslashreplace"))
    except Exception:
        pass


def compact(value: Any, limit: int = 120) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = sanitize_text(value)
    else:
        try:
            text = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
        except TypeError:
            text = sanitize_text(str(value))
    text = " ".join(text.split())
    return text[:limit]


def sanitize_text(text: str) -> str:
    return text.encode("utf-8", errors="backslashreplace").decode("utf-8", errors="replace")


def ascii_label(text: str, limit: int = 72) -> str:
    safe = sanitize_text(text)
    safe = safe.encode("ascii", errors="replace").decode("ascii")
    safe = re.sub(r"\?+", "?", safe)
    safe = " ".join(safe.split())
    return safe[:limit].strip()


def patch_summary(command: str) -> str:
    actions: list[tuple[str, str]] = []
    for line in sanitize_text(command).splitlines():
        match = re.match(r"\*\*\* (Add|Update|Delete) File: (.+)", line.strip())
        if match:
            action = match.group(1)
            filename = Path(match.group(2).strip()).name
            actions.append((action, ascii_label(filename, 36)))

    if not actions:
        return "Apply patch"

    action, filename = actions[0]
    if action == "Add":
        verb = "Create"
    elif action == "Delete":
        verb = "Delete"
    else:
        verb = "Edit"

    suffix = f" (+{len(actions) - 1})" if len(actions) > 1 else ""
    return f"{verb} {filename}{suffix}"


def bash_summary(command: str) -> str:
    text = ascii_label(command, 80)
    if not text:
        return "Run shell command"
    return f"Run {text}"


def permission_summary(tool: str, command: str) -> str:
    lower_tool = tool.lower()
    if "apply_patch" in lower_tool or command.lstrip().startswith("*** Begin Patch"):
        return patch_summary(command)
    if lower_tool in ("bash", "shell", "shell_command") or "bash" in lower_tool:
        return bash_summary(command)
    if command:
        return ascii_label(command, 72)
    return f"Approve {ascii_label(tool, 48)}"


def log_hook(message: str) -> None:
    try:
        stamp = datetime.now().isoformat(timespec="seconds")
        line = sanitize_text(f"{stamp} pid={os.getpid()} {message}\n")
        with LOG_PATH.open("ab") as f:
            f.write(line.encode("utf-8", errors="replace"))
    except Exception:
        pass


def first_text(data: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def describe_permission(data: dict[str, Any]) -> tuple[str, str]:
    tool = first_text(
        data,
        (
            "tool_name",
            "toolName",
            "tool",
            "name",
            "command",
            "cmd",
            "hook_event_name",
            "hookEventName",
        ),
    )
    if not tool:
        tool = "Codex action"

    tool_input = data.get("tool_input") or data.get("toolInput") or data.get("input") or data.get("arguments")
    command = ""
    if isinstance(tool_input, dict):
        command = first_text(tool_input, ("command", "cmd", "path", "file", "name"))
    elif isinstance(tool_input, str):
        command = tool_input
    if not command:
        command = first_text(data, ("reason", "description", "message"))

    tool_label = ascii_label(tool, 48) or "Codex action"
    details = permission_summary(tool_label, command)
    return tool_label, details


def ask_bridge(host: str, port: int, timeout: float, tool: str, details: str) -> str | None:
    request = {
        "type": "permission",
        "tool": tool,
        "details": details,
        "timeout": timeout,
    }
    body = json.dumps(request, separators=(",", ":"), ensure_ascii=True) + "\n"
    try:
        with socket.create_connection((host, port), timeout=5.0) as sock:
            sock.settimeout(timeout + 5.0)
            sock.sendall(body.encode("utf-8", errors="backslashreplace"))
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
    except OSError as exc:
        log_hook(f"bridge unavailable: {exc}")
        print(f"watch permission hook: bridge unavailable: {exc}", file=sys.stderr)
        return None

    raw = b"".join(chunks).decode("utf-8", errors="replace").strip()
    if not raw:
        return None
    try:
        response = json.loads(raw)
    except json.JSONDecodeError:
        print(f"watch permission hook: invalid bridge response: {raw}", file=sys.stderr)
        return None
    if not response.get("ok"):
        log_hook(f"bridge error: {response.get('error')}")
        print(f"watch permission hook: bridge error: {response.get('error')}", file=sys.stderr)
        return None
    decision = str(response.get("decision") or "").lower()
    if decision in ("once", "allow", "approved", "yes"):
        return "allow"
    return "deny"


def hook_event_name(data: dict[str, Any]) -> str:
    value = data.get("hook_event_name") or data.get("hookEventName")
    return str(value or "PermissionRequest")


def is_edit_tool(tool: str, data: dict[str, Any]) -> bool:
    lower_tool = tool.lower()
    if lower_tool in ("apply_patch", "edit", "write"):
        return True
    tool_input = data.get("tool_input") or data.get("toolInput") or data.get("input") or data.get("arguments")
    command = ""
    if isinstance(tool_input, dict):
        command = first_text(tool_input, ("command", "cmd"))
    elif isinstance(tool_input, str):
        command = tool_input
    return command.lstrip().startswith("*** Begin Patch")


def emit_decision(event_name: str, behavior: str, message: str = "") -> None:
    if event_name == "PreToolUse":
        hook_output: dict[str, Any] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": behavior,
        }
        if behavior == "deny" and message:
            hook_output["permissionDecisionReason"] = message
    else:
        decision: dict[str, Any] = {"behavior": behavior}
        if behavior == "deny" and message:
            decision["message"] = message
        hook_output = {
            "hookEventName": "PermissionRequest",
            "decision": decision,
        }

    payload = {
        "hookSpecificOutput": hook_output,
    }
    print(json.dumps(payload, separators=(",", ":")))


def main() -> int:
    try:
        log_hook("hook process started")
        args = parse_args()
        hook_input = read_hook_input()
        event_name = hook_event_name(hook_input)
        tool, details = describe_permission(hook_input)
        log_hook(f"event={event_name} tool={tool} details={details}")
        if event_name == "PreToolUse" and not is_edit_tool(tool, hook_input):
            log_hook(f"ignored PreToolUse tool={tool}")
            return 0
        if event_name not in ("PermissionRequest", "PreToolUse"):
            log_hook(f"ignored event={event_name}")
            return 0
        decision = ask_bridge(args.host, args.port, args.timeout, tool, details)
        if decision is None:
            log_hook("decision=none")
            return 0
        log_hook(f"decision={decision}")
        if decision == "allow":
            emit_decision(event_name, "allow")
        else:
            emit_decision(event_name, "deny", "Denied from Codex Box watch.")
        return 0
    except Exception as exc:
        log_hook(f"fatal={type(exc).__name__}: {exc}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
