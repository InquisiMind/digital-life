"""语音输出通道（TTS）—— 本地播放，不依赖飞书。

引擎策略（2026-08-18 更新）：三引擎并存——
Kokoro（本地 ONNX，离线高音质，~3s 出声）；
macOS Siri 神经语音（say，本地合成，
零网络零抖动，~1s 出声）；edge-tts（微软云端，音质上限更高但端点
间歇抖动严重——半小时内每条都重试）作为可选高音质配置
（app.yaml perception.tts_engine: edge / tts_voice: zh-CN-YunxiNeural）。

voice 参数兼容两种形式：
  - "zh-CN-YunxiNeural" 等 edge-tts 名 → edge 引擎
  - "Reed"/"Rocko"/"Eddy" 等 macOS 声音名 → say 引擎
未配置时默认 say + Reed（Siri 神经男声，本地）。
"""
from __future__ import annotations

import logging
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

# edge-tts 二进制解析：PATH 优先，绝对路径兜底（agent 子进程 PATH 常是极简版，
# 找不到 edge-tts 会 FileNotFoundError 降级 say —— 9/16 云熙切换踩坑）
_EDGE_TTS_FALLBACK = str(Path(__file__).resolve().parents[2] / ".venv" / "bin" / "edge-tts")

def _edge_tts_exe() -> str | None:
    import shutil
    exe = shutil.which("edge-tts")
    if not exe and os.access(_EDGE_TTS_FALLBACK, os.X_OK):
        exe = _EDGE_TTS_FALLBACK
    return exe


logger = logging.getLogger(__name__)

# 默认引擎：macOS Siri 神经语音（本地、稳定、快）；可选 "edge"（云端高音质）
DEFAULT_ENGINE = os.getenv("DIGITAL_LIFE_TTS_ENGINE", "say")
# say 默认声：Reed = Siri 神经中文男声；edge 默认声：云希
DEFAULT_SAY_VOICE = "Reed"
DEFAULT_EDGE_VOICE = "zh-CN-YunxiNeural"

DEFAULT_KOKORO_VOICE = "zf_017:0.83,zm_014:0.17"
KOKORO_SERVER_URL = os.getenv("KOKORO_SERVER_URL", "http://127.0.0.1:8300")
KOKORO_VENV_PYTHON = "/Users/zhanghaopu/Documents/探索项目/digital-life/.venv/bin/python"
KOKORO_SERVER_SCRIPT = "/Users/zhanghaopu/Documents/探索项目/digital-life/scripts/kokoro_tts_server.py"

# 是否启用 TTS（环境变量 / 实例配置控制）
DEFAULT_ENABLED = os.getenv("DIGITAL_LIFE_TTS", "0") == "1"


def _looks_like_edge_voice(voice: str) -> bool:
    """edge-tts 声音名形如 zh-CN-YunxiNeural（含 '-' 且全 ASCII）。"""
    return bool(voice) and "-" in voice and voice.isascii()

def _looks_like_kokoro_voice(voice: str) -> bool:
    """Kokoro voice names: zf_xxx (中文女声), zm_xxx (中文男声), bf_xxx, bm_xxx, etc.
    Also supports blend syntax: zf_017:0.83,zm_014:0.17"""
    if not voice or len(voice) < 4:
        return False
    # Blend syntax: contains ':' and ','
    if ':' in voice and ',' in voice:
        parts = voice.split(',')
        for p in parts:
            tokens = p.strip().split(':')
            if len(tokens) < 2 or len(tokens[0]) < 4 or tokens[0][2] != '_' or tokens[0][0] not in ('z', 'b', 'a'):
                return False
        return True
    # Single voice: zf_xxx
    return voice[2] == "_" and voice[0] in ("z", "b", "a")




def _resolve_say_voice(voice: str) -> str:
    """say 声音名解析：多语言声（Reed/Rocko/Eddy…）必须带语言后缀。

    ``say -v Reed`` 不带语言时匹配到列表第一个（英语版）——英语声读
    中文完全没法听（"乱码感"）。本机实测正确形式：``Reed (中文（中国大陆）)``。
    已带括号后缀的（Tingting 等单语言声不需要）原样返回。
    """
    v = (voice or "").strip() or DEFAULT_SAY_VOICE
    if "(" in v:
        return v
    # Siri 多语言声名单 → 中文（中国大陆）变体
    _MULTI_LANG = {"Reed", "Rocko", "Eddy", "Flo", "Grandma", "Grandpa", "Sandy", "Shelley"}
    if v in _MULTI_LANG:
        return f"{v} (中文（中国大陆）)"
    return v


def _strip_markdown(text: str) -> str:
    """剥掉 markdown 格式符号，保留纯文本（供 TTS 朗读）。

    规则：
    - **bold** / *italic* / __bold__ / _italic_ → 去符号留文字
    - `code` / ```code``` → 去符号留文字
    - [link](url) → 只留 link 文字
    - # 标题 / ## 标题 → 去掉 # 前缀
    - - / * / 1. 列表项 → 去掉前缀符号
    - |table| → 转成逗号分隔的纯文本行
    - --- / === / *** 分隔线 → 删除
    - > 引用 → 去掉 > 前缀
    """
    # 代码块 ```...``` → 保留内容
    text = re.sub(r"```\n?", "", text)
    # 行内代码 `code` → 保留内容
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # 链接 [text](url) → 只留 text
    text = re.sub(r"\[([^]]*)\]\([^)]*\)", r"\1", text)
    # 加粗+斜体 ***text*** → text
    text = re.sub(r"\*{3}([^*]+)\*{3}", r"\1", text)
    # 加粗 **text** / __text__ → text
    text = re.sub(r"\*{2}([^*]+)\*{2}", r"\1", text)
    text = re.sub(r"_{2}([^_]+)_{2}", r"\1", text)
    # 斜体 *text* / _text_ → text（注意不误伤 * 列表标记）
    text = re.sub(r"(?<!^)(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", text)
    # 分隔线 --- / === / *** → 删除整行
    text = re.sub(r"^[\s]*[-=\*]{3,}[\s]*$", "", text, flags=re.MULTILINE)
    # 标题 # / ## / ### → 去前缀
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # 引用 > text → text
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
    # 表格分隔行 |---|---| → 删除（在处理 pipe 之前）
    text = re.sub(r"^\|?[\s:]*-{2,}[\s:]*(\|[\s:]*-{2,}[\s:]*)*\|?\s*$", "", text, flags=re.MULTILINE)
    # 表格行 | a | b | → a, b
    text = re.sub(r"^\||\|$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\|", "，", text)
    # 列表标记 - / * / + 开头 → 去掉（保留内容）
    text = re.sub(r"^[\s]*[-*+]\s+", "", text, flags=re.MULTILINE)
    # 数字列表 1. / 2. → 去掉（保留内容）
    text = re.sub(r"^[\s]*\d+\.\s+", "", text, flags=re.MULTILINE)
    return text


def _clean_text_for_tts(text: str) -> str:
    """清理文本供 TTS 朗读：去 @ 标签、剥 markdown 格式、合并空白。

    markdown 格式符号（星号、竖线、井号等）被 TTS 读出来会非常奇怪，
    必须在送入合成引擎前剥掉，只保留纯文本内容。
    """
    # 去 @ 标签
    text = re.sub(r"<at[^>]*>.*?</at>", "", text)
    text = re.sub(r"<at[^>]*/>", "", text)
    # 剥 markdown 格式
    text = _strip_markdown(text)
    # 去多余空白
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _is_mostly_cjk(text: str) -> bool:
    """文本是否以中文为主（用于 say 降级判定——Tingting 读中文可以，读英文很怪）。"""
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    letters = sum(1 for c in text if c.isalpha())
    return letters == 0 or cjk / letters >= 0.5


def _stderr_tail(stderr: bytes) -> str:
    """取 stderr 的最后一个异常行（traceback 头几行没信息量）。"""
    lines = (stderr or b"").decode(errors="replace").strip().splitlines()
    for line in reversed(lines):
        if line.strip() and not line.startswith(" "):
            return line.strip()[:120]
    return lines[-1][:120] if lines else "unknown"


def _notify_turn_anchor(iid: str, key: str) -> None:
    """链路计时锚点上报（no-op 安全）：web_voice 会话在收集时记首轮时刻。"""
    if not iid:
        return
    try:
        from infrastructure.perception.web_voice import get_web_voice_hub

        get_web_voice_hub().note_tts_timing(iid, key)
    except Exception:
        pass


def speak(text: str, *, voice: str = "", rate: str = "+0%") -> bool:
    """语音播报一段文本（排队，异步）。返回是否成功入队。

    多次调用自动排队串行播放（zero 可以分多次表达，不会叠音）；
    stop_playback() 打断当前播放并丢弃全部未播的排队项。

    Args:
        text: 要播放的文本（会自动清理标记）
        voice: 声音名。Kokoro 声（"zf_001"/"zm_030"…）→ 本地 ONNX 合成（离线高音质）；
            macOS 声（"Reed"/"Rocko"/"Eddy"…）→ say 本地合成；
            edge-tts 名（"zh-CN-YunxiNeural"…）→ 云端合成（高音质，有抖动）。
            空串 = 引擎默认声（say→Reed / edge→云希 / kokoro→zf_001）。
        rate: 语速调整（"+0%" 正常 / "+60%" 快60%；edge 和 Kokoro 引擎均生效）
    """
    clean = _clean_text_for_tts(text)
    if not clean:
        return False
    if _is_muted():
        # 静音纪元（barge-in）：本轮输出只出文字不出声；返回 True 让
        # 调用方的 sent=True → 日志/前端文字照常，音频被抑制。
        logger.info("TTS muted (barge-in epoch), text-only: %s", clean[:30])
        return True
    _ensure_player()
    _write_tts_marker()  # 入队即写：合成期间也算"即将播"，防回声句钻合成间隙
    # 实例 ID 必须在调用线程（唤醒线程，ContextVar 有效）捕获——合成/播放
    # 线程没有实例上下文，web sink 推送会找错对象（2026-10-28 实测踩坑）
    try:
        from infrastructure.config import get_app_instance_id as _gii

        iid = _gii() or ""
    except Exception:
        iid = ""
    # 讯飞在线：入队时按句切块（2026-10-30 首段延迟实测根因——整句一次合成
    # RTF≈0.2，8s 长句要合成 2s，"文字到了声音姗姗来迟"的大头）。句级块独立
    # seq，3 个合成线程并行出声：首句 ~0.3-0.8s 即起播，浏览器队列无缝续播。
    # 注意要句级不要逗号碎块——块边界=韵律断点，逗号级切碎 = 一顿一顿。
    parts: list[str] = [clean]
    if (voice or "").startswith(("xf:", "xfs:")):
        parts = split_for_xf(clean) or [clean]
    global _next_seq
    with _ready_lock:
        base = _next_seq
        _next_seq += len(parts)
    for i, part in enumerate(parts):
        _tts_queue.put((base + i, part, voice, rate, _generation, iid))
    _notify_turn_anchor(iid, "speak")  # 链路锚点④：模型首条可解析输出 → TTS 调用
    return True


# ── 播放队列：单线程串行合成+播放（多次表达自动排队）─────────────────────────
_tts_queue: "queue.Queue[tuple[str, str, str, int]]" = queue.Queue()
_player_lock = threading.Lock()
_player_thread: threading.Thread | None = None
# 打断代数：stop_playback 时 +1，使入队/合成中的旧代语音全部失效
_generation = 0


def _ensure_player() -> None:
    """懒启动合成线程×3 + 播放线程（第一次 speak 时起，常驻）。"""
    with _player_lock:
        global _player_thread
        if _player_thread is None:
            for i in range(3):
                threading.Thread(
                    target=_synth_worker_loop, name=f"tts-synth-{i}", daemon=True
                ).start()
            threading.Thread(target=_play_loop, name="tts-play", daemon=True).start()
            _player_thread = True




# ── 播放流水线（2026-10-28 重构：合成∥播放，治"半句一顿"）─────────────────────
# 旧结构：单线程 串行 合成→播→合成→播——第一句播的 3-5s 里没人合成第二句，
# 播完再花 1-2s 现场合成 → 句间空窗（"输出半句顿一下"的根源）。
# 新结构：speak() 发号入队 → N 个合成线程并行出音频字节 → 1 个播放线程按号
# 顺序消费（web 会话推浏览器 / 本地 afplay）。模型出字快于说话，合成永远
# 跑在播放前面，句间零等待——停顿只来自标点本身。

_ready_lock = threading.Condition()
_ready_audio: dict[int, tuple[bytes, str, str, int] | None] = {}
_next_seq = 0
_next_play_seq = 0
# 静音纪元（barge-in）：被打断后，本轮剩余输出全部只出文字不出声，
# 直到用户下一句成句注入（web_voice 调 unmute）或超时兜底。
_muted_until = 0.0

# ── 假打断恢复捕获（2026-10-30，LiveKit false_interruption 的文本层版本）──
# stop_playback 时捕获"被打断的剩余文本"（本地正在播 + ready 未播 + 队列未合成），
# 同时记录已下发浏览器的 (seq, text) 段日志；web_voice 判定"误判打断"
# （2.5s 内无后续人声文本）时用 collect_recovery_texts 取走续播。
# 停止指令/白字即停的调用方不消费这些文本，捕获本身无副作用。
_now_playing_seq = -1
_now_playing_text = ""
_play_activity = 0                       # 每次 _play_bytes +1（恢复前有新播放则放弃）
_web_segment_log: list[tuple[int, str]] = []   # 已推浏览器的段（seq, text）
_pending_recovery: dict[int, str] = {}   # 停播时捕获的未播段（seq → text）


def note_web_playback(instance_id: str, started: bool) -> None:
    """浏览器回报音频播放起止（web 会话的 TTS 标记精确生命周期）。

    浏览器 AudioContext 开始出声 → started=True（touch 标记）；队列播空/
    被 stop_tts 打断 → started=False（清标记）。旧模式靠本地 afplay 进程
    结束清标记，浏览器模式没有本地进程——没有这个回报，标记只能 30s 过期，
    回声闸门和打断判断全建立在过时的"正在播报"状态上（2026-10-28 打断
    失灵事故根因）。
    """
    p_ = _tts_marker_path_for(instance_id)
    if p_ is None:
        return
    try:
        if started:
            p_.parent.mkdir(parents=True, exist_ok=True)
            p_.touch()
        else:
            p_.unlink(missing_ok=True)
    except Exception:
        pass


def _tts_marker_path_for(instance_id: str) -> "Path | None":
    try:
        from infrastructure.config import get_project_root

        if not instance_id:
            return None
        return get_project_root() / "var" / "run" / "voice" / f"tts_active_{instance_id}"
    except Exception:
        return None


def mute_playback(seconds: float = 20.0) -> None:
    """静音接下来的 TTS。文字链路（express/日志/前端显示）不受影响。"""
    global _muted_until
    _muted_until = time.time() + seconds


def unmute_playback() -> None:
    global _muted_until
    _muted_until = 0.0


def _is_muted() -> bool:
    return time.time() < _muted_until


def _store_ready(seq: int, result) -> None:
    with _ready_lock:
        _ready_audio[seq] = result
        _ready_lock.notify_all()


def _synth_worker_loop() -> None:
    while True:
        seq, text, voice, rate, gen, iid = _tts_queue.get()
        logger.info("TTS synth begin: seq=%d %r iid=%s", seq, text[:20], iid[:8])
        _notify_turn_anchor(iid, "synth")  # 链路锚点⑤：合成线程取到首块
        if gen != _generation:
            logger.info("TTS synth skip (stale gen): seq=%d %r", seq, text[:20])
            _store_ready(seq, None)
            continue
        try:
            data, ext = _synth_one(text, voice, rate, gen)
            result = (data, ext, text, gen, iid) if gen == _generation else None
        except Exception as exc:
            logger.warning("TTS synth failed: %s (%s)", exc, text[:30])
            result = None
        _store_ready(seq, result)


def _play_loop() -> None:
    global _next_play_seq
    while True:
        try:
            with _ready_lock:
                while _next_play_seq not in _ready_audio:
                    _ready_lock.wait(timeout=0.5)
                seq = _next_play_seq
                item = _ready_audio.pop(seq)
                _next_play_seq += 1
            if item is None:
                continue
            data, ext, text, gen, iid = item
            if gen != _generation or _is_muted():
                logger.info("TTS audio dropped (interrupted/muted): %s", text[:30])
                continue
            logger.info("TTS play begin: seq→%d %r %dKB", seq, text[:20], len(data) // 1024)
            _play_bytes(data, ext, gen, text, iid, seq)
            logger.info("TTS play end: %r", text[:20])
        except Exception:
            # 播放线程是常驻循环：单条异常绝不能杀死它（2026-10-28 NameError
            # 事故：线程崩死后 TTS 全静默、无任何日志）
            logger.exception("TTS play loop item failed (loop continues)")




# ── Kokoro 本地引擎（离线、无抖动）────────────────────────────────────────────
_kokoro_server_checked = False
_kokoro_server_available = False


def _ensure_kokoro_server() -> bool:
    """检查 Kokoro server 是否在运行，没运行则自动启动。返回是否可用。

    Server 启动需要 ~5s（加载 ONNX 模型），首次调用会阻塞等待。
    """
    global _kokoro_server_checked, _kokoro_server_available, _kokoro_server_last_check
    if _kokoro_server_checked:
        if not _kokoro_server_available:
            # 每 60s 重试一次
            import time as _t
            if _t.time() - _kokoro_server_last_check > 60:
                _kokoro_server_checked = False
            else:
                return False
        else:
            return True

    import time as _t
    _kokoro_server_last_check = _t.time()
    _kokoro_server_checked = True

    # Check health
    try:
        import requests
        r = requests.get(f"{KOKORO_SERVER_URL}/api/health", timeout=2)
        # 兼容两代 server 响应：新版 {"engine": "kokoro"}；旧版（2026-10 起
        # 常驻的那只）{"status":"ok","model":"kokoro-v1.1-zh..."} 无 engine 字段
        # ——只认 engine 会把健康服务判死，全部回退 say（2026-10-28 播报变声事故）
        body = {}
        try:
            body = r.json()
        except Exception:
            body = {}
        healthy = (
            r.status_code == 200
            and (body.get("engine") == "kokoro"
                 or str(body.get("status")) == "ok"
                 or "kokoro" in str(body.get("model") or ""))
        )
        if healthy:
            _kokoro_server_available = True
            logger.info("Kokoro server already running (model=%s)", body.get("model"))
            return True
    except Exception:
        pass

    # Start server in background
    logger.info("Starting Kokoro server...")
    try:
        subprocess.Popen(
            [KOKORO_VENV_PYTHON, KOKORO_SERVER_SCRIPT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd="/Users/zhanghaopu/Documents/探索项目/digital-life",
        )
    except Exception as exc:
        logger.warning("Failed to start Kokoro server: %s", exc)
        _kokoro_server_available = False
        return False

    # Wait for server to be ready (max 15s)
    for _ in range(30):
        _t.sleep(0.5)
        try:
            import requests
            r = requests.get(f"{KOKORO_SERVER_URL}/api/health", timeout=2)
            if r.status_code == 200 and r.json().get("engine") == "kokoro":
                _kokoro_server_available = True
                logger.info("Kokoro server started successfully")
                return True
        except Exception:
            pass

    logger.warning("Kokoro server failed to start within 15s")
    _kokoro_server_available = False
    return False


def _rate_to_speed(rate: str) -> float:
    """将 edge-tts 风格 rate ('+60%', '-20%') 转为 Kokoro speed (1.6, 0.8)。"""
    try:
        pct = int(rate.replace('%', '').replace('+', ''))
        speed = 1.0 + pct / 100.0
        return min(max(speed, 0.5), 2.0)
    except (ValueError, AttributeError):
        return 1.0


# ── Kokoro 长句拆短并行合成（2026-10-28 播报提速）─────────────────────────────
# 实测 kokoro ONNX 合成耗时 ∝ 文本长度（6字 0.8s / 30字 5.1s），整句串行让
# "提前播报"的收益被合成吃掉。按标点拆 ≤12 字的子句并行请求再拼接 WAV，
# 30 字 5.1s → 3 路并行 ≈ 1.7s，首句出声时间 ≈ 最短子句的合成时间。

_KOKORO_CHUNK_MAX = 9
_KOKORO_CHUNK_SPLIT = "，。！？；、,.!?; \n"

# 讯飞在线合成块长（2026-10-30）：句级切块的上限。首版按 12 字碎块切导致
# 每个逗号一次独立合成 = 每个逗号一次韵律停顿，听感"一顿一顿"（zhp 卡顿
# 反馈）——讯飞 RTF≈0.2-0.3 吃得下句级块（20-30 字 ≈5-7s 音频合成 1-2s），
# 停顿只应出现在句子边界（自然节奏）。
_XF_CHUNK_MAX = 30


def split_for_xf(text: str, max_len: int = _XF_CHUNK_MAX) -> list[str]:
    """讯飞系（v2/超拟人）朗读块：句级优先（保韵律连读），超长句逗号二段切。

    与 split_for_tts（kokoro，9 字碎块）语义不同：合成器按块独立出声，
    块边界 = 韵律断点——只在句号级断才自然。逗号只在句子超过 max_len 时
    作为兜底切点，无任何标点的超长串硬切。
    """
    import re

    out: list[str] = []
    # 1) 句号级切分（终止符保留在句尾）
    for s in re.findall(r"[^。！？；!?;\n]*[。！？；!?;\n]|[^。！？；!?;\n]+", text):
        s = s.strip()
        if not s:
            continue
        if len(s) <= max_len:
            out.append(s)
            continue
        # 2) 超长句：逗号/顿号处贪心合并到 ~max_len
        parts = [p for p in re.split(r"(?<=[，、,])", s) if p]
        buf = ""
        for p in parts:
            if buf and len(buf) + len(p) > max_len:
                out.append(buf)
                buf = p
            else:
                buf += p
        if buf:
            out.append(buf)
    # 3) 无标点的超长残片（如长数字串）：硬切
    final: list[str] = []
    for piece in out:
        while len(piece) > max_len:
            final.append(piece[:max_len])
            piece = piece[max_len:]
        if piece:
            final.append(piece)
    return final or [text]


def split_for_tts(text: str, max_len: int = _KOKORO_CHUNK_MAX) -> list[str]:
    """把文本拆成 ≤max_len 的朗读子句（标点处切，不劈词尽量保语义）。"""
    out: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in _KOKORO_CHUNK_SPLIT and len(buf) >= max_len // 2:
            out.append(buf)
            buf = ""
    if buf.strip():
        out.append(buf)
    return [c for c in out if c.strip()]


def merge_wav_bytes(wavs: list[bytes]) -> bytes:
    """把同一参数的多个 WAV 字节流按序合并为一个（wave 模块解包再打包）。"""
    import io
    import wave

    if len(wavs) == 1:
        return wavs[0]
    params = None
    frames = b""
    for w in wavs:
        with wave.open(io.BytesIO(w), "rb") as wf:
            if params is None:
                params = wf.getparams()
            frames += wf.readframes(wf.getnframes())
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setparams(params)
        out.writeframes(frames)
    return buf.getvalue()


def _kokoro_synth(text: str, voice: str, rate: str, timeout: int = 30) -> bytes:
    """合成整段文本为 WAV 字节。长文本拆短并行请求（单请求 ∝ 长度）。"""
    import requests as _req
    from concurrent.futures import ThreadPoolExecutor

    chunks = split_for_tts(text)
    speed = _rate_to_speed(rate)

    def _one(chunk: str) -> bytes:
        r = _req.post(
            f"{KOKORO_SERVER_URL}/api/tts",
            json={"text": chunk, "voice": voice or DEFAULT_KOKORO_VOICE, "speed": speed},
            timeout=timeout,
        )
        if r.status_code != 200:
            raise RuntimeError(f"kokoro HTTP {r.status_code}")
        return r.content

    if len(chunks) <= 1:
        return _one(text.strip() or text)
    with ThreadPoolExecutor(max_workers=min(4, len(chunks))) as pool:
        parts = list(pool.map(_one, chunks))
    return merge_wav_bytes(parts)


# ── 讯飞在线合成（v2 TTS，2026-10-30 zhp 拍板接入）──────────────────────────
# wss://tts-api.xfyun.cn/v2/tts ｜ HMAC-SHA256 鉴权（老教程的 hmac-sha1 会被
# 拒："algorithm hmac-sha1 not supported"）。凭据与 RTASR 同组同应用通用
# （voice_sense.yaml web_voice.stream_asr 段，控制台按应用授权服务即可）。
# 音色以 "xf:" 前缀路由（如 xf:xiaoyan）。实测整句合成+传输 ≈0.13s
# （本地 Kokoro 同句 1-3.3s 且单调劣化）。输出 raw PCM 16k → 这里包 WAV。

def _xfyun_creds() -> tuple[str, str, str]:
    """(app_id, api_key, api_secret) 从 voice_sense.yaml stream_asr 段读。"""
    try:
        import yaml

        cfg = yaml.safe_load(
            (Path(__file__).resolve().parents[2] / "config" / "voice_sense.yaml").read_text(encoding="utf-8")
        ) or {}
        sa = (cfg.get("web_voice") or {}).get("stream_asr") or {}
        return str(sa.get("app_id") or ""), str(sa.get("api_key") or ""), str(sa.get("api_secret") or "")
    except Exception:
        return ("", "", "")


def _xfyun_synth(text: str, voice: str, rate: str, timeout: int = 15) -> bytes:
    """讯飞 v2 TTS 合成 → WAV 字节（16k/16bit/mono）。"""
    import base64 as _b64
    import hashlib as _hl
    import hmac as _hm
    import io as _io
    import json as _json
    import wave as _wave
    from datetime import datetime as _dt, timezone as _tz
    from urllib.parse import urlencode as _ue
    from websocket import create_connection as _cw

    app_id, api_key, api_secret = _xfyun_creds()
    if not (app_id and api_key and api_secret):
        raise RuntimeError("xfyun TTS credentials missing (voice_sense.yaml stream_asr)")

    host = "tts-api.xfyun.cn"; path = "/v2/tts"
    date = _dt.now(_tz.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    sig = _b64.b64encode(_hm.new(
        api_secret.encode(),
        f"host: {host}\ndate: {date}\nGET {path} HTTP/1.1".encode(),
        _hl.sha256,
    ).digest()).decode()
    auth = _b64.b64encode(
        f'api_key="{api_key}", algorithm="hmac-sha256", '
        f'headers="host date request-line", signature="{sig}"'.encode()
    ).decode()
    url = f"wss://{host}{path}?{_ue({'authorization': auth, 'date': date, 'host': host})}"

    ws = _cw(url, timeout=timeout)
    try:
        ws.send(_json.dumps({
            "common": {"app_id": app_id},
            "business": {"aue": "raw", "auf": "audio/L16;rate=16000",
                         "vcn": voice or "xiaoyan", "tte": "UTF8"},
            "data": {"status": 2, "text": _b64.b64encode(text.encode("utf-8")).decode()},
        }))
        pcm = b""
        while True:
            d = _json.loads(ws.recv())
            code = d.get("code")
            if code != 0:
                raise RuntimeError(f"xfyun TTS code={code}: {d.get('message')}")
            pcm += _b64.b64decode(d.get("data", {}).get("audio") or "")
            if d.get("data", {}).get("status") == 2:
                break
    finally:
        try:
            ws.close()
        except Exception:
            pass
    if not pcm:
        raise RuntimeError("xfyun TTS returned no audio")
    pcm = _trim_edge_silence(pcm, 16000)
    buf = _io.BytesIO()
    with _wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(pcm)
    return buf.getvalue()


# ── 讯飞超拟人 TTS（2026-10-30 zhp 指定接入，xfs: 前缀路由）──────────────────
# 文档 https://www.xfyun.cn/doc/spark/super smart-tts.html
# wss://cbm01.cn-huabei-1.xf-yun.com/v1/private/mcd9m97e6
# 与 v2 同款 HMAC-SHA256 签名（实测现有 RTASR 凭据直接可用）；
# 协议差异：text 要 base64；音频分片在 payload.audio.audio（base64）。
# 实测延迟（12 字块）：x5_*_flow RTF≈0.2-1.2 可用；x6 系列 4-10 不可用
# （11-28s/块，免费档排队严重）——候选表里 x6 标注"延迟不稳"。
_XF_SUPER_HOST = "cbm01.cn-huabei-1.xf-yun.com"
_XF_SUPER_PATH = "/v1/private/mcd9m97e6"


def _rate_to_xf_super_speed(rate: str) -> int:
    """edge 风格 rate ('+30%') → 超拟人 speed 0-100（50=默认，100=两倍）。"""
    try:
        pct = int(rate.replace("%", "").replace("+", ""))
        return int(min(100, max(0, 50 * (1 + pct / 100.0))))
    except (ValueError, AttributeError):
        return 50


def _trim_edge_silence(
    pcm: bytes, rate: int, *, head_keep_ms: int = 60, tail_keep_ms: int = 180,
    threshold: float = 0.008,
) -> bytes:
    """裁掉合成音频自带的边缘静音（2026-10-30 zhp"块间卡顿"实测根因）。

    超拟人每块尾部自带 ~750ms 静音（逗号/句号无差别）、头部 ~78ms——切块
    播报时每个块边界都叠 0.75s 死寂。裁到保留自然呼吸的短垫（尾 180ms /
    头 60ms，≈-42dB 以下算静音）。v2 小燕尾静音仅 16ms，本函数为 no-op。
    """
    try:
        import numpy as _np

        x = _np.frombuffer(pcm, dtype=_np.int16)
        if x.size < rate // 5:
            return pcm  # 太短不动
        amp = _np.abs(x.astype(_np.float32) / 32768.0)
        nz = _np.where(amp > threshold)[0]
        if nz.size == 0:
            return pcm  # 全静音（异常合成）不动，交给上层报错逻辑
        head = max(0, nz[0] - int(rate * head_keep_ms / 1000))
        tail = min(x.size, nz[-1] + 1 + int(rate * tail_keep_ms / 1000))
        if head == 0 and tail == x.size:
            return pcm  # 本来就没多少边缘静音
        return x[head:tail].tobytes()
    except Exception:
        return pcm  # 裁剪失败宁可留静音也不能坏音频


def _xf_super_synth(text: str, vcn: str, rate: str, timeout: int = 15) -> bytes:
    """超拟人合成 → WAV 字节（24k/16bit/mono，副语言更自然）。凭据同 v2。"""
    import base64 as _b64
    import hashlib as _hl
    import hmac as _hm
    import io as _io
    import json as _json
    import wave as _wave
    from datetime import datetime as _dt, timezone as _tz
    from urllib.parse import urlencode as _ue
    from websocket import create_connection as _cw

    app_id, api_key, api_secret = _xfyun_creds()
    if not (app_id and api_key and api_secret):
        raise RuntimeError("xfyun super TTS credentials missing (voice_sense.yaml stream_asr)")

    date = _dt.now(_tz.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    sig = _b64.b64encode(_hm.new(
        api_secret.encode(),
        f"host: {_XF_SUPER_HOST}\ndate: {date}\nGET {_XF_SUPER_PATH} HTTP/1.1".encode(),
        _hl.sha256,
    ).digest()).decode()
    auth = _b64.b64encode(
        f'api_key="{api_key}", algorithm="hmac-sha256", '
        f'headers="host date request-line", signature="{sig}"'.encode()
    ).decode()
    url = (f"wss://{_XF_SUPER_HOST}{_XF_SUPER_PATH}?"
           f"{_ue({'authorization': auth, 'date': date, 'host': _XF_SUPER_HOST})}")

    ws = _cw(url, timeout=timeout)
    try:
        ws.send(_json.dumps({
            "header": {"app_id": app_id, "status": 2},
            "parameter": {"tts": {
                "vcn": vcn or "x5_lingyuzhao_flow",
                "speed": _rate_to_xf_super_speed(rate),
                "volume": 50, "pitch": 50, "bgs": 0,
                "audio": {"encoding": "raw", "sample_rate": 24000,
                          "channels": 1, "bit_depth": 16},
            }},
            "payload": {"text": {
                "encoding": "utf8", "compress": "raw", "format": "plain",
                "status": 2, "seq": 0,
                "text": _b64.b64encode(text.encode("utf-8")).decode(),
            }},
        }))
        pcm = b""
        chunks = 0
        while True:
            d = _json.loads(ws.recv())
            chunks += 1
            header = d.get("header") or {}
            code = header.get("code")
            if code not in (None, 0):
                raise RuntimeError(f"xfyun super TTS code={code}: {header.get('message')}")
            au = ((d.get("payload") or {}).get("audio") or {})
            pcm += _b64.b64decode(au.get("audio") or "")
            if au.get("status") == 2 or header.get("status") == 2:
                break
            if chunks > 400:
                raise RuntimeError("xfyun super TTS too many chunks")
    finally:
        try:
            ws.close()
        except Exception:
            pass
    if not pcm:
        raise RuntimeError("xfyun super TTS returned no audio")
    pcm = _trim_edge_silence(pcm, 24000)
    buf = _io.BytesIO()
    with _wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(pcm)
    return buf.getvalue()


# ── VoxCPM2（Yoolee 内网 TTS，2026-10-29 zhp 提供接口）─────────────────────────
# ws://<host>/ws/tts?api_key=... ｜ JSON 控制帧 + 二进制 48k PCM16LE mono
# 一次性调用模式：每次合成单独建连、发完即关（文档 2.3 兼容说明支持）。
# 音色名以 "vox:" 前缀路由（如 vox:cs_lora_refmix），端点/密钥在
# voice_sense.yaml web_voice.voxcpm 段。内网不可达时回退 say。

def _voxcpm_endpoint() -> tuple[str, str, str, str]:
    """(ws_url_with_key, voice, quality, api_key) 从 voice_sense.yaml 读。"""
    try:
        import yaml

        cfg = yaml.safe_load(
            (Path(__file__).resolve().parents[2] / "config" / "voice_sense.yaml").read_text(encoding="utf-8")
        ) or {}
        vx = (cfg.get("web_voice") or {}).get("voxcpm") or {}
        url = str(vx.get("url") or "ws://172.31.3.159:18083/ws/tts")
        key = str(vx.get("api_key") or "test")
        sep = "&" if "?" in url else "?"
        return (
            f"{url}{sep}api_key={key}",
            str(vx.get("voice") or "cs_lora_refmix"),
            str(vx.get("quality") or "standard"),
            key,
        )
    except Exception:
        return ("ws://172.31.3.159:18083/ws/tts?api_key=test", "cs_lora_refmix", "standard", "test")


# VoxCPM 熔断（2026-10-29：key 失效 403 时每句白试一次握手再回退，拖慢出声）
_voxcpm_fail_streak = 0
_voxcpm_blocked_until = 0.0


def _voxcpm_available() -> bool:
    return time.time() >= _voxcpm_blocked_until


def _note_voxcpm_result(ok: bool) -> None:
    global _voxcpm_fail_streak, _voxcpm_blocked_until
    if ok:
        _voxcpm_fail_streak = 0
        return
    _voxcpm_fail_streak += 1
    if _voxcpm_fail_streak >= 3:
        _voxcpm_blocked_until = time.time() + 300  # 冷却 5 分钟
        _voxcpm_fail_streak = 0
        logger.warning("voxcpm 熔断 5 分钟（连续失败）——期间直接走回退引擎")


def _voxcpm_synth(text: str, voice: str) -> bytes:
    """一段文本 → WAV 字节（48k PCM 包装成 RIFF，浏览器/afplay 通吃）。"""
    import json as _json
    import uuid as _uuid
    import wave as _wave
    import io as _io

    import websocket as _ws

    url, default_voice, quality, _ = _voxcpm_endpoint()
    turn = _uuid.uuid4().hex[:12]
    req = {
        "id": f"{turn}-0",
        "text": text,
        "voice": voice or default_voice,
        "quality": quality,
        "turn_id": turn,
        "sticky_key": turn,
        "tts_unit_index": 0,
    }
    ws = _ws.create_connection(url, timeout=3)
    pcm = bytearray()
    started = False
    try:
        ws.send(_json.dumps(req, ensure_ascii=False))
        ws.settimeout(20)  # 首块 5s 由下方整体限时兜底；块间空闲 20s
        import time as _time

        deadline = _time.time() + 20
        first_deadline = _time.time() + 5
        while _time.time() < deadline:
            msg = ws.recv()
            if isinstance(msg, (bytes, bytearray)):
                if not started:
                    continue  # start 前的残留音频丢弃（协议 2.4）
                pcm.extend(msg)
                continue
            try:
                ev = _json.loads(msg)
            except Exception:
                raise RuntimeError("voxcpm 非法响应（非 JSON 文本帧）")
            et = ev.get("type")
            if et == "start":
                started = True
                first_deadline = _time.time() + 5
            elif et in ("done", "skip"):
                break
            elif et == "error":
                raise RuntimeError(f"voxcpm error 事件: {str(ev)[:120]}")
            # queued / candidate_selected / speaker_gate_result → 忽略
            if not pcm and _time.time() > first_deadline + 15:
                raise RuntimeError("voxcpm 超时无音频")
        else:
            raise RuntimeError("voxcpm 合成超时")
    finally:
        try:
            ws.close()
        except Exception:
            pass
    if not pcm:
        raise RuntimeError("voxcpm 无音频输出")
    # 48k PCM16LE mono → WAV
    buf = _io.BytesIO()
    with _wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(bytes(pcm))
    return buf.getvalue()


def _synth_one(text: str, voice: str, rate: str, gen: int) -> tuple[bytes, str]:
    """合成一段文本为音频字节 ``(data, ext)``。引擎按 voice 名分流，失败自动降级 say。"""
    if (voice or "").startswith("xfs:"):
        # 超拟人（2026-10-30 zhp 指定）：xfs:<vcn>。失败降级 v2 → 本地链
        try:
            return _xf_super_synth(text, voice[4:], rate), "wav"
        except Exception as exc:
            logger.warning("xfyun super synth failed: %s (%s) — fallback v2/local",
                           str(exc)[:120], text[:30])
            try:
                return _xfyun_synth(text, "xiaoyan", rate), "wav"
            except Exception:
                pass
            if _ensure_kokoro_server():
                try:
                    return _kokoro_synth(text, DEFAULT_KOKORO_VOICE, rate), "wav"
                except Exception:
                    pass
            return _say_synth(text, DEFAULT_SAY_VOICE), "wav"
    if (voice or "").startswith("xf:"):
        # 讯飞在线合成（2026-10-30）：默认引擎。失败降级本地链，语音不中断
        try:
            return _xfyun_synth(text, voice[3:], rate), "wav"
        except Exception as exc:
            logger.warning("xfyun synth failed: %s (%s) — fallback local", str(exc)[:120], text[:30])
            if _ensure_kokoro_server():
                try:
                    return _kokoro_synth(text, DEFAULT_KOKORO_VOICE, rate), "wav"
                except Exception:
                    pass
            return _say_synth(text, DEFAULT_SAY_VOICE), "wav"
    if (voice or "").startswith("vox:"):
        if not _voxcpm_available():
            return _say_synth(text, DEFAULT_SAY_VOICE), "wav"
        try:
            data = _voxcpm_synth(text, voice[4:])
            _note_voxcpm_result(True)
            return data, "wav"
        except Exception as exc:
            _note_voxcpm_result(False)
            logger.warning("voxcpm synth failed（内网不通/key 失效？）: %s — fallback say", str(exc)[:100])
            return _say_synth(text, DEFAULT_SAY_VOICE), "wav"
    if _looks_like_kokoro_voice(voice):
        if _ensure_kokoro_server():
            try:
                return _kokoro_synth(text, voice, rate), "wav"
            except Exception as exc:
                logger.warning("kokoro synth failed: %s (%s)", exc, text[:30])
        return _say_synth(text, _SAY_FALLBACK.get("zh-CN-YunxiNeural", "Tingting")), "wav"
    if _looks_like_edge_voice(voice or ""):
        data = _edge_synth(text, voice or DEFAULT_EDGE_VOICE, rate)
        if data:
            return data, "mp3"
        if _is_mostly_cjk(text):
            return _say_synth(text, _SAY_FALLBACK.get(voice or "", "Tingting")), "wav"
        raise RuntimeError("edge-tts synth failed (non-CJK, no fallback)")
    return _say_synth(text, voice or DEFAULT_SAY_VOICE), "wav"


def _say_synth(text: str, say_voice: str) -> bytes:
    """macOS say 合成为 WAV 字节（LEI16@24k，浏览器 decodeAudioData / afplay 通吃）。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False, dir="/tmp")
    tmp.close()
    try:
        subprocess.run(
            ["/usr/bin/say", "-v", _resolve_say_voice(say_voice), "-o", tmp.name,
             "--data-format=LEI16@24000", text],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=120, check=True,
        )
        with open(tmp.name, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


def _edge_synth(text: str, voice: str, rate: str) -> bytes | None:
    """edge-tts 云端合成为 mp3 字节。快速窗口 2 次重试扛瞬时抖动；失败返回 None。"""
    exe = _edge_tts_exe()
    if not exe:
        return None
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False, dir="/tmp")
    tmp.close()
    try:
        for backoff in (0, 1):
            if backoff:
                time.sleep(backoff)
            r = subprocess.run(
                [exe, "--voice", voice, "--rate", rate,
                 "--text", text, "--write-media", tmp.name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
            )
            if r.returncode == 0 and os.path.getsize(tmp.name) > 0:
                with open(tmp.name, "rb") as f:
                    return f.read()
        return None
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


def _send_web_tts(audio_bytes: bytes, text: str, iid: str = "", seq: int = -1) -> bool:
    """有活跃网页语音会话 → 音频字节推浏览器播放。无会话/失败返回 False。

    ``iid`` 优先用 speak 时捕获的（合成/播放线程无实例上下文，必须显式传）；
    空则退回当前线程 ContextVar。2026-10-28 事故教训：本函数曾被批量清理
    误删只剩调用点 → 播放线程 NameError 崩死、TTS 全静默——播放线程必须
    顶层兜底异常（见 _play_loop 的 except），删除函数用编译+grep 双验证。
    """
    if not iid:
        try:
            from infrastructure.config import get_app_instance_id

            iid = get_app_instance_id() or ""
        except Exception:
            iid = ""
    if not iid:
        return False
    try:
        from infrastructure.perception.web_voice import get_web_voice_hub

        ok = get_web_voice_hub().push_tts_audio(iid, audio_bytes, text, seq)
        if ok:
            if seq >= 0:
                _web_segment_log.append((seq, text))
                del _web_segment_log[:-40]  # 有界：只留最近的（恢复窗口只看最近一轮）
            logger.info("voice → web browser: %d chars (%dKB)", len(text), len(audio_bytes) // 1024)
        return ok
    except Exception:
        logger.debug("web tts push failed", exc_info=True)
        return False


def _play_bytes(data: bytes, ext: str, gen: int, text: str, iid: str = "", seq: int = -1) -> None:
    """播放一段合成音频：网页会话活跃 → 推浏览器；否则本地 afplay（可被打断）。"""
    global _now_playing_seq, _now_playing_text, _play_activity
    _play_activity += 1
    _now_playing_seq = seq
    _now_playing_text = text
    if _send_web_tts(data, text, iid, seq):
        return
    tmp = tempfile.NamedTemporaryFile(suffix="." + ext, delete=False, dir="/tmp")
    tmp.close()
    try:
        with open(tmp.name, "wb") as f:
            f.write(data)
        player = subprocess.Popen(["afplay", tmp.name],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _register_playback(player)
        try:
            player.wait(timeout=300)
        finally:
            _unregister_playback(player)
        if player.returncode == 0:
            logger.info("voice played: %d chars (%s)", len(text), ext)
        else:
            logger.info("voice playback ended early (rc=%s, likely barge-in)", player.returncode)
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


# ── 播放打断（barge-in）──────────────────────────────────────────────────────
# 跟踪当前播放进程：用户开口/按快捷键时 stop_playback() 立即停掉 zero 的播报，
# 防止麦克风把 zero 自己的声音录进去（ASR 回声）。
_playback_lock = threading.Lock()
_playback_proc: subprocess.Popen | None = None


def stop_playback(grace_seconds: float = 2.0) -> bool:
    """打断全部语音输出：当前播放 + 所有排队未播的。返回是否有东西被打断。

    - 代数 +1：队列中未处理的项、正在合成中的项全部失效丢弃
    - 清空播放队列
    - 停掉当前正在播的进程（先 SIGTERM，``grace_seconds`` 内不退出再 SIGKILL）
    - 捕获被打断的剩余文本到 ``_pending_recovery``（假打断恢复用；无消费方时无副作用）
    """
    global _generation
    _generation += 1
    dropped = 0
    collected: dict[int, str] = {}
    while True:
        try:
            item = _tts_queue.get_nowait()
            collected[item[0]] = item[1]
            dropped += 1
        except queue.Empty:
            break

    # 流水线 ready 缓冲清空 + 补洞：被丢弃/作废的序号填 None，
    # 防播放线程等一个永远不会来的序号（死锁）
    global _next_seq
    with _ready_lock:
        for s, it in _ready_audio.items():
            if it is not None and s >= _next_play_seq:
                collected[s] = it[2]
        _ready_audio.clear()
        for s in range(_next_play_seq, _next_seq):
            _ready_audio[s] = None
        _ready_lock.notify_all()

    if _now_playing_text:
        collected.setdefault(_now_playing_seq, _now_playing_text)
    global _pending_recovery
    if collected:
        # 只有真打断了东西才覆盖（空停 no-op 不能冲掉上一次捕获的恢复文本）
        _pending_recovery = collected

    with _playback_lock:
        proc = _playback_proc
    interrupted = False
    if proc is not None and proc.poll() is None:
        interrupted = True
        try:
            proc.terminate()
            try:
                proc.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)
        except Exception:
            pass

    # 浏览器模式：本地既无播放进程也无本地队列（dropped/interrupted 都是 0），
    # 但浏览器在播——stop_tts 必须无条件发，不能挂在本地判定分支里
    # （2026-10-28 打断失灵主因：条件永假 → 浏览器永远收不到停播指令）。
    web_stopped = False
    try:
        from infrastructure.perception.web_voice import get_web_voice_hub

        web_stopped = get_web_voice_hub().stop_web_tts()
    except Exception:
        pass
    # 打断=全部作废（含合成中），立即清回声标记：真人打断后紧接着注入的
    # 转写句是真话，不能被残留标记当回声丢掉。
    p = _tts_marker_path()
    if p is not None:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
    if dropped or interrupted or web_stopped:
        logger.info("TTS interrupted (barge-in): local_playing=%s, queued dropped=%d, web_stopped=%s",
                    interrupted, dropped, web_stopped)
        return True
    return False


def play_activity() -> int:
    """播放活动计数（每次开始播一段 +1）。假打断恢复前用它检测
    "停播后是否已有新播放开始"——有则不续播，避免叠音。"""
    return _play_activity


def collect_recovery_texts(after_seq: int | None) -> list[str]:
    """取走假打断续播文本（调用即消费，状态清零）。

    ``after_seq``：浏览器回报的"stop_tts 时正在播的段序号"——浏览器队列里
    排在它之后被丢弃的段（已下发未播）+ 停播时服务端未下发的段，按 seq
    序返回。``None`` = 无浏览器回报（本地 afplay 模式），从最近下发/正在播
    的段全部恢复。
    """
    global _pending_recovery
    merged: dict[int, str] = {}
    if after_seq is None:
        merged.update(_web_segment_log)
    else:
        merged.update((s, t) for s, t in _web_segment_log if s > after_seq)
    merged.update(_pending_recovery)
    _web_segment_log.clear()
    _pending_recovery = {}
    return [merged[s] for s in sorted(merged) if (merged[s] or "").strip()]


def _register_playback(proc: subprocess.Popen) -> None:
    with _playback_lock:
        global _playback_proc
        _playback_proc = proc
    _write_tts_marker()


def _unregister_playback(proc: subprocess.Popen) -> None:
    with _playback_lock:
        global _playback_proc
        if _playback_proc is proc:
            _playback_proc = None
    _clear_tts_marker_if_idle()


# ── 回声闸门标记（2026-10-28 网页语音会话）──────────────────────────────────
# 播放期间写 var/run/voice/tts_active_<iid>；master 的 web_voice 会话在注入
# 转写句前查此标记，丢弃"扬声器→麦克风"回声转写出的假句子（详见
# web_voice.is_tts_active）。进程崩溃不清除时靠 mtime 新鲜窗口兜底。

def _tts_marker_path() -> Path | None:
    try:
        from infrastructure.config import get_app_instance_id, get_project_root
        iid = get_app_instance_id()
        if not iid:
            return None
        return get_project_root() / "var" / "run" / "voice" / f"tts_active_{iid}"
    except Exception:
        return None


def _write_tts_marker() -> None:
    p = _tts_marker_path()
    if p is None:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
    except Exception:
        pass


def _clear_tts_marker_if_idle() -> None:
    """仅当既无播放进程、队列也空时清标记（串播间隙/合成期保持标记）。"""
    with _playback_lock:
        still_playing = _playback_proc is not None
    p = _tts_marker_path()
    if still_playing or p is None:
        return
    if not _tts_queue.empty():
        return
    try:
        p.unlink(missing_ok=True)
    except Exception:
        pass



def is_tts_enabled(instance_id: str | None = None) -> bool:
    """检查 TTS 是否启用。

    优先读实例 app.yaml 的 perception.tts_enabled，fallback 到环境变量。
    """
    if instance_id:
        try:
            import yaml

            cfg_path = Path(__file__).resolve().parents[2] / "apps" / instance_id / "config" / "app.yaml"
            if cfg_path.exists():
                cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
                perc = cfg.get("perception") or {}
                return bool(perc.get("tts_enabled", False))
        except Exception:
            pass
    return DEFAULT_ENABLED


def get_tts_voice(instance_id: str | None = None) -> str:
    """读取配置的 TTS 语音（决定引擎：macOS 声名 → say，edge-tts 名 → 云端）。

    未配置时按引擎默认：say → Reed（Siri 神经男声，本地稳定）；
    显式配置 zh-CN-* 名则用 edge-tts（高音质，容忍抖动）。
    """
    return _get_perc_field(instance_id, "tts_voice") or _default_voice()


def get_tts_rate(instance_id: str | None = None) -> str:
    """读取配置的 TTS 语速（"+10%" 形式；仅 edge-tts 生效，say 忽略）。"""
    return _get_perc_field(instance_id, "tts_rate") or "+0%"


def _get_perc_field(instance_id: str | None, key: str) -> str:
    if instance_id:
        try:
            import yaml

            cfg_path = Path(__file__).resolve().parents[2] / "apps" / instance_id / "config" / "app.yaml"
            if cfg_path.exists():
                cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
                perc = cfg.get("perception") or {}
                val = str(perc.get(key) or "").strip()
                if val:
                    return val
        except Exception:
            pass
    return ""


def _default_voice() -> str:
    if DEFAULT_ENGINE == "edge":
        return DEFAULT_EDGE_VOICE
    return DEFAULT_SAY_VOICE
