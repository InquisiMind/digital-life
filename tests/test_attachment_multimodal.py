"""附件 registry + 飞书 image 接入 + sense_image 工具 回归测试。

覆盖：
1. attachments registry: register / get / 去重 / 跨实例隔离
2. messages.db attachments_json 列 migration + storage
3. sense_image 工具：mock attachment + mock GLM vision → 验证返回内容 + 调用 payload 正确
4. register_attachment 工具：本地图片落盘到 attachments/
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ─────────────────────────────────────────────────────────────────────
# 1. attachments registry
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_instance_attachments(monkeypatch, tmp_path):
    """隔离实例 + 附件目录到 tmp_path。"""
    iid = "test-multimodal-iid"
    from infrastructure.config import set_current_instance_id, reset_current_instance_id
    token = set_current_instance_id(iid)
    # tmp_path 当作项目根，data dir 也在 tmp
    from infrastructure.config import get_project_root
    orig_root = get_project_root()
    monkeypatch.setattr("infrastructure.config.get_project_root", lambda: tmp_path)
    # 清全局 cache
    import infrastructure.persistence.instance.attachments as att_mod
    att_mod._dbs.clear()
    try:
        yield iid
    finally:
        att_mod._dbs.clear()
        reset_current_instance_id(token)


def test_register_get_attachment_roundtrip(isolated_instance_attachments, monkeypatch):
    iid = isolated_instance_attachments
    from infrastructure.persistence.instance.attachments import (
        register_attachment, get_attachment, attachments_dir,
    )
    # 准备一个假图文件
    fake_png = attachments_dir(iid) / "fake.png"
    fake_png.write_bytes(b"\x89PNG\r\n\x1a\n fake png content")
    att = register_attachment(
        instance_id=iid, source="feishu", source_key="img_v3_test1",
        mime="image/png", local_path=str(fake_png),
    )
    assert att.attachment_id == "feishu:img_v3_test1"
    assert att.kind == "image"
    assert att.size_bytes == fake_png.stat().st_size
    # 读回
    got = get_attachment("feishu:img_v3_test1")
    assert got is not None
    assert got.sha256  # sha256 不空


def test_dedup_with_same_source_key(isolated_instance_attachments):
    """同 (source, source_key) 二次 register 返回同 ID（UNIQUE 约束）。"""
    iid = isolated_instance_attachments
    from infrastructure.persistence.instance.attachments import (
        register_attachment, attachments_dir,
    )
    fake_png = attachments_dir(iid) / "fake_dedup.png"
    fake_png.write_bytes(b"dedup test")
    att1 = register_attachment(
        instance_id=iid, source="feishu", source_key="img_v3_dedup",
        mime="image/png", local_path=str(fake_png),
    )
    att2 = register_attachment(
        instance_id=iid, source="feishu", source_key="img_v3_dedup",
        mime="image/png", local_path=str(fake_png),  # 二次注册
    )
    assert att1.attachment_id == att2.attachment_id


def test_save_bytes_as_attachment(isolated_instance_attachments):
    """便利函数：bytes 直接 register，sha256 自动算。"""
    iid = isolated_instance_attachments
    from infrastructure.persistence.instance.attachments import (
        save_bytes_as_attachment, get_attachment,
    )
    data = b"\x89PNG\r\n\x1a\n real png 1245"
    att = save_bytes_as_attachment(
        instance_id=iid, source="feishu", source_key="img_v3_save",
        data=data, mime="image/png",
    )
    # sha256 是这些字节的 hash
    import hashlib
    expected_sha = hashlib.sha256(data).hexdigest()
    assert att.sha256 == expected_sha
    assert Path(att.local_path).exists()
    assert Path(att.local_path).read_bytes() == data


def test_attachment_kind_inference(isolated_instance_attachments):
    """Attachment.kind 按 mime 推断：image/audio/video/file。"""
    iid = isolated_instance_attachments
    from infrastructure.persistence.instance.attachments import (
        register_attachment, attachments_dir, Attachment,
    )
    p = attachments_dir(iid) / "x.txt"
    p.write_bytes(b"x")
    img_att = register_attachment(instance_id=iid, source="test", source_key="img1",
                                   mime="image/jpeg", local_path=str(p))
    aud_att = register_attachment(instance_id=iid, source="test", source_key="aud1",
                                   mime="audio/opus", local_path=str(p))
    vid_att = register_attachment(instance_id=iid, source="test", source_key="vid1",
                                   mime="video/mp4", local_path=str(p))
    file_att = register_attachment(instance_id=iid, source="test", source_key="f1",
                                    mime="application/pdf", local_path=str(p))
    assert img_att.kind == "image"
    assert aud_att.kind == "audio"
    assert vid_att.kind == "video"
    assert file_att.kind == "file"


# ─────────────────────────────────────────────────────────────────────
# 2. messages.db attachments_json
# ─────────────────────────────────────────────────────────────────────


def test_messages_db_receives_attachment_ids(monkeypatch, tmp_path):
    """record_inbound + attachments= → 入库后 list_messages 读回该字段。"""
    import domain.messages as M
    monkeypatch.setattr(M, "messages_db_path", lambda iid=None: tmp_path / "state.db")
    M._ensure_schema()
    msg_id = M.record_inbound(
        chat_id="oc_test_mul",
        sender_id="ou_zhp", sender_name="zhp",
        text="[图片 feishu:img_v3_test]",
        msg_id="om_test_mul_1",
        source="feishu", sender_kind="human",
        attachments=["feishu:img_v3_test", "feishu:img_v3_test2"],
    )
    assert msg_id is not None
    msgs = M.list_messages("oc_test_mul", limit=5)
    assert len(msgs) == 1
    assert msgs[0]["attachments"] == ["feishu:img_v3_test", "feishu:img_v3_test2"]


def test_messages_db_attachments_empty_when_not_provided(monkeypatch, tmp_path):
    """老逻辑兼容：不传 attachments 时 attachments 字段 = []。"""
    import domain.messages as M
    monkeypatch.setattr(M, "messages_db_path", lambda iid=None: tmp_path / "state.db")
    M._ensure_schema()
    M.record_inbound(
        chat_id="oc_test_noatt",
        sender_id="ou_zhp", sender_name="zhp",
        text="hello",
        msg_id="om_test_noatt_1",
    )
    msgs = M.list_messages("oc_test_noatt", limit=5)
    assert msgs[0]["attachments"] == []


def test_list_plain_text_renders_attachment_hint(monkeypatch, tmp_path):
    """list_plain_text 末尾附 [附件 xxx] 提示——chat_stream 段让模型看到"这里有图"。"""
    import domain.messages as M
    monkeypatch.setattr(M, "messages_db_path", lambda iid=None: tmp_path / "state.db")
    M._ensure_schema()
    M.record_inbound(
        chat_id="oc_test_text",
        sender_id="ou_zhp", sender_name="zhp",
        text="[图片 feishu:img_v3_x]",
        msg_id="om_test_text_1",
        attachments=["feishu:img_v3_x"],
    )
    out = M.list_plain_text("oc_test_text", limit=5)
    assert "[附件 feishu:img_v3_x]" in out


# ─────────────────────────────────────────────────────────────────────
# 3. sense_image 工具
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def vision_test_instance(monkeypatch, tmp_path):
    iid = "test-vision-iid"
    from infrastructure.config import set_current_instance_id, reset_current_instance_id
    token = set_current_instance_id(iid)
    from infrastructure.config import get_project_root
    monkeypatch.setattr("infrastructure.config.get_project_root", lambda: tmp_path)
    # 清全局 cache
    import infrastructure.persistence.instance.attachments as att_mod
    att_mod._dbs.clear()
    try:
        yield iid
    finally:
        att_mod._dbs.clear()
        reset_current_instance_id(token)


def test_sense_image_returns_vision_description(vision_test_instance, monkeypatch):
    """sense_image 正常路径：mock GLM vision API → 返回中文描述。"""
    iid = vision_test_instance
    # 准备附件
    from infrastructure.persistence.instance.attachments import (
        save_bytes_as_attachment, attachments_dir,
    )
    fake_png = b"\x89PNG\r\n\x1a\n fake image bytes for vision"
    att = save_bytes_as_attachment(
        instance_id=iid, source="feishu", source_key="img_v3_vision",
        data=fake_png, mime="image/png",
    )

    # mock httpx.post + API key（不读真 secrets.env）
    import interfaces.tools.vision_tool as vt
    monkeypatch.setattr(vt, "_get_llm_api_key", lambda: "fake_api_key_for_test")
    monkeypatch.setattr(vt, "_get_vision_model", lambda: "glm-4.6v")
    monkeypatch.setattr(vt, "_get_llm_base_url", lambda: "https://api.example.test/v1")

    def fake_post(url, json=None, headers=None, timeout=None):
        # 验证 payload 是 OpenAI 兼容的多模态格式
        assert json["model"] == "glm-4.6v"  # default
        msgs = json["messages"]
        assert len(msgs) >= 1
        content = msgs[0]["content"]
        assert isinstance(content, list)
        parts_types = [p.get("type") for p in content]
        assert "image_url" in parts_types
        assert "text" in parts_types
        # 找 image_url part，验证 format
        img_part = next(p for p in content if p["type"] == "image_url")
        assert img_part["image_url"]["url"].startswith("data:image/png;base64,")
        return MagicMock(
            status_code=200,
            json=lambda: {
                "choices": [{"message": {"content": "这是一张测试图片，描述内容为 fake image"}}],
            },
        )

    monkeypatch.setattr(vt.httpx, "post", fake_post)

    result = vt._handle_sense_image({"attachment_id": "feishu:img_v3_vision",
                                     "question": "图里是什么"})
    assert "测试图片" in result
    assert "fake image" in result


def test_sense_image_rejects_non_image_mime(vision_test_instance, monkeypatch):
    """mime 不是 image 的附件：返错（拒绝走 vision）。"""
    iid = vision_test_instance
    from infrastructure.persistence.instance.attachments import save_bytes_as_attachment
    att = save_bytes_as_attachment(
        instance_id=iid, source="feishu", source_key="f_v3_voice_test",
        data=b"OPUS_HEADER fake", mime="audio/opus",
    )
    from interfaces.tools.vision_tool import _handle_sense_image
    result = _handle_sense_image({"attachment_id": "feishu:f_v3_voice_test"})
    assert "仅支持图片" in result or "error" in result.lower()


def test_sense_image_handles_missing_attachment(vision_test_instance):
    """attachment_id 不存在 → tool_error。"""
    from interfaces.tools.vision_tool import _handle_sense_image
    result = _handle_sense_image({"attachment_id": "feishu:not_exists_xxx"})
    assert "不存在" in result or "error" in result.lower()


# ─────────────────────────────────────────────────────────────────────
# 4. register_attachment 工具（模型自登记本地图）
# ─────────────────────────────────────────────────────────────────────


def test_register_attachment_local_png_file(vision_test_instance, tmp_path):
    """register_attachment(path) 正常路径：读 bytes + 探测 mime + 落盘 + register。"""
    iid = vision_test_instance
    # 卷一个假 PNG（可 imghdr 识别的 magic）
    src = tmp_path / "fake_chart.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n some png content")
    from interfaces.tools.vision_tool import _handle_register_attachment
    import json as _json
    result_str = _handle_register_attachment({"path": str(src),
                                              "description": "candidate pool chart"})
    result = _json.loads(result_str)
    assert result["ok"] is True
    assert "attachment_id" in result
    assert result["mime"].startswith("image/")
    # 应该落盘
    from infrastructure.persistence.instance.attachments import get_attachment
    att = get_attachment(result["attachment_id"])
    assert att is not None
    assert Path(att.local_path).exists()
    assert att.source == "local"


def test_register_attachment_missing_file_returns_error(vision_test_instance, tmp_path):
    """path 不存在 → tool_error。"""
    from interfaces.tools.vision_tool import _handle_register_attachment
    result = _handle_register_attachment({"path": str(tmp_path / "no_exist.png")})
    assert "不存在" in result
