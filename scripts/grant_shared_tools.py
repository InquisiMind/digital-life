#!/usr/bin/env python3
"""给实例授予 shared 层自注册工具（存量迁移 / 显式启用 / 收回）。

shared 工具（agent 用 register_tool 以 scope=shared 注册的）自 2026-10-16 起
从所有默认面排除，仅经授予可见——"全实例可选启用"按字面落实。注册者注册时
自动获授予；存量工具（如 zero/alpha 写的 web_search/web_fetch/stock_quote）
用本脚本补授予给作者。

用法（在仓库根目录）：
  python3 scripts/grant_shared_tools.py --list                    # 看 shared 清单与现有授予
  python3 scripts/grant_shared_tools.py zero alpha                # 授予全部 shared 工具
  python3 scripts/grant_shared_tools.py zero --tools web_search web_fetch
  python3 scripts/grant_shared_tools.py 小王 --revoke             # 收回全部

实例可用显示名（zero / alpha / 小王…）或 uuid。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MANIFEST = ROOT / "shared" / "tools" / "tools_manifest.json"


def _shared_items() -> list[dict]:
    if not MANIFEST.exists():
        return []
    data = json.loads(MANIFEST.read_text(encoding="utf-8") or "{}")
    return list(data.get("items") or [])


def _resolve_instance(ref: str) -> str:
    """显示名 → uuid；本身是已注册 uuid 则原样返回。"""
    from infrastructure.config import _load_registry

    reg = _load_registry()
    if ref in reg:
        return ref
    hits = [iid for iid, meta in reg.items() if (meta.get("display_name") or "") == ref]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise SystemExit(f"找不到实例：{ref}（既不是 uuid 也不是唯一显示名）")
    raise SystemExit(f"显示名 {ref} 命中多个实例：{hits}，请用 uuid")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("instances", nargs="*", help="实例显示名或 uuid")
    ap.add_argument("--tools", nargs="*", help="只处理这些工具（manifest 里的裸名，不带 app_）")
    ap.add_argument("--revoke", action="store_true", help="收回而非授予")
    ap.add_argument("--list", action="store_true", help="列出 shared 工具与现有授予")
    args = ap.parse_args()

    items = _shared_items()
    names = [i["name"] for i in items if i.get("name")]
    if args.tools:
        unknown = set(args.tools) - set(names)
        if unknown:
            raise SystemExit(f"manifest 里没有这些工具：{sorted(unknown)}")
        names = [n for n in names if n in set(args.tools)]

    from domain import tool_visibility as tv

    if args.list or not args.instances:
        print(f"shared 工具（{MANIFEST.relative_to(ROOT)}）：")
        for n in names:
            print(f"  - app_{n}")
        from infrastructure.config import _load_registry

        print("\n现有授予（source=shared_tool:*）：")
        for iid, meta in _load_registry().items():
            grants = {s: t for s, t in tv.durable_grants(iid).items() if s.startswith("shared_tool:")}
            if grants:
                print(f"  {meta.get('display_name') or iid} ({iid[:8]}): {sorted(x for ts in grants.values() for x in ts)}")
        return 0

    for ref in args.instances:
        iid = _resolve_instance(ref)
        for n in names:
            source = f"shared_tool:{n}"
            if args.revoke:
                tv.revoke(iid, source=source)
            else:
                tv.grant(iid, [f"app_{n}"], source=source)
        verb = "收回" if args.revoke else "授予"
        print(f"✅ {ref} ({iid[:8]}) {verb}: {[f'app_{n}' for n in names]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
