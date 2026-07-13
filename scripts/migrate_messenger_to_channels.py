#!/usr/bin/env python3
"""一次性迁移：messenger 段 → channels.<type> 段（向后兼容收敛）。

背景：
  - 历史上飞书/微信通道字段散落在两个段：``messenger`` 与 ``channels.<type>``。
  - 飞书 App ID 路径不一致曾导致「控制台改了不生效」的 bug（GitHub issue）。
  - 修复后所有读写统一到 ``channels.<type>.<field>``，``messenger`` 段作废。

用法（仓库根目录）：
    # 默认 dry-run：只打印变更，不写盘
    python3 scripts/migrate_messenger_to_channels.py

    # 实际写盘（自动备份 .bak）
    python3 scripts/migrate_messenger_to_channels.py --apply

    # 单个实例
    python3 scripts/migrate_messenger_to_channels.py <instance_id> --apply

幂等：脚本可重复运行；``messenger`` 段不存在时 nothing to do。
占位 ``cli_xxxxxxxxx``（init_instance 模板默认值）会被 clean：若
``channels.feishu.app_id`` 是占位但 ``messenger.app_id`` 是真值，用真值覆盖。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# init_instance 模板里的占位 App ID —— 不算真实配置，迁移时若 channels.feishu.app_id
# 是占位、messenger 段有真值，用真值覆盖占位。
PLACEHOLDER_APP_IDS = {"cli_xxxxxxxxx", "cli_xxxxxxxxxxxxx"}

# messenger 段 → channels.<type>. 字段映射。
# 表头隐含的 ``type`` 默认是 ``feishu``（与历史 messenger.type 默认一致）。
MESSENGER_DEFAULT_TYPE = "feishu"


@dataclass
class MigrationReport:
    """单实例迁移结果（可序列化、可断言）。"""

    instance_id: str
    changed: bool = False
    backed_up: bool = False
    migrated_fields: list[str] = field(default_factory=list)
    skipped_fields: list[str] = field(default_factory=list)
    placeholders_cleaned: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.changed:
            return f"[{self.instance_id[:8]}] ⚠ skip: messenger 段不存在，无需迁移"
        parts = [f"[{self.instance_id[:8]}] ✓ migrated"]
        if self.migrated_fields:
            parts.append("  写入: " + ", ".join(self.migrated_fields))
        if self.placeholders_cleaned:
            parts.append("  清理占位: " + ", ".join(self.placeholders_cleaned))
        if self.skipped_fields:
            parts.append("  跳过(已存在真实值): " + ", ".join(self.skipped_fields))
        if self.warnings:
            parts.extend(f"  ⚠ {w}" for w in self.warnings)
        return "\n".join(parts)


def migrate_yaml(raw_cfg: dict[str, Any]) -> tuple[dict[str, Any], MigrationReport]:
    """把 ``messenger`` 段迁移到 ``channels.<type>``。

    纯函数：返回新 dict（不修改入参）+ report。CLI 层负责磁盘 IO。

    迁移规则（每个字段）：
      - messenger 段不存在或缺字段 → skip
      - channels.<type> 已存在同名字段且**非占位** → skip（不覆盖用户手配）
      - channels.<type> 已存在同名字段且为占位 → 用 messenger 真值覆盖
      - 其它 → 写入

    完成后删除 ``messenger`` 段。
    """
    report = MigrationReport(instance_id="<in-memory>")
    messenger = raw_cfg.get("messenger")
    if not isinstance(messenger, dict) or not messenger:
        new_cfg = dict(raw_cfg)
        return new_cfg, report

    new_cfg = {k: v for k, v in raw_cfg.items() if k != "messenger"}
    # channels 段要保证是嵌套 dict，复制一份避免修改入参的子对象
    channels_in = raw_cfg.get("channels") or {}
    if not isinstance(channels_in, dict):
        report.warnings.append("channels 段非 dict，已重置为空 dict")
        channels_in = {}
    # 深拷贝一层（迁移只在本层修改）
    channels: dict[str, Any] = {
        name: dict(cfg) if isinstance(cfg, dict) else cfg
        for name, cfg in channels_in.items()
    }

    msgr_type = str(messenger.get("type") or MESSENGER_DEFAULT_TYPE)
    target = channels.get(msgr_type)
    if not isinstance(target, dict):
        target = {"type": msgr_type}
        channels[msgr_type] = target
    elif "type" not in target:
        target["type"] = msgr_type

    def _is_placeholder_app_id(value: Any) -> bool:
        return isinstance(value, str) and value.strip() in PLACEHOLDER_APP_IDS

    # ── app_id ──
    msgr_app_id = messenger.get("app_id")
    if isinstance(msgr_app_id, str) and msgr_app_id.strip():
        existing = target.get("app_id")
        if existing is None:
            target["app_id"] = msgr_app_id
            report.migrated_fields.append(f"channels.{msgr_type}.app_id")
        elif _is_placeholder_app_id(existing):
            target["app_id"] = msgr_app_id
            report.placeholders_cleaned.append(
                f"channels.{msgr_type}.app_id 占位→真值"
            )
        elif existing == msgr_app_id:
            report.skipped_fields.append(f"channels.{msgr_type}.app_id (值一致)")
        else:
            report.skipped_fields.append(
                f"channels.{msgr_type}.app_id (保留已有真值，未覆盖 messenger)"
            )
            report.warnings.append(
                f"channels.{msgr_type}.app_id 与 messenger.app_id 冲突，"
                "迁移默认信任 channels（已生效路径），请人工核对"
            )

    # ── feishu_domain ── (飞书域名；仅 type=feishu 有意义)
    msgr_domain = messenger.get("feishu_domain")
    if isinstance(msgr_domain, str) and msgr_domain.strip():
        key = "feishu_domain"
        if target.get(key) is None:
            target[key] = msgr_domain
            report.migrated_fields.append(f"channels.{msgr_type}.{key}")
        elif target.get(key) == msgr_domain:
            report.skipped_fields.append(f"channels.{msgr_type}.{key} (值一致)")
        else:
            report.skipped_fields.append(f"channels.{msgr_type}.{key} (保留已有值)")

    # ── chat_ids ──
    msgr_chat_ids = messenger.get("chat_ids")
    if msgr_chat_ids:
        if isinstance(msgr_chat_ids, list):
            existing = target.get("chat_ids")
            if not existing:
                target["chat_ids"] = list(msgr_chat_ids)
                report.migrated_fields.append(f"channels.{msgr_type}.chat_ids")
            elif existing == msgr_chat_ids:
                report.skipped_fields.append(
                    f"channels.{msgr_type}.chat_ids (值一致)"
                )
            else:
                # 合并去重：channels 已有优先，补入 messenger 独有的
                merged = list(existing) + [
                    c for c in msgr_chat_ids if c not in existing
                ]
                target["chat_ids"] = merged
                report.migrated_fields.append(
                    f"channels.{msgr_type}.chat_ids (合并 {len(merged) - len(existing)} 条)"
                )
        else:
            report.warnings.append(
                f"messenger.chat_ids 非 list，跳过：{type(msgr_chat_ids).__name__}"
            )

    # ── wechat_domain ── 特殊：历史上 messenger.wechat_domain 实际写入路径应迁到
    # channels.wechat.domain (字段名也变化)
    msgr_wechat_domain = messenger.get("wechat_domain")
    if isinstance(msgr_wechat_domain, str) and msgr_wechat_domain.strip():
        wechat = channels.get("wechat")
        if not isinstance(wechat, dict):
            wechat = {"type": "wechat_clawbot"}
            channels["wechat"] = wechat
        elif "type" not in wechat:
            wechat["type"] = "wechat_clawbot"
        if wechat.get("domain") is None:
            wechat["domain"] = msgr_wechat_domain
            report.migrated_fields.append("channels.wechat.domain")
        elif wechat.get("domain") == msgr_wechat_domain:
            report.skipped_fields.append("channels.wechat.domain (值一致)")
        else:
            report.skipped_fields.append("channels.wechat.domain (保留已有值)")

    new_cfg["channels"] = channels
    report.changed = True
    return new_cfg, report


def _discover_instances(apps_dir: Path) -> list[Path]:
    return sorted(
        p for p in apps_dir.iterdir()
        if p.is_dir() and (p / "config" / "app.yaml").is_file()
    )


def migrate_instance(
    instance_dir: Path,
    *,
    apply: bool,
) -> MigrationReport:
    """迁移单个实例（apps/<id>）。读取 + （可选）写盘 + 备份。"""
    iid = instance_dir.name
    yaml_path = instance_dir / "config" / "app.yaml"
    report = MigrationReport(instance_id=iid)

    if not yaml_path.is_file():
        report.warnings.append(f"app.yaml 不存在: {yaml_path}")
        return report

    try:
        raw_cfg = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        report.warnings.append(f"yaml 解析失败: {exc}")
        return report

    if not isinstance(raw_cfg, dict):
        report.warnings.append("app.yaml 顶层非 dict，跳过")
        return report

    new_cfg, new_report = migrate_yaml(raw_cfg)
    # 把纯函数 report 的内容并入磁盘 report
    report.changed = new_report.changed
    report.migrated_fields = new_report.migrated_fields
    report.skipped_fields = new_report.skipped_fields
    report.placeholders_cleaned = new_report.placeholders_cleaned
    report.warnings.extend(new_report.warnings)
    if not report.changed:
        return report

    if not apply:
        return report

    # 备份
    backup = yaml_path.with_suffix(".yaml.bak")
    if not backup.exists():
        shutil.copy2(yaml_path, backup)
        report.backed_up = True
    # 写盘
    yaml_path.write_text(
        yaml.safe_dump(new_cfg, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "instance_id", nargs="?", default=None,
        help="可选：单个实例 ID；不传则跑 apps/ 下所有实例",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="实际写盘（默认 dry-run，不修改文件）",
    )
    parser.add_argument(
        "--apps-dir", default=None,
        help="apps 目录（默认仓库根目录的 apps/，主要供测试用）",
    )
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parent.parent
    apps_dir = Path(args.apps_dir) if args.apps_dir else root / "apps"
    if not apps_dir.is_dir():
        print(f"✗ apps/ 目录不存在: {apps_dir}", file=sys.stderr)
        return 1

    if args.instance_id:
        targets = [apps_dir / args.instance_id]
        if not targets[0].is_dir():
            print(f"✗ 实例不存在: {targets[0]}", file=sys.stderr)
            return 1
    else:
        targets = _discover_instances(apps_dir)
        if not targets:
            print("⚠ 没找到任何实例", file=sys.stderr)
            return 1

    print(
        f"模式: {'APPLY (写盘)' if args.apply else 'DRY-RUN (只打印)'}  "
        f"实例数: {len(targets)}\n"
    )
    for inst_dir in targets:
        report = migrate_instance(inst_dir, apply=args.apply)
        print(report.summary())
        if report.backed_up:
            print(f"  备份: {(inst_dir / 'config' / 'app.yaml.bak')}")

    if not args.apply:
        print(
            "\n这是 DRY-RUN。确认无误后加 --apply 实际写盘（自动备份 .bak）。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
