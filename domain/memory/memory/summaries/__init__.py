"""Memory summary extraction helpers."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, List, Optional


IGNORE_TOOLS = frozenset(
    {
        "sense_vitals",
        "sense_time",
        "sense_self",
        "sense_wake_reason",
        "sense_event_queue",
        "sense_event_detail",
        "sense_memory",
        "sense_scratchpad",
        "sense_goals",
        "sense_daily",
        "sense_todos",
        "sense_sent_log",
        "rest",
    }
)


def _extract_file_ops_from_terminal(command: str) -> List[str]:
    """从 terminal command 里抽"创建/修改文件"动作, 返回形如 ['path'] 摘要。

    覆盖常见模式:
      cat > a.md, > a.md, touch a.md, mkdir a, mv b c, cp b c,
      echo ... > a.md, tee a.md, curl -o / mv -O / wget -O,
      sed -i, rsync ... dst/
    """
    ops = []
    cmd = command.strip()
    # 限定只看 "命令前缀" 决定模式, 避免把 git log | grep '>abc' 这类误抓
    # cat > / >> /  <<EOF
    for m in re.finditer(r'(?:>>?|\bc cat\b)\s*([^\s|;&]+)', cmd[:300]):
        p = m.group(1).strip().strip('"\'')
        if p and p not in ('EOF', '&1', '2', '1'):
            ops.append(p)
    # touch
    for m in re.finditer(r'\btouch\s+([^\s|;&]+)', cmd):
        ops.append(m.group(1).strip().strip('"\''))
    # mkdir
    for m in re.finditer(r'\bmkdir\s+(?:-p\s+)?([^\s|;&]+)', cmd):
        ops.append(m.group(1).strip().strip('"\'') + '/(dir)')
    # mv / cp with destination
    for m in re.finditer(r'\b(?:mv|cp)\s+(?:-[rfv]+\s+)?([^\s|;&]+)\s+([^\s|;&]+)', cmd):
        ops.append(f"{m.group(1)} → {m.group(2)}")
    # tee
    for m in re.finditer(r'\btee\s+(?:-[a]\s+)?([^\s|;&]+)', cmd):
        ops.append(m.group(1).strip().strip('"\''))
    # curl/wget -O
    for m in re.finditer(r'\b(?:curl|wget)\s+.*?-O\s+([^\s|;&]+)', cmd):
        ops.append(m.group(1).strip().strip('"\''))
    # 截断长路径最后两段
    cleaned = []
    for p in ops:
        # 取最后 3 段路径, 太长难看
        parts = p.split('/')
        cleaned.append('/'.join(parts[-3:]) if len(parts) > 3 else p)
    # 去重 + 截 5 个防爆炸
    seen = set()
    out = []
    for p in cleaned:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out[:5]


def _extract_file_ops_from_code(code: str) -> List[str]:
    """从 execute_code 里抽 open(path, 'w') / Path(path).write_text 等。"""
    ops = []
    # open('xxx', 'w') / open('xxx', 'a')
    for m in re.finditer(r"open\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"][wa]", code):
        ops.append(m.group(1))
    # Path('xxx').write_text / .write_bytes
    for m in re.finditer(r"Path\(\s*['\"]([^'\"]+)['\"]\s*\)\.write_(?:text|bytes)", code):
        ops.append(m.group(1))
    # Path('xxx').mkdir
    for m in re.finditer(r"Path\(\s*['\"]([^'\"]+)['\"]\s*\)\.mkdir", code):
        ops.append(m.group(1) + '/(dir)')
    # 截断长路径
    cleaned = []
    for p in ops:
        parts = p.split('/')
        cleaned.append('/'.join(parts[-3:]) if len(parts) > 3 else p)
    seen = set()
    out = []
    for p in cleaned:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out[:5]


def summarize_tool_call(name: str, args: Dict[str, Any]) -> str:
    """Compress a tool call into one readable summary line.

    对"产出类"工具(add_lesson/register_*/promote_memory/terminal 写文件/execute_code 写文件)
    抽取关键内容, 让 digest/read-recall 看到"做了什么产出"而非"工具: name"。
    """
    if name in IGNORE_TOOLS:
        return ""

    if name == "write_diary":
        first_line = args.get("text", "").split("\n")[0][:60].strip()
        return f"日记: {first_line}"

    if name == "express_to_human":
        text = args.get("text", "")[:60].strip()
        return f"发消息: {text}"

    if name == "record_thought":
        text = args.get("text", "")
        tag = args.get("tag", "")
        first_line = text.split("\n")[0][:60].strip()
        return f"思绪[{tag}]: {first_line}" if tag else f"思绪: {first_line}"

    if name == "update_scratchpad":
        return "更新笔记"

    # ── 产出类: 沉淀永久记忆 ──
    if name == "add_lesson":
        text = (args.get("text") or args.get("lesson") or "")[:60].strip()
        return f"沉淀 lesson: {text}" if text else "沉淀 lesson"
    if name == "add_insight":
        text = (args.get("text") or args.get("content") or "")[:60].strip()
        kind = (args.get("kind") or "insight").strip()
        return f"沉淀 insight[{kind}]: {text}" if text else f"沉淀 insight[{kind}]"
    if name == "update_rules":
        text = (args.get("text") or "")[:60].strip()
        return f"更新规则: {text}" if text else "更新规则"
    if name == "update_self_knowledge":
        text = (args.get("text") or "")[:60].strip()
        return f"更新自我认知: {text}" if text else "更新自我认知"

    # ── 产出类: cognition 形成 ──
    if name == "promote_memory":
        s = (args.get("summary") or "")[:60].strip()
        return f"形成认知: {s}" if s else "形成认知"
    if name == "supersede_memory":
        s = (args.get("summary") or args.get("new_body") or "")[:60].strip()
        return f"取代认知: {s}" if s else "取代认知"
    if name in ("revise_memory", "cluster_born_memory", "signal_memory"):
        s = (args.get("summary") or args.get("text") or "")[:60].strip()
        action = {"revise_memory": "修订认知", "cluster_born_memory": "聚类衍生",
                  "signal_memory": "信号记忆"}.get(name, name)
        return f"{action}: {s}" if s else action

    # ── 产出类: 能力注册 ──
    if name == "register_skill":
        n = (args.get("name") or "")[:60].strip()
        return f"注册 skill: {n}" if n else "注册 skill"
    if name == "register_tool":
        n = (args.get("name") or "")[:60].strip()
        return f"注册 tool: {n}" if n else "注册 tool"
    if name == "register_attachment":
        p = (args.get("path") or "")[:60].strip()
        return f"登记附件: {p}" if p else "登记附件"

    # ── 产出类: todo 完成动作 ──
    if name == "todo":
        action = (args.get("action") or "").strip()
        if action == "done":
            tid = (args.get("todo_id") or "")[:20].strip()
            return f"完成任务: {_short_id(tid)}"
        if action == "create":
            t = (args.get("title") or "")[:60].strip()
            return f"创建待办: {t}" if t else "创建待办"
        if action in ("cancel", "start", "update", "pause"):
            tid = (args.get("todo_id") or "")[:20].strip()
            return f"待办 {action}: {_short_id(tid)}"
        return f"待办: {action}" if action else "工具: todo"

    # ── 产出类: terminal / execute_code 文件操作 ──
    if name == "terminal":
        cmd = (args.get("command") or "").strip()
        paths = _extract_file_ops_from_terminal(cmd)
        if paths:
            return f"写文件: {', '.join(paths)}"
        # 普通命令: 取前 50 chars, 不可见
        return f"命令: {cmd[:50]}" if cmd else "terminal"
    if name == "execute_code":
        code = (args.get("code") or "").strip()
        paths = _extract_file_ops_from_code(code)
        if paths:
            return f"写文件: {', '.join(paths)}"
        return "执行代码" if code else "execute_code"

    # 注:manage_goals/manage_plan/manage_daily/manage_work/sense_* 等
    # 工具已于 2026-07-17 退役(handler 保留可 dispatch, schema 不再注入)。
    # 这里删除它们的历史 summary 分支, 让它们走通用 fallback——避免后人误以为还在用。
    # 退役工具的 args 多含 _deprecated_hint,自然在通用字符截断里投机出现。

    return f"工具: {name}"


def _short_id(tid: str) -> str:
    """短 id 加省略号 (若过长)。"""
    if not tid:
        return "?"
    return tid if len(tid) <= 12 else tid[:10] + '..'


def extract_energy(text: str) -> Optional[float]:
    match = re.search(r"精力([\d.]+)", text)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    return None


def dedup_tool_summaries(summaries: List[str]) -> List[str]:
    """Deduplicate adjacent summaries and merge their counts."""
    if not summaries:
        return []
    result = []
    previous = None
    count = 0
    for summary in summaries:
        prefix = re.sub(r"^(日记|思绪|发消息|工具|更新笔记|更新规则|目标|每日计划|规划|完成任务|追加任务|创建待办|待办[^:]*|沉淀 \w+|更新自我认知|写文件|命令|形成认知|取代认知|修订认知|聚类衍生|信号记忆|注册 \w+|登记附件|计划|工作)(\[.*?\])?:\s*", "", summary)
        if prefix == previous:
            count += 1
        else:
            if previous and count > 1:
                result[-1] = result[-1].replace(previous, f"{previous} ×{count}", 1)
            result.append(summary)
            previous = prefix
            count = 1
    if previous and count > 1:
        result[-1] = result[-1].replace(previous, f"{previous} ×{count}", 1)
    return result


def format_session_digest(digest: Dict[str, Any]) -> str:
    lines = [
        f"[{digest['time']}] {digest['wake_reason']}, "
        f"{digest['duration_sec']}s, {digest['message_count']}msgs"
    ]
    for summary in digest["tool_summary"]:
        lines.append(f"  · {summary}")
    if digest["energy_range"]:
        energy_start, energy_end = digest["energy_range"]
        lines.append(f"  {energy_start:.0f} → {energy_end:.0f}")
    lines.append(f"  结束: {digest['end_reason']}")
    return "\n".join(lines)


def extract_topics(text: str) -> List[str]:
    phrases = re.findall(r"[\u4e00-\u9fff]{2,6}", text)
    boring = {
        "精力",
        "消息",
        "用户",
        "笔记",
        "日记",
        "思绪",
        "结束",
        "唤醒",
        "自然醒",
        "主动休息",
        "完成",
        "继续",
        "消耗",
        "降下",
        "不够",
        "自己",
    }
    meaningful = [phrase for phrase in phrases if phrase not in boring and len(phrase) >= 2]
    counts = Counter(meaningful)
    return [phrase for phrase, count in counts.most_common(5) if count >= 2]


__all__ = [
    "IGNORE_TOOLS",
    "dedup_tool_summaries",
    "extract_energy",
    "extract_topics",
    "format_session_digest",
    "summarize_tool_call",
    "_extract_file_ops_from_terminal",
    "_extract_file_ops_from_code",
]
