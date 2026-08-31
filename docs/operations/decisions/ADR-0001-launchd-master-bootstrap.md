# ADR-0001：开机自启——launchd 薄壳拉起 master
- 状态：proposed ｜ 日期：2026-08-31 ｜ 提案人：Zero ｜ 决策人：zhp（待）

## 背景与动机
2026-08-28 15:48 Mac 整机重启，master + 双实例进程全部消失且无开机自启机制，
裸奔 3 天至 08-31 15:54 才被人工拉起；周五 21:00 复盘 / 23:20 dream 断档。
外围件（social-takeover / token-monitor）各自有 launchd 守护，唯独核心入口裸奔。
不对称证据：同机 ai.openclaw.gateway.plist（node 网关 18789）RunAtLoad+KeepAlive 齐备，
08-28 重启后它活着、master 死了——守护有无的差异直接可见。

## 决策
OS 层只做一件事：开机把 master gateway 拉起（LaunchAgent, RunAtLoad）。
所有自愈逻辑保持在 master 进程内（InstanceSupervisor 已有 worker watchdog：
死亡自动 spawn / 300s 3 次熔断），守护逻辑纯 Python、平台无关。

## 备选方案
- cron @reboot：macOS 支持不稳定，弃。
- 守护逻辑写进 plist（KeepAlive+脚本轮询）：把平台无关逻辑塞进平台绑定层，换设备即废，弃。

## 影响面
新增 `~/Library/LaunchAgents/com.zhp.digital-life-master.plist`；
master 生命周期从此由 launchd 管理。KeepAlive 用 dict 形式 `SuccessfulExit=false`：
仅异常退出（被 kill/崩溃）才重拉，主动停机不拉；且熔断逻辑在 InstanceSupervisor
（worker 级、master 进程内），与 master 进程级 KeepAlive 无交互冲突。

## 验证方式
`launchctl kickstart -k` 杀 master 验证自动恢复；`sudo reboot` 后验证全链路自动恢复。

## 回退方式
`launchctl unload ~/Library/LaunchAgents/com.zhp.digital-life-master.plist && rm <plist>`

## 关联
事故：2026-08-28 15:48 整机重启（OPS_CHANGELOG 首页）；ADR-0003（外围件守护先例）。
预写物：apps/c2a5c8e8…/workspace/governance/launchd_prewrite/（plist+start_master.sh，
拍板后 cp 落 scripts/ + ~/Library/LaunchAgents/）。
## 实施要点（坑清单）
- TCC：master 现运行解释器是 miniconda python3.11（Framework 3.12 缺 fastapi 等依赖，
实测 import 即崩，换解释器不可行）。launchd 首拉时 macOS 会弹 TCC 授权框，
需 zhp 手动允许一次（或系统设置→完全磁盘访问预先添加 miniconda3/bin/python3.11）。
wrapper 结构复刻 social-takeover 已验证 6 天模式（ADR-0003 同源）。
- 网络未就绪会重演 08-28 15:28 API 超时风暴：plist 加 ThrottleInterval 或脚本内等待网络。
