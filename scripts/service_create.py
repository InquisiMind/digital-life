#!/usr/bin/env python3
"""服务创建与管理 CLI — 服务型改造（设计文档 v1.1）刀 1 的手工验收入口。

用法：
  # 1. 一次性引导 echo 验收定义（apps/echo-def/，已存在则跳过）
  python3 scripts/service_create.py bootstrap-echo

  # 2. 从定义创建服务（打印 service_id，目录骨架同步生成）
  python3 scripts/service_create.py create --def echo-def [--name 客服A] [--project proj_x]

  # 3. 投一条消息进服务队列（等价"客户发消息"，调度循环会拉起 worker）
  python3 scripts/service_create.py send <service_id> "你好"

  # 4. 归档 / 恢复 / 列表
  python3 scripts/service_create.py archive <service_id>
  python3 scripts/service_create.py activate <service_id>
  python3 scripts/service_create.py list [--all]

验收剧本（刀 1）：bootstrap-echo → create → send（master 在跑时 ~10s 内
spawn worker，日志在 apps/{def}/services/{sid}/data/var/logs/）→ archive →
再 send（应 DROPPED reason=archived，不再拉起）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from infrastructure.config import get_project_root  # noqa: E402

ECHO_APP_YAML = """\
# echo 服务定义 — 刀 1 验收用最小 agent 定义
# runtime_kind: definition 是关键标记：发现/注册表/supervisor 都不会把它
# 当实例拉起；它的运行体是 services 注册表里的服务。
runtime_kind: definition
active: false
display_name: echo-def
model:
  name: glm-5.3
  provider: glm
  base_url: https://open.bigmodel.cn/api/coding/paas/v4
channels: {}             # 服务型默认无 IM 通道（web 通道见刀 3）
skills: []               # 定义层技能按需追加，同定义的所有服务共享
"""

ECHO_PERSONA = """\
# Echo 服务

你是一个回声服务，用于验证服务型运行时的隔离与生命周期。

规则：
- 收到任何消息，原样复述一遍，并在开头加上 "[echo]"。
- 不使用任何工具，不发起主动唤醒，不做总结归档。
- 用一句话回复，不要展开。
"""

ECHO_SECRETS = """\
# echo 定义的密钥（同定义的所有服务共享本文件）。
# 留空则继承 shell 环境的 GLM_API_KEY；或取消注释填入：
# GLM_API_KEY=
"""


def _cmd_bootstrap_echo(_args: argparse.Namespace) -> int:
    def_dir = get_project_root() / "apps" / "echo-def"
    cfg = def_dir / "config" / "app.yaml"
    if cfg.exists():
        print(f"✅ echo 定义已存在: {cfg}")
        return 0
    (def_dir / "config").mkdir(parents=True, exist_ok=True)
    (def_dir / "persona").mkdir(parents=True, exist_ok=True)
    (def_dir / "skills").mkdir(parents=True, exist_ok=True)
    (def_dir / "config" / "app.yaml").write_text(ECHO_APP_YAML, encoding="utf-8")
    (def_dir / "config" / "secrets.env").write_text(ECHO_SECRETS, encoding="utf-8")
    (def_dir / "persona" / "LIFE_PERSONA.md").write_text(ECHO_PERSONA, encoding="utf-8")
    print(f"✅ echo 定义已创建: {def_dir}")
    return 0


def _cmd_create(args: argparse.Namespace) -> int:
    from domain.service import create_service as domain_create

    svc = domain_create(
        args.def_id,
        service_id=args.id or None,
        project_id=args.project or "",
        display_name=args.name or "",
    )
    print(f"✅ 服务已创建: {svc['service_id']}  (def={svc['agent_def_id']}, status={svc['status']})")
    return 0


def _cmd_send(args: argparse.Namespace) -> int:
    from domain.service import emit_to_service

    event_id = emit_to_service(args.service_id, "message", {"text": args.text})
    if event_id:
        print(f"✅ 事件已入队: event_id={event_id}（调度循环 ~10s 内拉起 worker）")
        return 0
    print("❌ 事件被拒（未注册 / 已归档 / 未订阅）——见 master 日志 SERVICE_EVENT_DROPPED")
    return 1


def _cmd_archive(args: argparse.Namespace) -> int:
    from domain.service import archive_service

    print("✅ 已归档" if archive_service(args.service_id) else "❌ 服务不存在")
    return 0


def _cmd_activate(args: argparse.Namespace) -> int:
    from domain.service import activate_service

    print("✅ 已恢复" if activate_service(args.service_id) else "❌ 服务不存在")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    from domain.service import list_services

    rows = list_services(status=None if args.all else "active")
    if not rows:
        print("（空）")
        return 0
    for r in rows:
        print(
            f"{r['service_id']}  def={r['agent_def_id']}  status={r['status']}"
            f"  project={r.get('project_id') or '-'}  subs={r.get('subscriptions')}"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("bootstrap-echo", help="创建 echo 验收定义（apps/echo-def/）").set_defaults(
        func=_cmd_bootstrap_echo
    )

    p_create = sub.add_parser("create", help="从定义创建服务")
    p_create.add_argument("--def", dest="def_id", required=True, help="agent 定义目录名")
    p_create.add_argument("--name", default="", help="服务显示名")
    p_create.add_argument("--project", default="", help="所属项目 ID（可空）")
    p_create.add_argument("--id", default="", help="指定 service_id（缺省自动生成）")
    p_create.set_defaults(func=_cmd_create)

    p_send = sub.add_parser("send", help="向服务投一条 message 事件")
    p_send.add_argument("service_id")
    p_send.add_argument("text")
    p_send.set_defaults(func=_cmd_send)

    p_arch = sub.add_parser("archive", help="归档服务（开关关）")
    p_arch.add_argument("service_id")
    p_arch.set_defaults(func=_cmd_archive)

    p_act = sub.add_parser("activate", help="恢复服务")
    p_act.add_argument("service_id")
    p_act.set_defaults(func=_cmd_activate)

    p_list = sub.add_parser("list", help="列出服务")
    p_list.add_argument("--all", action="store_true", help="含已归档")
    p_list.set_defaults(func=_cmd_list)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
