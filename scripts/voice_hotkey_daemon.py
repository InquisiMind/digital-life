#!/usr/bin/env python3
"""语音快捷键 daemon — 全局快捷键控制 Zero 的持续听觉状态。

独立子进程，常驻。监听全局快捷键 cmd+shift+h：
  - 按一次 → POST /internal/voice/control {action: "start_session"}  开启持续听觉
  - 再按一次 → POST /internal/voice/control {action: "stop_session"} 关闭

基于 perception_daemon.py 的 pynput 逻辑，去掉了录屏录音。
仅给 Zero 实例使用（instance_id 默认值硬编码）。

依赖（按需，缺失时降级）：
  - pynput（全局快捷键）— 缺失则退化成 CLI 模式（回车切换）
  - httpx（HTTP 请求）— 必需

用法：
  python scripts/voice_hotkey_daemon.py [--hotkey cmd+shift+h]
                                         [--instance INSTANCE_ID]
                                         [--endpoint http://localhost:8642]
                                         [--mode hotkey|cli]

环境变量（覆盖默认）：
  VOICE_HOTKEY / VOICE_INSTANCE / VOICE_ENDPOINT / VOICE_MODE
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

# ── 用户反馈：macOS 通知 + 系统声音 ───────────────────────────────────────────
# 复用 perception_daemon 的反馈设计：闭眼也能知道状态。

_SOUNDS = {
    "start":    "/System/Library/Sounds/Glass.aiff",   # 清脆"叮" = 持续听觉开启
    "stop":     "/System/Library/Sounds/Basso.aiff",   # 低沉 = 关闭
    "ok":       "/System/Library/Sounds/Hero.aiff",    # 成功
    "fail":     "/System/Library/Sounds/Basso.aiff",    # 失败
}


def _macos_notify(title: str, message: str) -> None:
    """弹 macOS 通知中心通知。非 macOS / osascript 失败 → 静默。"""
    try:
        script = f'display notification {repr(message)} with title {repr(title)}'
        subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _play_sound(key: str) -> None:
    path = _SOUNDS.get(key)
    if not path:
        return
    try:
        subprocess.Popen(
            ["afplay", path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def feedback(kind: str, *, message: str = "", cli: bool = False) -> None:
    titles = {
        "start": ("🎙 语音会话已开启", "正在听…再按一次结束"),
        "stop":  ("⏹ 语音会话已关闭", "语音会话已结束"),
        "ok":    ("✅ 已送达", message or "voice control 成功"),
        "fail":  ("❌ 操作失败", message or "请查看日志"),
    }
    title, msg = titles.get(kind, (kind, message))
    _macos_notify(title, msg)
    _play_sound(kind)
    if cli:
        print(f"  {title} — {msg}")


# ── voice control 调用 ────────────────────────────────────────────────────────

DEFAULT_ENDPOINT = "http://localhost:8642"
DEFAULT_INSTANCE = "c2a5c8e8-e4f5-4c69-be3e-aac49903081d"  # Zero
DEFAULT_HOTKEY = "cmd+shift+h"

logger = logging.getLogger("voice_hotkey_daemon")


def post_voice_control(
    *,
    endpoint: str,
    action: str,
    instance_id: str,
    cli: bool = False,
) -> dict:
    """POST /internal/voice/control，返回响应 JSON。"""
    import httpx

    url = f"{endpoint.rstrip('/')}/internal/voice/control"
    body = {"action": action, "instance_id": instance_id}
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.post(url, json=body)
            r.raise_for_status()
            result = r.json()
        ok = result.get("ok", False)
        state = result.get("state", "")
        feedback("ok" if ok else "fail",
                 message=f"状态: {state}" if state else "",
                 cli=cli)
        return result
    except Exception as exc:
        logger.error("post_voice_control failed: %s", exc)
        feedback("fail", message=f"{type(exc).__name__}: {exc}"[:80], cli=cli)
        return {"ok": False, "error": str(exc)}


# ── 状态管理 ─────────────────────────────────────────────────────────────────

class VoiceToggle:
    """管理 start_session/stop_session toggle 状态。

    状态来源：本地跟踪 + 服务端 status 校准。
    首次按 → dialog；再按 → inactive；循环往复。
    如果不确定当前状态，先查一次 status。
    """

    def __init__(self, *, endpoint: str, instance_id: str, cli: bool):
        self._endpoint = endpoint
        self._instance_id = instance_id
        self._cli = cli
        self._is_active = False  # True = active 模式, False = inactive
        self._lock = threading.Lock()

    def toggle(self) -> None:
        with self._lock:
            if self._is_active:
                # 当前是 dialog → 关闭
                self._is_active = False
                threading.Thread(
                    target=post_voice_control,
                    kwargs=dict(endpoint=self._endpoint, action="stop_session",
                                instance_id=self._instance_id, cli=self._cli),
                    daemon=True,
                ).start()
            else:
                # 当前是 inactive → 开启
                self._is_active = True
                feedback("start", cli=self._cli)
                threading.Thread(
                    target=post_voice_control,
                    kwargs=dict(endpoint=self._endpoint, action="start_session",
                                instance_id=self._instance_id, cli=self._cli),
                    daemon=True,
                ).start()

    def sync_state(self) -> None:
        """启动时查一次 status，校准本地状态。"""
        try:
            import httpx
            url = f"{self._endpoint.rstrip('/')}/internal/voice/control"
            with httpx.Client(timeout=5.0) as client:
                r = client.post(url, json={"action": "status"})
                r.raise_for_status()
                result = r.json()
            state = result.get("state", "inactive")
            self._is_active = (state == "dialog")
            logger.info("synced state: %s (is_active=%s)", state, self._is_active)
        except Exception as exc:
            logger.warning("sync_state failed (using default): %s", exc)


# ── 主循环 ───────────────────────────────────────────────────────────────────


def run_daemon(*, endpoint: str, instance_id: str, hotkey: str, mode: str) -> None:
    is_cli = mode == "cli"
    toggle = VoiceToggle(endpoint=endpoint, instance_id=instance_id, cli=is_cli)
    toggle.sync_state()

    if mode == "cli":
        _run_cli_loop(toggle.toggle)
    else:
        _run_hotkey_loop(hotkey, toggle.toggle)


def _run_cli_loop(toggle_fn) -> None:
    print("=== 语音快捷键 daemon（CLI 模式）===")
    print("按回车切换 持续听觉 开/关；输入 q 退出。")
    while True:
        try:
            cmd = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if cmd == "q":
            break
        toggle_fn()


def _run_hotkey_loop(hotkey: str, toggle_fn) -> None:
    try:
        from pynput import keyboard  # type: ignore
    except ImportError:
        logger.error("pynput 未安装，回退到 CLI 模式。pip install pynput 后可用全局快捷键。")
        _run_cli_loop(toggle_fn)
        return

    # 解析 "cmd+shift+h" → [Key.cmd, Key.shift, 'v']
    parts = [p.strip().lower() for p in hotkey.split("+") if p.strip()]
    mods_map = {
        "cmd": keyboard.Key.cmd,
        "cmd_l": keyboard.Key.cmd_l,
        "cmd_r": keyboard.Key.cmd_r,
        "ctrl": keyboard.Key.ctrl,
        "shift": keyboard.Key.shift,
        "alt": keyboard.Key.alt,
        "option": keyboard.Key.alt,
    }
    expected_mods = set()
    expected_key: str | None = None
    for p in parts:
        if p in mods_map:
            expected_mods.add(mods_map[p])
        else:
            expected_key = p

    pressed_mods: set = set()

    def on_press(key):
        if key in expected_mods or key in set(mods_map.values()):
            pressed_mods.add(key)
        else:
            k = getattr(key, "char", None) or getattr(key, "name", None)
            if (
                expected_key
                and k
                and k.lower() == expected_key
                and expected_mods
                and expected_mods.issubset(_matching_mods(pressed_mods, mods_map))
            ):
                toggle_fn()

    def on_release(key):
        pressed_mods.discard(key)

    print(f"=== 语音快捷键 daemon（快捷键模式：{hotkey}）===")
    print("按一次开启持续听觉，再按一次关闭。Ctrl+C 退出。")
    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        listener.join()


def _matching_mods(pressed: set, mods_map: dict) -> set:
    """把 pressed 里的变体键（cmd_l/cmd_r）归一化到 expected 集合。"""
    out = set()
    for k in pressed:
        out.add(k)
        if k in (mods_map.get("cmd_l"), mods_map.get("cmd_r"), mods_map.get("cmd")):
            out.add(mods_map["cmd"])
        if k in (mods_map.get("shift"),):
            out.add(mods_map["shift"])
        if k in (mods_map.get("ctrl"),):
            out.add(mods_map["ctrl"])
        if k in (mods_map.get("alt"), mods_map.get("option")):
            out.add(mods_map["alt"])
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="语音快捷键 daemon")
    parser.add_argument("--endpoint",
                        default=os.getenv("VOICE_ENDPOINT", DEFAULT_ENDPOINT))
    parser.add_argument("--instance",
                        default=os.getenv("VOICE_INSTANCE", DEFAULT_INSTANCE),
                        help="目标实例 ID（默认 Zero）")
    parser.add_argument("--hotkey",
                        default=os.getenv("VOICE_HOTKEY", DEFAULT_HOTKEY))
    parser.add_argument("--mode",
                        default=os.getenv("VOICE_MODE", "hotkey"),
                        choices=["hotkey", "cli"])
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    run_daemon(
        endpoint=args.endpoint,
        instance_id=args.instance,
        hotkey=args.hotkey,
        mode=args.mode,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
