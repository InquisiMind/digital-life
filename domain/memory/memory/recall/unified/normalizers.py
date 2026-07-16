"""P3 T039 — project / todo normalizer。让项目档案和待办进检索池。

设计:
- spec §User Story 3: "新增一类记忆源只需提供归一器 + 一组基线值"
- data-model.md: projects = cognition(authority 0.8), todos = experience(authority 0.5)

每个 source 产出一批 Slice(经 unified.slice.to_row 写入 chunks 表)。
被 scheduler 在 session end 调用一次(低频,跟 consolidate_after_session 同节奏)。
"""

from __future__ import annotations

import logging
import time
from typing import Iterator

from domain.memory.memory.recall.unified.slice import Slice, register_normalizer
from domain.memory.memory.recall.vector import _get_db, _chunk_hash

logger = logging.getLogger("domain.memory.recall.unified.normalizers")

# 注册基线值(§FR-303: 加源只需登记基线)
# 注意:这些 source 的名字也在 `_BASELINES` 里出现是 ok 的 — baseline 表不互斥,
# register_normalizer 是 register 而不是 exclusively define。
try:
    register_normalizer("project", {
        "phase": "cognition", "source_kind": "project_card",
        "authority": 0.8, "permanence": 0.9,
    })
except Exception:
    pass

try:
    register_normalizer("todo", {
        "phase": "experience", "source_kind": "todo",
        "authority": 0.4, "permanence": 0.2,
    })
except Exception:
    pass


def _load_projects() -> Iterator[dict]:
    """从 projects/*/project.yaml 读取项目。返回 dict id/name/description/status/manager。
    loader.load_all_projects() 返回 dict[str, ProjectConfig],值是 ProjectConfig dataclass。
    """
    try:
        from domain.project.loader import load_all_projects
        result = load_all_projects()
        # load 返回 dict[str, ProjectConfig];iterate values
        items_iter = result.values() if isinstance(result, dict) else result
        for p in items_iter:
            if isinstance(p, dict):
                # 兜底:返回 dict 序列化路径
                proj = p.get("project", p)
                yield {
                    "id": proj.get("id", ""),
                    "name": proj.get("name", ""),
                    "description": proj.get("description", ""),
                    "status": proj.get("status", ""),
                    "manager": proj.get("manager", ""),
                }
                continue
            # 主路径:ProjectConfig dataclass
            yield {
                "id": getattr(p, "id", ""),
                "name": getattr(p, "name", ""),
                "description": getattr(p, "description", ""),
                "status": getattr(p, "status", ""),
                "manager": getattr(p, "manager", ""),
            }
    except Exception as e:
        logger.warning("projects index: load_all_projects failed: %s", e)
        from pathlib import Path
        root = Path(__file__).resolve().parents[6]
        proj_root = root / "projects"
        if not proj_root.exists():
            return
        import yaml
        for ypath in proj_root.glob("*/project.yaml"):
            try:
                with open(ypath, encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                proj = data.get("project", data)
                if isinstance(proj, dict):
                    yield {
                        "id": proj.get("id", ""),
                        "name": proj.get("name", ""),
                        "description": proj.get("description", ""),
                        "status": proj.get("status", ""),
                        "manager": proj.get("manager", ""),
                    }
            except Exception:
                continue


def _load_todos(*, limit: int = 200) -> Iterator[dict]:
    """从 global_todos.db 读取活跃 todo。
    list_tasks 签名: status_filter / project_id / source / assignee_instance
    (没有 limit 参数 — list_tasks 全表返,我们外层取前 limit 个)
    """
    try:
        from domain.todos.crud import list_tasks
        seen = 0
        for status in ("in_progress", "planned"):
            rows = list_tasks(status_filter=status) or []
            for t in rows:
                yield t
                seen += 1
                if seen >= limit:
                    return
    except Exception as e:
        logger.warning("todos index: list_tasks failed: %s", e)


def _body_of_project(p: dict) -> str:
    name = str(p.get("name") or "").strip()
    desc = str(p.get("description") or "").strip()
    status = str(p.get("status") or "").strip()
    manager = str(p.get("manager") or "").strip()[:8]  # instance_id 截短避免泄漏
    parts = [f"# 项目:{name}"]
    if status:
        parts.append(f"状态: {status}")
    if desc:
        parts.append(desc)
    if manager:
        parts.append(f"(负责实例: {manager}…)")
    return "\n".join(parts)


def _body_of_todo(t: dict) -> str:
    """构造 todo slice 的 body, 把关键状态都纳入(用户 2026-07-16 发现 bug:
    完成 todo 跟未完成 todo 在切片里没差,关键信息丢了)。
    """
    title = str(t.get("title") or "").strip()
    desc = str(t.get("description") or "").strip()
    status = str(t.get("status") or "").strip()
    priority = str(t.get("priority") or "").strip()
    deadline = str(t.get("deadline") or "").strip()
    pid = str(t.get("project_id") or "").strip()
    criteria = str(t.get("acceptance_criteria") or "").strip()
    parts = [f"# 待办:{title}"]
    # 状态段落: status / priority / deadline / project 显式
    meta_line_parts = []
    if status:
        meta_line_parts.append(f"状态:{status}")
    if priority:
        meta_line_parts.append(f"优先级:{priority}")
    if deadline:
        meta_line_parts.append(f"截止:{deadline}")
    if pid:
        meta_line_parts.append(f"所属项目:{pid}")
    if meta_line_parts:
        parts.append(" · ".join(meta_line_parts))
    if desc:
        parts.append(desc)
    if criteria:
        parts.append(f"验收标准: {criteria}")
    return "\n".join(parts)


def index_projects_and_todos(*, max_total: int = 60, now: float | None = None) -> int:
    """把当前 projects/ + global_todos 转成 Slice 写进 chunks(幂等 + 增量)。
    每个 project / todo 一个 Slice。chunk_hash 用 stable id,保证多次调用幂等。
    返回写入/更新的行数。
    """
    if now is None:
        now = time.time()

    # 1. 构造一批 Slice 候选
    slices: list[Slice] = []
    for p in _load_projects():
        if not p.get("name"):
            continue
        pid = str(p.get("id") or p.get("name"))
        slice = Slice(
            source="project",
            chunk_hash=f"project:{pid}",
            body=_body_of_project(p),
            phase="cognition",
            source_kind="project_card",
            authority=0.8,
            permanence=0.9,
            freshness=1.0,
            attention_tokens=[str(p.get("name"))] if p.get("name") else [],
            provenance=f"project:{pid}",
            created_at=now,
        )
        slices.append(slice)
        if len(slices) >= max_total:
            return _flush(slices)
    for t in _load_todos():
        if not t.get("title"):
            continue
        tid = str(t.get("id") or t.get("title"))
        # 关键元数据进 slice 元字段(用户 2026-07-16 bug):
        # - status 进 source_kind 利于前端展示 + facade 过滤
        # - status=done/cancelled → freshness 衰一档(已完成的该让位未完成的)
        # - priority 调 authority(high 紧迫 → 召回权重高)
        # - tags 进 entity_links 当导航锚点(替代之前的空 [])
        # - title 仍进 attention_tokens(让搜索 'todo名' 容易命中)
        status = str(t.get("status") or "").strip()
        priority = str(t.get("priority") or "").strip().lower()
        tags = t.get("tags") or []
        if not isinstance(tags, list):
            try:
                import json as _json
                tags = _json.loads(tags) if isinstance(tags, str) else []
            except Exception:
                tags = []
        # authority 按 priority 分档
        auth = 0.4
        if priority == "high":
            auth = 0.6
        elif priority == "low":
            auth = 0.3
        # freshness 按 status
        fresh = 1.0
        if status in ("done", "cancelled"):
            fresh = 0.3  # 完成的"冷掉", 但不归档(响应历史查询)
        # cognition_state 给已完成的打 archived(§6.4 归档不硬删)
        cog_state = "archived" if status in ("done", "cancelled") else None
        # entity_links: tags + title 本身 + project_id
        links: list[str] = []
        for tag in tags:
            tag = str(tag).strip()
            if tag:
                links.append(tag)
        if t.get("title"):
            links.append(str(t["title"]))
        if t.get("project_id"):
            links.append(str(t["project_id"]))
        slice = Slice(
            source="todo",
            chunk_hash=f"todo:{tid}",
            body=_body_of_todo(t),
            phase="experience",
            source_kind=f"todo:{status}" if status else "todo",
            authority=auth,
            permanence=0.2,
            freshness=fresh,
            cognition_state=cog_state,
            entity_links=links,
            attention_tokens=[str(t.get("title"))] if t.get("title") else [],
            provenance=f"todo:{tid}",
            created_at=now,
        )
        slices.append(slice)
        if len(slices) >= max_total:
            break

    return _flush(slices)


def _flush(slices: list[Slice]) -> int:
    """把 slices 写进 chunks 表。无 embedding 暂不投检索语义路;
    但 FTS5 触发器会让它们走词法路 + attention 提权让它们在 unified_recall 里被命中。
    """
    if not slices:
        return 0
    db = None
    count = 0
    try:
        db = _get_db()
    except Exception as e:
        logger.warning("projects/todos index: cannot open vec db: %s", e)
        return 0
    try:
        for s in slices:
            row = s.to_row()
            # 不写 embedding(为 NULL)。FTS5 触发器会把 text 索引进去,
            # 词法 + attention 仍能拉到(spec §FR-201 三路并存、向量挂降级不影响其它路)。
            cols = list(row.keys())
            placeholders = ",".join("?" * len(cols))
            col_list = ",".join(cols)
            try:
                db.execute(
                    f"INSERT OR REPLACE INTO chunks ({col_list}) VALUES ({placeholders})",
                    [row[c] for c in cols],
                )
                count += 1
            except Exception as per_row:
                logger.debug("failed to insert slice %s: %s", s.chunk_hash, per_row)
        db.commit()
        logger.info("projects/todos index: %d slices persisted", count)
        return count
    finally:
        if db is not None:
            db.close()


__all__ = ["index_projects_and_todos"]
