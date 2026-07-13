#!/usr/bin/env python3
"""飞书 App ID 修改不生效 —— 一键诊断脚本（只读不写）。

用法（仓库根目录）：
    python3 scripts/diagnose-feishu-config.py [instance_id]

    不带参数会跑所有实例。

它检查每个实例的：
  1. app.yaml 里的 channels.feishu.app_id（磁盘事实）
  2. ConfigCenter 读出的 origin（路由/middleware 链路的可见值）
  3. 外部进程 env / ContextVar 是否干扰
  4. 文件权限 / mtime（写过但没生效？）

输出会用 ✓ / ✗ / ⚠ 醒目标记。
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

# 让脚本能直接从仓库根跑
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

APPS = ROOT / "apps"


def color(s: str, code: int) -> str:
    if not sys.stdout.isatty():
        return s
    return f"\033[{code}m{s}\033[0m"


OK = lambda s: color(s, 32)    # green
WARN = lambda s: color(s, 33)  # yellow
BAD = lambda s: color(s, 31)   # red
DIM = lambda s: color(s, 90)   # grey


def read_yaml_value(yaml_path: Path, dotted: str) -> tuple[object, bool]:
    """(value, key_exists_in_file)"""
    if not yaml_path.exists():
        return "", False
    try:
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        return f"<parse-error: {e}>", False
    cur: object = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return "", False
        cur = cur[part]
    return cur, True


def diagnose(iid: str) -> None:
    print()
    print(color("=" * 70, 90))
    print(color(f"  实例 {iid}", 1))
    print(color("=" * 70, 90))

    inst_dir = APPS / iid
    if not inst_dir.is_dir():
        print(BAD(f"  ✗ 目录不存在: {inst_dir}"))
        return

    yaml_path = inst_dir / "config" / "app.yaml"
    env_path = inst_dir / "config" / "secrets.env"

    # ── 1. 磁盘事实 ──────────────────────────────────────────────
    print()
    print(color("【1. 磁盘事实】", 36))
    if not yaml_path.exists():
        print(BAD(f"  ✗ {yaml_path} 不存在 ← 这是 origin='default' 的根因"))
        print(DIM("    你在控制台看到的「默认配置」就是这个意思"))
    else:
        app_id, exists = read_yaml_value(yaml_path, "channels.feishu.app_id")
        if not exists:
            print(BAD("  ✗ app.yaml 存在，但没有 channels.feishu.app_id 这个 key"))
            print(DIM("    yaml 里缺这个字段 → ConfigCenter 返回 default=''"))
            print(DIM("    通常是手动编辑过 yaml 删掉/改了结构，或旧实例未跑迁移脚本"))
        else:
            masked = f"{app_id}".strip()
            if not masked:
                print(WARN("  ⚠ channels.feishu.app_id 存在但为空字符串"))
            else:
                print(OK(f"  ✓ channels.feishu.app_id = {masked}"))

    # ── 2. ConfigCenter 视角（程序读到的）──────────────────────
    print()
    print(color("【2. ConfigCenter 读到的值】", 36))
    try:
        os.environ.pop("DIGITAL_LIFE_INSTANCE_ID", None)
        from infrastructure.config import set_current_instance_id, reset_current_instance_id
        from application.console.config_center import ConfigCenterWorkflow
        token = set_current_instance_id(iid)
        try:
            r = ConfigCenterWorkflow().config(iid)
        finally:
            reset_current_instance_id(token)
        if r.status_code != 200:
            print(BAD(f"  ✗ config() 返回 {r.status_code}: {r.payload.get('error')}"))
        else:
            for s in r.payload["sections"]:
                if s["key"] != "feishu":
                    continue
                for f in s["fields"]:
                    val = "「留空=未配置」" if f["secret"] else repr(f["value"])
                    print(f"  {f['key']:30} value={val} origin={f['origin']:10} configured={f['configured']}")
                    if f["origin"] == "default":
                        print(BAD(f"     ↑ origin=default ← 这就是你说的「默认配置」状态"))
    except Exception as e:
        print(BAD(f"  ✗ 调 ConfigCenter 异常: {e}"))

    # ── 3. 进程 env 污染检测 ─────────────────────────────────────
    print()
    print(color("【3. 进程级 ENV 污染检测】", 36))
    suspicious = [k for k in (
        "DIGITAL_LIFE_INSTANCE_ID",
        "L4_AGENT_ID",
        "DIGITAL_LIFE_EMPLOYEE_ID",
    ) if os.environ.get(k)]
    if suspicious:
        print(WARN(f"  ⚠ 当前 shell 设了 {suspicious}"))
        print(DIM("    如果是后台进程跑的，这会让 ConfigCenter 走偏到别的实例"))
    else:
        print(OK("  ✓ 当前 shell 无 instance env，ConfigCenter 用传入的 employee_id 直定位文件"))

    # ── 4. 文件权限 / mtime ─────────────────────────────────────
    print()
    print(color("【4. 文件权限 / 时间戳】", 36))
    if yaml_path.exists():
        st = yaml_path.stat()
        mode = stat.filemode(st.st_mode)
        writable = os.access(yaml_path, os.W_OK)
        import datetime
        mtime = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        perm_str = OK(mode) if writable else BAD(f"{mode} (不可写!)")
        print(f"  {yaml_path.name:12} {perm_str}  mtime={mtime}  writable={writable}")
        if not writable:
            print(BAD("     ↑ 进程没写权限 → 保存会 200 但磁盘不变"))
    if env_path.exists():
        st = env_path.stat()
        writable = os.access(env_path, os.W_OK)
        mode = stat.filemode(st.st_mode)
        perm_str = OK(mode) if writable else BAD(f"{mode} (不可写!)")
        print(f"  secrets.env    {perm_str}  writable={writable}")


def discover_instances() -> list[str]:
    """所有有 config/app.yaml 的实例目录。"""
    out = []
    for entry in sorted(APPS.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if (entry / "config" / "app.yaml").exists():
            out.append(entry.name)
    return out


def main() -> int:
    if not APPS.exists():
        print(BAD(f"apps/ 目录不存在: {APPS}"))
        return 1

    if len(sys.argv) > 1:
        diagnose(sys.argv[1])
    else:
        instances = discover_instances()
        if not instances:
            print(BAD("没找到任何实例（apps/*/config/app.yaml 都不存在）"))
            return 1
        for iid in instances:
            diagnose(iid)

    print()
    print(color("=" * 70, 90))
    print(color("  诊断完成。把上面的输出贴给我就行。", 1))
    print(color("=" * 70, 90))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
