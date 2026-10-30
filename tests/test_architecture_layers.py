"""架构分层守卫：基座五层禁止 import 业务层（business.*）。

分层契约（2026-10-23 zhp 拍板）：
- 基座 = gateway / interfaces / application / domain / infrastructure——
  通用运行时（实例型与服务型两类一等 agent 形态、通用机制），不知道任何
  具体业务的存在。
- 业务 = business/{app}（如元理 AI 战略咨询平台）——产品面前端、管理台
  HTTP 面、业务工具/治理/模版，全部住这里。
- 两层的唯一接缝：activate.py 经 infrastructure.extension 注册（工具模块、
  API 路由、事件类型/闸、调度任务、工具面策略、工作区提供者），以及
  apps/{id} 目录契约。业务不改基座文件；基座不 import 业务。

历史上真实漏过一次：服务台 admin 路由住在 application/api 且 import 了
business.consulting.tool_face（2026-10-23 已迁回业务层）。本守卫防再犯。
"""

from __future__ import annotations

import re
from pathlib import Path

BASE_ROOTS = ("gateway", "interfaces", "application", "domain", "infrastructure")
_IMPORT_BUSINESS = re.compile(r"^\s*(from\s+business[.\s]|import\s+business[\s.])")


def _py_files(root: Path):
    for p in sorted(root.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        yield p


def test_base_layers_do_not_import_business():
    repo = Path(__file__).resolve().parent.parent
    violations: list[str] = []
    for base in BASE_ROOTS:
        base_dir = repo / base
        if not base_dir.is_dir():
            continue  # 守卫不假设目录必然存在（打包/裁剪环境）
        for p in _py_files(base_dir):
            for lineno, line in enumerate(
                p.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if _IMPORT_BUSINESS.match(line):
                    violations.append(f"{p.relative_to(repo)}:{lineno}: {line.strip()}")
    assert not violations, (
        "基座层出现对业务层（business.*）的 import——分层契约被破坏，"
        "请把业务代码移回 business/{app} 或经 extension 扩展点倒转依赖：\n"
        + "\n".join(violations)
    )
