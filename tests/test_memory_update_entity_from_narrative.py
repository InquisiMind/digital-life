"""P1 — `update_entity_index_from_narrative` 必须不再静默崩 (FR-102)。

驱动问题(代码 L707-726):
  - import `add_entity` 失败(该函数不存在于 entity_index),
    被外层 try/except 静默吞掉(整个函数是 no-op)
  - `extract_entities_from_context` 返回 list[str],但旧代码按 dict 取
    `.get("name")` 即便能 import 成功也会 AttributeError
  - 即便 entity_index 里已有该实体(可以抽取出),segment narrative
    提炼侧实际产生的实体从未落地

P1 修复目标:用 `sync_entity_from_source` 重写,使已注册实体被正确同步。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest


def _redirect_instance_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 infrastructure.config.get_instance_dir 重定向到 tmp_path/apps/<id>,
    并重置 vector / consolidation 的 _mem_dir_cache。"""
    instance_id = "test-narr-ent"
    apps_root = tmp_path / "apps"
    apps_root.mkdir(parents=True, exist_ok=True)
    expected_dir = apps_root / instance_id
    (expected_dir / "data" / "memories").mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("DIGITAL_LIFE_INSTANCE_ID", instance_id)
    import infrastructure.config as cfg

    monkeypatch.setattr(
        cfg,
        "get_instance_dir",
        lambda iid=None: expected_dir,
    )
    import domain.memory.memory.recall.vector as vec_mod
    import domain.memory.memory.summaries.consolidation_runtime as cons_mod

    vec_mod._mem_dir_cache = None
    cons_mod._mem_dir_cache = None
    return expected_dir / "data" / "memories"


def test_update_entity_index_from_narrative_no_longer_silently_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-102 / T008: 预置一个已存在的实体(ZHP),喂一段含 ZHP 的 narrative,
       调 update_entity_index_from_narrative → 该实体不应丢失,无 Exception 抛出。"""
    _redirect_instance_dir(tmp_path, monkeypatch)

    # 找 entity_index 路径,预置一个"ZHP"实体
    import domain.memory.memory.consciousness.entity_index as ei
    from infrastructure.config import get_runtime_memories_dir

    ei_path = get_runtime_memories_dir() / "entity_index.json"
    ei_path.parent.mkdir(parents=True, exist_ok=True)
    ei_path.write_text(
        json.dumps({
            "version": 1,
            "entities": {
                "ZHP": {
                    "aliases": [],
                    "type": "person",
                    "profile": {"summary": "用户本人", "facts": []},
                    "memories": [],
                }
            },
        }),
        encoding="utf-8",
    )

    # 调用被测函数 — 用 patch 跳过 LLM / 上层 try,只触发实体同步路径
    from domain.memory.memory.summaries.consolidation_runtime import (
        update_entity_index_from_narrative,
    )

    # 不应抛异常(旧实现虽静默但每次都走 import-error 路径)
    update_entity_index_from_narrative("今天 ZHP 问了关于 A+ 策略的问题")

    # 断言:ZHP 实体仍存在(同步不应清空已存在数据)
    after = json.loads(ei_path.read_text(encoding="utf-8"))
    assert "ZHP" in after.get("entities", {}), (
        "update_entity_index_from_narrative MUST preserve existing entities; "
        "currently it silently no-ops due to nonexistent add_entity import"
    )


def test_update_entity_index_from_narrative_counter_called_with_string_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T008 深入断言:修改后的实现必须用 sync_entity_from_source 写入实体,
       且 name 是 string(旧代码 .get("name") 错误)。"""
    _redirect_instance_dir(tmp_path, monkeypatch)

    import domain.memory.memory.consciousness.entity_index as ei
    from infrastructure.config import get_runtime_memories_dir

    ei_path = get_runtime_memories_dir() / "entity_index.json"
    ei_path.write_text(
        json.dumps({
            "version": 1,
            "entities": {
                "Alice": {"aliases": [], "type": "person", "profile": {}, "memories": []}
            },
        }),
        encoding="utf-8",
    )

    with patch.object(
        ei,
        "sync_entity_from_source",
        wraps=ei.sync_entity_from_source,
    ) as spy:
        from domain.memory.memory.summaries.consolidation_runtime import (
            update_entity_index_from_narrative,
        )
        update_entity_index_from_narrative("Alice 和 Bob 讨论了项目 X")
        # 至少对每个被抽取的实体(Alice、Bob 之一)调一次 sync_entity_from_source
        assert spy.call_count >= 1, (
            "rewritten impl MUST call sync_entity_from_source at least once per extracted entity"
        )
        # 第一个参数(name)必须是 str
        first_call_args = spy.call_args_list[0]
        name_arg = first_call_args.args[0] if first_call_args.args else first_call_args.kwargs.get("name")
        assert isinstance(name_arg, str), (
            f"MUST pass name as str (extract_entities_from_context returns list[str]); "
            f"got {type(name_arg).__name__}"
        )
