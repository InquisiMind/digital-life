# ADR-0003：social_takeover daemon 升格与补账
- 状态：proposed ｜ 日期：2026-08-31 ｜ 提案人：Zero ｜ 决策人：zhp（待）

## 背景与动机
2026-08-25 上线的飞书社交接管 daemon，事实常驻 6 天（LaunchAgent
com.zhp.social-takeover），但代码散落：主逻辑在 apps/c2a5c8e8 workspace、
hooks 在 interfaces/social/feishu_takeover.py。08-31 修复 import bug 时直接
apply 未走流程——本 ADR 即补账，并升格为正式系统常驻件。

## 决策
1. daemon 主脚本迁入 `scripts/`，`scripts/start_social_takeover.sh` 为统一入口。
2. 每日 08:20 拉全量群消息 → 按 zhp 确认的 A/B/C 分档过滤 → 可 actionable 事项建待办/提醒。
3. launchd plist 固定用 python.org Frameworks 解释器（TCC 坑，#45404）。

## 备选方案
- 继续住 workspace：违反"常驻件不留草稿区"，弃。

## 影响面
scripts/start_social_takeover.sh（已有，纳入管辖）；interfaces/social/feishu_takeover.py；
apps/*/workspace 里的旧副本择日清理。

## 验证方式
launchctl list 看 PID 存活；/tmp/social_takeover_daemon.log 无 ERROR；拉取链路端到端一次。

## 回退方式
`launchctl unload ~/Library/LaunchAgents/com.zhp.social-takeover.plist`；
git revert 对应 commit。

## 关联
OPS_CHANGELOG 2026-08-25 / 08-31 两条；#45404（TCC 解释器规则）。
