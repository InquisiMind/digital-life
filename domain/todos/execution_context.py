"""Build structured execution context from task metadata."""

from __future__ import annotations

import json
import re

from ._infra import tasks_dir


def build_execution_context(
    task: dict,
    speckit: dict | None,
    plans: list[dict],
    notes: list[dict],
    workspace_path: str,
    sessions: list[dict],
) -> dict:
    result: dict = {
        "requirements": [],
        "steps": [],
        "hints": [],
        "map": {"workspace_dir": workspace_path, "artifacts": [], "speckit_dir": None},
        "requirements_block": "",
    }

    ws_dir = tasks_dir() / task["id"]
    if ws_dir.exists():
        for name in ("NOTES.md", "DAILY.md", "PLAN.md", "WORK.md"):
            if (ws_dir / name).exists():
                result["map"]["artifacts"].append(name)
    if speckit:
        result["map"]["speckit_dir"] = f"{workspace_path}/speckit"
        for name in speckit:
            result["map"]["artifacts"].append(f"speckit/{name}")

    if speckit and speckit.get("spec.md"):
        spec = speckit["spec.md"]
        result["requirements_block"] = spec[:2000] if len(spec) > 2000 else spec

        for line in spec.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                continue
            if "必须" in line or "要求" in line or "不能" in line or "应该" in line:
                result["requirements"].append(line[:200])
            elif re.match(r"^[\-\*\d]+[\.\)]", line) and len(line) > 10:
                result["requirements"].append(line[:200])

    if speckit and speckit.get("tasks.md"):
        task_lines = speckit["tasks.md"].splitlines()
        step_num = 0
        for line in task_lines:
            line = line.strip()
            if re.match(r"^[\-\*]", line) and len(line) > 3:
                step_num += 1
                done = "[x]" in line.lower() or "[✓]" in line
                result["steps"].append({
                    "id": step_num,
                    "status": "done" if done else "pending",
                    "content": line.lstrip("-* ")[:200],
                })
    if speckit and speckit.get("tasks.json"):
        try:
            tasks_data = json.loads(speckit["tasks.json"])
            if isinstance(tasks_data, list):
                for t in tasks_data:
                    result["steps"].append({
                        "id": t.get("id", len(result["steps"]) + 1),
                        "status": t.get("status", "pending"),
                        "content": str(t.get("content") or t.get("title") or "")[:200],
                    })
        except (json.JSONDecodeError, TypeError):
            pass

    if plans:
        for p in plans:
            existing = {s["content"] for s in result["steps"]}
            content = str(p.get("content") or "")[:200]
            if content and content not in existing:
                result["steps"].append({
                    "id": p.get("id"),
                    "status": p.get("status", "pending"),
                    "content": content,
                })

    if notes:
        note_lines = [
            f"[{n.get('created_at', '')}] {str(n.get('content', ''))[:120]}"
            for n in notes[:3]
        ]
        result["recent_notes_abstract"] = "\n".join(note_lines)

    if sessions:
        result["past_progress"] = "\n".join(
            f"- {s.get('started_at', '')}: {str(s.get('digest', ''))[:150]}"
            for s in sessions[:3]
        )

    result["hints"] = _execution_hints(task, plans, notes, speckit)

    return result


def _execution_hints(task: dict, plans: list[dict], notes: list[dict], speckit: dict | None) -> list[str]:
    hints = []
    title = str(task.get("title", ""))
    description = str(task.get("description", "") or "")

    if not speckit and not plans:
        hints.append("此任务尚未拆解。必须先 task_plan create 拆出至少 2-3 个可执行步骤再动手")
    if speckit:
        hints.append("speckit 文件已就绪。先读 spec.md 理解目标，读 tasks.md 看具体步骤，再按顺序执行")
    if plans:
        pending = [p for p in plans if p.get("status") == "pending"]
        if pending:
            hints.append(f"当前有 {len(pending)} 个待执行的计划步骤，从第一个开始")

    is_code = any(kw in title + description for kw in ("代码", "修复", "bug", "开发", "实现", "脚本", "API", "数据", "部署", "测试"))
    if is_code:
        hints.append("这是代码工程任务：terminal 探查项目结构，execute_code 验证逻辑，实际运行测试而非只读源码")

    is_research = any(kw in title + description for kw in ("调研", "分析", "搜索", "了解", "学习", "研究", "报告", "文档"))
    if is_research:
        hints.append("这是调研任务：web_search 搜资料，terminal 做数据分析，task_note 整理发现")

    hints.append("每完成一个步骤：task_plan complete 标记 + task_note add 记录（做了什么、结果是什么、下一步）")
    if not notes:
        hints.append("此任务还没有笔记。从现在开始每次执行都要记录——这是你跨越 session 的记忆")

    hints.append("所有步骤做完后验证产出，然后用发言工具汇报 + task done 标记完成")
    hints.append("遇到阻塞不要硬撑：task_note 记录阻塞原因，尝试替代方案，仍不行就用发言工具（你的嘴，见系统提示词）请求协助")

    return hints
