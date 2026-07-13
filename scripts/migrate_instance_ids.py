#!/usr/bin/env python3
"""Migrate existing instance directories from name-based to UUID-based.

Usage:
    python scripts/migrate_instance_ids.py
    python scripts/migrate_instance_ids.py --dry-run
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import uuid as _uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

APPS_DIR = ROOT / "apps"
REGISTRY_PATH = APPS_DIR / "instances.yaml"


def _legacy_instances() -> list[str]:
    """Find legacy instances (non-UUID dirs with persona/ subdirectory)."""
    if not APPS_DIR.exists():
        return []
    result = []
    for entry in sorted(APPS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name == "instances.yaml" or entry.name.startswith("."):
            continue
        # Skip UUID-format directories (already migrated)
        if len(entry.name) == 36 and entry.name.count("-") == 4:
            continue
        if (entry / "persona").is_dir():
            result.append(entry.name)
    return result


def _stop_gateway() -> bool:
    """Try to stop the gateway. Returns True if stopped or not running."""
    pid_file = ROOT / "var" / "run" / "digital-life.pid"
    # Also check the instance-scoped PID file
    alt_pid_files = []
    apps_dir = ROOT / "apps"
    if apps_dir.exists():
        for entry in apps_dir.iterdir():
            candidate = entry / "data" / "run" / "digital-life.pid"
            if candidate.exists():
                alt_pid_files.append(candidate)

    all_pid_files = [pid_file] + alt_pid_files
    running_pid = None
    for pf in all_pid_files:
        if pf.exists():
            try:
                pid = int(pf.read_text().strip())
                os.kill(pid, 0)
                running_pid = pid
                break
            except (ProcessLookupError, PermissionError, FileNotFoundError, ValueError):
                pass

    if running_pid is None:
        return True

    print(f"Gateway 正在运行 (PID={running_pid})，请先手动停止:")
    print(f"  digital-life stop")
    return False


def migrate(dry_run: bool = False) -> dict[str, str]:
    """Migrate legacy instances. Returns {legacy_name: uuid} mapping."""
    import yaml

    legacy = _legacy_instances()
    if not legacy:
        print("没有需要迁移的旧实例。")
        return {}

    # Load existing registry (if any)
    registry = {}
    if REGISTRY_PATH.exists():
        data = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8")) or {}
        registry = data.get("instances", {}) if isinstance(data, dict) else {}

    mapping: dict[str, str] = {}
    for name in legacy:
        # Check if already migrated
        already = None
        for uuid_key, meta in registry.items():
            if meta.get("legacy_name") == name or meta.get("display_name") == name:
                already = uuid_key
                break

        if already:
            print(f"  ✓ {name} 已迁移 → {already}")
            mapping[name] = already
            continue

        instance_uuid = str(_uuid.uuid4())
        mapping[name] = instance_uuid

        old_dir = APPS_DIR / name
        new_dir = APPS_DIR / instance_uuid

        print(f"\n迁移实例: {name} → {instance_uuid}")
        print(f"  旧目录: {old_dir}")
        print(f"  新目录: {new_dir}")

        if new_dir.exists():
            raise SystemExit(f"  ✗ 目标目录已存在: {new_dir}")

        if dry_run:
            print(f"  [DRY RUN] 将重命名 {old_dir} → {new_dir}")
        else:
            old_dir.rename(new_dir)
            print(f"  ✓ 目录已重命名")

        registry[instance_uuid] = {
            "display_name": name,
            "legacy_name": name,
            "created_at": "",
        }
        print(f"  ✓ 已加入注册表 (display_name={name})")

    if not dry_run:
        REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {"instances": registry}
        tmp = REGISTRY_PATH.with_suffix(".yaml.tmp")
        tmp.write_text(
            yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        tmp.replace(REGISTRY_PATH)
        print(f"\n✓ 注册表已写入: {REGISTRY_PATH}")

    return mapping


def verify(mapping: dict[str, str]) -> bool:
    """Verify all expected files exist after migration."""
    all_ok = True
    for name, uuid_str in mapping.items():
        new_dir = APPS_DIR / uuid_str
        checks = [
            (new_dir / "persona").is_dir(),
            (new_dir / "data").is_dir(),
            (new_dir / "config").is_dir(),
        ]
        if not all(checks):
            print(f"  ✗ {name} ({uuid_str}): 目录结构不完整")
            all_ok = False
        else:
            # Verify data files
            data_dir = new_dir / "data"
            if (data_dir / "state.db").exists():
                size = (data_dir / "state.db").stat().st_size
                print(f"  ✓ {name} → {uuid_str} (state.db: {size:,} bytes)")
            else:
                print(f"  ✓ {name} → {uuid_str}")
    return all_ok


def main() -> int:
    parser = argparse.ArgumentParser(description="迁移旧实例到 UUID 目录")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不实际执行")
    args = parser.parse_args()

    if args.dry_run:
        print("=== DRY RUN 模式，不会实际修改文件 ===\n")

    legacy = _legacy_instances()
    if not legacy:
        print("没有需要迁移的旧实例。")
        return 0

    print(f"发现 {len(legacy)} 个旧实例: {legacy}\n")

    if not args.dry_run:
        if not _stop_gateway():
            print("\n请先手动停止 gateway: digital-life stop")
            return 1

    mapping = migrate(dry_run=args.dry_run)

    if not args.dry_run and mapping:
        print("\n--- 验证 ---")
        ok = verify(mapping)
        if ok:
            print("\n✅ 迁移完成！")
            print("\n旧名称 → 新 UUID 映射:")
            for name, uuid_str in mapping.items():
                print(f"  {name} → {uuid_str}")
            print(f"\n现在可以重启 gateway: digital-life restart")
        else:
            print("\n⚠ 验证发现问题，请检查目录结构")
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
