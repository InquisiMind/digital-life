"""Configuration helpers for the project-owned Digital Life runtime.

配置层级（2026-06-17 重构）：
  1. config/default.yaml          — 系统级（端口/tick/精力/budget）
  2. apps/<id>/config/app.yaml    — 实例级（模型/飞书/技能/群聊）
  3. apps/<id>/config/secrets.env — 实例密钥（LLM_API_KEY / FEISHU_APP_SECRET）

旧的 config/secrets.env 全局文件已删除——密钥下沉到每实例。
旧的 apps/<id>/data/config.yaml 已废弃——实例配置统一到 config/app.yaml。
旧的 instances.yaml 已删除——实例发现改为扫 apps/*/config/app.yaml。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from infrastructure.config import (
    get_global_default_config_path,
    get_project_root,
)


_ENV_PATTERN = re.compile(r"\$\{([^}:]+)(?::-(.*?))?\}")


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base. Override wins on key conflict."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_yaml_file(path: Path) -> dict[str, Any]:
    """Load and return a YAML config file as dict. Returns {} on any error."""
    try:
        with path.open(encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _get_instance_config_path() -> Path:
    """apps/<id>/config/app.yaml — 统一走路径中枢（2026-09-29 服务型改造）。

    此前这里直接拼 apps/{id}，绕过了中枢；服务 worker 的模型/提示词配置
    会解析到不存在的 apps/{service_id}/config/。中枢版本对服务型二跳到
    定义层 apps/{agent_def_id}/config/app.yaml，实例行为零变化。
    """
    from infrastructure.config import get_instance_config_path

    return get_instance_config_path()


def _get_instance_secrets_path() -> Path:
    """apps/<id>/config/secrets.env — 统一走路径中枢（服务二跳到定义层密钥）。"""
    from infrastructure.config import get_instance_env_path

    return get_instance_env_path()


# 关键环境变量强制清单：永远以配置文件为准，shell export 也覆盖不了。
FORCED_ENV_KEYS = frozenset({
    "GLM_API_KEY",            # LLM 凭据（旧名，向后兼容）
    "LLM_API_KEY",            # LLM 凭据（新名，中性的 provider 无关 key 字段）
    "FEISHU_APP_SECRET",      # 飞书密钥
})


def load_runtime_dotenv(*, runtime_home: Path | None = None, project_env: Path | None = None) -> None:
    """加载实例的 config/secrets.env 到 os.environ。

    加载顺序（先找到的就加载）：
      1. apps/<id>/config/secrets.env  ← 实例密钥（新标准位置）
      2. project_env 参数（兼容老接口）
      3. runtime_home/.env（兼容老接口）

    FORCED_ENV_KEYS 中的变量强制覆盖（防 shell 污染旧 key）。
    """
    paths_to_try = []
    # 新标准位置：实例 config/secrets.env
    secrets_path = _get_instance_secrets_path()
    if secrets_path.exists():
        paths_to_try.append(secrets_path)
    # 兼容老接口
    if project_env and project_env.exists():
        paths_to_try.append(project_env)
    legacy_data_env = (runtime_home or Path("")) / ".env"
    if legacy_data_env.exists():
        paths_to_try.append(legacy_data_env)

    for path in paths_to_try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key in FORCED_ENV_KEYS:
                os.environ[key] = value
            else:
                os.environ.setdefault(key, value)


def expand_env_vars(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: expand_env_vars(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_env_vars(item) for item in value]
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda match: os.getenv(match.group(1), match.group(2) or ""), value)
    return value


def load_runtime_config(path: Path | None = None) -> dict[str, Any]:
    """加载合并的运行时配置。

    层级（后者覆盖前者）：
      1. config/default.yaml          — 系统级
      2. apps/<id>/config/app.yaml    — 实例级（deep_merge 覆盖）

    path 参数用于测试覆盖。正常流程不传——自动解析到当前实例。
    """
    load_runtime_dotenv()

    # 1. Base layer: global default.yaml
    result: dict[str, Any] = {}
    global_default = get_global_default_config_path()
    if global_default.exists():
        result = _load_yaml_file(global_default)

    # 2. Instance layer: apps/<id>/config/app.yaml（新版统一位置）
    instance_config = path or _get_instance_config_path()
    if instance_config.exists():
        instance_cfg = _load_yaml_file(instance_config)
        result = deep_merge(result, instance_cfg)

    return expand_env_vars(result) if isinstance(result, dict) else {}


def parse_reasoning_effort(effort: str | None) -> dict[str, str]:
    value = (effort or "").strip().lower()
    return {"effort": value} if value else {}


def resolve_runtime_provider(requested: str | None = None, config: dict[str, Any] | None = None) -> dict[str, str]:
    cfg = config or load_runtime_config()
    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    provider = (requested or os.getenv("DIGITAL_LIFE_INFERENCE_PROVIDER") or model_cfg.get("provider") or "").strip()
    # model name: 新格式 model.name → 旧格式 model.default
    model = str(model_cfg.get("name") or model_cfg.get("default") or model_cfg.get("model") or "").strip()
    # api_key: 优先从 YAML 读（model.api_key），fallback 到实例 secrets.env 加载的 env 变量
    api_key = str(model_cfg.get("api_key") or os.getenv("LLM_API_KEY") or os.getenv("GLM_API_KEY") or "").strip()
    base_url = str(model_cfg.get("base_url") or os.getenv("GLM_BASE_URL") or "").strip()
    api_mode = str(model_cfg.get("api_mode") or "chat_completions").strip() or "chat_completions"

    # Match custom_providers by name (supports both "custom:name" and bare "name")
    custom_name = None
    if provider.startswith("custom:"):
        custom_name = provider.split(":", 1)[1]
        provider_name = "custom"
    else:
        provider_name = provider or "openai"
        if provider_name not in ("openai",):
            custom_name = provider_name

    if custom_name:
        for item in cfg.get("custom_providers", []) or []:
            if isinstance(item, dict) and item.get("name") == custom_name:
                api_key = str(item.get("api_key") or item.get("api_key_env") or api_key).strip()
                # Resolve api_key_env: if the value looks like an env var name (not a real key), read from env
                api_key_env = str(item.get("api_key_env") or "").strip()
                if api_key_env and not (api_key_env.startswith("$") or len(api_key_env) > 80):
                    api_key = os.getenv(api_key_env, api_key)
                base_url = str(item.get("base_url") or base_url).strip()
                model = str(item.get("model") or model).strip()
                api_mode = str(item.get("api_mode") or api_mode).strip()
                provider_name = "custom"
                break

    api_key = os.getenv("OPENAI_API_KEY") if provider_name == "openai" and not api_key else api_key
    return {
        "provider": provider_name,
        "api_key": api_key or os.getenv("DIGITAL_LIFE_API_KEY", ""),
        "base_url": base_url or os.getenv("DIGITAL_LIFE_BASE_URL", ""),
        "api_mode": api_mode,
        "model": model,
    }


def resolve_channel_name(platform: str, name: str = "default") -> str:
    if name != "default":
        return name
    return (
        os.getenv(f"{platform.upper()}_HOME_CHANNEL")
        or os.getenv("LARK_HOME_CHANNEL")
        or ""
    ).strip()

