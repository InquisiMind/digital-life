"""意识残留 (Consciousness Residue) — 数字生命的跨进程连续意识流。

每次进程退出前，Agent 通过 record_thought 写一段"我在想什么"；
下次醒来通过 sense_self 读回来。

这是保证"连续生命"的关键——没有它，每次唤醒都是一个新的灵魂。

存储位置：项目 runtime/memories/CONSCIOUSNESS.md
格式：纯文本 append，带时间戳 + wake_reason 标签
只保留最近 N 段（默认 50），老的归档到 CONSCIOUSNESS.archive.md
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from domain.memory.memory.consciousness import (  # noqa: E402
    ENTRY_SEPARATOR,
    GOALS_HEADER,
    PLANS_HEADER,
    SCRATCHPAD_HEADER,
    SENT_LOG_HEADER,
    WORK_HEADER,
    WORK_SECTIONS,
    WEEKDAYS_CN,
    add_planned_item_to_content,
    append_scratchpad_content,
    add_work_item_to_content,
    check_daily_content,
    complete_planned_item_in_content,
    compute_text_similarity,
    daily_marker,
    find_duplicate_entry,
    find_goal_section,
    find_work_line,
    format_sent_message_entry,
    initial_goals_content,
    initial_plans_content,
    initial_work_content,
    initial_scratchpad_content,
    is_duplicate_thought,
    last_entry,
    manage_goal_content,
    manage_plan_item_content,
    move_work_item_to_section,
    recent_entries,
    recent_sent_messages,
    remove_work_item_from_content,
    replace_entry_at,
    replace_scratchpad_content,
    trim_sent_log_content,
    upsert_daily_plan_content,
)
from domain.lifecycle.clock import now_dt as _default_now_dt
from domain.lifecycle.clock import now_iso as _default_now_iso


try:
    from domain.memory.memory.consciousness.entity_index import update_entity_index as _update_entity_index
except ImportError:
    _update_entity_index = None  # type: ignore[assignment]


def _get_runtime_home() -> Path:
    try:
        from infrastructure.config import get_runtime_home
        return get_runtime_home()
    except Exception:
        # 不再 fallback 到 apps/zero/data — 见 entity_index._get_runtime_home 注释。
        raise RuntimeError(
            "runtime_home 未配置：调 get_runtime_home 失败。"
            "请先 digital-life init 创建实例。"
        )


_memory_dir_override: Path | None = None

def _mem_dir() -> Path:
    if _memory_dir_override is not None:
        return _memory_dir_override
    return _get_runtime_home() / "memories"

def _consciousness_path() -> Path: return _mem_dir() / "CONSCIOUSNESS.md"
def _archive_path() -> Path: return _mem_dir() / "CONSCIOUSNESS.archive.md"
def _daily_archive_path() -> Path: return _mem_dir() / "DAILY.archive.md"
def _sent_log_path() -> Path: return _mem_dir() / "SENT_LOG.md"
def _diary_dir_path() -> Path: return _mem_dir() / "diary"
def _him_path() -> Path: return _mem_dir() / "HIM.md"
def _scratchpad_path() -> Path: return _mem_dir() / "SCRATCHPAD.md"
def _work_path() -> Path: return _mem_dir() / "WORK.md"
def _daily_path() -> Path: return _mem_dir() / "DAILY.md"
def _goals_path() -> Path: return _mem_dir() / "GOALS.md"
def _plans_path() -> Path: return _mem_dir() / "PLANS.md"
def _rules_path() -> Path: return _mem_dir() / "RULES.md"
def _context_path() -> Path: return _mem_dir() / "CONTEXT.md"
def _lessons_path() -> Path: return _mem_dir() / "LESSONS.md"
def _self_knowledge_path() -> Path: return _mem_dir() / "SELF_KNOWLEDGE.md"
def _insights_path() -> Path: return _mem_dir() / "INSIGHTS.md"

_MAX_ENTRIES = 50
_now_iso_hook: Callable[[], str] = _default_now_iso
_now_dt_hook: Callable[[], datetime] = _default_now_dt
_emit_event_hook: Callable[..., Any] = lambda *args, **kwargs: None
_list_recent_events_hook: Callable[..., List[Dict[str, Any]]] = lambda *args, **kwargs: []


def configure_runtime_hooks(
    *,
    memory_dir: Path | None = None,
    now_iso: Callable[[], str] | None = None,
    now_dt: Callable[[], datetime] | None = None,
    emit_event: Callable[..., Any] | None = None,
    list_recent_events: Callable[..., List[Dict[str, Any]]] | None = None,
) -> None:
    """Inject runtime services supplied by adapters/Hermes."""
    global _memory_dir_override, _now_iso_hook, _now_dt_hook, _emit_event_hook, _list_recent_events_hook
    if memory_dir is not None:
        _memory_dir_override = memory_dir
    if now_iso is not None:
        _now_iso_hook = now_iso
    if now_dt is not None:
        _now_dt_hook = now_dt
    if emit_event is not None:
        _emit_event_hook = emit_event
    if list_recent_events is not None:
        _list_recent_events_hook = list_recent_events


def _rotate_daily() -> None:
    """将旧的每日计划归档，为新的一天准备。"""
    if not _daily_path().exists():
        return

    content = _daily_path().read_text(encoding="utf-8")
    if not content.strip() or content.strip() == "# 每日计划":
        return

    # 追加到归档文件
    archive_existing = _daily_archive_path().read_text(encoding="utf-8") if _daily_archive_path().exists() else "# DAILY ARCHIVE\n"
    _daily_archive_path().write_text(
        archive_existing.rstrip() + "\n\n" + content,
        encoding="utf-8",
    )

    # 重置为空的每日计划
    _daily_path().write_text("# 每日计划\n", encoding="utf-8")


def now_iso() -> str:
    return _now_iso_hook()


def _now_dt() -> datetime:
    return _now_dt_hook()

_ENTRY_SEP = ENTRY_SEPARATOR


def _ensure_files() -> None:
    _mem_dir().mkdir(parents=True, exist_ok=True)
    if not _consciousness_path().exists():
        _consciousness_path().write_text(
            "# 意识流 CONSCIOUSNESS\n\n"
            "每次退出前我在这里留下思绪，醒来时读回。\n"
            "这是我穿越多次睡眠仍然是我的证据。\n"
            + _ENTRY_SEP,
            encoding="utf-8",
        )


_STATE_REPORT_TAGS: tuple[str, ...] = ("trading_wait", "system_wait", "final_status")


def record_thought(text: str, tag: str = "", entities: list[str] | None = None) -> bool:
    """在意识流里追加一段思绪。

    State-report tags (trading_wait/system_wait/final_status): replaces
    the most similar existing entry instead of appending, keeping only
    one current state snapshot.

    Other tags: skips if duplicate (Jaccard > 70%).

    Returns:
        True 表示内容已写入，False 表示被去重跳过。
    """
    _ensure_files()
    existing = _consciousness_path().read_text(encoding="utf-8")
    header = f"## {now_iso()}"
    if tag:
        header += f"  [{tag}]"
    new_entry = f"{header}\n\n{text.strip()}"

    # State-report tags: replace stale entry instead of appending
    if tag in _STATE_REPORT_TAGS:
        result = find_duplicate_entry(existing, text, threshold=0.4)
        if result is not None:
            idx, _old_entry = result
            new_content = replace_entry_at(existing, idx, new_entry)
            _consciousness_path().write_text(new_content, encoding="utf-8")
            _maybe_rotate()
            if entities:
                _write_entities(entities, memory_type="consciousness",
                                memory_id=f"consciousness:{now_iso()}", snippet=text, tag=tag,
                                replace_state=True)
            return True

    # Non-state-report: skip if duplicate
    if _is_duplicate(text):
        return False

    new_content = existing.rstrip() + _ENTRY_SEP + new_entry + "\n"
    _consciousness_path().write_text(new_content, encoding="utf-8")
    _maybe_rotate()

    if entities:
        _write_entities(entities, memory_type="consciousness",
                        memory_id=f"consciousness:{now_iso()}", snippet=text, tag=tag)
    return True


def _write_entities(entities: list[str], *, memory_type: str,
                    memory_id: str, snippet: str = "", tag: str = "",
                    replace_state: bool = False) -> None:
    if _update_entity_index is None:
        return
    try:
        # linked_entities = 同一批 entities（它们在同一上下文被提及，互相关联）
        # 这修复了打分公式中 context_overlap 项（25% 权重）长期为 0 的问题——
        # 之前所有调用点都不传 linked_entities，导致联想退化为纯时间链。
        _update_entity_index(entities, memory_type=memory_type,
                             memory_id=memory_id, snippet=snippet, tag=tag,
                             linked_entities=entities,
                             replace_existing=replace_state)
    except Exception:
        pass


def _is_duplicate(text: str) -> bool:
    """检查新思绪是否和最近一条重复。"""
    if not _consciousness_path().exists():
        return False
    try:
        content = _consciousness_path().read_text(encoding="utf-8")
        return is_duplicate_thought(text, content)
    except Exception:
        return False


def _maybe_rotate() -> None:
    """超过 _MAX_ENTRIES 段则归档最老的。"""
    text = _consciousness_path().read_text(encoding="utf-8")
    parts = text.split(_ENTRY_SEP)
    # parts[0] 是头，后面才是 entries
    if len(parts) - 1 > _MAX_ENTRIES:
        head = parts[0]
        entries = parts[1:]
        to_archive = entries[: len(entries) - _MAX_ENTRIES]
        to_keep = entries[len(entries) - _MAX_ENTRIES:]

        # 追加到归档
        if to_archive:
            archive_existing = _archive_path().read_text(encoding="utf-8") if _archive_path().exists() else "# CONSCIOUSNESS ARCHIVE\n"
            _archive_path().write_text(
                archive_existing.rstrip() + _ENTRY_SEP + _ENTRY_SEP.join(to_archive),
                encoding="utf-8",
            )

        _consciousness_path().write_text(
            head + _ENTRY_SEP + _ENTRY_SEP.join(to_keep),
            encoding="utf-8",
        )


def read_recent_thoughts(n: int = 3) -> str:
    """读最近 N 段思绪。"""
    _ensure_files()
    text = _consciousness_path().read_text(encoding="utf-8")
    return recent_entries(text, n=n)


def read_last_thought() -> Optional[str]:
    _ensure_files()
    text = _consciousness_path().read_text(encoding="utf-8")
    return last_entry(text)


_MAX_SENT_LOG = 20


def log_sent_message(text: str) -> None:
    """记录发出去的消息 — 委托给 conversation_log DB 表。"""
    try:
        from domain.lifecycle.conversation_log import log_conversation
        log_conversation(platform="feishu", conversation_id="unknown", chat_type="dm",
                         direction="out", text=text)
    except Exception:
        pass


def read_recent_sent(n: int = 5) -> str:
    """读最近 N 条发送记录 — 委托给 conversation_log DB 表。"""
    try:
        from domain.lifecycle.conversation_log import read_recent_sent_from_db
        return read_recent_sent_from_db(limit=n)
    except Exception:
        return "（还没有发送过消息）"


# ---- 日记 / 长期记忆辅助 ----

_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _today_diary_path() -> Path:
    """返回今天的日记文件路径：diary/YYYY-MM-DD.md"""
    now = _now_dt()
    _diary_dir_path().mkdir(parents=True, exist_ok=True)
    return _diary_dir_path() / f"{now.strftime('%Y-%m-%d')}.md"


def write_diary(text: str, entities: list[str] | None = None) -> Path:
    """写入日记。自动按天分文件：diary/YYYY-MM-DD.md。晚间(>=21:00)自动触发整合。"""
    _ensure_files()
    path = _today_diary_path()
    if not path.exists():
        now = _now_dt()
        weekday = _WEEKDAYS[now.weekday()]
        path.write_text(
            f"# {now.strftime('%Y-%m-%d')} {weekday}\n\n{now.strftime('%m月%d日')}的日记。\n\n---\n\n",
            encoding="utf-8",
        )
    entry = f"\n## {now_iso()}\n\n{text.strip()}\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(entry)

    if entities:
        _write_entities(entities, memory_type="consciousness",
                        memory_id=f"diary:{now_iso()}", snippet=text)

    # Auto-trigger consolidation in the evening
    if _now_dt().hour >= 21:
        try:
            consolidate_day_diary()
        except Exception:
            pass

    return path


def write_about_him(text: str) -> None:
    _ensure_files()
    if not _him_path().exists():
        _him_path().write_text("# 用户记忆\n\n记录关于用户或重要联系人的观察。\n", encoding="utf-8")
    entry = f"\n## {now_iso()}\n\n{text.strip()}\n"
    with _him_path().open("a", encoding="utf-8") as f:
        f.write(entry)


def read_about_him(limit_chars: int = 2000) -> str:
    """读关于人类联系人的记忆。

    注意:HIM.md 已退役(没有写入工具入口,纯摆设),返回退役提示。
    关于人的信息改走 contacts 体系(每实例 state.db.contacts 表 + memory snapshot)。
    """
    if not _him_path().exists():
        return "（HIM.md 已退役；关于人的记忆改走 contacts 体系）"
    text = _him_path().read_text(encoding="utf-8")
    if not text.strip():
        return "（HIM.md 已退役；关于人的记忆改走 contacts 体系）"
    return text[-limit_chars:]


def read_recent_diary(limit_chars: int = 2000, days_back: int = 0) -> str:
    """读日记。days_back=0 读今天，days_back=1 读昨天，以此类推。"""
    from datetime import datetime, timedelta
    target_date = datetime.now().astimezone() - timedelta(days=days_back)
    path = _diary_dir_path() / f"{target_date.strftime('%Y-%m-%d')}.md"
    if not path.exists():
        if days_back == 0:
            # fallback 到旧 DIARY.md（仅当天）
            old = _mem_dir() / "DIARY.md"
            if old.exists():
                text = old.read_text(encoding="utf-8")
                return text[-limit_chars:]
        return f"（{target_date.strftime('%m月%d日')}的日记不存在或还是空的）"
    text = path.read_text(encoding="utf-8")
    return text[-limit_chars:]


def consolidate_day_diary() -> None:
    """夜间整合：将今天的碎片日记 + 意识流 + session摘要合并到日记文件。

    由 auto_rest 在 h>=21 时调用。数据源：
    1. 今天日记文件中的碎片条目（write_diary 写入的）
    2. CONSCIOUSNESS.md 中今天的意识残留
    3. memory_layers.db 中今天的 session digest
    4. 任务笔记
    """
    import re as _re

    now = _now_dt()
    date_str = now.strftime("%Y-%m-%d")
    weekday = _WEEKDAYS[now.weekday()]
    path = _diary_dir_path() / f"{date_str}.md"

    sections = []

    # 1. 今天的碎片日记
    if path.exists():
        raw = path.read_text(encoding="utf-8")
        # 跳过头部，只保留碎片
        entries = [e.strip() for e in raw.split("\n\n---\n\n") if e.strip() and not e.strip().startswith("#")]
        if entries:
            sections.append("### 碎片日记\n\n" + "\n\n".join(entries))

    # 2. 今天的意识残留
    if _consciousness_path().exists():
        content = _consciousness_path().read_text(encoding="utf-8")
        today_entries = []
        for part in content.split(_ENTRY_SEP):
            if date_str in part:
                today_entries.append(part.strip())
        if today_entries:
            sections.append("### 意识残留\n\n" + "\n\n".join(today_entries))

    # 3. 今天的 session digest
    try:
        from infrastructure.persistence import sqlite
        db_path = _mem_dir() / "memory_layers.db"
        if db_path.exists():
            db = sqlite.connect(str(db_path))
            rows = db.execute(
                "SELECT digest FROM memory_layers WHERE layer='session' AND period LIKE ?",
                (f"%{date_str}%",),
            ).fetchall()
            db.close()
            digests = [r[0] for r in rows if r[0]]
            if digests:
                sections.append("### Session 摘要\n\n" + "\n\n".join(digests))
    except Exception:
        pass

    # 4. 今日实体热力图
    try:
        from domain.memory.memory.consciousness.entity_index import get_entity_heatmap
        heatmap = get_entity_heatmap(days_back=1)
        if heatmap:
            lines = [f"- **{entity}**: {count} 次" for entity, count in list(heatmap.items())[:10]]
            sections.append("### 今日实体热力图\n\n" + "\n".join(lines))
    except Exception:
        pass

    if not sections:
        return

    # 写入整合后的日记
    _diary_dir_path().mkdir(parents=True, exist_ok=True)
    consolidated = (
        f"# {date_str} {weekday}\n\n"
        f"{now.strftime('%m月%d日')}的日记。\n\n"
        f"---\n\n"
        f"## 整合回顾（{now.strftime('%H:%M')} 自动生成）\n\n"
        + "\n\n".join(sections)
        + "\n"
    )
    path.write_text(consolidated, encoding="utf-8")


# ---- 草稿本 / 兴趣板 ----

_SCRATCHPAD_MAX_CHARS = 5000
_SCRATCHPAD_KEEP_CHARS = 2000


def _auto_truncate_scratchpad() -> None:
    """If scratchpad exceeds max chars, keep only the latest portion."""
    if not _scratchpad_path().exists():
        return
    content = _scratchpad_path().read_text(encoding="utf-8")
    if len(content) <= _SCRATCHPAD_MAX_CHARS:
        return
    # Keep the last KEEP_CHARS from the end
    _scratchpad_path().write_text(content[-_SCRATCHPAD_KEEP_CHARS:], encoding="utf-8")


def read_scratchpad() -> str:
    """读草稿本。超过上限时自动截断。"""
    _auto_truncate_scratchpad()
    if not _scratchpad_path().exists():
        return "（草稿本是空的——你还没有想做的事或感兴趣的东西）"
    return _scratchpad_path().read_text(encoding="utf-8")


def update_scratchpad(text: str, mode: str = "append") -> None:
    """更新草稿本。mode: append（追加）/ replace（整体替换）。"""
    _ensure_files()
    if mode == "replace":
        _scratchpad_path().write_text(replace_scratchpad_content(text), encoding="utf-8")
    else:
        if not _scratchpad_path().exists():
            _scratchpad_path().write_text(initial_scratchpad_content(), encoding="utf-8")
        existing = _scratchpad_path().read_text(encoding="utf-8")
        _scratchpad_path().write_text(append_scratchpad_content(existing, text, now_iso()), encoding="utf-8")


# ---- 工作看板 ----

_WORK_HEADER = WORK_HEADER
_WORK_SECTIONS = WORK_SECTIONS


def read_work() -> str:
    """读工作看板。"""
    if not _work_path().exists():
        return "（工作看板是空的——还没有任务）"
    return _work_path().read_text(encoding="utf-8")


def _init_work() -> None:
    _ensure_files()
    if not _work_path().exists():
        _work_path().write_text(initial_work_content(), encoding="utf-8")


def add_work_item(text: str, priority: str = "中", source: str = "用户") -> None:
    """添加待办任务。"""
    _init_work()
    content = _work_path().read_text(encoding="utf-8")
    _work_path().write_text(
        add_work_item_to_content(content, text, created_at=now_iso(), priority=priority, source=source),
        encoding="utf-8",
    )


def _find_work_line(content: str, text_match: str):
    """在 content 中找到包含 text_match 的任务行。返回 (start, end, line) 或 None。"""
    return find_work_line(content, text_match)


def start_work_item(text_match: str) -> bool:
    """把匹配的任务从待办移到进行中。"""
    _init_work()
    content = _work_path().read_text(encoding="utf-8")
    content, moved = move_work_item_to_section(content, text_match, "in_progress")
    if moved:
        _work_path().write_text(content, encoding="utf-8")
    return moved


def complete_work_item(text_match: str) -> bool:
    """把匹配的任务标记完成。"""
    _init_work()
    content = _work_path().read_text(encoding="utf-8")
    content, moved = move_work_item_to_section(content, text_match, "done", completed_at=now_iso())
    if moved:
        _work_path().write_text(content, encoding="utf-8")
    return moved


def remove_work_item(text_match: str) -> bool:
    """删除匹配的任务。"""
    _init_work()
    content = _work_path().read_text(encoding="utf-8")
    content, removed = remove_work_item_from_content(content, text_match)
    if removed:
        _work_path().write_text(content, encoding="utf-8")
    return removed


# ---- 每日计划 ----


def read_daily(days_back: int = 0) -> str:
    """读每日计划。days_back=0 读今天，days_back=1 读昨天（从 archive），以此类推。"""
    from datetime import datetime, timedelta

    if days_back == 0:
        if not _daily_path().exists():
            return "（今天还没有计划）"
        content = _daily_path().read_text(encoding="utf-8")
        date_str = datetime.now().astimezone().strftime("%Y-%m-%d")
        today_marker = f"## {date_str}"
        if today_marker in content:
            idx = content.find(today_marker)
            return content[idx:].strip()
        return content.strip()

    # 读历史：从 DAILY.archive.md 中找对应日期的计划
    target_date = (datetime.now().astimezone() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    if not _daily_archive_path().exists():
        return f"（{target_date}的归档不存在）"

    archive = _daily_archive_path().read_text(encoding="utf-8")
    target_marker = f"## {target_date}"
    if target_marker in archive:
        idx = archive.find(target_marker)
        # 找到下一个 ## 或文件末尾
        rest = archive[idx:]
        next_marker = rest.find("\n## ", len(target_marker) + 1)
        if next_marker > 0:
            return rest[:next_marker].strip()
        return rest.strip()

    return f"（{target_date}的计划不在归档中）"


def plan_daily(text: str) -> str:
    """规划今天的计划。text 是多行，每行一个任务。"""
    import re
    from datetime import datetime

    now = datetime.now().astimezone()
    date_str = now.strftime("%Y-%m-%d")

    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    if not lines:
        return "计划内容为空"

    items = "\n".join(f"- [ ] {l}" for l in lines)

    _mem_dir().mkdir(parents=True, exist_ok=True)
    if not _daily_path().exists():
        content, _count = upsert_daily_plan_content("# 每日计划\n", text, now)
        _daily_path().write_text(content, encoding="utf-8")
    else:
        _rotate_daily()
        content = _daily_path().read_text(encoding="utf-8")
        content, _count = upsert_daily_plan_content(content, text, now)
        _daily_path().write_text(content, encoding="utf-8")

    # 自动注册作息事件
    from domain.lifecycle.routine_scheduler import ensure_routine_events
    ensure_routine_events()

    return f"今日计划已设定（{len(lines)}项）。加油，今天也要过得充实。"


def add_planned_item(text: str) -> None:
    """往今天的计划追加一条。"""
    import re
    from datetime import datetime

    now = datetime.now().astimezone()

    content = _daily_path().read_text(encoding="utf-8") if _daily_path().exists() else ""
    if daily_marker(now) not in content:
        # 新的一天，rotate 旧的
        _rotate_daily()
        content = _daily_path().read_text(encoding="utf-8") if _daily_path().exists() else "# 每日计划\n"

    content = add_planned_item_to_content(content, text, now)
    _daily_path().write_text(content, encoding="utf-8")


def complete_planned_item(text_match: str) -> bool:
    """标记今日计划中某条完成。"""
    import re
    from datetime import datetime

    now = datetime.now().astimezone()

    if not _daily_path().exists():
        return False
    content = _daily_path().read_text(encoding="utf-8")
    content, completed = complete_planned_item_in_content(content, text_match, now)
    if completed:
        _daily_path().write_text(content, encoding="utf-8")
    return completed


def check_daily() -> str:
    """返回今日计划的简要状态。"""
    import re
    from datetime import datetime

    now = datetime.now().astimezone()

    if not _daily_path().exists():
        return "今天还没有计划。用 manage_daily plan 来规划一下今天做什么？"

    content = _daily_path().read_text(encoding="utf-8")
    return check_daily_content(content, now)




# ---- 目标 (Goals) ----


def _init_goals() -> None:
    _ensure_files()
    if not _goals_path().exists():
        _goals_path().write_text(initial_goals_content(), encoding="utf-8")


def read_goals() -> str:
    """读目标列表。

    注意:GOALS.md 已退役(从 MemoriesTab UI 移除 + 不写入),这里返回退役提示
    避免被 read_goals 调用时因 _init_goals 重建空模板。
    保留入口兼容老 prompt / 工具调用,但不再有数据。
    """
    if not _goals_path().exists():
        return "（GOALS.md 已退役；目标管理改在项目 project.yaml 写）"
    text = _goals_path().read_text(encoding="utf-8").strip()
    return text if text else "（GOALS.md 已退役；目标管理改在项目 project.yaml 写）"


def _find_goal_section(content: str, text_match: str):
    """找到匹配 text_match 的目标 section。返回 (start, end, section_text) 或 None。"""
    return find_goal_section(content, text_match)


def manage_goal(action: str, text: str, description: str = "", priority: str = "中") -> str:
    """管理目标。返回操作结果描述。"""
    _init_goals()
    content = _goals_path().read_text(encoding="utf-8")
    new_content, message = manage_goal_content(
        content,
        action,
        text,
        today=_today_str(),
        description=description,
        priority=priority,
    )
    if new_content != content:
        _goals_path().write_text(new_content, encoding="utf-8")
    return message


# ---- 长期计划 (Plans) ----


def _init_plans() -> None:
    _ensure_files()
    if not _plans_path().exists():
        _plans_path().write_text(initial_plans_content(), encoding="utf-8")


def read_plans() -> str:
    """读长期计划。"""
    _init_plans()
    return _plans_path().read_text(encoding="utf-8")


def manage_plan_item(action: str, goal: str, text: str) -> str:
    """管理长期计划的里程碑。"""
    _init_plans()
    content = _plans_path().read_text(encoding="utf-8")
    new_content, message = manage_plan_item_content(content, action, goal, text)
    if new_content != content:
        _plans_path().write_text(new_content, encoding="utf-8")
    return message


def _today_str() -> str:
    from datetime import datetime
    return datetime.now().astimezone().strftime("%Y-%m-%d")


# ---- 行为规则 (RULES) ----


def read_rules() -> str:
    """读行为规则。小而硬的文件，每次都全文读。"""
    if not _rules_path().exists():
        return "（还没有行为规则）"
    return _rules_path().read_text(encoding="utf-8")


def update_rules(text: str, mode: str = "append", entities: list[str] | None = None) -> None:
    """更新行为规则。mode: append（追加）/ replace（整体替换）。

    append 模式下自动去重：如果存在相同的 ## 场景：XXX 标题则覆盖旧段。
    """
    _ensure_files()
    if not _rules_path().exists():
        _rules_path().write_text("# 行为规则 RULES\n\n", encoding="utf-8")
    if mode == "replace":
        _rules_path().write_text(text, encoding="utf-8")
        if entities:
            _write_entities(entities, memory_type="rule",
                            memory_id=f"rule-replace:{now_iso()}", snippet=text[:200])
        return

    # Append mode with title dedup
    import re as _re
    existing = _rules_path().read_text(encoding="utf-8")

    # Extract ## 场景：XXX from new text
    title_match = _re.search(r"##\s*场景[：:]\s*(.+)", text)
    if title_match:
        scene_title = title_match.group(1).strip()
        # Check if same title exists in existing rules
        for match in _re.finditer(r"##\s*场景[：:]\s*(.+)", existing):
            if match.group(1).strip() == scene_title:
                # Found duplicate title → replace the old section
                start = match.start()
                next_section = _re.search(r"\n##\s", existing[start + len(match.group()):])
                if next_section:
                    end = start + len(match.group()) + next_section.start()
                else:
                    end = len(existing)
                new_content = existing[:start] + text.strip() + "\n" + existing[end:]
                _rules_path().write_text(new_content, encoding="utf-8")
                if entities:
                    _write_entities(entities, memory_type="rule",
                                    memory_id=f"rule:{scene_title}", snippet=text[:200])
                return

    # No duplicate title → normal append
    entry = f"\n{text.strip()}\n"
    with _rules_path().open("a", encoding="utf-8") as f:
        f.write(entry)
    if entities:
        _write_entities(entities, memory_type="rule",
                        memory_id=f"rule:{now_iso()}", snippet=text[:200])


# ---- 交接上下文 (CONTEXT) ----


def read_context() -> str:
    """读当前交接上下文。每天覆盖一次，文件小。"""
    if not _context_path().exists():
        return ""
    return _context_path().read_text(encoding="utf-8")


def update_context(text: str) -> None:
    """写入交接上下文，覆盖旧内容。"""
    _ensure_files()
    _context_path().write_text(f"# 交接上下文\n\n{text.strip()}\n", encoding="utf-8")


# ---- 经验教训 (LESSONS) ----


def read_lessons(n: int = 3) -> str:
    """读最近 n 条教训。"""
    if not _lessons_path().exists():
        return ""
    content = _lessons_path().read_text(encoding="utf-8")
    entries = [e.strip() for e in content.split("\n---\n") if e.strip() and not e.strip().startswith("#")]
    if not entries:
        return ""
    return "\n---\n".join(entries[-n:])


# ---- 灵感碎片 (INSIGHTS) ----

_INSIGHT_FILE_HEADER = """# 灵感碎片 INSIGHTS

过程中的碎片——闪念、卡点、质疑、警告——尽量短一句记一笔。
self_review 时统一拾起来集中处理。

kind 语义：
- idea    闪现的洞察（不一定验证）
- doubt   对当前做法/假设的质疑
- block   具体卡点（卡在哪、缺什么、可能解法）
- warning 反复出现的模式 / 违反规则的警觉
- status  跨睡眠的连续性（保留语义；record_thought kind=status 走 CONSCIOUSNESS.md 不写这里）
"""


def append_insight(*, kind: str, text: str, tag: str = "", entities: list[str] | None = None) -> Path:
    """追加一条灵感碎片到 INSIGHTS.md。

    Args:
        kind: idea / doubt / block / warning（status 不应走这里）
        text: 一句话内容
        tag: 可选补充
        entities: 关联实体（暂只记录不入向量库）

    Returns 写入后的文件路径。
    """
    if kind == "status":
        # status 不该走这里——record_thought 上层已分流，但兜底
        return _insights_path()

    _ensure_files()
    p = _insights_path()
    if not p.exists():
        p.write_text(_INSIGHT_FILE_HEADER, encoding="utf-8")
    # 一行紧凑记录，方便 sense_insights parse
    ts = now_iso()
    text_clean = (text or "").strip().replace("\n", " ")
    tag_clean = (tag or "").strip()
    tag_part = f" [{tag_clean}]" if tag_clean else ""
    line = f"\n- [{kind}] {ts}{tag_part} {text_clean}"
    with p.open("a", encoding="utf-8") as f:
        f.write(line)

    if entities:
        _write_entities(entities, memory_type="insight",
                        memory_id=f"insight:{ts}", snippet=text)

    return p


def read_insights(*, days_back: int = 1, kinds: list[str] | None = None) -> str:
    """读近 days_back 天内的灵感碎片，可按 kind 过滤。

    返回 markdown 文本，按时间倒序，用于 self_review / 任意 wake 上下文。
    """
    if not _insights_path().exists():
        return ""
    content = _insights_path().read_text(encoding="utf-8")
    # 跳过 header
    lines = [l for l in content.splitlines() if l.startswith("- [")]
    cutoff = _now_dt() - timedelta(days=days_back)
    out: list[str] = []
    for line in lines:
        # parse "[kind] ts ..."
        m = re.match(r"^-\s*\[(\w+)\]\s+([^\s]+)", line)
        if not m:
            continue
        kind = m.group(1)
        ts_str = m.group(2)
        try:
            dt = datetime.fromisoformat(ts_str)
        except Exception:
            continue
        if dt < cutoff:
            continue
        if kinds and kind not in kinds:
            continue
        out.append(line)
    if not out:
        return ""
    return "\n".join(out)


def clear_insights_older_than(days: int = 7) -> int:
    """self_review 收口后清理超期 insight，返回清理条数。"""
    if not _insights_path().exists():
        return 0
    content = _insights_path().read_text(encoding="utf-8")
    cutoff = _now_dt() - timedelta(days=days)
    keep: list[str] = []
    removed = 0
    in_header = True
    for line in content.splitlines():
        if in_header:
            keep.append(line)
            if line.startswith("- [") or line.startswith("kind 语义"):
                in_header = False
            continue
        m = re.match(r"^-\s*\[(\w+)\]\s+([^\s]+)", line)
        if not m:
            keep.append(line)
            continue
        try:
            dt = datetime.fromisoformat(m.group(2))
            if dt < cutoff:
                removed += 1
                continue
        except Exception:
            pass
        keep.append(line)
    _insights_path().write_text("\n".join(keep), encoding="utf-8")
    return removed


def add_lesson(text: str, entities: list[str] | None = None, section: str = "other") -> None:
    """追加一条经验教训,按主题分节写入。

    section 是主题分类,LESSONS.md 按这个组织。可选:
      - trading    交易策略 / 量化
      - system     代码工程 / 系统行为
      - tool       工具使用 (express_to_human / terminal / sense_*)
      - workflow   工作方式 / 复盘方法论
      - rule       沟通规则 / 权限边界
      - other      其他
    自动去重:与新近条目相似度>60%则合并。
    若对应 section 在 LESSONS.md 还没建,自动创建 ## 标题。

    模型拿 prompt 时「最近 3 条 lessons」按时间倒序取(不分 section,
    但下次可以让 selector 按 wake 主题相关联)。
    """
    # 标准化 section
    _VALID_SECTIONS = {
        "trading": "交易策略",
        "system": "代码工程",
        "tool": "工具使用",
        "workflow": "工作方式",
        "rule": "沟通规则",
        "other": "其他",
    }
    if section not in _VALID_SECTIONS:
        section = "other"
    section_title = _VALID_SECTIONS[section]

    _ensure_files()
    if not _lessons_path().exists():
        _lessons_path().write_text("# 经验教训\n\n", encoding="utf-8")

    content = _lessons_path().read_text(encoding="utf-8")

    # 确保对应 ## section 存在;不存在就追加到末尾
    section_marker = f"## {section_title}"
    if section_marker not in content:
        if not content.endswith("\n\n"):
            content = content.rstrip("\n") + "\n\n"
        content += f"{section_marker}\n\n"
        _lessons_path().write_text(content, encoding="utf-8")

    # 解析整个文件:split by ## 标题获得各 section 文本
    sections = {}  # {section_title: [entry_str]}
    section_order = []  # 保持原顺序
    cur_title = "_header"  # 文件头(不在 ## 下的部分)
    cur_entries = []
    for line in content.split("\n"):
        if line.startswith("## "):
            # 收尾前一段
            sections[cur_title] = cur_entries
            section_order.append(cur_title)
            cur_title = line[3:].strip()
            cur_entries = []
        else:
            cur_entries.append(line)
    sections[cur_title] = cur_entries
    section_order.append(cur_title)
    if section_title not in sections:
        # 兜底:fallback to other
        section_title = "其他"
        if section_title not in sections:
            sections[section_title] = []
            section_order.append(section_title)

    my_section_entries = "\n".join(sections[section_title])
    my_section_items = [
        e.strip() for e in my_section_entries.split("\n---\n")
        if e.strip() and not e.strip().startswith("##")
    ]

    # Check last 5 entries for similarity > 60% → merge
    merged = False
    for i in range(len(my_section_items) - 1, max(len(my_section_items) - 6, -1), -1):
        if i < 0:
            break
        similarity = compute_text_similarity(text, my_section_items[i])
        if similarity > 0.6:
            ts = _now_dt().strftime("%Y-%m-%d %H:%M")
            my_section_items[i] = f"{my_section_items[i]}\n（{ts} 再次确认：{text.strip()[:80]}）"
            merged = True
            break

    if not merged:
        ts = _now_dt().strftime("%Y-%m-%d %H:%M")
        my_section_items.append(f"[{ts}] {text.strip()}")

    # 重新组装整份 LESSONS.md
    sections[section_title] = ("\n" + "\n---\n\n".join(my_section_items)).split("\n")
    new_lines = []
    for stitle in section_order:
        if stitle == "_header":
            new_lines.extend(sections[stitle])
        else:
            # 清理尾部空行 + 确保节末有 \n
            sect_text = "\n".join(sections[stitle]).rstrip()
            new_lines.append(f"## {stitle}\n")
            if sect_text:
                new_lines.append(sect_text)
            new_lines.append("")  # 空行分隔
    new_content = "\n".join(new_lines).rstrip() + "\n"
    _lessons_path().write_text(new_content, encoding="utf-8")

    if entities:
        try:
            from domain.memory.memory.consciousness.entity_index import bump_verification_for_entities
            bump_verification_for_entities(entities, "lesson")
        except Exception:
            pass
        _write_entities(entities, memory_type="lesson",
                        memory_id=f"lesson:{section}:{ts}", snippet=text)


# ---- 自我认知 (SELF_KNOWLEDGE) ----


def read_self_knowledge() -> str:
    """读自我认知档案。观察自己的行为模式。"""
    if not _self_knowledge_path().exists():
        return ""
    return _self_knowledge_path().read_text(encoding="utf-8")


def update_self_knowledge(text: str, mode: str = "append") -> None:
    """更新自我认知档案。mode: append（追加）/ replace（整体替换）。"""
    _ensure_files()
    if not _self_knowledge_path().exists():
        _self_knowledge_path().write_text("# 自我认知\n\n观察到的行为模式。这是我的自省档案。\n\n", encoding="utf-8")
    if mode == "replace":
        _self_knowledge_path().write_text(text, encoding="utf-8")
    else:
        entry = f"\n- [{now_iso()}] {text.strip()}\n"
        with _self_knowledge_path().open("a", encoding="utf-8") as f:
            f.write(entry)


def dedup_lessons() -> str:
    """对 lessons 做机械相似度分析，找出可能的重复条目。

    返回 JSON 字符串，供 weekly_review 使用。
    """
    import json as _json

    if not _lessons_path().exists():
        return _json.dumps({"groups": [], "note": "没有 lessons 文件"})

    content = _lessons_path().read_text(encoding="utf-8")
    entries = [e.strip() for e in content.split("\n---\n") if e.strip() and not e.strip().startswith("#")]

    if len(entries) < 2:
        return _json.dumps({"total": len(entries), "groups": [], "note": f"只有 {len(entries)} 条，无需去重"})

    groups = []
    used: set[int] = set()
    for i, e1 in enumerate(entries):
        if i in used:
            continue
        group = [i]
        for j, e2 in enumerate(entries):
            if j <= i or j in used:
                continue
            sim = compute_text_similarity(e1, e2)
            if sim > 0.7:
                group.append(j)
                used.add(j)
        if len(group) > 1:
            groups.append({
                "indices": group,
                "entries": [entries[idx][:120] for idx in group],
                "suggestion": "合并为一条精华版" if len(group) >= 3 else "可能重复，考虑合并",
            })
            used.add(i)

    return _json.dumps({"total": len(entries), "groups_found": len(groups), "groups": groups}, ensure_ascii=False)


def check_memory_health() -> str:
    """检查各记忆文件的健康状况，返回 JSON 字符串。"""
    import json as _json

    report: dict[str, Any] = {}

    # RULES
    try:
        rules = read_rules()
        rules_lines = len(rules.splitlines()) if rules else 0
        rules_sections = rules.count("## 场景") if rules else 0
        report["rules"] = {"lines": rules_lines, "sections": rules_sections,
                           "warning": "超过 20 条，建议精简" if rules_sections > 20 else None}
    except Exception as e:
        report["rules"] = {"error": str(e)}

    # LESSONS
    try:
        lessons = read_lessons(n=50)
        lessons_entries = len([e for e in lessons.split("\n---\n") if e.strip() and not e.strip().startswith("#")]) if lessons else 0
        report["lessons"] = {"entries": lessons_entries,
                             "warning": "超过 15 条，建议整理" if lessons_entries > 15 else None}
    except Exception as e:
        report["lessons"] = {"error": str(e)}

    # SCRATCHPAD
    try:
        sp = read_scratchpad()
        sp_chars = len(sp) if sp else 0
        report["scratchpad"] = {"chars": sp_chars,
                                "warning": "超过 5000 字符，已自动截断" if sp_chars > 5000 else None}
    except Exception as e:
        report["scratchpad"] = {"error": str(e)}

    # CONSCIOUSNESS
    try:
        ct = read_recent_thoughts(n=50)
        ct_entries = len(ct.split(_ENTRY_SEP)) - 1 if ct else 0
        report["consciousness"] = {"entries": ct_entries,
                                   "warning": "超过 50 条，将自动归档" if ct_entries > 50 else None}
    except Exception as e:
        report["consciousness"] = {"error": str(e)}

    return _json.dumps(report, ensure_ascii=False)


__all__ = [
    "record_thought",
    "read_recent_thoughts",
    "read_last_thought",
    "write_diary",
    "read_recent_diary",
    "write_about_him",
    "read_about_him",
    "read_scratchpad",
    "update_scratchpad",
    "read_work",
    "add_work_item",
    "start_work_item",
    "complete_work_item",
    "remove_work_item",
    "read_goals",
    "manage_goal",
    "read_plans",
    "manage_plan_item",
    "read_rules",
    "update_rules",
    "read_context",
    "update_context",
    "read_lessons",
    "add_lesson",
    "read_self_knowledge",
    "update_self_knowledge",
    "dedup_lessons",
    "check_memory_health",
    "read_daily",
    "plan_daily",
    "add_planned_item",
    "complete_planned_item",
    "check_daily",
    "log_sent_message",
    "read_recent_sent",
]
