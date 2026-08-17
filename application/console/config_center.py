"""Structured configuration workflow for the employee console."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from application.contracts import UseCaseResult
from infrastructure.config import (
    get_app_persona_path,
    get_app_skills_dir,
    get_instance_config_path,
    get_instance_env_path,
    get_project_root,
    get_runtime_home,
    get_runtime_memories_dir,
    get_runtime_state_db_path,
)


@dataclass(frozen=True)
class ConfigField:
    key: str
    label: str
    section: str
    source: str
    value_type: str = "string"
    path: str | None = None
    default: Any = ""
    secret: bool = False
    restart_required: bool = True
    readonly: bool = False
    options: tuple[str, ...] = ()
    description: str = ""


SECTION_META: dict[str, dict[str, str]] = {
    "employee":  {"label": "实例身份", "description": "数字员工身份。"},
    "model":     {"label": "模型配置", "description": "主模型 + Provider + API Key。"},
    "feishu":    {"label": "飞书通道", "description": "飞书凭证。改动后需重启网关。"},
    "wechat":    {"label": "微信通道", "description": "ClawBot 凭证。仅私聊。点扫码登录自动获取 Token。"},
    "behavior":  {"label": "群聊行为", "description": "注意关键词 / Owner——与通道无关，是数字生命的行为策略。"},
    "runtime":   {"label": "运行节律 / 精力策略", "description": "心跳、token 上限、精力折算系数。"},
    "tasks":     {"label": "任务执行策略", "description": "最大轮数、推理强度。"},
    "perception": {"label": "感知系统", "description": "快捷键触发录屏录音，视觉模型理解后注入事件。需授予辅助功能/屏幕录制/麦克风权限。改动后需重启网关。"},
    "social":    {"label": "社交接管范围", "description": "飞书全接管的群聊白名单/黑名单。控制数字生命以你的身份拉取哪些群的消息（隐私边界）。改动后下一轮拉取生效。"},
}


FIELDS: tuple[ConfigField, ...] = (
    # ════════════ 实例身份 ════════════
    ConfigField("display_name", "显示名称", "employee", "yaml", path="display_name", description="实例身份名（chat_stream「我自己」/ 前端实例标签 / 日志前缀）。"),

    # ════════════ 模型配置 ════════════
    ConfigField(
        "model.name", "主模型", "model", "yaml", path="model.name",
        default="glm-5.2",
        description="任意 OpenAI 兼容模型（glm-5.2 / deepseek-v3 / gpt-4o）。",
    ),
    ConfigField(
        "model.provider", "Provider", "model", "yaml", path="model.provider",
        default="glm",
        description="glm / deepseek / generic_openai / openai_reasoning。",
    ),
    ConfigField(
        "model.base_url", "Base URL", "model", "yaml", path="model.base_url",
        default="https://open.bigmodel.cn/api/paas/v4",
        description="LLM API endpoint。",
    ),
    ConfigField(
        "model.vision", "Vision 模型", "model", "yaml", path="model.vision",
        default="glm-4.6v",
        description="sense_image 工具使用的视觉模型（智谱 glm-4.6v / glm-5v-turbo / glm-4.5v）。"
                    "用在收到图片消息时的图片描述，默认 glm-4.6v。",
    ),
    ConfigField(
        "LLM_API_KEY", "API Key", "model", "env", secret=True,
        description="LLM API Key（敏感）。留空保存 = 保留当前值。",
    ),

    # ════════════ 飞书通道（固定展示，不依赖动态检测）════════════
    # ConfigField.key 是前端逻辑标识符（保持稳定便于 baseline 比对），
    # field.path 才是 yaml 落盘的真实路径。所有通道字段统一收敛到
    # channels.<type>.<field>，与 registry/adapter 读取侧一致。
    ConfigField(
        "messenger.app_id", "飞书 App ID", "feishu", "yaml", path="channels.feishu.app_id",
        description="飞书自建应用 App ID（cli_xxx）。",
    ),
    ConfigField(
        "FEISHU_APP_SECRET", "飞书 App Secret", "feishu", "env", secret=True,
        description="飞书 App Secret（敏感）。留空保存 = 保留当前值。",
    ),
    ConfigField(
        "messenger.feishu_domain", "飞书域名", "feishu", "yaml", path="channels.feishu.feishu_domain",
        default="https://open.feishu.cn",
        description="国内 open.feishu.cn / 国际 open.larksuite.com。",
    ),

    # ════════════ 微信通道（固定展示）════════════
    ConfigField(
        "messenger.wechat_domain", "微信 API 域名", "wechat", "yaml", path="channels.wechat.domain",
        default="https://ilinkai.weixin.qq.com",
        description="ClawBot iLink API 域名。",
    ),
    ConfigField(
        "WECHAT_BOT_TOKEN", "ClawBot Token", "wechat", "env", secret=True,
        description="ClawBot 扫码登录后自动写入。留空 = 未开通微信。",
    ),

    # ════════════ 群聊行为（与通道无关，是实例行为）════════════
    ConfigField(
        "group_chat.attention_keywords", "群聊关键词（立即响应）", "behavior", "yaml", "array",
        path="group_chat.attention_keywords",
        description="含这些词的群消息立即响应。其余群消息走 30s 累积窗口。",
    ),
    ConfigField(
        "group_chat.owner_names", "群聊 Owner（立即响应）", "behavior", "yaml", "array",
        path="group_chat.owner_names",
        description="这些人发言立即响应。",
    ),

    # ════════════ 运行时 ════════════
    ConfigField("L4_TICK_INTERVAL", "心跳间隔（秒）", "runtime", "env", "number", default=60),
    ConfigField(
        "DIGITAL_LIFE_STALE_RUNNING_SECONDS", "Stale RUNNING 判定阈值（秒）", "runtime", "env", "number",
        default=1800,
        description="cron 判定 wake 是否还活着的时间窗。仅在 wake 既无进程内标志、又无近期 turn 心跳时回退 BLOCKED。",
    ),
    ConfigField(
        "DIGITAL_LIFE_WAKE_ZOMBIE_SECONDS", "僵尸 wake 抢占阈值（秒）", "runtime", "env", "number",
        default=600,
        description="wake 占用 instance lock 超过此值视为僵尸，允许新 wake 抢占。必须小于 Stale RUNNING 阈值。",
    ),
    ConfigField("DIGITAL_LIFE_TOKEN_HOURLY_LIMIT", "Token 小时上限", "runtime", "env", "number", default=50000000),
    ConfigField("DIGITAL_LIFE_TOKEN_DAILY_LIMIT", "Token 日上限", "runtime", "env", "number", default=50000000),
    ConfigField("DIGITAL_LIFE_ENERGY_PER_KTOKEN_INPUT", "输入 token 精力系数", "runtime", "env", "number", default=0.005),
    ConfigField("DIGITAL_LIFE_ENERGY_PER_KTOKEN_OUTPUT", "输出 token 精力系数", "runtime", "env", "number",
        default=0.05, description="一天满跑 2000 万 token 耗尽 100 精力。"),
    ConfigField("DIGITAL_LIFE_ENERGY_RECOVERY_PER_HOUR", "每小时精力恢复", "runtime", "env", "number",
        default=25.0, description="默认 25 — 4 小时不动从 0 回满血。"),

    # ════════════ LLM 超时 / 重试 ════════════
    ConfigField(
        "DIGITAL_LIFE_API_READ_TIMEOUT", "LLM read 超时（秒）", "runtime", "env", "number", default=180,
        description="单次 LLM 调用等待响应的最长时间。GLM 长推理调高可减少无谓重试。",
    ),
    ConfigField(
        "DIGITAL_LIFE_LLM_CALL_MAX_DURATION", "单次 LLM 调用总时长上限（秒）", "runtime", "env", "number", default=240,
        description="含全部重试的 wall-clock 预算。耗尽即抛出，让事件级退避接管。必须低于 Stale 阈值 / 僵尸阈值。",
    ),
    ConfigField(
        "DIGITAL_LIFE_LLM_MAX_NET_RETRIES", "网络错误重试上限", "runtime", "env", "number", default=3,
        description="timeout/conn reset 等网络错误的指数退避重试次数（10/20/40s）。",
    ),
    ConfigField(
        "DIGITAL_LIFE_LLM_MAX_429_RETRIES", "429 限流重试上限", "runtime", "env", "number", default=3,
        description="429 限流的指数退避重试次数（5/10/20s，优先尊重 Retry-After）。",
    ),

    # ════════════ 任务策略 ════════════
    ConfigField("agent.max_turns", "最大执行轮数", "tasks", "yaml", "number", path="agent.max_turns", default=90),
    ConfigField(
        "agent.reasoning_effort", "推理强度", "tasks", "yaml", "select",
        path="agent.reasoning_effort", default="medium",
        options=("minimal", "low", "medium", "high", "xhigh"),
    ),

    # ════════════ 感知系统（feature 003-perception）════════════
    ConfigField(
        "perception.enabled", "启用感知", "perception", "yaml", "boolean",
        path="perception.enabled", default=False,
        description="开启后，本实例启动时自动拉起快捷键 daemon。首次需在 macOS 授予辅助功能/屏幕录制/麦克风权限。",
    ),
    ConfigField(
        "perception.hotkey", "快捷键", "perception", "yaml", "hotkey",
        path="perception.hotkey", default="cmd+shift+p",
        description='触发录屏录音的全局快捷键。点击右侧框后按下组合键自动捕获。不同实例配不同键，互不冲突。',
    ),
    ConfigField(
        "perception.vision_model", "视觉模型", "perception", "yaml",
        path="perception.vision_model", default="glm-4.6v",
        description="理解录屏画面的视觉模型。glm-4.6v / glm-5v-turbo。",
    ),
    ConfigField(
        "perception.frame_fps", "抽帧帧率", "perception", "yaml", "number",
        path="perception.frame_fps", default=2.0,
        description="录屏抽帧频率（帧/秒）。2 即每秒 2 帧，录屏画面变化慢，够用且省体积。",
    ),
    ConfigField(
        "perception.max_capture_seconds", "最大录制时长（秒）", "perception", "yaml", "number",
        path="perception.max_capture_seconds", default=120,
        description="单次录制最长秒数。超时自动结束并处理。",
    ),
    ConfigField(
        "perception.context_recent_turns", "视觉上下文轮数", "perception", "yaml", "number",
        path="perception.context_recent_turns", default=5,
        description="视觉模型带的主意识最近对话轮数（背景）。0 = 无背景纯看画面。",
    ),
    ConfigField(
        "perception.tts_enabled", "语音回复", "perception", "yaml", "boolean",
        path="perception.tts_enabled", default=False,
        description="开启后，快捷键呼唤的回复通过本地语音播放（macOS say）。需要 macOS。",
    ),
    ConfigField(
        "perception.tts_voice", "TTS 语音", "perception", "yaml",
        path="perception.tts_voice", default="Tingting",
        description="macOS 语音名。Tingting（婷婷，中文女声）/ Shelley / Sinji 等。",
    ),
    ConfigField(
        "perception.wake_words", "唤醒词", "perception", "yaml", "array",
        path="perception.wake_words", default=[],
        description="语音唤醒词列表（持续监听模式用）。填入实例名及 ASR 变体，如 zero/Zero/塞罗/吉洛。系统自动识别并路由到本实例。",
    ),
    ConfigField(
        "social.takeover.mode", "接管范围模式", "social", "yaml",
        path="social.takeover.mode", default="all",
        options=["all", "allowlist", "blocklist"],
        description="飞书全接管的群聊范围：all=拉全部群（默认）；allowlist=只拉白名单里的群；blocklist=排除黑名单里的群。",
    ),
    ConfigField(
        "social.takeover.allowlist", "接管白名单", "social", "yaml", "array",
        path="social.takeover.allowlist", default=[],
        description="mode=allowlist 时生效。填群名或 chat_id（oc_ 开头），如：数字生命讨论群。只拉这些群的消息。",
    ),
    ConfigField(
        "social.takeover.blocklist", "接管黑名单", "social", "yaml", "array",
        path="social.takeover.blocklist", default=[],
        description="mode=blocklist 时生效。填群名或 chat_id。不拉这些群的消息（隐私排除，如家人群）。",
    ),
)


class ConfigCenterWorkflow:
    """Expose a product-level configuration registry instead of raw env files."""

    def config(self, employee_id: str | None = None) -> UseCaseResult:
        try:
            env_path = get_instance_env_path(employee_id)
            yaml_path = get_instance_config_path(employee_id)
            env = self._load_env(env_path)
            yaml_config = self._load_yaml(yaml_path)
            sections = self._sections(env, yaml_config, employee_id)
            return UseCaseResult({
                "sections": sections,
                "schema": [self._field_schema(field) for field in FIELDS],
                "paths": self._paths(employee_id),
                "config": self._legacy_runtime_policy(env, yaml_config),
            })
        except Exception as exc:
            return UseCaseResult({"error": str(exc)}, 500)

    def update_config(self, body: dict[str, Any], employee_id: str | None = None) -> UseCaseResult:
        try:
            if not isinstance(body, dict):
                return UseCaseResult({"error": "Invalid JSON body"}, 400)
            values = body.get("values") if isinstance(body.get("values"), dict) else body
            fields_by_key = {field.key: field for field in FIELDS}
            env_updates: dict[str, str | None] = {}
            yaml_updates: dict[str, Any] = {}

            for key, raw_value in values.items():
                field = fields_by_key.get(key)
                if not field or field.readonly:
                    continue
                if field.secret and (raw_value is None or str(raw_value).strip() == ""):
                    continue
                value = self._coerce_value(raw_value, field)
                if field.source == "env":
                    env_updates[field.key] = self._env_text(value, field)
                elif field.source == "yaml" and field.path:
                    yaml_updates[field.path] = value

            # 显式用 employee_id 定位实例文件,避免依赖 ContextVar(middleware 可能未覆盖
            # 的内部调用方直调时会读到错实例)。
            env_path = get_instance_env_path(employee_id)
            yaml_path = get_instance_config_path(employee_id)
            if env_updates:
                self._write_env_updates(env_updates, env_path)
            if yaml_updates:
                self._write_yaml_updates(yaml_updates, yaml_path)
            return self.config(employee_id)
        except ValueError as exc:
            return UseCaseResult({"error": str(exc)}, 400)
        except Exception as exc:
            return UseCaseResult({"error": str(exc)}, 500)

    def _sections(self, env: dict[str, str], yaml_config: dict[str, Any], employee_id: str | None) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {key: [] for key in SECTION_META}
        for field in FIELDS:
            if field.section not in grouped:
                grouped[field.section] = []
        for field in FIELDS:
            grouped[field.section].append(self._field_payload(field, env, yaml_config))
        return [
            {
                "key": key,
                "label": meta["label"],
                "description": meta.get("description", ""),
                "fields": grouped.get(key, []),
            }
            for key, meta in SECTION_META.items()
            if grouped.get(key)  # 只返回有字段的 section
        ]

    def _field_payload(self, field: ConfigField, env: dict[str, str], yaml_config: dict[str, Any]) -> dict[str, Any]:
        value, origin = self._field_value(field, env, yaml_config)
        configured = self._is_configured(value)
        if field.secret:
            value = ""
        return {
            **self._field_schema(field),
            "value": value,
            "configured": configured,
            "origin": origin,
        }

    @staticmethod
    def _field_schema(field: ConfigField) -> dict[str, Any]:
        return {
            "key": field.key,
            "label": field.label,
            "section": field.section,
            "source": field.source,
            "type": field.value_type,
            "secret": field.secret,
            "restart_required": field.restart_required,
            "readonly": field.readonly,
            "options": list(field.options),
            "description": field.description,
        }

    def _field_value(self, field: ConfigField, env: dict[str, str], yaml_config: dict[str, Any]) -> tuple[Any, str]:
        # env 字段别名映射：新名 → 旧名（向后兼容旧 secrets.env 里的字段名）
        ENV_ALIASES = {
            "LLM_API_KEY": ["GLM_API_KEY"],   # 新名 LLM_API_KEY 兼容旧名 GLM_API_KEY
        }
        env_keys_to_try = [field.key] + ENV_ALIASES.get(field.key, [])

        if field.source == "env":
            for ek in env_keys_to_try:
                if ek in env:
                    return self._coerce_value(env[ek], field), "secrets.env"
            for ek in env_keys_to_try:
                if ek in os.environ:
                    return self._coerce_value(os.environ[ek], field), "process.env"
            return self._coerce_value(field.default, field), "default"
        if field.source == "yaml" and field.path:
            value = self._get_nested(yaml_config, field.path)
            if value is not None:
                return self._coerce_value(value, field), "local.yaml"
        return self._coerce_value(field.default, field), "default"

    @staticmethod
    def _is_configured(value: Any) -> bool:
        if isinstance(value, bool):
            return True
        if isinstance(value, list):
            return len(value) > 0
        return value is not None and str(value).strip() != ""

    @staticmethod
    def _legacy_runtime_policy(env: dict[str, str], yaml_config: dict[str, Any]) -> dict[str, Any]:
        """Snapshot summary used by the status page. Mirrors fields that are
        actually consumed by the runtime (token gate, energy simulation,
        tick interval) so the displayed numbers reflect real behavior.
        历史快照里曾包含 VITALS_/DEBUG_ENERGY_RECOVERY_AMOUNT/L4_HEARTBEAT_HOURS
        等环境变量，但代码并不读它们——已于精简中一并删除（避免让用户以为改了会生效）。
        """
        def number(key: str, default: float) -> float:
            try:
                return float(env.get(key, default))
            except (TypeError, ValueError):
                return default

        return {
            "runtime": {
                "tick_interval": number("L4_TICK_INTERVAL", 60),
                "token_hourly_limit": number("DIGITAL_LIFE_TOKEN_HOURLY_LIMIT", 50_000_000),
                "token_daily_limit": number("DIGITAL_LIFE_TOKEN_DAILY_LIMIT", 50_000_000),
            },
            "energy": {
                "per_ktoken_input": number("DIGITAL_LIFE_ENERGY_PER_KTOKEN_INPUT", 0.005),
                "per_ktoken_output": number("DIGITAL_LIFE_ENERGY_PER_KTOKEN_OUTPUT", 0.05),
            },
            "agent": {
                "runtime_provider": env.get("DIGITAL_LIFE_RUNTIME_PROVIDER", "l4"),
                "max_turns": ConfigCenterWorkflow._get_nested(yaml_config, "agent.max_turns") or 90,
                "reasoning_effort": ConfigCenterWorkflow._get_nested(yaml_config, "agent.reasoning_effort") or "medium",
            },
        }

    @staticmethod
    def _load_env(path: Path) -> dict[str, str]:
        if not path.exists():
            return {}
        values: dict[str, str] = {}
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
        return values

    @staticmethod
    def _write_env_updates(updates: dict[str, str | None], path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        # 反向别名：新字段写入时也保留老 key（向后兼容已有 secrets.env）。
        # 如 LLM_API_KEY 写入时如果文件已有 GLM_API_KEY 行 → 替换它（不新增一列）。
        ENV_WRITE_ALIASES = {
            "LLM_API_KEY": "GLM_API_KEY",
        }
        pending = dict(updates)
        written: list[str] = []
        consumed_keys: set[str] = set()
        for raw in lines:
            stripped = raw.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                written.append(raw)
                continue
            key = stripped.split("=", 1)[0].strip()
            # 精确匹配
            if key in pending:
                value = pending.pop(key)
                consumed_keys.add(key)
                if value not in (None, ""):
                    written.append(f"{key}={value}")
                continue
            # 别名匹配：旧 key 在文件里，新字段在 pending
            for new_key, old_key in ENV_WRITE_ALIASES.items():
                if key == old_key and new_key in pending:
                    value = pending.pop(new_key)
                    consumed_keys.add(new_key)
                    if value not in (None, ""):
                        written.append(f"{old_key}={value}")  # 保留旧 key 名
                    break
            else:
                written.append(raw)
        for key, value in pending.items():
            if key in consumed_keys:
                continue
            if value not in (None, ""):
                written.append(f"{key}={value}")
        path.write_text("\n".join(written).rstrip() + "\n", encoding="utf-8")

    @staticmethod
    def _load_yaml(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    @staticmethod
    def _write_yaml_updates(updates: dict[str, Any], path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = ConfigCenterWorkflow._load_yaml(path)
        for dotted, value in updates.items():
            ConfigCenterWorkflow._set_nested(raw, dotted, value)
        path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")

    @staticmethod
    def _get_nested(data: dict[str, Any], dotted: str) -> Any:
        current: Any = data
        for part in dotted.split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return current

    @staticmethod
    def _set_nested(data: dict[str, Any], dotted: str, value: Any) -> None:
        current = data
        parts = dotted.split(".")
        for part in parts[:-1]:
            next_value = current.get(part)
            if not isinstance(next_value, dict):
                next_value = {}
                current[part] = next_value
            current = next_value
        current[parts[-1]] = value

    @staticmethod
    def _coerce_value(value: Any, field: ConfigField) -> Any:
        if field.value_type == "array":
            if value is None or value == "":
                return []
            if isinstance(value, list):
                return [str(v).strip() for v in value if str(v).strip()]
            text = str(value)
            try:
                parsed = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                raise ValueError(f"{field.label} must be a YAML array: {exc}") from exc
            if parsed is None:
                return []
            if not isinstance(parsed, list):
                raise ValueError(f"{field.label} must be a list")
            return [str(v).strip() for v in parsed if str(v).strip()]
        if field.value_type == "boolean":
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in {"1", "true", "yes", "on"}:
                return True
            if text in {"0", "false", "no", "off"}:
                return False
            raise ValueError(f"{field.label} must be boolean")
        if field.value_type == "number":
            if value in (None, ""):
                return field.default
            number = float(value)
            return int(number) if number.is_integer() else number
        return "" if value is None else str(value).strip()

    @staticmethod
    def _env_text(value: Any, field: ConfigField) -> str:
        if field.value_type == "boolean":
            return "true" if bool(value) else "false"
        return str(value).strip()

    @staticmethod
    def _paths(employee_id: str | None) -> dict[str, str]:
        return {
            "project_root": str(get_project_root()),
            "runtime_home": str(get_runtime_home()),
            "config_path": str(get_instance_config_path(employee_id)),
            "env_path": str(get_instance_env_path(employee_id)),
            "state_db": str(get_runtime_state_db_path()),
            "memories_dir": str(get_runtime_memories_dir()),
            "persona_path": str(get_app_persona_path(employee_id)),
            "skills_dir": str(get_app_skills_dir(employee_id)),
        }


__all__ = ["ConfigCenterWorkflow"]
