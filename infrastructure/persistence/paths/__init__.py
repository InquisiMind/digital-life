"""Central runtime path resolver — instance-scoped."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimePaths:
    root: Path

    @classmethod
    def from_env(cls) -> "RuntimePaths":
        root = Path(os.environ.get("L4_HOME", Path.cwd())).expanduser().resolve()
        return cls(root=root)

    @property
    def data(self) -> Path:
        """apps/{id}/data/ — canonical runtime data root（服务型二跳，2026-09-29）。

        此前直接拼 apps/{env_id}/data，绕过路径中枢：服务 worker 的
        execution traces 会写进 apps/{service_id}/data 影子目录（被
        InstanceDB db-guard 拒绝 → wake 内崩溃）。服务型运行体的 data
        在 apps/{agent_def_id}/services/{service_id}/data/（D1=B）。
        """
        rid = _instance_id()
        try:
            from infrastructure.persistence.services_registry import resolve_service_def

            def_id = resolve_service_def(rid)
            if def_id:
                return self.root / "apps" / def_id / "services" / rid / "data"
        except Exception:
            pass
        return self.root / "apps" / rid / "data"

    @property
    def db(self) -> Path:
        return self.data / "db"

    @property
    def events(self) -> Path:
        return self.data / "events"

    @property
    def memories(self) -> Path:
        return self.data / "memories"

    @property
    def workspaces(self) -> Path:
        return self.data / "workspaces"

    # Legacy compatibility properties
    @property
    def runtime(self) -> Path:
        return self.data


def _instance_id() -> str:
    return (
        os.environ.get("DIGITAL_LIFE_INSTANCE_ID")
        or os.environ.get("L4_AGENT_ID")
        or os.environ.get("DIGITAL_LIFE_EMPLOYEE_ID")
        or "zero"
    ).strip("/") or "zero"

