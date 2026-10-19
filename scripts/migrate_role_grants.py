#!/usr/bin/env python3
"""存量服务的岗位授予迁移（幂等）。

2026-10-16 工具可见性重构后，运行时只认授予库（data/tool_visibility.db），
不再读 services.tools_json。重构前创建的服务只有行、没有授予——本脚本对
每个服务行重跑 apply_role_face（行 tools → 定义层回退 → 全量，同一交集
数学），把岗位面补发为 source="role" 授予。重复执行安全（整行 upsert）。

用法（仓库根目录）：
  python3 scripts/migrate_role_grants.py            # 全部服务
  python3 scripts/migrate_role_grants.py --dry-run  # 只看不写
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from business.consulting.tool_face import apply_role_face, effective_role_tools
    from domain import tool_visibility as tv
    from infrastructure.persistence import services_registry as sr

    rows = sr.list_services()
    missing = [s for s in rows if "role" not in tv.durable_grants(s["service_id"])]
    print(f"服务行 {len(rows)} 个，其中缺岗位授予 {len(missing)} 个")
    for svc in rows:
        sid = svc["service_id"]
        eff = effective_role_tools(sid, svc.get("tools"))
        if args.dry_run:
            print(f"  {sid} ({svc.get('display_name') or '-'}): {len(eff)} 个协作工具")
            continue
        apply_role_face(sid, svc.get("tools"))
    if not args.dry_run:
        left = [s for s in rows if "role" not in tv.durable_grants(s["service_id"])]
        print(f"✅ 已重发 {len(rows)} 个服务的岗位授予；仍缺授予 {len(left)} 个")

    # PM 三件套补齐（2026-10-16 扩组：suspend/resume/complete 进授予组后不再
    # 人人可见；新模版 PM 角色已带全三件，存量项目的 PM 行多半只有 suspend）。
    from business.consulting import projects_registry

    pm_tools = ["suspend_project", "resume_project", "project_complete"]
    fixed = 0
    for prj in projects_registry.list_projects():
        pm = prj.get("pm_id") or ""
        svc = sr.lookup_service(pm) if pm else None
        if not svc:
            continue
        tools = svc.get("tools")
        if not (isinstance(tools, list) and tools):
            continue  # 未显式收紧 → 全量组，三件套已含
        missing = [t for t in pm_tools if t not in tools]
        if not missing:
            continue
        fixed += 1
        if args.dry_run:
            print(f"  PM {pm} ({svc.get('display_name') or '-'}) 缺 {missing}")
        else:
            apply_role_face(pm, tools + missing)
    print(f"{'将' if args.dry_run else '已'}补齐 PM 三件套: {fixed} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
