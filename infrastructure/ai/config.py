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


def _read_env_key_file(path: Path, key: str) -> str:
    """直接 parse 密钥文件取单个变量（不经 os.environ——进程级共享 env 不可污染）。"""
    try:
        if not path.exists():
            return ""
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            k, v = stripped.split("=", 1)
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def resolve_agent_model_profile(target_id: str, *, _depth: int = 0) -> dict[str, str] | None:
    """解析 ``model.profile: agent:<id>`` 指针 → 该 agent 的有效模型配置。

    基座内置形态（后台子代理继承委托人的 key，见 docs/design/background-subagent.md）：
      1. 目标自有配置 apps/{target}/config/app.yaml 的 model 段（走路径中枢——
         服务二跳到 apps/{def}/services/{sid}/config/，席位指针本身也在这里）；
         若仍是 agent: 指针则递归解析（深度限 3，防环）。
      2. 服务无自有配置 → 定义层 app.yaml 的 model 段。
      3. 密钥：apps/{target}/config/secrets.env，服务侧回落定义层 secrets.env。
    指针即引用：委托人换 key 只改自己一处，子代理下个 worker 自动跟随（每次
    wake 重读的既有热更新语义）。业务形态（customer 访问码）不在此处理。
    """
    target = str(target_id or "").strip()
    if not target or _depth > 3:
        return None

    from infrastructure.config import get_instance_dir, get_instance_env_path, get_project_root

    apps_root = get_project_root() / "apps"
    entry: dict[str, str] = {}
    model: dict[str, Any] = {}

    own_cfg = _load_yaml_file(get_instance_dir(target) / "config" / "app.yaml")
    raw = own_cfg.get("model")
    if isinstance(raw, dict) and raw:
        model = raw
    if not model:
        try:
            from infrastructure.persistence import services_registry

            def_id = services_registry.resolve_service_def(target)
        except Exception:
            def_id = None
        if def_id:
            def_cfg = _load_yaml_file(apps_root / def_id / "config" / "app.yaml")
            raw = def_cfg.get("model")
            if isinstance(raw, dict) and raw:
                model = raw

    prof = str(model.get("profile") or "").strip()
    if prof.startswith("agent:"):
        nested = resolve_agent_model_profile(prof.split(":", 1)[1], _depth=_depth + 1)
        if nested:
            entry.update(nested)
    for k in ("api_key", "base_url", "name"):
        v = str(model.get(k) or "").strip()
        if v:
            entry[k] = v
    if not entry.get("api_key"):
        key = _read_env_key_file(get_instance_env_path(target), "LLM_API_KEY")
        if not key:
            try:
                from infrastructure.persistence import services_registry

                def_id = services_registry.resolve_service_def(target)
            except Exception:
                def_id = None
            if def_id:
                key = _read_env_key_file(
                    apps_root / def_id / "config" / "secrets.env", "LLM_API_KEY"
                )
        if key:
            entry["api_key"] = key
    return entry or None


def load_runtime_config(path: Path | None = None) -> dict[str, Any]:
    """加载合并的运行时配置。

    层级（后者覆盖前者）：
      1. config/default.yaml          — 系统级（入库；模型全局默认在此）
      2. config/local.yaml            — 机器级/密钥（不入 git；model.api_key 在此）
      3. apps/<id>/config/app.yaml    — 实例级（deep_merge 覆盖）

    path 参数用于测试覆盖。正常流程不传——自动解析到当前实例。
    """
    load_runtime_dotenv()

    # 1. Base layer: global default.yaml
    result: dict[str, Any] = {}
    global_default = get_global_default_config_path()
    if global_default.exists():
        result = _load_yaml_file(global_default)

    # 2. Machine layer: config/local.yaml（不入 git——密钥/机器偏好写这里）
    local_path = get_project_root() / "config" / "local.yaml"
    if local_path.exists():
        result = deep_merge(result, _load_yaml_file(local_path))

    # 3. Instance layer: apps/<id>/config/app.yaml（新版统一位置；
    #    服务型二跳到定义层——模型/提示词配置共享自 agent 定义）
    instance_config = path or _get_instance_config_path()
    if instance_config.exists():
        instance_cfg = _load_yaml_file(instance_config)
        result = deep_merge(result, instance_cfg)
    # 4. Service-own layer: apps/{def}/services/{sid}/config/app.yaml（存在才生效）
    #    服务实例的自有覆盖（如测试席位盖章的 model 段）——盖在定义层之上；
    #    不存在时行为与现状完全一致（向后兼容）。
    try:
        from infrastructure.config import get_instance_dir, get_app_instance_id
        own = get_instance_dir(get_app_instance_id()) / "config" / "app.yaml"
        if own.exists() and own != instance_config:
            own_cfg = _load_yaml_file(own)
            result = deep_merge(result, own_cfg)
            # 席位引用：model.profile → 用户配置（config/local.yaml 的 testers[码]）。
            # 实例只存指针不存密钥——用户改 key 只改用户配置一处，
            # 下一个 worker 进程（每次唤醒新拉起）自动生效，无需刷新实例。
            prof = str((own_cfg.get("model") or {}).get("profile") or "").strip()
            if prof:
                entry = None
                if prof.startswith("agent:"):
                    # 基座内置形态：agent:<id> —— 继承另一运行体的模型与 key
                    # （后台子代理 → 委托人）。业务形态（customer 访问码）不走这里。
                    entry = resolve_agent_model_profile(prof.split(":", 1)[1])
                if not entry:
                    try:
                        # 用户自助配置优先（业务存储，经扩展钩子取）
                        from infrastructure import extension as _ext
                        entry = _ext.resolve_model_profile(prof)
                    except Exception:
                        entry = None
                if not entry:
                    entry = (result.get("testers") or {}).get(prof)  # 管理员预置兜底
                if isinstance(entry, dict):
                    m = result.setdefault("model", {})
                    for k in ("api_key", "base_url", "name"):
                        v = str(entry.get(k) or "").strip()
                        if v:
                            m[k] = v
    except Exception:
        pass

    return expand_env_vars(result) if isinstance(result, dict) else {}


def parse_reasoning_depth(config: dict[str, Any] | None) -> dict[str, Any]:
    """从三层合并后的运行配置取这个人的思考深度，产出 provider 的 reasoning_config。

    - ``agent.reasoning_depth``：quick | standard | deep（人的基本配置）；缺省或
      非法值回落 DEFAULT_REASONING_DEPTH（非法值记 WARNING，不静默）。
    - ``model.reasoning_depth_map``：对单档厂商值的声明式覆盖（网关值域与家族
      不同时用，如 newapi deep→max）；只保留合法档位键与非空字符串值。
    每次模型调用的深度就是这里的 depth——运行时没有别的场景规则。
    """
    import logging

    from infrastructure.ai.providers import DEFAULT_REASONING_DEPTH, REASONING_DEPTHS

    cfg = config if isinstance(config, dict) else {}
    agent_cfg = cfg.get("agent") if isinstance(cfg.get("agent"), dict) else {}
    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}

    depth = str(agent_cfg.get("reasoning_depth") or "").strip().lower()
    if depth and depth not in REASONING_DEPTHS:
        logging.getLogger("digital_life.infrastructure.ai.config").warning(
            "agent.reasoning_depth=%r 不在 %s 中，按 %s 处理",
            depth, "/".join(REASONING_DEPTHS), DEFAULT_REASONING_DEPTH,
        )
        depth = ""
    out: dict[str, Any] = {"depth": depth or DEFAULT_REASONING_DEPTH}

    raw_map = model_cfg.get("reasoning_depth_map")
    if isinstance(raw_map, dict):
        depth_map = {
            str(k).strip().lower(): str(v).strip()
            for k, v in raw_map.items()
            if str(k).strip().lower() in REASONING_DEPTHS and str(v or "").strip()
        }
        if depth_map:
            out["depth_map"] = depth_map
    return out


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

