"""Regression: 群消息 urgency 分流 + debounce 合并 + 30s 窗口语义。

历史 BUG:11:18 zero wake 224/225 几乎并发触发(45s 间隔),各自基于"前一条消息
还没人回应"的假设回复,产生 3 条主题高度相似的群消息(zero: 任务归属建议 x2)。

根因:不区分注意力等级,每条群消息都立即触发 wake。
解决:fixture 跑 100 轮 immediately → 一组 "@我" / "owner" / "keyword" 立即响应,
      其他累积 30s 合并一次 wake。
"""
from __future__ import annotations


def _classify(text, mentions_bot, sender_name, *, attention_keywords=None, owner_names=None):
    """复刻 handler._classify_group_urgency 的算法,纯函数测试。"""
    if mentions_bot:
        return "immediate"
    if attention_keywords and text:
        if any(kw in text for kw in attention_keywords):
            return "immediate"
    if owner_names and sender_name in owner_names:
        return "immediate"
    return "soft"


def test_mention_is_immediate():
    assert _classify("hello", mentions_bot=True, sender_name="foo") == "immediate"


def test_owner_is_immediate():
    assert _classify("随便说点什么", mentions_bot=False, sender_name="张浩普",
                     owner_names=["张浩普"]) == "immediate"


def test_keyword_is_immediate():
    assert _classify("zero 你怎么看", mentions_bot=False, sender_name="李",
                     attention_keywords=["zero", "Zero"]) == "immediate"
    # 大小写敏感,用户自己定义关键词全量
    assert _classify("Zero 你怎么看", mentions_bot=False, sender_name="李",
                     attention_keywords=["zero", "Zero"]) == "immediate"


def test_sibling_echo_is_soft():
    """alpha 广播在群里,@ 不是我、不含我名字、不是我 owner → soft"""
    assert _classify("盯盘监控报告：金发科技", mentions_bot=False, sender_name="alpha",
                     attention_keywords=["zero", "Zero"], owner_names=["张浩普"]) == "soft"


def test_other_member_is_soft():
    assert _classify("今日股票不错", mentions_bot=False, sender_name="小明",
                     attention_keywords=["zero", "Zero"], owner_names=["张浩普"]) == "soft"


def test_no_keywords_configured_still_classifies_by_mention_and_owner():
    """没配置 attention_keywords 的实例退化为只看 mention + owner。"""
    assert _classify("hello", mentions_bot=True, sender_name="foo") == "immediate"
    assert _classify("hello", mentions_bot=False, sender_name="foo") == "soft"
    assert _classify("hello", mentions_bot=False, sender_name="张浩普",
                     owner_names=["张浩普"]) == "immediate"


def test_debounce_channel_like_matching(tmp_path, monkeypatch):
    """debounce channel 匹配必须用 LIKE 而非严格 =,否则子通道前缀的 row 不会命中。

    历史 11:18 现场:emit_event 写的 channel = "instance:{uuid}/gateway:lark:group",
    但 _apply_debounce 查的 channel = "instance:{uuid}"(不含 /gateway:lark:group),
    严格 = 永远不命中,7 条 group_message 全部独立 INSERT,各自触发 wake。

    直接调 _apply_debounce + 模拟一条已存在的 row,验证 LIKE 命中。
    """
    import sqlite3
    import json
    from domain.lifecycle import events as events_mod

    # group_message 的默认 debounce_window_s 已改成 0(合并移到 ingress adapter)。
    # 但本测试覆盖的是 _apply_debounce 内部的 LIKE channel 匹配回归保护,与窗口
    # 是否启用无关——此处临时打开窗口,让执行流走到 LIKE 分支。
    import domain.lifecycle.event_registry as event_registry_mod
    monkeypatch.setattr(
        event_registry_mod,
        "resolve_event_config",
        lambda kind: {"debounce_window_s": (60, 60), "merge_policy": "accumulate"},
    )

    fake_db = tmp_path / "events.db"
    c = sqlite3.connect(str(fake_db))
    c.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT, payload TEXT, channel TEXT, fire_at TEXT,
            consumed_at TEXT, created_at TEXT, consumed_by_session_id TEXT
        );
    """)
    # 模拟一条已存在的未消费 group_message,channel = 完整 instance+subchannel
    # (emit_event 实际写这种格式,因为 line 213 拼 explicit channel)
    full_channel = events_mod._get_instance_channel() + "/gateway:lark:group"
    c.execute(
        "INSERT INTO events (kind, payload, channel, consumed_at, created_at) VALUES (?, ?, ?, NULL, ?)",
        ("group_message", json.dumps({"text": "hello A", "sender_name": "李"}, ensure_ascii=False),
         full_channel, events_mod.now_iso())
    )
    c.commit()
    c.close()

    driver = sqlite3.connect(str(fake_db))
    driver.row_factory = sqlite3.Row
    monkeypatch.setattr(events_mod, "_conn", lambda: driver, raising=False)

    # 直接调 _apply_debounce,fire_at=None 触发 debounce 分支
    result = events_mod._apply_debounce(
        "group_message",
        {"text": "hello B", "sender_name": "张三"},
        None,
    )
    # 如果 LIKE 修复生效 → 命中已存在 row,返回 event_id(int)
    # 如果 BUG 存在(严格 = ) → 返回 None
    assert result is not None, \
        "debounce 应该 LIKE 命中已存在同 instance 前缀的 row,但严格 = 失败了"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS {name}")
            except Exception as e:
                print(f"  FAIL {name}: {e}")
