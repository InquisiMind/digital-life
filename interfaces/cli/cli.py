"""Console command launcher for the local digital employee runtime."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
VAR_DIR = ROOT / "var"
RUN_DIR = VAR_DIR / "run"
LOG_DIR = VAR_DIR / "logs"
PID_FILE = RUN_DIR / "digital-life.pid"           # master PID（兼容现有 digital-life.pid）
MASTER_PID_FILE = RUN_DIR / "digital-life-master.pid"
META_FILE = RUN_DIR / "digital-life.json"
LOG_FILE = LOG_DIR / "digital-life.log"
LOCAL_CONFIG = CONFIG_DIR / "local.yaml"
DEFAULT_CONFIG = CONFIG_DIR / "default.yaml"

IS_WINDOWS = os.name == "nt"


def _is_running(pid: int) -> bool:
    """检查进程是否存活。Windows 和 Unix 路径不同。"""
    if IS_WINDOWS:
        # Windows: os.kill(pid, 0) 不支持。
        # 用 ctypes OpenProcess 检查进程是否还在。
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _cleanup_pid_files() -> None:
    for path in (PID_FILE, META_FILE, MASTER_PID_FILE):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass


def _cleanup_instance_pid_files() -> None:
    """清理 var/run/digital-life-<instance>.pid 残留。

    防呆：如果 PID 还活着（说明上次是被 SIGKILL 而非 graceful stop 杀的，遗留了
    孤儿 worker），先并发 kill 它们。否则多个 worker 同时写 chats.db / state.db
    会读到 stale snapshot——正是 6-11 那次重复回复 bug 的根因（我手动 kill
    listen PID 但留了 8 个孤儿 worker）。
    """
    if not RUN_DIR.exists():
        return
    killed: list[int] = []
    for f in RUN_DIR.glob("digital-life-*.pid"):
        try:
            pid = int(f.read_text().strip())
        except (ValueError, OSError):
            try:
                f.unlink()
            except Exception:
                pass
            continue
        # 该 PID 还在跑？kill 它（先 TERM 后 KILL 兜底）
        if _is_running(pid):
            try:
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
            killed.append(pid)
        try:
            f.unlink()
        except Exception:
            pass
    # 给 TERM 最多 3 秒，没死就 KILL
    if killed:
        deadline = time.time() + 3.0
        while time.time() < deadline and any(_is_running(p) for p in killed):
            time.sleep(0.2)
        for pid in killed:
            if _is_running(pid):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                    print(f"killed stale instance worker: pid={pid}")
                else:
                    print(f"stopped stale instance worker: pid={pid}")


def _tail_log(lines: int = 40) -> str:
    if not LOG_FILE.exists():
        return ""
    content = LOG_FILE.read_text(errors="replace").splitlines()
    return "\n".join(content[-lines:])


def _prepare_runtime_home() -> None:
    """Create var/ directories for launcher state."""
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _find_free_port(start: int = 8642, limit: int = 20) -> int:
    for port in range(start, start + limit):
        if not _port_in_use(port):
            return port
    raise SystemExit(f"No free API server port found in range {start}-{start + limit - 1}")


def _resolve_api_port(raw: str | None) -> int | None:
    if not raw or raw == "auto":
        return _find_free_port()
    try:
        port = int(raw)
    except ValueError as exc:
        raise SystemExit(f"Invalid --api-port value: {raw}") from exc
    if not 1 <= port <= 65535:
        raise SystemExit(f"Invalid --api-port value: {raw}")
    return port


def _base_env(api_port: int | None = None, cwd: Path | None = None) -> dict[str, str]:
    pythonpath = os.environ.get("PYTHONPATH", "")
    paths = [str(ROOT)]
    if pythonpath:
        paths.append(pythonpath)

    env = os.environ.copy()
    # 清除代理 —— 飞书 WebSocket 需要直连
    for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
               "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        env.pop(_k, None)
    env.update(
        {
            "PYTHONPATH": os.pathsep.join(paths),
            "L4_HOME": str(ROOT),
            "PWD": str(cwd or ROOT),
        }
    )
    if api_port is not None:
        env["API_SERVER_ENABLED"] = "true"
        env["API_SERVER_PORT"] = str(api_port)
    # 合并 SSL 证书：certifi 公共 CA + ZCode 代理 CA
    combined_cert = Path.home() / ".zcode" / "v2" / "acp-traffic-proxy" / "combined-ca.pem"
    try:
        if combined_cert.exists():
            env["SSL_CERT_FILE"] = str(combined_cert)
            env["REQUESTS_CA_BUNDLE"] = str(combined_cert)
    except (PermissionError, OSError):
        pass  # 沙箱权限限制 → 跳过, 用系统默认 CA
    return env


def _runtime_command() -> list[str]:
    return [sys.executable, str(ROOT / "gateway" / "main.py")]


def _run_foreground(api_port: int | None = None) -> int:
    _prepare_runtime_home()
    return subprocess.call(_runtime_command(), cwd=str(ROOT), env=_base_env(api_port, ROOT))


def _start(args: argparse.Namespace) -> int:
    _prepare_runtime_home()
    pid = _read_pid()
    if pid and _is_running(pid):
        print(f"digital-life already running: pid={pid}")
        print(f"log={LOG_FILE}")
        return 0
    if pid:
        _cleanup_pid_files()

    # 清理上次残留的 instance 子进程 PID 文件（master 重启后这些都不再有效）
    _cleanup_instance_pid_files()

    api_port = _resolve_api_port(args.api_port)

    if args.foreground:
        return _run_foreground(api_port)

    log_handle = LOG_FILE.open("a", encoding="utf-8")
    log_handle.write(
        f"\n--- digital-life start api_port={api_port or '-'} "
        f"at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n"
    )
    log_handle.flush()
    popen_kwargs: dict[str, Any] = {
        "cwd": str(ROOT),
        "env": _base_env(api_port, ROOT),
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
    }
    if IS_WINDOWS:
        # Windows: CREATE_NEW_PROCESS_GROUP 让 master 能收到 Ctrl-Break 信号
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        # Unix: 新 session（detached），master 死了子进程也活
        popen_kwargs["start_new_session"] = True

    process = subprocess.Popen(
        _runtime_command(),
        **popen_kwargs,
    )
    PID_FILE.write_text(str(process.pid))
    META_FILE.write_text(
        json.dumps(
            {
                "pid": process.pid,
                "started_at": time.time(),
                "log": str(LOG_FILE),
                "runtime_home": str(VAR_DIR),
                "api_port": api_port,
            },
            indent=2,
            ensure_ascii=False,
        )
    )

    time.sleep(args.health_wait)
    code = process.poll()
    if code is not None:
        _cleanup_pid_files()
        print(f"digital-life failed to stay running, exit_code={code}")
        tail = _tail_log()
        if tail:
            print(tail)
        return code or 1

    suffix = f", api_port={api_port}" if api_port is not None else ""
    print(f"digital-life started: pid={process.pid}{suffix}")
    print(f"log={LOG_FILE}")
    return 0


def _stop(args: argparse.Namespace) -> int:
    pid = _read_pid()
    if not pid:
        print("digital-life is not running")
        _cleanup_pid_files()
        # 清理可能残留的 instance PID 文件
        _cleanup_instance_pid_files()
        return 0
    if not _is_running(pid):
        print(f"digital-life pid file was stale: pid={pid}")
        _cleanup_pid_files()
        _cleanup_instance_pid_files()
        return 0

    print(f"stopping digital-life: pid={pid}")
    if IS_WINDOWS:
        # Windows: 没有 killpg / SIGTERM / SIGKILL
        # 用 taskkill /T /PID 杀进程树（master + 所有子进程）
        try:
            subprocess.run(
                ["taskkill", "/T", "/PID", str(pid)],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            _cleanup_pid_files()
            _cleanup_instance_pid_files()
            return 0
        except PermissionError:
            os.kill(pid, signal.SIGTERM)

    # master 收到 SIGTERM 后会通过 InstanceSupervisor 优雅 stop 所有子进程，
    # 这可能需要 5-15 秒（每个实例 FeishuAdapter.stop + cron join）
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        if not _is_running(pid):
            _cleanup_pid_files()
            _cleanup_instance_pid_files()
            print("digital-life stopped")
            return 0
        time.sleep(0.25)

    if args.kill:
        if IS_WINDOWS:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, timeout=10,
                )
            except Exception:
                pass
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                os.kill(pid, signal.SIGKILL)
        _cleanup_pid_files()
        print("digital-life killed after timeout")
        return 0

    print("digital-life did not stop before timeout")
    return 1


def _status(_args: argparse.Namespace) -> int:
    pid = _read_pid()
    if not pid or not _is_running(pid):
        if pid:
            print(f"digital-life stopped (stale pid={pid})")
            _cleanup_pid_files()
        else:
            print("digital-life stopped")
        return 3

    api_port = None
    if META_FILE.exists():
        try:
            meta = json.loads(META_FILE.read_text())
            api_port = meta.get("api_port")
        except json.JSONDecodeError:
            pass
    suffix = f", api_port={api_port}" if api_port else ""
    print(f"digital-life running: pid={pid}{suffix}")
    print(f"log={LOG_FILE}")
    return 0


def _restart(args: argparse.Namespace) -> int:
    stop_args = argparse.Namespace(timeout=args.timeout, kill=True)
    _stop(stop_args)
    # Lark WS 后端需要 ~1-2 秒释放旧连接；多实例需要更长时间确保所有 bot WS 都释放
    time.sleep(2.5)
    start_args = argparse.Namespace(
        foreground=False,
        health_wait=args.health_wait,
        api_port=args.api_port,
    )
    return _start(start_args)


def _logs(args: argparse.Namespace) -> int:
    if args.follow:
        return subprocess.call(["tail", "-n", str(args.lines), "-f", str(LOG_FILE)])
    tail = _tail_log(args.lines)
    if tail:
        print(tail)
    else:
        print(f"log file does not exist yet: {LOG_FILE}")
    return 0


def _init(args: argparse.Namespace) -> int:
    """Bootstrap new Digital Life instances.

    无 --display-name → bootstrap 默认 zero + alpha（experience pack,体验用）
    有 --display-name <name> → 建 1 个自定义实例
    """
    if args.display_name:
        from infrastructure.bootstrap.instance import init_instance
        init_instance(args.display_name)
        print(f"✓ Instance '{args.display_name}' bootstrapped.")
        print("  下一步: digital-life start")
    else:
        # 默认 bootstrap zero + alpha
        from infrastructure.http.server import ensure_default_instances
        created = ensure_default_instances()
        if created:
            print(f"✓ 已 bootstrap {len(created)} 个实例: {', '.join(n for n, _ in created)}")
            print("  下一步: digital-life start")
        else:
            print("✓ 实例已存在,无需重新 bootstrap。要建新实例用 --display-name <name>")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="digital-life")
    subparsers = parser.add_subparsers(dest="command")

    start = subparsers.add_parser("start", help="Start the digital life runtime")
    # default=None: 不传时走代码默认 8642;可 shell export API_SERVER_PORT 覆盖
    # 固定端口,避免端口漂移导致的浏览器/cache/文档对不上 bug
    # 传 'auto' 仍可显式找空闲端口(开发场景); 数字显式锁定
    start.add_argument("--api-port", default=None, help="API server port (默认 8642 走 config); 'auto' 找空闲或显式数字")
    start.add_argument("--foreground", action="store_true", help="Run in foreground instead of background")
    start.add_argument("--health-wait", type=float, default=2.0, help="Seconds to wait for early startup failure")
    start.set_defaults(func=_start)

    stop = subparsers.add_parser("stop", help="Stop the digital life runtime")
    stop.add_argument("--timeout", type=float, default=20.0)
    stop.add_argument("--kill", action=argparse.BooleanOptionalAction, default=True)
    stop.set_defaults(func=_stop)

    restart = subparsers.add_parser("restart", help="Restart the digital life runtime")
    restart.add_argument("--api-port", default=None, help="API server port (默认 8642 走 config); 'auto' 找空闲或显式数字")
    restart.add_argument("--timeout", type=float, default=20.0)
    restart.add_argument("--health-wait", type=float, default=2.0)
    restart.set_defaults(func=_restart)

    status = subparsers.add_parser("status", help="Show runtime status")
    status.set_defaults(func=_status)

    logs = subparsers.add_parser("logs", help="Show runtime logs")
    logs.add_argument("-n", "--lines", type=int, default=80)
    logs.add_argument("-f", "--follow", action="store_true")
    logs.set_defaults(func=_logs)

    init_cmd = subparsers.add_parser("init", help="Bootstrap new Digital Life instances")
    init_cmd.add_argument("--display-name", default=None,
                         help="Custom instance name. 不传则 bootstrap 默认 zero + alpha")
    init_cmd.set_defaults(func=_init)

    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
