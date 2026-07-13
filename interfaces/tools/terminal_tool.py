"""Terminal tool — execute shell commands locally.

"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import uuid
from typing import Any

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 50_000
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07")
SHELL_ARGV = ["/bin/zsh" if os.path.exists("/bin/zsh") else "/bin/sh", "-lc"]


def _strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def _safe_command_preview(command: Any, limit: int = 200) -> str:
    if command is None:
        return "<None>"
    if isinstance(command, str):
        return command[:limit]
    try:
        return repr(command)[:limit]
    except Exception:
        return f"<{type(command).__name__}>"


# ── Background process tracking ──

_bg_processes: dict[str, subprocess.Popen] = {}
_bg_lock = __import__("threading").Lock()


def _start_cleanup() -> None:
    """Remove finished background processes."""
    with _bg_lock:
        finished = [sid for sid, p in _bg_processes.items() if p.poll() is not None]
        for sid in finished:
            _bg_processes.pop(sid, None)


def terminal_tool(
    command: str,
    background: bool = False,
    timeout: int | None = None,
    workdir: str | None = None,
) -> str:
    """Execute a shell command locally.

    Args:
        command: The shell command to run.
        background: If True, start in background and return session_id.
        timeout: Seconds to wait (default 180, max 600 for foreground).
        workdir: Working directory for this command.
    """
    try:
        if not isinstance(command, str):
            return json.dumps({
                "output": "",
                "exit_code": -1,
                "error": f"Invalid command: expected string, got {type(command).__name__}",
            }, ensure_ascii=False)

        effective_timeout = timeout or 180
        cwd = workdir or os.getcwd()

        if background:
            _start_cleanup()
            session_id = uuid.uuid4().hex[:12]
            proc = subprocess.Popen(
                [*SHELL_ARGV, command],
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
            with _bg_lock:
                _bg_processes[session_id] = proc
            return json.dumps({
                "output": "Background process started",
                "session_id": session_id,
                "pid": proc.pid,
                "exit_code": 0,
            }, ensure_ascii=False)

        # Foreground: reject excessive timeout
        if effective_timeout > 600:
            return json.dumps({
                "error": f"Foreground timeout {effective_timeout}s exceeds 600s max. Use background=true.",
            }, ensure_ascii=False)

        result = subprocess.run(
            [*SHELL_ARGV, command],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            stdin=subprocess.DEVNULL,
        )

        output = (result.stdout or "") + (result.stderr or "")
        exit_code = result.returncode

        # Truncate
        if len(output) > MAX_OUTPUT_CHARS:
            head = output[: int(MAX_OUTPUT_CHARS * 0.4)]
            tail = output[-int(MAX_OUTPUT_CHARS * 0.6):]
            omitted = len(output) - len(head) - len(tail)
            output = (
                head
                + f"\n\n... [OUTPUT TRUNCATED — {omitted} chars omitted "
                f"out of {len(output)} total] ...\n\n"
                + tail
            )

        output = _strip_ansi(output)

        result_dict = {
            "output": output.strip(),
            "exit_code": exit_code,
        }
        if exit_code != 0:
            result_dict["error"] = f"Command exited with code {exit_code}"

        return json.dumps(result_dict, ensure_ascii=False)

    except subprocess.TimeoutExpired:
        return json.dumps({
            "output": "",
            "exit_code": 124,
            "error": f"Command timed out after {effective_timeout}s",
        }, ensure_ascii=False)
    except Exception as e:
        logger.error("terminal_tool exception: %s", e, exc_info=True)
        return json.dumps({
            "output": "",
            "exit_code": -1,
            "error": f"Failed to execute command: {e}",
        }, ensure_ascii=False)


def _bg_process_status(session_id: str) -> dict | None:
    """Poll status of a background process."""
    with _bg_lock:
        proc = _bg_processes.get(session_id)
    if proc is None:
        return None
    rc = proc.poll()
    if rc is not None:
        with _bg_lock:
            _bg_processes.pop(session_id, None)
        return {"status": "completed", "exit_code": rc, "session_id": session_id}
    return {"status": "running", "pid": proc.pid, "session_id": session_id}


def process_tool(
    action: str = "poll",
    session_id: str = "",
) -> str:
    """Manage background processes: poll, kill, or wait."""
    if action == "poll":
        if not session_id:
            with _bg_lock:
                running = [
                    {"session_id": sid, "pid": p.pid, "status": "running"}
                    for sid, p in _bg_processes.items()
                    if p.poll() is None
                ]
            return json.dumps({"processes": running, "count": len(running)}, ensure_ascii=False)
        status = _bg_process_status(session_id)
        if status is None:
            return json.dumps({"error": f"No process with session_id {session_id}"}, ensure_ascii=False)
        return json.dumps(status, ensure_ascii=False)

    elif action == "kill":
        if not session_id:
            return json.dumps({"error": "session_id is required for kill"}, ensure_ascii=False)
        with _bg_lock:
            proc = _bg_processes.pop(session_id, None)
        if proc is None:
            return json.dumps({"error": f"No process with session_id {session_id}"}, ensure_ascii=False)
        try:
            proc.kill()
            return json.dumps({"status": "killed", "session_id": session_id}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    elif action == "wait":
        if not session_id:
            return json.dumps({"error": "session_id is required for wait"}, ensure_ascii=False)
        with _bg_lock:
            proc = _bg_processes.get(session_id)
        if proc is None:
            return json.dumps({"error": f"No process with session_id {session_id}"}, ensure_ascii=False)
        try:
            rc = proc.wait(timeout=300)
            with _bg_lock:
                _bg_processes.pop(session_id, None)
            return json.dumps({"status": "completed", "exit_code": rc, "session_id": session_id}, ensure_ascii=False)
        except subprocess.TimeoutExpired:
            return json.dumps({"status": "still_running", "session_id": session_id}, ensure_ascii=False)

    else:
        return json.dumps({"error": f"Unknown action: {action}. Use poll, kill, or wait."}, ensure_ascii=False)


# ── Task context helper ──

def _get_task_workspace_for_tool() -> tuple[str | None, str | None]:
    """获取当前 active todo 的 workspace（如果有）。

    没有 active todo 时 fallback 到 **实例的 workspace 目录**
    ``apps/<instance_id>/workspace/``，而不是项目根。

    理由：terminal 是模型的"双手"，不应该被强制绑到 todo 才能用
    （2026-06-14 wake 841 bug 反馈）；但默认 cwd 也不能是项目根——否则
    模型 ad-hoc 写盘会落到根顶层（如 7/5 贝塔把文章写到根 articles/ 越界）。
    实例 workspace 是天然的 sandbox：本实例的产出物应在这里，跨实例不冲突。

    若 instance_id 解析失败（如外部直调未设 ContextVar），再降级回项目根，
    不阻断工具调用。
    """
    # 1. 优先：active todo 的 workspace
    try:
        from domain.todos._infra import get_active_task_workspace
        task_id, ws = get_active_task_workspace()
        if task_id and ws:
            return task_id, str(ws)
    except Exception:
        pass
    # 2. 默认：当前实例的 workspace 目录 apps/<iid>/workspace/
    try:
        from infrastructure.config import get_instance_dir, get_app_instance_id
        iid = get_app_instance_id()
        if iid:
            ws = get_instance_dir(iid) / "workspace"
            ws.mkdir(parents=True, exist_ok=True)
            return None, str(ws)
    except Exception:
        pass
    # 3. 最后降级：项目根（ContextVar 未设或异常时）
    from pathlib import Path
    repo_root = Path(__file__).resolve().parents[2]
    return None, str(repo_root)


# ── Handler wrappers for registry ──

def _handle_terminal(args: dict, **kw: Any) -> str:
    # 不再强制 todo 上下文。没 active todo 时 fallback 到 repo 根目录。
    _task_id, task_ws = _get_task_workspace_for_tool()

    workdir = args.get("workdir")
    if not workdir:
        workdir = str(task_ws)

    result = terminal_tool(
        command=args.get("command", ""),
        background=args.get("background", False),
        timeout=args.get("timeout"),
        workdir=workdir,
    )
    # Consume energy
    try:
        from domain.vital.simulation import ENERGY_COST_PER_CALL
        from domain.vital import consume_energy
        consume_energy(ENERGY_COST_PER_CALL, reason="terminal")
    except Exception:
        pass
    return result


def _handle_process(args: dict, **kw: Any) -> str:
    return process_tool(
        action=args.get("action", "poll"),
        session_id=args.get("session_id", ""),
    )


# ── Registry ──

from interfaces.tools.registry import registry

registry.register(
    name="terminal",
    toolset="actions",
    schema={
        "name": "terminal",
        "description": (
            "Execute shell commands on the local machine. "
            "Do NOT use cat/head/tail to read files — use file tools instead. "
            "Do NOT use grep/rg/find to search — use search tools instead. "
            "Do NOT use sed/awk to edit files — use patch instead. "
            "Foreground (default): Returns INSTANTLY when done. Set timeout=300 for long builds. "
            "Background: Set background=true to get a session_id, use process(action='poll') to check progress."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute.",
                },
                "background": {
                    "type": "boolean",
                    "description": "Run in background. Returns session_id for process() polling.",
                    "default": False,
                },
                "timeout": {
                    "type": "integer",
                    "description": "Max seconds to wait (default 180, max 600 foreground).",
                    "minimum": 1,
                },
                "workdir": {
                    "type": "string",
                    "description": "Working directory (absolute path).",
                },
            },
            "required": ["command"],
        },
    },
    handler=_handle_terminal,
    emoji="💻",
    max_result_size_chars=100_000,
)

registry.register(
    name="process",
    toolset="actions",
    schema={
        "name": "process",
        "description": (
            "Manage background processes started by terminal. "
            "action='poll': list all or check one by session_id. "
            "action='kill': kill a process. "
            "action='wait': block until process completes (max 5 min)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "poll, kill, or wait",
                    "enum": ["poll", "kill", "wait"],
                },
                "session_id": {
                    "type": "string",
                    "description": "The session_id returned by terminal with background=true.",
                },
            },
            "required": ["action"],
        },
    },
    handler=_handle_process,
    emoji="🔧",
)
