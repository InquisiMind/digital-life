# OPS_CHANGELOG：运行时改动台账
> 通报管"知会"，本台账管"对账"。凡动 domain/infrastructure/gateway/interfaces/scripts
> 或部署/重启常驻件，一笔一行。漏报的以 `git log` vs 本表差异现形。
> 规则：能预判的事前记，紧急的事后 24h 内补记。系统级设计决策另见
> [architecture/decisions/](../architecture/decisions/README.md)。

| 日期 | 组件 | 动作 | 动机 | 回退 | 关联 |
| --- | --- | --- | --- | --- | --- |
| 2026-08-25 | scripts/social_takeover daemon + com.zhp.social-takeover (launchd) | 部署上线 | 每日 08:20 社交接管（A/B/C 分档） | launchctl unload | ADR-0003（补账） |
| 2026-08-27 | apps/*/workspace exit_sweep.patch | 落盘（**未 apply**） | 会话退出扫尾未投递事件 | 删文件即可 | 待 zhp 审 |
| 2026-08-28 15:48 | 整机（事故） | Mac 重启，master+双实例裸奔 3 天 | 系统 BUG 跑崩（zhp 确认） | — | ADR-0001 动机 |
| 2026-08-31 16:05 | interfaces/social/feishu_takeover.py | 紧急修复：import 源指向错误单行 patch，apply 即生效 | daemon 反复 ImportError | /tmp 备份 + git revert | ADR-0003 |
| 2026-08-31 16:06 | com.zhp.social-takeover (launchd) | 重启验证 | 修复后重启 | — | 上行 |
