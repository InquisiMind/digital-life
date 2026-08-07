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
