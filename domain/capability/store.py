"""Manifest-based metadata store for self-registered tools and skills.

Each scope level keeps a ``manifest.json`` tracking registered items.
Reading all manifests across scopes gives the full inventory — no DB
needed for v1. The manifest is simple JSON, easy to audit via frontend.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from domain.capability.paths import project_root, resolve_manifest_path

logger = logging.getLogger(__name__)


def _read_manifest(path: Path) -> dict[str, list[dict[str, Any]]]:
    if not path.exists():
        return {"items": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"items": []}


def _write_manifest(path: Path, data: dict[str, list[dict[str, Any]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def upsert_manifest_entry(
    scope: str,
    kind: str,  # "tools" or "skills"
    *,
    name: str,
    description: str = "",
    instance_id: str = "",
    project_id: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Insert or update an entry in the manifest for (scope, kind)."""
    root = project_root()
    manifest_path = root / resolve_manifest_path(
        scope, kind, instance_id=instance_id, project_id=project_id,
    )
    data = _read_manifest(manifest_path)
    items = data.setdefault("items", [])
    # Find existing by name and replace, or append.
    entry = {
        "name": name,
        "description": description,
        "version": "1",
        "registered_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "scope": scope,
        **(extra or {}),
    }
    found = False
    for i, item in enumerate(items):
        if item.get("name") == name:
            entry["version"] = str(int(item.get("version", "0")) + 1)
            items[i] = entry
            found = True
            break
    if not found:
        items.append(entry)
    _write_manifest(manifest_path, data)
    return entry


def _read_all_manifests_for_instance(
    kind: str,
    instance_id: str,
) -> list[dict[str, Any]]:
    """Read manifests from personal + all projects the instance participates in + shared."""
    root = project_root()
    results: list[dict[str, Any]] = []

    # Personal
    p = root / resolve_manifest_path("personal", kind, instance_id=instance_id)
    for item in _read_manifest(p).get("items", []):
        item = dict(item)
        item.setdefault("scope", "personal")
        results.append(item)

    # Shared（可见性由授予决定——热加载按 scope 打旗标，默认面排除）
    s = root / resolve_manifest_path("shared", kind, instance_id=instance_id)
    for item in _read_manifest(s).get("items", []):
        item = dict(item)
        item.setdefault("scope", "shared")
        results.append(item)

    # Projects — scan all active projects the instance belongs to
    try:
        from domain.project.loader import load_all_projects
        for pid, cfg in load_all_projects().items():
            if cfg.status != "active":
                continue
            if not cfg.get_position_for_instance(instance_id):
                continue
            proj_p = root / resolve_manifest_path("project", kind, instance_id=instance_id, project_id=pid)
            for item in _read_manifest(proj_p).get("items", []):
                item = dict(item)
                item.setdefault("scope", "project")
                item["project_id"] = pid
                item["project_name"] = cfg.name
                results.append(item)
    except Exception:
        pass

    return results


def list_all_tools(instance_id: str) -> list[dict[str, Any]]:
    return _read_all_manifests_for_instance("tools", instance_id)


def list_all_skills(instance_id: str) -> list[dict[str, Any]]:
    return _read_all_manifests_for_instance("skills", instance_id)


def list_my_tools_and_skills(instance_id: str) -> dict[str, list[dict[str, Any]]]:
    return {
        "tools": list_all_tools(instance_id),
        "skills": list_all_skills(instance_id),
    }
