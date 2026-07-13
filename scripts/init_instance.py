#!/usr/bin/env python3
"""Bootstrap a new Digital Life instance.

Creates the full directory structure under a UUID-based directory,
generates config/app.yaml + config/secrets.env, and initializes state DB.

Usage:
    # 交互式（推荐）:
    python scripts/init_instance.py

    # 纯 CLI:
    python scripts/init_instance.py --display-name "Beta" \\
        --model glm-5.2 --glm-api-key "xxx" \\
        --feishu-app-id "cli_xxx" --feishu-app-secret "xxx"

环节:
  1. 创建目录骨架 (persona / config / data)
  2. 拷人设模板 (LIFE_PERSONA.md)
  3. 生成 config/app.yaml + config/secrets.env
  4. 初始化全部 SQLite schema
  5. 建 BLOCKED life affair
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import uuid as _uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 默认技能列表（新实例标配）
DEFAULT_SKILLS = [
    "daily_planner",
    "proactive",
    "self_review",
    "project_management",
    "project_bootstrap",
    "entity_curation",
    "todo_execution",
    "todo_planning",
]


# ── 校验 ──

def _validate_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise SystemExit("显示名称不能为空")
    return name


# ── 目录 ──

def _create_dirs(inst_dir: Path) -> None:
    for sub in ("persona", "config", "data"):
        (inst_dir / sub).mkdir(parents=True, exist_ok=True)
    print("  ✓ 目录结构已创建")


def _copy_templates(inst_dir: Path) -> None:
    templates_dir = ROOT / "config" / "templates" / "persona"
    persona_dir = inst_dir / "persona"
    if not templates_dir.is_dir():
        print(f"  ⚠ 模板目录不存在: {templates_dir}")
        return
    copied = 0
    for f in sorted(templates_dir.iterdir()):
        if f.is_file() and not (persona_dir / f.name).exists():
            shutil.copy2(f, persona_dir / f.name)
            copied += 1
    if copied:
        print(f"  ✓ 已拷贝 {copied} 个人设模板")


# ── 配置文件生成 ──

def _gen_config(inst_dir: Path, display_name: str, *,
                model: str, glm_api_key: str,
                feishu_app_id: str, feishu_app_secret: str) -> None:
    """生成 config/app.yaml + config/secrets.env。"""
    config_dir = inst_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)

    # attention_keywords
    is_ascii = display_name.isascii()
    if is_ascii:
        kws = []
        for v in (display_name, display_name.upper(), display_name.capitalize(), display_name.lower()):
            if v not in kws:
                kws.append(v)
    else:
        kws = [display_name]
    keywords_yaml = "\n".join(f'    - "{k}"' for k in kws)

    # ── app.yaml ──
    app_yaml = f"""# {display_name} 实例配置
# 由 init_instance.py 生成，可手动修改。修改后 digital-life restart 生效。

active: true

# ── 实例信息 ──
display_name: {display_name}

# ── 模型 ──
model:
  name: {model}
  provider: glm
  base_url: https://open.bigmodel.cn/api/paas/v4
  # api_key 从 config/secrets.env 的 LLM_API_KEY 读取（旧名 GLM_API_KEY 也兼容）

# ── 消息通道（多通道并列）──
# 飞书：填 app_id + 在 secrets.env 填 FEISHU_APP_SECRET
# 微信：在控制台 /instance/<id>/config 点「微信扫码登录」获取 WECHAT_BOT_TOKEN
# 所有通道字段统一在 channels.<type> 下，无 messenger 旧段。
channels:
  feishu:
    type: feishu
    feishu_domain: https://open.feishu.cn     # 国内默认，国际版改 https://open.larksuite.com
    app_id: {feishu_app_id or '"cli_xxxxxxxxx"'}  # 控制台「飞书通道」填写
    # app_secret 从 config/secrets.env 的 FEISHU_APP_SECRET 读取
  wechat:
    type: wechat_clawbot
    domain: https://ilinkai.weixin.qq.com
    # bot_token 从 config/secrets.env 的 WECHAT_BOT_TOKEN 读取（扫码登录自动写入）

# ── 群聊行为 ──
group_chat:
  attention_keywords:
{keywords_yaml}
  owner_names: []

# ── 已注册技能 ──
skills:
"""
    for s in DEFAULT_SKILLS:
        app_yaml += f"  - {s}\n"

    (config_dir / "app.yaml").write_text(app_yaml, encoding="utf-8")
    print("  ✓ config/app.yaml 已创建")

    # ── secrets.env ──
    secrets_env = f"""# {display_name} 实例密钥（不入 git）
LLM_API_KEY={glm_api_key or "你的 LLM API Key（GLM/DeepSeek/OpenAI 均可）"}
FEISHU_APP_SECRET={feishu_app_secret or "你的飞书 App Secret"}
WECHAT_BOT_TOKEN=
# WECHAT_BOT_TOKEN 通过控制台扫码登录自动填入，留空表示未开通微信通道
"""
    (config_dir / "secrets.env").write_text(secrets_env, encoding="utf-8")
    print("  ✓ config/secrets.env 已创建")


# ── 数据库 ──

def _init_schemas() -> None:
    from domain.lifecycle.schema import init_all_schemas
    init_all_schemas()
    print("  ✓ state.db 已初始化")


def _create_affair() -> None:
    from domain.orchestration.lifecycle_orchestration.bootstrap.runtime import ensure_life_affair
    ensure_life_affair()
    print("  ✓ Life affair 已创建 (BLOCKED)")


# ── 交互 ──

def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        return input(f"{prompt}{suffix}: ").strip() or default
    except EOFError:
        return default


# ── 主入口 ──

def init_instance(display_name: str, *,
                  model: str = "glm-5.2",
                  glm_api_key: str = "",
                  feishu_app_id: str = "",
                  feishu_app_secret: str = "") -> Path:
    display_name = _validate_name(display_name)
    instance_uuid = str(_uuid.uuid4())
    inst_dir = ROOT / "apps" / instance_uuid

    if inst_dir.exists():
        raise SystemExit(f"实例目录已存在: {inst_dir}")

    os.environ["DIGITAL_LIFE_INSTANCE_ID"] = instance_uuid

    print(f"\n初始化新实例")
    print(f"  UUID:  {instance_uuid}")
    print(f"  名称:  {display_name}")
    print(f"  模型:  {model}")
    print(f"  目录:  {inst_dir}\n")

    _create_dirs(inst_dir)
    _copy_templates(inst_dir)
    _gen_config(inst_dir, display_name,
                model=model, glm_api_key=glm_api_key,
                feishu_app_id=feishu_app_id, feishu_app_secret=feishu_app_secret)
    _init_schemas()
    _create_affair()

    print(f"""
✅ 实例创建完成。

  UUID: {instance_uuid}
  名称: {display_name}

下一步:
  1. 人设定制（可选）: 编辑 {inst_dir}/persona/LIFE_PERSONA.md
  2. 密钥确认: 编辑 {inst_dir}/config/secrets.env 填入 LLM_API_KEY + FEISHU_APP_SECRET
  3. 启动: digital-life start
  4. 验证: http://localhost:8642/employee/{instance_uuid}/
""")
    return inst_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化新的数字生命实例")
    parser.add_argument("--display-name", default="", help="实例显示名称")
    parser.add_argument("--model", default="glm-5.2", help="模型名称（默认 glm-5.2）")
    parser.add_argument("--glm-api-key", "--llm-api-key", dest="glm_api_key", default="", help="LLM API Key（GLM / DeepSeek / OpenAI 等均可）")
    parser.add_argument("--feishu-app-id", default="", help="飞书 App ID")
    parser.add_argument("--feishu-app-secret", default="", help="飞书 App Secret")
    parser.add_argument("--no-interactive", action="store_true", help="跳过交互")
    args = parser.parse_args()

    display_name = args.display_name
    model = args.model
    glm_key = args.glm_api_key
    feishu_id = args.feishu_app_id
    feishu_secret = args.feishu_app_secret

    if not args.no_interactive:
        print("\n── 实例初始化向导 ──\n")
        display_name = display_name or _ask("实例显示名称")
        model = _ask("模型名称", model)
        glm_key = glm_key or _ask("LLM API Key（任意 OpenAI 兼容模型）")
        feishu_id = feishu_id or _ask("飞书 App ID (cli_xxx)")
        feishu_secret = feishu_secret or _ask("飞书 App Secret")

    init_instance(display_name,
                  model=model, glm_api_key=glm_key,
                  feishu_app_id=feishu_id, feishu_app_secret=feishu_secret)


if __name__ == "__main__":
    main()
