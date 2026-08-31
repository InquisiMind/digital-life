# Architecture Decisions（ADR）

架构决策记录。每笔系统层修改/常驻件部署必须有一份对应 ADR，回答三问：
**代码在哪（git）/ 为何存在（本文）/ 怎么回退（回退段）**。

## 规范
- 文件名：`ADR-NNNN-<slug>.md`，编号连号，只增不改；推翻旧决策出新 ADR 并将旧文标 `superseded`。
- 状态：`proposed`（提案）→ `accepted`（zhp/决策人确认）→ `superseded`（被替代）。
- 紧急修复允许先 apply 后补 ADR，但 24h 内必须补齐并在 OPS_CHANGELOG 留痕。
- 模板九段：背景与动机 / 决策 / 备选方案 / 影响面 / 验证方式 / 回退方式 / 关联。

## 索引
| 编号 | 标题 | 状态 | 日期 |
| --- | --- | --- | --- |
| [ADR-0001](ADR-0001-launchd-master-bootstrap.md) | 开机自启：launchd 薄壳拉起 master | proposed | 2026-08-31 |
| [ADR-0002](ADR-0002-monitor-v2.md) | token_budget_monitor v2（含目录卫生巡检） | proposed | 2026-08-31 |
| [ADR-0003](ADR-0003-social-takeover-daemon.md) | social_takeover daemon 升格与补账 | proposed | 2026-08-31 |
