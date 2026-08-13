"""infrastructure.perception.frames 纯函数单测（spec FR-004）。

不依赖真实视频/PIL，只测时间戳规划与 base64 编码逻辑。
"""
from __future__ import annotations

import base64

from infrastructure.perception import frames


def test_select_frame_timestamps_basic():
    """2fps、10s、上限 12 → 从 0.25s 起，每 0.5s 一帧，截到 12 帧。"""
    ts = frames.select_frame_timestamps(10.0, fps=2.0, max_frames=12)
    assert ts[0] == 0.25
    assert ts[1] == 0.75
    assert len(ts) == 12  # 0.25..6.25 共 12 个，<10s 但被 max_frames 截断
    assert ts[-1] < 10.0


def test_select_frame_timestamps_max_frames_caps():
    """短时长 + 高 fps 时，max_frames 是硬上限。"""
    ts = frames.select_frame_timestamps(100.0, fps=10.0, max_frames=5)
    assert len(ts) == 5
    step = 1.0 / 10.0
    assert abs(ts[0] - step / 2) < 1e-6


def test_select_frame_timestamps_zero_or_negative():
    """边界：时长/帧率/上限 ≤0 → 空列表。"""
    assert frames.select_frame_timestamps(0.0, fps=2.0, max_frames=10) == []
    assert frames.select_frame_timestamps(10.0, fps=0.0, max_frames=10) == []
    assert frames.select_frame_timestamps(10.0, fps=2.0, max_frames=0) == []


def test_estimate_capture_seconds_roundtrip():
    """反推时长 = 帧数 / 帧率。"""
    assert frames.estimate_capture_seconds(20, fps=2.0) == 10.0
    assert frames.estimate_capture_seconds(0, fps=2.0) == 0.0


def test_encode_image_bytes_basic():
    """_encode_image_bytes 输出合法 data URI。"""
    raw = b"\x89PNG fake"
    uri = frames._encode_image_bytes(raw, "image/png")
    assert uri.startswith("data:image/png;base64,")
    payload = uri.split(",", 1)[1]
    assert base64.b64decode(payload) == raw


def test_encode_image_file_no_pil_fallback(tmp_path, monkeypatch):
    """无 PIL 时 encode_image_file 仍能返回 data URI（原始字节降级）。

    通过让 Pillow import 失败来模拟"未安装"。
    """
    fake_img = tmp_path / "x.png"
    fake_img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    # 注入 ImportError 到 _resize_encode 内部的 PIL import
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("simulated no-PIL")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    uri = frames.encode_image_file(fake_img, max_width=100)
    assert uri.startswith("data:")
    # 能解码回原始字节
    payload = uri.split(",", 1)[1]
    import base64 as b64

    assert b64.b64decode(payload) == b"\x89PNG\r\n\x1a\nfake"
