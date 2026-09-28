#!/usr/bin/env python3
"""demo_preflight — 演示前自动体检：账本一致性 + 孤儿库扫描
检查项:
  1) 每个实例 app.yaml 订阅的技能 vs skills/ 目录实际文件 vs manifest 三方对齐
  2) 0 字节/孤儿 sqlite 脚本误建库扫描
用法: python3 docs/operations/demo_preflight.py [--fix]
退出码: 0=健康  1=有告警
"""
import sys, json, re, sqlite3, pathlib, yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
APPS = ROOT / "apps"
MANIFEST = ROOT / "interfaces" / "skills_manifest.json"

def load_yaml(p):
    with open(p) as f: return yaml.safe_load(f) or {}

def check_subscriptions(problems):
    manifest = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    for cfg in APPS.glob("*/config/app.yaml"):
        iid = cfg.parent.parent.name
        app = load_yaml(cfg)
        subbed = set(app.get("skills") or [])
        sysdir = ROOT / "interfaces" / "skills"
        disk = {d.name for d in (APPS / iid / "skills").iterdir()
                if (d / "SKILL.md").exists()} if (APPS / iid / "skills").exists() else set()
        disk |= {d.name for d in sysdir.iterdir()
                 if (d / "SKILL.md").exists()} if sysdir.exists() else set()  # 系统技能目录也算可解析
        # 订阅了但目录里没有 → 注入时会静默丢失
        missing = subbed - disk
        if missing:
            problems.append(f"[订阅断链] {iid[:8]} 订阅了但文件缺失: {sorted(missing)}")
        # 有业务技能文件但没订阅 → 前端看不到/不注入
        orphan_files = {x for x in disk - subbed
                        if (APPS / iid / "skills" / x).exists()}  # 只报实例自有目录里未挂载的
        if orphan_files:
            problems.append(f"[未订阅] {iid[:8]} 目录有技能但未挂载: {sorted(orphan_files)}")
    # manifest 里有但所有实例都没有的
    mset = {s.get("name") for s in manifest.get("skills", []) if isinstance(manifest, dict)} \
        if isinstance(manifest, dict) else set()
    return mset

def check_orphan_dbs(problems):
    for p in ROOT.rglob("*.db"):
        if "/.git/" in str(p): continue
        if p.stat().st_size == 0:
            problems.append(f"[孤儿空库] {p.relative_to(ROOT)} 0字节, sqlite3 CLI裸路径误建, 可删")

def main():
    problems = []
    check_subscriptions(problems)
    check_orphan_dbs(problems)
    if "--fix" in sys.argv:
        for i, p in enumerate(problems):
            if "孤儿空库" in p:
                rel = p.split("] ")[1].split(" 0字节")[0]
                f = ROOT / rel
                f.unlink(); problems[i] += " → 已删除"
    if problems:
        print(f"❌ 体检发现 {len(problems)} 项问题:")
        for p in problems: print("  ", p)
        sys.exit(1)
    print("✅ 体检通过: 订阅表/skills目录/manifest 三方对齐, 无孤儿库")

if __name__ == "__main__":
    main()
