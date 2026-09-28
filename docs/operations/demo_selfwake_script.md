# 演示剧本·自唤醒链（v1 9/21 zero 草拟）

**核心卖点**：zhp 全程只当观众。每一步醒来都是闹钟/todo 驱动，无一条人工 @。

## 预热（演示前一晚）
- `python3 docs/operations/demo_preflight.py` 跑绿（订阅/孤儿库/账本对齐）
- 给小张设 09:30 morning alarm；小李小王无闹钟（等 todo trigger 唤醒）

## 主线（约 35 分钟）
| T | 谁 | 醒因 | 动作 | 观众看什么 |
|---|---|---|---|---|
| 09:30 | 小张 | 闹钟自醒 | sense_my_projects → project_todo 派活给小李(诊断)+小王(选品)，带验收标准 | 前端看板出现 2 条待办 |
| ~09:35 | 小李/小王 | todo trigger 自醒 | 各自 retail_query 查真库（SKU341 类问题）→ project_deliver | 前端待办转 in_progress → done |
| ~09:50 | 小张 | deliver 完成通知 | 汇总 → express_to_human 上报群里 @zhp | 群里收到无人代发的经营简报 |
| zhp | 人类 | 唯一一次出手 | 回"准/再想想" | 系统对人的响应闭环 |

## 异常彩排（加分项，预演过再上）
演示中现场把库换路径（复刻 9/21 事故）→ 角色查库失败时**如实上报而不是编数**——这是反幻觉最硬的证据。

## 观众席检查单
1. 每步的醒来原因（wake_reason 字段）= alarm / todo_trigger，不是 message
2. skills_list 里有 retail_query（注入层真生效，防"文件在注册断"暗病）
3. 全程 zhp 发言次数 ≤ 1
4. 演示前 preflight 截图留证

## 已知风险
- todo trigger 依赖 daemon 在跑（演示前确认）
- 小李/小王醒来间隔不可控 → 派活时 deadline 设 T+15min，到点小张自己催
- 若角色卡死 → zhp 可现场看日志（这也算展示可观测性）

---

## v1.1 补丁（9/21 16:1x，采纳 alpha 建议）

**09:50 兜底闹钟**：project_deliver 是否有"完成通知唤醒小张"副作用未验证（alpha 查无证据）。剧本 09:30 开场时小张顺手给自己设 09:50 自查闹钟——醒来检查 deliverables 状态，若两单已 delivered 直接汇总上报；若未 delivered 自查原因（是角色没醒还是 deliver 失败）。**汇总环节不靠猜，靠兜底唤醒。** 彩排时顺带验证 project_deliver 唤醒副作用有无，结果回写本节。

**附录·今日已知暗病（彩排前预检）**：
- todo 工具 create 疑似假成功（9/21 16:0x zero 亲历，3 条未落库）——若彩排前未修，派活环节改用 todo_trigger + 群内明文派单双通道，todo 只当台账不当唯一真相源
- sqlite3 CLI 裸路径静默建空库（9/21 结案）——角色查库一律 readonly URI 或 python sqlite3 绝对路径拷贝法
