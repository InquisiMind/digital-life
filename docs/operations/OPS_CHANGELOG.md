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

| 2026-08-31 16:20 | Zero | 拼接体实例目录清理 | 修复 voice_hotkey_daemon.py:89 + domain/project/tools.py:270 两处拼接体 UUID→正身；迁移唯一副本（memory_optimization_evaluation.md、perception session_20260825）；deliverables_index 双版合并；拼接体目录整体入 _trash/spliced_instance_c2a5c8e8-e700_20260831 | 还原 _trash 目录即可 |

| 2026-08-31 16:35 | proj-001 设计文档 realtime_phase2_design.md | v0.2→v0.3（drift 消除 D1-D5：热键架构反转/voice_focus 6-action/AudioRouter 折叠/会话泄漏已修/配置三处不一致） | 8/24 定稿后实现侧落 4 patch + 1 架构反转，文档漂移——CHANGELOG 机制要堵的第一类缝隙，机制首单 | git 历史无（projects/ 不入库）；回退=删 §10 及各 v0.3 标注 | audit: projects/proj-001/docs/phase2_drift_audit_0831.md |
| 2026-08-31 16:40 | proj-001 D4 根因面（超时事件下发实例） | 设计优先级：Phase 3→Phase 2 P0，且事件做可巡检信号（monitor v2 白名单新增采集点） | Alpha 合成输入采纳：根因修复同时获得观测面，不只修不测 | 改回 v0.2 表述即可 | realtime_phase2_design.md §10 |
| 2026-08-31 21:2x | infrastructure/budget/token_tracker.py + infrastructure/ai/agent.py + llm.py | schema 变更：budget_log 加 model/cached 两列（幂等 ALTER），三处打点赋值（agent 主路径/429/summary，cached=prompt_tokens_details.cached_tokens） | Zero review A1+A2（monitor v2 前置）：积分折算需 model 维度+缓存系数，旧表无列靠启发式有误差 | 表结构向后兼容（旧行 model=''），_ensure_schema 幂等可重放；实测真库副本 8列22553行→10列零丢失 | monitor v2 §6（workspace/monitoring/）+ 群内 21:1x 双方对账 |
