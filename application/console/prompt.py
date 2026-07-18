"""Employee console prompt configuration workflow."""

from __future__ import annotations

from typing import Any

from application.contracts import UseCaseResult
from application.runtime_provider import console_runtime_adapter
from infrastructure.config import get_runtime_config_path


class PromptConsoleWorkflow:
    """Read and update prompt overrides without exposing HTTP details."""

    def prompts(self, employee_id: str | None = None) -> UseCaseResult:
        """Return system prompts only (LIFE_PERSONA + L4_LIFECYCLE_PROMPT).

        Event-related prompts are served via :meth:`events`.
        """
        prompts: list[dict[str, Any]] = []
        overrides = self._load_overrides()

        def get_prompt(name: str, default: str) -> str:
            return overrides.get(name, default)

        try:
            from domain.memory.context.selectors import MISSING_LIFE_PERSONA, load_life_persona
            from infrastructure.config import get_app_persona_path

            persona_path = get_app_persona_path(employee_id)
            life_persona = load_life_persona(employee_id)

            prompts.append({
                "name": "人格定义 (LIFE_PERSONA)",
                "key": "LIFE_PERSONA",
                "layer": "系统 Prompt",
                "trigger": "L4 唤醒时作为 ephemeral_system_prompt 注入",
                "file": persona_path.as_posix(),
                "content": life_persona,
                "original": MISSING_LIFE_PERSONA,
                "overridden": persona_path.exists(),
            })
        except Exception:
            pass
        try:
            from domain.identity.system_prompts import L4_LIFECYCLE_PROMPT

            prompts.append({
                "name": "L4 生命周期 (全局只读)",
                "key": "L4_LIFECYCLE_PROMPT",
                "layer": "全局 Prompt",
                "trigger": "始终注入 system prompt",
                "file": "domain/identity/system_prompts/__init__.py",
                "content": L4_LIFECYCLE_PROMPT,
                "original": L4_LIFECYCLE_PROMPT,
                "overridden": False,
                "read_only": True,
            })
        except Exception:
            pass

        return UseCaseResult({"prompts": prompts})

    def events(self, employee_id: str | None = None) -> UseCaseResult:
        """Return registered event types with full metadata + editable prompt template."""
        overrides = self._load_overrides()
        items: list[dict[str, Any]] = []

        try:
            items = console_runtime_adapter().list_event_prompt_configs(overrides)
        except Exception as exc:
            return UseCaseResult({"error": str(exc), "events": []}, 500)

        return UseCaseResult({"events": items})

    def update_prompt(self, name: str, content: str, employee_id: str | None = None) -> UseCaseResult:
        try:
            if not name:
                return UseCaseResult({"error": "name required"}, 400)

            import yaml
            from infrastructure.config import get_runtime_config_path

            if name == "LIFE_PERSONA":
                from infrastructure.config import get_app_persona_path

                persona_path = get_app_persona_path(employee_id)
                persona_path.parent.mkdir(parents=True, exist_ok=True)
                persona_path.write_text(content.rstrip() + "\n", encoding="utf-8")
                self._hot_reload(name, content)
                return UseCaseResult({"ok": True, "name": name, "file": persona_path.as_posix()})

            if name == "L4_LIFECYCLE_PROMPT":
                # 全局 prompt — 不在实例级维护。更新方式: 改代码 → restart。
                return UseCaseResult({
                    "error": "L4_LIFECYCLE_PROMPT 是全局 prompt, 不在实例级维护。请改代码后 restart。"
                }, 403)

            cfg_path = get_runtime_config_path()
            raw = {}
            if cfg_path.exists():
                raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            prompts_override = raw.get("prompts_override", {})
            prompts_override[name] = content
            raw["prompts_override"] = prompts_override
            cfg_path.write_text(yaml.dump(raw, default_flow_style=False, allow_unicode=True), encoding="utf-8")
            self._hot_reload(name, content)
            return UseCaseResult({"ok": True, "name": name})
        except Exception as exc:
            return UseCaseResult({"error": str(exc)}, 500)

    @staticmethod
    def _load_overrides() -> dict[str, str]:
        try:
            import yaml

            cfg_path = get_runtime_config_path()
            if not cfg_path.exists():
                return {}
            raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            overrides = raw.get("prompts_override", {})
            return overrides if isinstance(overrides, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _hot_reload(name: str, content: str) -> None:
        try:
            if name == "LIFE_PERSONA":
                import sys
                import domain.memory.context.selectors.persona as persona_selector

                persona_selector.LIFE_PERSONA = content
                legacy_persona = sys.modules.get("domain.identity.persona")
                if legacy_persona is not None:
                    legacy_persona.LIFE_PERSONA = content
            elif name == "L4_LIFECYCLE_PROMPT":
                import domain.identity.system_prompts as system_prompts

                system_prompts.L4_LIFECYCLE_PROMPT = content
            console_runtime_adapter().hot_reload_prompt(name, content)
        except Exception:
            return


__all__ = ["PromptConsoleWorkflow"]
