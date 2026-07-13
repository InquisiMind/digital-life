"""mid-session 注入消息事件上下文完整性回归测试。

历史 bug（7/13 wake #1927 call 23）：mid-session 注入的真人消息只渲染了
``[飞书消息 #eid] (sender) text`` 简陋模板，**完全丢了 chat_id / "对话"/"群"
标识 / chat_name / sender_position 等关键上下文**——模型在 mid-session
接到消息时既不知道是私聊还是群、更不知道回哪个 chat。结果：用户私聊机器人，
机器人却把回复发到了最近活跃的群里。

修复：``_render_signal_message`` 走 event_types.yaml 的 wake_prompt 模板，
与 wake 启动同源；私聊含 ``对话：{chat_id}``、群含 ``群：{chat_name}（{chat_id}）``；
回复时显式提示把 chat_id 传给 express_to_human。
"""
from __future__ import annotations


def test_render_private_message_contains_chat_id_and_sender():
    """私聊（kind=message）渲染：含 `对话：{chat_id}` + sender + text。

    契约：chat_id 必须出现在文末，模型自己看就知道回哪里——
    不需要硬塞『必须调用 express_to_human(chat_id=xxx)』这种具体调用教条。
    """
    from infrastructure.ai.agent import _render_signal_message

    ev = {
        "event_id": 9999,
        "kind": "message",
        "display_name": "人类消息",
        "payload": {
            "text": "你能听见我吗？",
            "sender_name": "张浩普",
            "chat_id": "oc_private_chat_abc123",
        },
    }
    out = _render_signal_message(ev)
    print("--- 渲染结果（私聊）---")
    print(out)
    print("--- 结束 ---")

    # 契约 1：含信号头（让模型在 mid-session noise 里识别这是新消息）
    assert "#9999 · 新消息到达" in out
    # 契约 2：含 chat_id（私聊模板的 `对话：{chat_id}`）
    assert "oc_private_chat_abc123" in out, "私聊 chat_id 必须出现"
    assert "对话" in out, "私聊模板必须含『对话』字段标识"
    # 契约 3：sender 和 text
    assert "张浩普" in out
    assert "你能听见我吗？" in out
    # 契约 4：不硬塞具体调用方式——chat_id 给到就行，调用细节交给模型
    assert 'express_to_human(chat_id=' not in out, (
        "不应硬塞具体调用语法；给到 chat_id 即可"
    )


def test_render_group_message_contains_chat_name_and_id():
    """群聊（kind=group_message）渲染：含 `群：{chat_name}（{chat_id}）` + sender。"""
    from infrastructure.ai.agent import _render_signal_message

    ev = {
        "event_id": 8888,
        "kind": "group_message",
        "display_name": "群聊消息",
        "payload": {
            "text": "@zero 候选池就绪，6 只标的",
            "sender_name": "alpha",
            "sender_position": "@alpha",
            "chat_name": "交易策略小队",
            "chat_id": "oc_group_xyz789",
            "source": "gateway:feishu",
        },
    }
    out = _render_signal_message(ev)
    print("--- 渲染结果（群聊）---")
    print(out)
    print("--- 结束 ---")

    # 群必须含 chat_name 和 chat_id
    assert "oc_group_xyz789" in out, "群 chat_id 必须出现"
    assert "交易策略小队" in out, "群 chat_name 必须出现"
    assert "群" in out, "群模板必须含『群』字段标识"
    assert "alpha" in out
    assert "候选池" in out


def test_render_differs_between_private_and_group():
    """同一 sender 同一 chat_id 不同 kind，渲染出来『对话』vs『群』不同。

    防止上游 bug——比如 group_message 模板被误解析成 message 模板。
    """
    from infrastructure.ai.agent import _render_signal_message

    common_payload = {
        "text": "test",
        "sender_name": "user",
        "chat_id": "oc_x",
        "chat_name": "群名",
    }
    private_out = _render_signal_message({
        "event_id": 1, "kind": "message", "payload": dict(common_payload),
    })
    group_out = _render_signal_message({
        "event_id": 1, "kind": "group_message", "payload": dict(common_payload),
    })
    # 两份应该不同：私聊含『对话』、群含『群：群名』
    assert "对话" in private_out
    assert "群" in group_out
    assert "群名" in group_out
    assert "群名" not in private_out, "私聊渲染不应该带 chat_name（私聊没群）"
    assert private_out != group_out


def test_render_fallback_when_yaml_template_missing():
    """非 message/group_message kind（应该走 fallback 模板）也能渲染且含 chat_id。

    覆盖防御：万一未来 event_registry 加了别的 message kind，未注册到 yaml 时仍可读。
    """
    from infrastructure.ai.agent import _render_signal_message

    ev = {
        "event_id": 123,
        "kind": "unknown_message_kind",
        "display_name": "未知消息",
        "payload": {
            "text": "fallback test",
            "sender_name": "tester",
            "chat_id": "oc_fallback_xyz",
        },
    }
    out = _render_signal_message(ev)
    assert "oc_fallback_xyz" in out, "fallback 模板也必须含 chat_id"
    assert "tester" in out
    assert "fallback test" in out


def test_render_without_chat_id_still_works():
    """无 chat_id 时（飞书侧异常）仍能渲染、不崩。

    模型自己会读懂这条没指定对话/群的消息——不需要额外提示。
    """
    from infrastructure.ai.agent import _render_signal_message

    ev = {
        "event_id": 5,
        "kind": "message",
        "payload": {"text": "test", "sender_name": "x"},  # 没 chat_id
    }
    out = _render_signal_message(ev)
    # 不崩、有 sender 和 text 即可
    assert "test" in out
    assert "#5" in out
