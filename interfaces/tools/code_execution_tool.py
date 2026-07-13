"""Code execution tool — run Python scripts in a sandboxed subprocess.

Keeps reset_exec_counter / increment_exec_counter for L4 scheduler compatibility.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid

logger = logging.getLogger(__name__)

MAX_STDOUT_BYTES = 50_000
MAX_STDERR_BYTES = 10_000
DEFAULT_TIMEOUT = 300

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07")


def _strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


# ── Execution counter (referenced by L4 scheduler) ──

_exec_counter = 0


def reset_exec_counter() -> None:
    global _exec_counter
    _exec_counter = 0


def increment_exec_counter() -> int:
    global _exec_counter
    _exec_counter += 1
    return _exec_counter


# ── Main entry point ──

def execute_code(code: str, timeout: int | None = None, cwd: str | None = None) -> str:
    """Run a Python script in a subprocess and return stdout.

    Args:
        code: Python source code to execute.
        timeout: Seconds before kill (default 300).
        cwd: Working directory for the subprocess (default: temp dir).
    """
    if not code or not code.strip():
        return json.dumps({"error": "No code provided.", "status": "error"}, ensure_ascii=False)

    effective_timeout = timeout or DEFAULT_TIMEOUT
    effective_cwd = cwd or tempfile.mkdtemp(prefix="dl_sandbox_")
    tmpdir = tempfile.mkdtemp(prefix="dl_sandbox_")
    exec_start = time.monotonic()
    tool_call_counter = [0]

    try:
        # Write script
        script_path = os.path.join(tmpdir, "script.py")
        with open(script_path, "w") as f:
            f.write(code)

        # Build safe child environment
        _SAFE_PREFIXES = ("PATH", "HOME", "USER", "LANG", "LC_", "TERM",
                          "TMPDIR", "TMP", "TEMP", "SHELL", "LOGNAME",
                          "XDG_", "PYTHONPATH", "VIRTUAL_ENV", "CONDA",
                          "DIGITAL_LIFE_")
        _SECRET_SUBSTRINGS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH")

        child_env = {}
        for k, v in os.environ.items():
            if any(s in k.upper() for s in _SECRET_SUBSTRINGS):
                continue
            if any(k.startswith(p) for p in _SAFE_PREFIXES):
                child_env[k] = v
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"

        proc = subprocess.Popen(
            [sys.executable, script_path],
            cwd=effective_cwd,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )

        try:
            stdout_bytes, stderr_bytes = proc.communicate(timeout=effective_timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout_bytes, stderr_bytes = proc.communicate()
            return json.dumps({
                "status": "timeout",
                "output": _strip_ansi(
                    (stdout_bytes or b"").decode("utf-8", errors="replace")
                )[:MAX_STDOUT_BYTES],
                "tool_calls_made": tool_call_counter[0],
                "duration_seconds": round(time.monotonic() - exec_start, 2),
                "error": f"Script timed out after {effective_timeout}s and was killed.",
            }, ensure_ascii=False)

        exit_code = proc.returncode or 0
        duration = round(time.monotonic() - exec_start, 2)

        stdout_text = _strip_ansi(
            (stdout_bytes or b"").decode("utf-8", errors="replace")
        )
        stderr_text = _strip_ansi(
            (stderr_bytes or b"").decode("utf-8", errors="replace")
        )

        # Truncate stdout
        if len(stdout_text) > MAX_STDOUT_BYTES:
            head = stdout_text[: int(MAX_STDOUT_BYTES * 0.4)]
            tail = stdout_text[-int(MAX_STDOUT_BYTES * 0.6):]
            omitted = len(stdout_text) - len(head) - len(tail)
            stdout_text = (
                head
                + f"\n\n... [OUTPUT TRUNCATED — {omitted:,} chars omitted "
                f"out of {len(stdout_text):,} total] ...\n\n"
                + tail
            )

        # Truncate stderr
        stderr_text = stderr_text[:MAX_STDERR_BYTES]

        result: dict = {
            "status": "success",
            "output": stdout_text,
            "tool_calls_made": tool_call_counter[0],
            "duration_seconds": duration,
        }

        if exit_code != 0:
            result["status"] = "error"
            result["error"] = stderr_text or f"Script exited with code {exit_code}"
            if stderr_text:
                result["output"] = stdout_text + "\n--- stderr ---\n" + stderr_text

        return json.dumps(result, ensure_ascii=False)

    except Exception as e:
        duration = round(time.monotonic() - exec_start, 2)
        logger.error("execute_code failed: %s", e, exc_info=True)
        return json.dumps({
            "status": "error",
            "error": str(e),
            "tool_calls_made": tool_call_counter[0],
            "duration_seconds": duration,
        }, ensure_ascii=False)

    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── Task context helper ──

def _get_task_workspace_for_tool() -> tuple[str | None, str | None]:
    """获取当前 active todo 的 workspace（如果有）。

    优先级：active todo workspace → **实例 workspace** ``apps/<iid>/workspace/``
    → 项目根（异常降级）。

    重要理由：
    - terminal / execute_code 是模型的"双手"，应该在任意时候都能用，不应该
      被强制绑到一个 todo 上才能跑（2026-06-14 wake 841 实例反馈：违背
      "待办是独立 entity"原则 + 实战时模型经常需要跑无关的 ad-hoc 代码，
      比如查 stock data、写一次性脚本）。
    - 但默认 cwd 不能是项目根——否则模型写盘会落到根顶层（如 7/5 贝塔把
      文章写到根 articles/ 越界 agent sandbox）。实例 workspace 是天然 sandbox。
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
    # 3. 最后降级：项目根 tmp/（ContextVar 未设或异常时）
    from pathlib import Path
    repo_root = Path(__file__).resolve().parents[2]
    tmp_ws = repo_root / "tmp"
    tmp_ws.mkdir(parents=True, exist_ok=True)
    return None, str(tmp_ws)


# ── Handler for registry ──

def _handle_execute_code(args: dict, **kw: Any) -> str:
    # 不再强制 todo 上下文。没 active todo 时 fallback 到 repo/tmp/。
    # 历史 bug：原来 block 了 execute_code 返回 "请先用 task start声明当前任务"
    # → 模型被迫走非代码路径（write_diary / update_context 等），实战能力大打折扣。
    _task_id, task_ws = _get_task_workspace_for_tool()

    result = execute_code(
        code=args.get("code", ""),
        timeout=args.get("timeout"),
        cwd=task_ws,
    )
    # Consume energy
    try:
        from domain.vital.simulation import ENERGY_COST_PER_CALL
        from domain.vital import consume_energy
        consume_energy(ENERGY_COST_PER_CALL, reason="execute_code")
    except Exception:
        pass
    return result


# ── Registry ──

from interfaces.tools.registry import registry

registry.register(
    name="execute_code",
    toolset="actions",
    schema={
        "name": "execute_code",
        "description": (
            "Run a Python script and get its stdout. "
            "Use this when you need to process data, run calculations, or chain operations. "
            "Print your result to stdout. Available: Python stdlib (json, re, math, csv, "
            "datetime, collections, itertools, pathlib, subprocess, etc.). "
            f"Limits: {DEFAULT_TIMEOUT}s timeout, {MAX_STDOUT_BYTES:,} byte stdout cap."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute. Print final result to stdout.",
                },
                "timeout": {
                    "type": "integer",
                    "description": f"Max seconds before kill (default {DEFAULT_TIMEOUT}).",
                    "minimum": 1,
                },
            },
            "required": ["code"],
        },
    },
    handler=_handle_execute_code,
    emoji="🐍",
    max_result_size_chars=100_000,
)
