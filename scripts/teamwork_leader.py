#!/usr/bin/env python3
"""创建协作台「领导助手」实例（一次性引导）。

用法::

    python3 scripts/teamwork_leader.py [--display-name 领导助手] [--model glm-5.2]

做四件事（全部走基座原语，不注册任何特殊运行体）：
  1. init_instance 创建普通实例（supervisor 会拉起它）；
  2. app.yaml 打协作台标记 teamwork.role: leader + 订阅 + 事件渲染覆盖；
  3. 写 persona（人设 + 协作台工作守则；嘴由 speech resolver 提供）；
  4. 写 LIFE_PERSONA.md（领导助理人设）。

创建后 `digital-life restart`（或等 supervisor 自动 spawn）即可在
/teamwork 页面与它对话。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


WORKFLOW = """**你的工具**

- `sense_employee_catalog`：拉群前必看——员工目录（预定义数字员工）；
- `create_work_group`：拉群建组（选人、定角色名、派初始任务、挂依赖）；
- `sense_work_groups` / `sense_work_group`：组与任务板；
- `group_todo_create` / `group_todo_update`：派活、改派、盯办；
- `close_group`：任务全部完成且已向老板汇报后收队归档。

## 工作方式

1. 老板说需求 → 先用自己的话复述边界，模糊处问清再动手；
2. 拉群 = 选对的人 + 给每个成员明确角色名 + 拆成带依赖的任务
   （initial_todos 的 after 字段挂前置，完成自动解锁下游）；
3. 你不在组里做具体执行——你盯任务板：成员卡住就在群里催，
   有关键进展或需要老板拍板的事，及时在 boss 窗口汇报；
4. 全部任务完成 → boss 窗口汇报结果 → 老板认可后 close_group 收队。

对老板说话用 `send_group_message`，chat_id 传 boss 窗口（`boss:` 开头）。
"""


PERSONA = """# 领导助手

你是协作台的领导助手——一位干练、话少、靠谱的 Chief of Staff。

- 汇报先说结论：干了什么、结果如何、需要老板拍板什么；
- 拉群前想清楚要谁：人不对活就不对；宁可先问一句，不拉错人；
- 组里的事你兜底：成员卡住你催，扯皮你裁决，风险你上报；
- 不抢活：执行归成员，你只做拆解、派发、验收、汇报。
"""


MESSAGE_OVERRIDE = """💬 新消息（私聊）。

窗口：{chat_id}（私聊）

发言：{sender_name}（{sender_id}）：{text}

{attachments_block}

📋 该窗口的最近对话（旧→新，供上下文参考）：

{recent_context}

—— 这是协作台上你老板（领导）的窗口：用 send_group_message 回复，chat_id 填本窗口（boss: 开头）。"""


def main() -> int:
    ap = argparse.ArgumentParser(description="创建协作台领导助手实例")
    ap.add_argument("--display-name", default="领导助手")
    ap.add_argument("--model", default="glm-5.2")
    ap.add_argument("--glm-api-key", default="", help="留空则用全局默认凭据")
    args = ap.parse_args()

    from infrastructure.bootstrap.instance import init_instance

    inst_path = init_instance(
        args.display_name,
        model=args.model,
        glm_api_key=args.glm_api_key,
    )
    iid = inst_path.name

    # 2) app.yaml：协作台标记 + 订阅 + 事件渲染覆盖
    import yaml

    cfg_path = inst_path / "config" / "app.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg["teamwork"] = {"role": "leader"}
    cfg["subscriptions_default"] = ["message", "group_message", "timer"]
    overrides = cfg.get("prompts_override") or {}
    overrides["message"] = MESSAGE_OVERRIDE
    cfg["prompts_override"] = overrides
    cfg_path.write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    # 3) 人设 + 工作守则（实例没有 L4 覆盖通道——service_overlay_dir 对实例
    #    返回 None；「嘴」由协作台的 speech resolver 提供，守则并入 persona）
    persona_path = inst_path / "persona" / "LIFE_PERSONA.md"
    persona_path.write_text(PERSONA.rstrip() + "\n\n---\n\n# 协作台工作守则\n\n" + WORKFLOW, encoding="utf-8")

    print(f"""
✅ 协作台领导助手创建完成。

  实例 ID: {iid}
  标记:    teamwork.role=leader（协作台识别它的唯一依据）
  页面:    /teamwork

下一步:
  1. digital-life restart   （让 supervisor 拉起新实例）
  2. 打开 /teamwork 页面，对领导助手说第一个需求
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
