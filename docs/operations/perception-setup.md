# 感知系统配置（Perception Setup）

感知系统让数字生命能"看见"屏幕、"听见"声音：按快捷键触发录屏录音，经视觉模型理解后注入事件。

## 前提

- macOS（屏幕录制 / 麦克风 / 辅助功能权限）
- 智谱 GLM API key（复用实例已配的主模型 key）
- 已安装可选依赖（见下）

## 1. 安装依赖

```bash
pip install mss sounddevice pynput
```

- `mss` — 屏幕截图
- `sounddevice` — 麦克风录音
- `pynput` — 全局快捷键监听
- `ffmpeg`（音频分段，推荐）— `brew install ffmpeg`

## 2. 授予 macOS 权限（关键，首次必须做）

打开 **系统设置 → 隐私与安全性**，把运行 digital-life 的程序（Terminal / iTerm / python）加入以下三项：

| 权限 | 路径 | 不授权的后果 |
|---|---|---|
| **辅助功能** | 隐私与安全性 → 辅助功能 | 快捷键按了没反应（pynput 无法监听） |
| **屏幕录制** | 隐私与安全性 → 屏幕录制 | 截图全黑 |
| **麦克风** | 隐私与安全性 → 麦克风 | 录音为空 |

> 授权后**必须重启 digital-life** 才生效（权限在进程启动时绑定）。

## 3. 开启感知（配置）

在实例配置（前端控制台 → 实例 → 配置 → 感知系统，或直接编辑 `apps/<id>/config/app.yaml`）：

```yaml
perception:
  enabled: true
  hotkey: "cmd+shift+p"        # 本实例的快捷键
  vision_model: "glm-4.6v"     # 视觉模型
  frame_fps: 2.0               # 抽帧帧率
  max_capture_seconds: 120     # 最大录制时长
  context_recent_turns: 5      # 视觉模型带的主意识对话轮数
```

保存后重启网关：

```bash
digital-life restart
```

## 4. 验证

```bash
digital-life logs -f | grep -i perception
```

应看到：

```
Instance c2a5c8e8 perception daemon started
```

然后按 **cmd+shift+p**：

1. 屏幕右上角弹出 `🔴 录制中`，听到 Glass 声
2. 再按一次 → `⏹ 已结束` + Basso 声
3. 处理完成 → `✅ 已送达`（含摘要）或 `❌ 上报失败`
4. 数字生命在飞书/会话里回应基于画面内容的回复

## 多实例

每个实例配自己的 `hotkey`，互不冲突：

- 实例 zero: `cmd+shift+p`
- 实例 alpha: `cmd+shift+a`

各自按各自的键，录屏注入到对应实例。

## 排错

### 麦克风（重要限制）

**现象**：录制的音频是静音（全 0），ASR 转写结果为空。日志会显示
`录制的音频是静音（全0），可能缺少麦克风权限——跳过 ASR，仅走视觉。`

**根因**：macOS 15+ (Sequoia) **只给 GUI app bundle 的前台进程授予麦克风权限**。
命令行/后台进程（包括通过 ZCode/Terminal/launchd 启动的 python）请求麦克风时
会被静默拒绝（不弹窗、返回全 0 样本）。系统设置→麦克风列表里的 "python3"
是 `/usr/bin/python3`（系统 Xcode shim），与 miniconda 的 python3.11 是不同的
TCC 条目，授权给系统 python 不等于授权给 miniconda python。

**现状**：视觉（录屏）完全正常（屏幕录制权限机制不同，会弹窗且已生效）。
仅麦克风拿不到。系统会自动降级——只走视觉，标注 transcript 为空。

**替代方案**（后续可选，不在本期）：
  - 写一个签名+公证的 macOS 原生 helper app（Swift）代录音频
  - 用飞书语音消息（audio msg_type 已支持下载）走 ASR，绕过本地麦克风
  - 从 GUI app 上下文（如菜单栏 app）启动 digital-life

### 其它

| 现象 | 原因 | 解决 |
|---|---|---|
| 快捷键按了没反应 | 辅助功能未授权 | 系统设置 → 辅助功能 → 加 Terminal/python → 重启 |
| 截图全黑 | 屏幕录制未授权 | 系统设置 → 屏幕录制 → 加 Terminal/python → 重启 |
| 录音为空 / 无转写 | 麦克风权限（见上"重要限制"） | macOS 限制，视觉降级正常工作 |
| 日志无 `perception daemon started` | `enabled: false` 或未重启 | 设 `enabled: true` + `digital-life restart` |
| `pynput 未安装` | 缺依赖 | `pip install pynput` |
| 上报 `❌ 余额不足` | GLM 账号欠费 | 充值（code 1113，熔断 1 小时） |

## 调试模式（CLI）

不想用快捷键时，可用 CLI 模式手动控制录制：

```bash
python scripts/perception_daemon.py --instance zero --mode cli
# 按回车开始/结束录制，输入 q 退出
```

## 工作原理

```
按下快捷键 → daemon 录屏(抽帧) + 录音
  → 视频剥离音轨 → 降采样 → base64
  → 音频分段(≤30s) → glm-asr-2512 转写
  → 构建精简视觉上下文(只读主意识最近 N 轮)
  → glm-4.6v(图片序列 + ASR + 上下文) → 结构化理解
  → POST /internal/perception/trigger
  → emit perception_signal 事件 → 主意识 wake/inject
```

感知信号作为 `perception_signal` 事件注入，与飞书消息等价——主意识空闲时唤醒，运行中时平滑注入（下一轮思考看到）。

## 网页语音会话（前端按钮入口，2026-10-28）

替代快捷键的前端入口：浏览器打开 `http://localhost:8642/voice`，点麦克风按钮进入全双工语音对话。

```
浏览器(AudioWorklet 16k PCM) → WS /internal/voice/ws
  → 讯飞 RTASR 大模型版实时转写（type=1 中间结果推浏览器实时字幕；
     type=0 确认句 = 云端断句点）
  → 每句 emit perception_signal（reply_channel=voice + quick_first_reply）
  → 实例首轮 LLM 调用降 quick 档速答，之后回基本档继续干活
  → 回复走 express_to_human → voice:speaker → 本地 TTS 扬声器
```

要点：
- **凭据**：实例 `app.yaml` 的 `perception.stream_asr`（app_id/api_key/api_secret）。两键易反：`api_key`=accessKeyId=控制台 APIKey（实测踩坑，填反握手报 35010）
- **回声闸门**：TTS 播放期间写 `var/run/voice/tts_active_<iid>` 标记（voice_output 维护），播放中收到的确认句按回声丢弃；打断（barge-in）立即清标记，真人句放行
- **默认实例**：`config/voice_sense.yaml` 的 `web_voice.default_instance`（当前小张）；连接时 `?instance=<id>` 可覆盖
- 单会话单槽位（第二个连接收 busy）；讯飞单会话上限 8h，15s 无音频自动补静音帧保活
- 相关模块：`infrastructure/perception/rtasr_llm.py`（讯飞流客户端）、`web_voice.py`（会话+闸门）、`application/api/voice_ws_routes.py`（WS 路由）、`interfaces/web/voice-console/`（前端）

## 语音对话提速与打断（2026-10-28 二期）

- **模型**：语音小张（6f307170）用 DS 官方 `deepseek-flash`（控制台名 DeepSeek-V4.1-Flash-1017）。实测同 payload 比 GLM 快：首响 1-3.6s（GLM 2-4s 且晚高峰尖刺 6-9s）。DS 严格校验由 `DeepSeekProvider`（providers.py）+ `_sanitize_payload_messages` 归一消化：null content→""、孤儿 tool→user、剥历史 reasoning_content、`thinking:disabled`（V4 思考模式拒绝"tool 结尾"的 agent 循环形状）
- **流式**：`agent._chat` SSE 流式消费（provider `supports_stream` 声明，env `DIGITAL_LIFE_LLM_STREAM=0` 可关），read 超时退化为 chunk 间隔
- **提前按句 TTS**：`infrastructure/ai/tool_stream.py` 增量解码 express_to_human 的 text 参数，句成即 `speak()`（不必等整段生成完）；分派时经 `voice_prespeak_offset` ContextVar 剥已播前缀只播尾巴，日志记全文。首句发声 ≈ 说完后 2s
- **打断（barge-in）**：播报中若讯飞 partial 与最近播报文本"覆盖率 <0.5"（`web_voice._maybe_barge_in`）= 真人插话 → `stop_playback()` 停声（模型继续干活、文字照常下发前端）；回声（覆盖率高）忽略。被打断后下一句绕过回声闸门直接注入
- **前端**：/voice 字幕区显示小张回复（绿色"小张 · "前缀）与打断提示
- **TTS 声音**：`perception.tts_voice` 配置。zm_030/zf_001=本地 Kokoro 神经声（kokoro_tts_server :8300）；zh-CN-*=edge-tts 云端；macOS 声名=say。讯飞在线合成未接——Kokoro/edge 已够用，要接再加引擎

## 三期：浏览器播放 + 流水线合成 + 打断静音（2026-10-28 晚）

- **TTS 从浏览器出**（`voice_output._send_web_tts`）：合成完的音频经 WS binary 帧推浏览器 AudioContext 播放——浏览器 AEC 从源头消掉自己播的声音（回声/打断误判的根治），且部署远端也能听到。无活跃网页会话时回退本地 afplay（老行为）
- **合成∥播放流水线**（`voice_output._synth_worker_loop ×3 + _play_loop`）：speak() 发号入队 → 3 线程并行合成 → 播放线程按序消费。旧"合成→播→合成→播"串行是"半句一顿"的根源；kokoro 长句另拆 ≤12 字子句并行请求（30 字 5.1s→2.2s）
- **打断静音纪元**（`mute_playback/unmute_playback`）：barge-in 后本轮剩余输出全部只出文字不出声（含流式生成中的后续句），用户句成句注入即解除。文字链路（前端 agent_reply/日志）全程不受影响
- 事故教训：播放线程是常驻循环，单条异常必须顶层兜底（NameError 一次 = TTS 永久静默）；批量删函数后用 `compileall` + AST 未定义引用扫描双验证
