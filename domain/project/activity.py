"""项目动态转译层（PRD v0.2 C14，P0 头号）——把团队的内部语言译成客户语言。

问题（场景走查 A2 抓获）：项目群回放给客户看的是团队黑话
（"@行业研究员 ptodo-41b2 置 done、deliver 转正、shared/研究/…"）。
客户要的是"🏭 研究员正在研究 → ✅ 已交付报告 → 🔍 质控审查中"。

本模块从既有数据**推导**动态流（不新增引擎写入，故事线/审计后续可复用）：
  1. 项目群消息：按角色 persona 的标记词分类翻译（【交付】【验收】【质控审查】
     【闭环】等），清洗 ID/路径/工具名黑话；
  2. deliver 版本：shared/.versions/{时间戳}/ 即一次转正交付时刻；
  3. 待办现状 → 阶段步骤条（预期管理 C16 的数据源）。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

# 标记词 → (kind, 客户语言动作)——与四角色 persona 的群播报格式对齐。
# 顺序即优先级（先匹配先赢）：闭环 > 质控结论 > 质控执行 > 修订 > 交付 > 验收 > 分派。
_KIND_RULES: list[tuple[str, str, str]] = [
    (r"【闭环】|本轮分析完成|本轮分析闭环|彻底闭环", "done", "宣布本轮分析闭环"),
    (r"⚠️|需修改", "qc", "质控提出了修改意见"),
    (r"✅[^\n]{0,12}通过|审查完毕", "qc", "质量审查通过"),
    (r"【质控审查】|【正式审查交付】|【收尾核查】|事后抽查|质量审查", "qc", "执行了质量把关"),
    (r"已修订|修订并更新", "revise", "按审查意见修订了交付物"),
    (r"【交付】|已交付|交付：", "deliver", "交付了"),
    (r"【验收】|验收通过|已验收", "accept", "验收通过"),
    (r"新委托已受理|请开始|分派|派给", "assign", "分派了任务"),
]

# 黑话清洗：服务 ID / 待办 ID / 工具名 / 内部路径
_STRIP_PATTERNS = [
    r"svc-[0-9a-f]{6,}", r"ptodo-[0-9a-f]+", r"prj-[0-9a-f]+",
    r"deliver_to_shared|project_deliver|project_file_\w+|project_todo_\w+",
    r"send_chat_message|sense_project_peers",
    r"shared/[^\s，。；)】]*", r"members/[^\s，。；)】]*",
]
_TITLE_RE = re.compile(r"[《「]([^》」]{2,40})[》」]")  # 只认书名/引号，不认【标记】

STAGE_OF_KIND = {
    "assign": "组队与分派",
    "deliver": "研究与交付",
    "accept": "研究与交付",
    "revise": "研究与交付",
    "qc": "质量把关",
    "done": "汇报闭环",
    "note": "协作中",
    "file": "研究与交付",
}

STAGE_ORDER = ["组队与分派", "研究与交付", "质量把关", "汇报闭环"]


def sanitize(text: str, limit: int = 80) -> str:
    """群消息 → 客户可读：去黑话、压缩空白、截断。"""
    t = text or ""
    for pat in _STRIP_PATTERNS:
        t = re.sub(pat, "", t)
    t = re.sub(r"@[^\s：:，,]+", "", t)  # @提及（角色名由 actor 字段呈现）
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\s*([，。；;：])\s*", r"\1", t)  # 标点前去空格
    t = re.sub(r"[，。；：]\s*[，。；：]", "，", t)  # 连续标点合一
    t = t.strip(" ，。；;：:·-")
    return t[:limit]


def classify(text: str) -> tuple[str, str]:
    """消息 → (kind, 动作词)。无命中归 note（协作交流）。"""
    for pat, kind, verb in _KIND_RULES:
        if re.search(pat, text):
            return kind, verb
    return "note", "在团队群交流"


def _ts_fmt(ts) -> str:
    dt = _parse_ts(ts)
    return dt.strftime("%m-%d %H:%M") if dt else ""


def _parse_ts(ts) -> Optional[datetime]:
    """消息/版本时间戳兼容：unix 秒 或 ISO 串。"""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts))
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(str(ts), fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(str(ts))
    except ValueError:
        return None


def _group_messages(project: dict) -> list[dict]:
    from infrastructure.config import resolve_runtime_dir

    db = resolve_runtime_dir(project["pm_id"]) / "data" / "messages.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db), timeout=3.0)
    try:
        rows = conn.execute(
            "SELECT ts, sender_name, text FROM messages"
            " WHERE chat_id = ? ORDER BY id",
            (f"svcgroup:{project['project_id']}",),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for ts, sender, text in rows:
        m = _TITLE_RE.search(text or "")
        title = m.group(1) if m else ""
        body = sanitize(text or "")
        dt = _parse_ts(ts)
        out.append({
            "ts_raw": dt.timestamp() if dt else 0.0,
            "ts": _ts_fmt(ts),
            "actor": sender or "",
            "kind": "", "verb": "", "title": title, "text": "", "detail": body or "",
        })
    return out


def build_project_activity(project_id: str) -> dict:
    """项目动态流 + 阶段步骤。返回 {activities, steps, current_stage}。"""
    from infrastructure.config import get_project_root
    from infrastructure.persistence import services_registry

    project = services_registry.lookup_project(project_id)
    if project is None:
        return {"activities": [], "steps": [], "current_stage": ""}

    sid_to_role = {
        s["service_id"]: s.get("display_name") or ""
        for s in services_registry.list_services_by_project(project_id)
    }

    activities: list[dict] = []

    # 1) 群消息 → 翻译
    for m in _group_messages(project):
        kind, verb = classify(m["detail"])
        actor = m["actor"] or sid_to_role.get(m["actor"], "")
        title = m["title"]
        if kind == "assign" and title in set(sid_to_role.values()):
            title = ""  # 分派消息的首个书名号常是角色名不是文档名
        text = f"{actor} {verb}" + (f"《{title}》" if title else "")
        if kind == "note" and m["detail"]:
            text = f"{actor}：{m['detail'][:60]}"
        activities.append({
            "ts": m["ts"], "ts_raw": m["ts_raw"], "actor": actor,
            "kind": kind, "stage": STAGE_OF_KIND[kind],
            "text": text, "detail": m["detail"],
        })

    # 2) deliver 版本时刻（.versions/{时间戳}/ 下每文件 = 一次转正）
    versions = get_project_root() / "projects" / project_id / "shared" / ".versions"
    if versions.is_dir():
        for vdir in versions.iterdir():
            if not vdir.is_dir():
                continue
            try:
                vt = datetime.strptime(vdir.name, "%Y%m%d_%H%M%S")
            except ValueError:
                continue
            for f in vdir.rglob("*"):
                if f.is_file():
                    rel = f.relative_to(vdir)
                    activities.append({
                        "ts": vt.strftime("%m-%d %H:%M"), "ts_raw": vt.timestamp(),
                        "actor": "", "kind": "file", "stage": STAGE_OF_KIND["file"],
                        "text": f"交付物更新：《{rel}》新版本已入档",
                        "detail": str(rel),
                    })

    activities.sort(key=lambda a: a["ts_raw"])
    for a in activities:
        a.pop("ts_raw", None)

    # 3) 阶段步骤（预期管理）：从待办现状推导四步
    todos = services_registry.list_project_todos(project_id)
    steps = []
    if not todos:
        steps = [{"name": s, "state": "pending"} for s in STAGE_ORDER]
        if project.get("created_at"):
            steps[0]["state"] = "doing"
    else:
        def _todo_state(keywords: list[str]) -> str:
            hit = [t for t in todos if any(k in t["title"] for k in keywords)]
            if not hit:
                return "pending"
            if all(t["status"] == "done" for t in hit):
                return "done"
            return "doing"
        steps = [
            {"name": "受理组队", "state": "done" if todos else "doing"},
            {"name": "双线研究", "state": _todo_state(["行业研究", "企业情况", "研究"])},
            {"name": "质量把关", "state": _todo_state(["审查", "质控"])},
            {"name": "结论汇报", "state": _todo_state(["汇报", "交付", "结论"])},
        ]
    current = next((s["name"] for s in steps if s["state"] == "doing"), "")
    if not current and steps and all(s["state"] == "done" for s in steps):
        current = "已完成"
    return {"activities": activities[-80:], "steps": steps, "current_stage": current}
