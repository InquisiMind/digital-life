"""飞书 reaction 表情收条状态机测试。

历史：原为三态（入站 ✅ → 处理中 🤔 → 撤回）。2026-07 简化为两态——
入站只登记 msg_id 不加表情，处理时才加 🤔，回复后撤 🤔。

核心契约（防止回归到三态）：
1. register_received 不再触发 adapter.add_reaction
2. mark_all_processing 不再 remove_reaction（无 ✅ 可撤）
3. 仅在 mark_all_processing 时给已登记 msg_id 加 THINKING
4. clear_all_reactions 撤掉 THINKING
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from application.ingress import reaction_state


@pytest.fixture(autouse=True)
def _isolate_state():
    """每个测试独立 _REACTIONS 状态 + 重置 _ADAPTER。"""
    reaction_state._REACTIONS.clear()
    reaction_state._ADAPTER = None
    yield
    reaction_state._REACTIONS.clear()
    reaction_state._ADAPTER = None


# ─────────────────── register_received：只登记不加表情 ───────────────────


def test_register_received_does_not_add_emoji():
    """关键不变量：入站登记时不再调 adapter.add_reaction（曾经加 ✅）。"""
    adapter = AsyncMock()
    adapter.add_reaction = AsyncMock(return_value="rid_done")

    asyncio.run(reaction_state.register_received("om_123", adapter))

    # msg_id 已登记
    assert "om_123" in reaction_state._REACTIONS
    # ★ 关键：adapter.add_reaction 没被调用（曾经会调一次加 ✅）
    adapter.add_reaction.assert_not_called()
    # 登记的条目 emoji 为空（等待 mark_all_processing 加 🤔）
    assert reaction_state._REACTIONS["om_123"]["emoji"] == ""
    assert reaction_state._REACTIONS["om_123"]["reaction_id"] == ""


def test_register_received_idempotent():
    """重复登记同一 msg_id 不覆盖（避免 batch 内重复消息多次登记）。"""
    asyncio.run(reaction_state.register_received("om_1", AsyncMock()))
    first_ts = reaction_state._REACTIONS["om_1"]["ts"]
    asyncio.run(reaction_state.register_received("om_1", AsyncMock()))
    assert reaction_state._REACTIONS["om_1"]["ts"] == first_ts


def test_register_received_empty_msg_id_noop():
    adapter = AsyncMock()
    asyncio.run(reaction_state.register_received("", adapter))
    assert len(reaction_state._REACTIONS) == 0


# ─────────────────── mark_all_processing：消费时加 🤔 ───────────────────


def test_mark_processing_adds_thinking_to_registered():
    """登记过的 msg_id → mark_all_processing 时加 THINKING。"""
    adapter = AsyncMock()
    adapter.add_reaction = AsyncMock(return_value="rid_thinking")
    asyncio.run(reaction_state.register_received("om_1", adapter))

    asyncio.run(reaction_state.mark_all_processing(adapter))

    adapter.add_reaction.assert_called_once_with("om_1", "THINKING")
    assert reaction_state._REACTIONS["om_1"]["emoji"] == "THINKING"
    assert reaction_state._REACTIONS["om_1"]["reaction_id"] == "rid_thinking"


def test_mark_processing_does_not_remove_first_emoji():
    """关键不变量：mark_all_processing 不再 remove_reaction（曾经会撤 ✅）。"""
    adapter = AsyncMock()
    adapter.add_reaction = AsyncMock(return_value="rid_t")
    adapter.remove_reaction = AsyncMock()
    asyncio.run(reaction_state.register_received("om_1", adapter))

    asyncio.run(reaction_state.mark_all_processing(adapter))

    # ★ 关键：从未调用 remove_reaction（旧实现会撤 ✅）
    adapter.remove_reaction.assert_not_called()


def test_mark_processing_skips_already_thinking():
    """已加过 THINKING 的不重复加（幂等，防 mark 多次叠加 emoji）。"""
    adapter = AsyncMock()
    adapter.add_reaction = AsyncMock(return_value="rid1")
    asyncio.run(reaction_state.register_received("om_1", adapter))
    asyncio.run(reaction_state.mark_all_processing(adapter))

    # 第二次 mark —— 不应再调 add_reaction
    asyncio.run(reaction_state.mark_all_processing(adapter))
    assert adapter.add_reaction.call_count == 1


def test_mark_processing_batch_all_msg_ids():
    """batch 多条消息：登记多个，mark 时全部加 THINKING。"""
    adapter = AsyncMock()
    adapter.add_reaction = AsyncMock(return_value="rid")
    asyncio.run(reaction_state.register_received("om_a", adapter))
    asyncio.run(reaction_state.register_received("om_b", adapter))
    asyncio.run(reaction_state.register_received("om_c", adapter))

    asyncio.run(reaction_state.mark_all_processing(adapter))

    assert adapter.add_reaction.call_count == 3
    for mid in ("om_a", "om_b", "om_c"):
        assert reaction_state._REACTIONS[mid]["emoji"] == "THINKING"


# ─────────────────── clear_all_reactions：撤回 THINKING ───────────────────


def test_clear_removes_thinking_emoji():
    """完整链路：登记 → mark 加 🤔 → clear 撤 🤔。"""
    adapter = AsyncMock()
    adapter.add_reaction = AsyncMock(return_value="rid_t")
    adapter.remove_reaction = AsyncMock()
    asyncio.run(reaction_state.register_received("om_1", adapter))
    asyncio.run(reaction_state.mark_all_processing(adapter))

    asyncio.run(reaction_state.clear_all_reactions(adapter))

    adapter.remove_reaction.assert_called_once_with("om_1", "rid_t")
    assert len(reaction_state._REACTIONS) == 0


def test_clear_noop_when_nothing_registered():
    """没有登记任何消息时 clear 不报错。"""
    adapter = AsyncMock()
    asyncio.run(reaction_state.clear_all_reactions(adapter))
    adapter.remove_reaction.assert_not_called()
