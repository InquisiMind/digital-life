"""Gateway 入口 — 支持 master / instance / service 三种角色。

启动模式：
  1. master 模式（默认）：HTTP server + InstanceSupervisor（管理子进程）
     + 服务调度循环（扫服务队列 → 抢 lease → spawn 服务 worker）
  2. instance 模式（--instance <uuid>）：只跑这一个实例的 FeishuAdapter + Cron
  3. service 模式（--service <sid>）：一次性 worker，处理该服务到期事件后退出

master 不连飞书 WS、不跑 cron；instance 不起 HTTP server。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# 修复 SSL 证书：Claude Code 的 traffic-proxy 根 CA 会破坏真实 HTTPS 连接
# 必须在所有使用 SSL 的 import（lark_oapi、httpx 等）之前执行
_ssl_file = os.environ.get("SSL_CERT_FILE", "")
if "acp-traffic-proxy" in _ssl_file or not _ssl_file:
    import certifi
    os.environ["SSL_CERT_FILE"] = certifi.where()
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())

# 绕过本地流量代理，让飞书和智谱 API 直连外网
_no_proxy = os.environ.get("NO_PROXY", "")
_proxy_bypass = "open.feishu.cn,*.feishu.cn,open.bigmodel.cn,*.bigmodel.cn"
if _no_proxy:
    os.environ["NO_PROXY"] = f"{_no_proxy},{_proxy_bypass}"
    os.environ["no_proxy"] = os.environ["NO_PROXY"]
else:
    os.environ["NO_PROXY"] = _proxy_bypass
    os.environ["no_proxy"] = _proxy_bypass

# 将项目根目录加入 sys.path，确保所有模块可被 import
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from infrastructure.config import get_runtime_config_path, get_runtime_home


# Master/Supervisor 的 PID 文件统一在项目根 var/run/，
# 不能用 get_runtime_home() —— 那是 per-instance 路径（apps/<id>/data/）
def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _var_run_dir() -> Path:
    return _project_root() / "var" / "run"


def _setup_logging(instance_id: str = "") -> None:
    config_path = get_runtime_config_path()
    try:
        from infrastructure.ai import load_runtime_config
        cfg = load_runtime_config(config_path)
        log_level = cfg.get("logging", {}).get("level", "INFO")
    except Exception:
        log_level = "INFO"

    instance_tag = instance_id or "master"
    base_format = f"%(asctime)s [{instance_tag}] [%(name)s] %(levelname)s %(message)s"
    formatter = logging.Formatter(base_format, datefmt="%Y-%m-%d %H:%M:%S")

    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    # 清掉默认 handlers（basicConfig 留下的）
    root.handlers.clear()

    # 1) stdout — 给 supervisor 重定向用
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    # 2) 主日志文件 — 每 1 天 rotate，保留 1 份历史（共 2 天：今天 + 昨天）
    # 路径必须显式按 instance_id 解析：
    #   - instance 子进程 → apps/{id}/data/var/logs/digital-life.log（per-instance 审计）
    #   - master 进程     → 项目根 var/logs/digital-life.log（项目级审计）
    # 不能用 get_runtime_home()：它走 ContextVar/env fallback 到 registry 第一个（=c2a5c8e8），
    # 结果 3 个子进程的 logger 全绑到 zero 的文件 —— alpha/beta 的实例日志自启动起从未被打开。
    # master 必须避开 per-instance 路径（和 _var_run_dir / PID 文件那条同款姿势）。
    try:
        from logging.handlers import TimedRotatingFileHandler
        from infrastructure.config import get_instance_data_dir
        if instance_id:
            data_dir = get_instance_data_dir(instance_id)
            log_dir = data_dir / "var" / "logs"
        else:
            log_dir = _project_root() / "var" / "logs"
        log_path = log_dir / "digital-life.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = TimedRotatingFileHandler(
            str(log_path),
            when="midnight",      # 每天 0 点 UTC rotate
            interval=1,           # 每天 1 次
            backupCount=1,        # 保留 1 份历史（昨天）= 总共 2 天
            encoding="utf-8",
            utc=True,
        )
        file_handler.setFormatter(formatter)
        file_handler.suffix = "%Y-%m-%d.log"
        root.addHandler(file_handler)
    except Exception:
        # fall back: 仅 stdout
        pass


def _load_dotenv() -> None:
    runtime_home = get_runtime_home()
    config_path = get_runtime_config_path()
    try:
        from infrastructure.ai import load_runtime_dotenv
        load_runtime_dotenv(runtime_home=runtime_home, project_env=config_path.parent / ".env")
    except Exception as exc:
        logging.warning("Failed to load runtime .env: %s", exc)


def _write_pid(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()))


def _check_pid_alive(pid_file: Path) -> bool:
    """Return True if the pid file points to a running process."""
    try:
        existing = int(pid_file.read_text().strip())
        if existing and existing != os.getpid():
            os.kill(existing, 0)
            return True
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        pass
    return False


def run_master() -> None:
    """master 进程：HTTP server + InstanceSupervisor。"""
    import asyncio
    from infrastructure.http.server import run_master_gateway

    master_pid = _var_run_dir() / "digital-life-master.pid"
    legacy_pid = _var_run_dir() / "digital-life.pid"  # 兼容旧 CLI
    if _check_pid_alive(master_pid) or _check_pid_alive(legacy_pid):
        existing = master_pid.read_text().strip() if master_pid.exists() else legacy_pid.read_text().strip()
        print(f"Master already running: pid={existing}")
        return
    _write_pid(master_pid)
    _write_pid(legacy_pid)

    logger = logging.getLogger("gateway.master")
    logger.info("Master gateway starting...")
    try:
        asyncio.run(run_master_gateway())
    finally:
        try:
            master_pid.unlink(missing_ok=True)
            legacy_pid.unlink(missing_ok=True)
        except OSError:
            pass


def run_instance(instance_id: str) -> None:
    """instance 子进程：只跑指定实例的 FeishuAdapter + Cron。"""
    import asyncio
    from infrastructure.http.server import run_instance_gateway

    pid_file = _var_run_dir() / f"digital-life-{instance_id}.pid"
    if _check_pid_alive(pid_file):
        print(f"Instance {instance_id} already running: pid={pid_file.read_text().strip()}")
        return
    _write_pid(pid_file)

    logger = logging.getLogger(f"gateway.instance.{instance_id[:8]}")
    logger.info("Instance gateway starting...")
    try:
        asyncio.run(run_instance_gateway(instance_id))
    finally:
        try:
            pid_file.unlink(missing_ok=True)
        except OSError:
            pass


def run_service(service_id: str) -> None:
    """服务 worker 子进程：处理该服务的到期事件后退出（ephemeral，不留常驻）。"""
    from gateway.service_worker import run_service_worker

    code = run_service_worker(service_id)
    if code:
        raise SystemExit(code)


def main() -> None:
    parser = argparse.ArgumentParser(description="Digital Life gateway")
    parser.add_argument("--instance", default="", help="实例 UUID（不传则跑 master）")
    parser.add_argument("--service", default="", help="服务 ID（一次性 worker：处理到期事件后退出）")
    args = parser.parse_args()

    _load_dotenv()
    _setup_logging(instance_id=args.service or args.instance)

    if args.service:
        run_service(args.service)
    elif args.instance:
        run_instance(args.instance)
    else:
        run_master()


if __name__ == "__main__":
    main()
