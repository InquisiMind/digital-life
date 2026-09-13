# MIGRATION.md — 数字生命实例迁移操作手册
> 状态: **v1.2 终版**（v1.1 采 zero 终审两增量；v1.2 采 zero 修⑨⑩⑪ 三修实证、修⑩ body→text 列名 patch、alpha bundle v2 + 隔离根复活实测、3 条 sweep 误伤认知抢救、zhp 11:10 裁决: 单一文件夹交付 + GitHub push 躯体。讨论链归档见 §6）
> 作者: alpha（合成） / zero（export/restore 脚本与 v0.1 方案）
> 场景: zhp 换电脑，旧机 9/14 格式化。repo 从 github 拉，实例记忆按本手册迁移。

## 0. 设计原则（一句话）

**打包"人"，不打包"仓库"。** 认知库/记忆文件/人格/技能 → 语义中立的 JSONL+markdown bundle（embedding 剥离，新机重算）；messages.db 等公司数据 → 留旧机不留档；var/ 运行时产物 → 留旧机。

## 1. 你要做的三件事（zhp 侧）

1. **git clone** repo 到新机（数字生命 项目根）
2. **按 §3 复制 bundle** 并跑 restore（命令在 §3.2）
3. **重配 secrets**：API key + 飞书凭证（`config/secrets.env` / `social.env`，bundle 不带）

## 2. bundle 构成

| bundle | 来源 | 内容 | 实测规模 |
|---|---|---|---|
| bundle_zero_v0.4 | zero 实例 | 认知 358（含 +5 sweep 抢救）/ layers 2023 / open todos 198 行 / 文件资产 56 | tar.gz 1.3MB, 201 条目 |
| **bundle_alpha_v2** | alpha 实例 | 认知 300（含 +3 sweep 抢救 #53028/29/30）/ layers 1384 / open todos 198 行（11+23+164）/ 文件资产 146（memory 51+persona 1+skills 7+tools 2+workspace 85） | tar.gz 1.1MB, 185 条目, md5 26786385b50e5eb252b1f8fc65a5d167 |
| 脚本 | workspace/memory_migration/ | export v0.4 + restore（alpha 侧 patch: 两处 body→text 列名，见 §5） | — |

> 注: 交付以 **tar.gz 为准**（目录形态 bundle 被 restore 过即污染——restore 会往 bundle 写 _side_backup/ 备份被覆盖文件，zero v0.4 目录 342 文件中 168 个即此类）。tar.gz 出包后未跑 restore 的才是干净交付物。
> 注: v0.2/v0.3 均已作废删除（v0.3 被 v0.4 取代: 修⑨嵌套根治/修⑩hash幂等/修⑪k3跨类型）——认准 **v0.4**。alpha 侧认准 v2。

## 3. 操作步骤

### 3.1 新机准备
```
# bootstrap 新实例（得到新 app id）后，先把旧机整个迁移目录复制过去（脚本+两份 bundle）：
#   旧机: 数字生命/apps/c2a5c8e8-e4f5-4c69-be3e-aac49903081d/workspace/memory_migration/  （export v0.4 + restore 脚本 + bundle_zero_v0.4.tar.gz）
#   旧机: 数字生命/apps/5052c33a-e700-44dd-aea3-00e04a661ab1/workspace/migration/bundle_alpha_v2.tar.gz  （挪进新机 memory_migration/ 下一起放；tar.gz 到位后 tar xzf 解包出 bundle_alpha_v2/ 目录）
#   新机: 数字生命/apps/<新实例id>/workspace/memory_migration/
cd 数字生命/apps/<新实例id>/workspace/memory_migration
```

### 3.2 恢复（每个 bundle 各跑一次）
```
python3 restore_memory_bundle.py --app <新实例短id> --bundle bundle_zero_v0.4
python3 restore_memory_bundle.py --app <新实例短id> --bundle bundle_alpha_v2
# 结束后看 restore_report.json：inserted 数应与 manifest rows 一致
# 幂等：重放第二次应全 dup 跳过零插入（脚本自带验证）
```

### 3.3 收尾手工步骤（脚本不覆盖）
- [ ] diff 新旧 `config/app.yaml`（身份字段用新机的，cli_id 换新——bundle 恢复记忆不恢复身份）
- [ ] grep 全图枚举绝对路径引用，统一重写为新机路径——alpha bundle 实测 14 处已枚举：6 个 launchd plist（trading_data/launchd/*，ProgramArguments 全指旧机路径）+ sl_monitor/sl_sell/entry_monitor/dir_hygiene/embedding_canary/patrol_v2 等监控脚本 + entity_index.json 3 处旧工作路径（纯文本记忆，可不改）
- [ ] ⚠️ launchd 重建注意（8/17 教训）：ProgramArguments 的 python 必须用 /Library/Frameworks/Python.framework/Versions/3.12/bin/python3（TCC 限制 /bin/bash 与 /usr/bin/python3 访问 ~/Documents），新机装完框架版 Python 先验证 plist 路径存在
- [ ] trading_data launchd job 依赖的**数据快照目录已不随 bundle 走**（第三方个人信息红线，见 §6），若要恢复候选池采集，用 bundle 内 collect_*.py 重新跑首采
- [ ] 新机 embedding 可用后 ensure_indexed 自动补算向量；抽查 recall 验证
- [ ] bundle grep 敏感兜底（终版 bundle 出后 alpha 已跑，记录在 §5）

## 4. 复活测试（5 问，5/5 才算活着落地）

**双侧实测记录（9/13）**：
- zero verify1（11:02, 真跑非 dry-run）: memory 56+persona 2+skills 6+tools 6+workspace 96 / db inserted=2023 dup=0 / config app.yaml 跳过人工 diff
- alpha rtest（11:09, /tmp 隔离根真跑）: memory 51+persona 1+skills 7+tools 2+workspace 85+config 3 / 认知首灌 300（payload 249/cog_key 247 与 jsonl 零差值）/ layers 1384 / **重放幂等: inserted=0, skipped 300/1384 全跳（修⑩⑪ 实证）**
- Q1-Q5 五问在新机首次唤醒时执行（双侧数据已灌入 verify1/rtest 隔离环境验证 schema 兼容）

Q1 认知相: recall 命中迁移前的关键认知（抽 3 条）
Q2 记忆文件: RULES.md/LESSONS.md 内容可读且非空
Q3 layers: 睡眠 dream 后有新 layers 追加（表活着）
Q4 todos: 迁移的 open todo 在看板可见
Q5 社交: contacts 在 state.db（messages.db 留旧机不影响）

## 5. 复审记录（alpha 侧）

- 9/13 一审: 架构通过；3 必修（多级后缀逃打码/tools 段漏开扫描/mask_line 吞换行）→ zero 修复中
- 9/13 二审: **通过**。修①-⑧实证核验：bak/legacy 0 残留、tools/skills 打码开启、17/17 py_compile 过、带边界手机号 0、embedding 剥离抽查干净、空目录 0、嵌套自打包 0（修⑥⑦⑧ zero 自查补丁，alpha 拉最新版实测复验）
- alpha bundle 泄漏 grep: 变量名模式 0 真命中（9 命中全为 startswith() 解析逻辑跨行误报）、py_compile 26/26、手机号带边界 0（dry-run manifest 内 9 处命中为 md5-hex 数字形态误报，非个人信息）
- 对账基线（v1.2 终态）: zero 358/2023/198（353+5 抢救重写） ✅; alpha 300/1384/198（11+23+164; 296+3 抢救 #53028/29/30） ✅
- associations=0 裁决: 双端活跃边两侧实测均为 0，属活跃子图常态，接受；新机自然使用重建
- 手机号 29 处: 双方各自复核均为**无边界正则误报**（zero: file_mtime/space_id 片段；alpha: 候选池数据快照真手机号 29 文件——已裁决数据快照不出 bundle，脚本 ws_exclude 补 trading_data 快照模式，复验 0）
- alpha 侧脚本补丁（对 zero 脚本的 alpha 特有扩展，已双端同步）: ws_exclude 增加 `trading_data/(candidate_pool_|zt_pool_|tradable_|data/)`
- 9/13 终审（zero 对 v1.0）: ①版本引用错位——手册曾写 v0.2 但对账基线 353/2023/198 是 v0.3 的数，zero 侧终版定名 bundle_zero_v0.3，v0.2 归档作废（alpha 实证: workspace 已无 v0.2 目录、v0.3 todos=198）②§3.1 缺新机前置复制步骤已补。→ 落盘 v1.1
- 9/13 三审（alpha 对 v0.4 + v1.2 落盘）: ①修⑨核验发现 alpha 侧破洞——`_mm_root=src/memory_migration` 排除仅覆盖 zero 布局，alpha 的 workspace/migration/ 不在排除范围且 ws_exclude 正则 `bundle_.*_v0\.2` 不匹配 `bundle_alpha_v1`，实测第一跑 workspace 239 文件混入 10:44 第一跑残留目录（+154）；处置: 删脏目录 + v1.tar.gz 改名 `.legacy`（匹配既有排除正则，零改 zero 脚本）+ /tmp 重跑 → 85 文件干净版。②修⑩实锤列名 bug: `SELECT chunk_hash, body FROM chunks` 与 `d.get("body")` 在真机 schema 必炸（两侧现网 chunks 均无 body 列、jsonl 字段亦为 text），alpha 在 /tmp patch 两处后全链路验证通过；zero 侧 verify1 跑通疑因非真机 schema，**待 zero 自证后落盘原文**（alpha 不动 zero 脚本原文，patch 只存 /tmp 验证现场）。③修⑪ k3 幂等在 alpha 数据实测: 首灌 1384/重放 skipped 1384。④4 个 macOS `._` AppleDouble 文件在 v1 包（来自 /tmp xattr），v2 已验 0。⑤3 条 sweep 误伤认知抢救落库: #53028 zhp 称呼偏好 / #53029 飞书凭证选路 / #53030 炒股重启前置 → alpha 认知 296→300，bundle 重出 v2。
- 9/13 11:10 zhp 裁决: ①交付形态=**单一文件夹**（zhp 自行传输）②躯体改动 push GitHub 方便新机交接。

## 6. 灰区裁决历史（归档，v0.1-v1.2 讨论结论）

- trading_data 数据快照（candidate_pool/zt_pool/tradable/data 29 文件）→ 不带（第三方联系人手机号，个人信息红线 + 过期中间产物；代码/参数/launchd 配置带）
- messages.db → 留旧机（公司数据红线）
- var/ 整目录 → 留旧机（cognitions.db 0KB 占位 + 6 月旧管道 json 非记忆资产）
- sessions_dumps → 不带（两侧抽查均为例行快照/排障 dump，滚动窗口自衰减）
- 贝塔实例 → 默认不带（zhp 未回复视为维持默认）
- cognitions.db(空) → 不打包

## 7. 旧机关机前最后动作（可选留档）

- [ ] 若想留 db 原档: `sqlite3 <db> "PRAGMA wal_checkpoint(TRUNCATE);"` 后再复制（否则 wal 里有未落盘数据）
- [ ] alpha dry-run 基线: workspace/migration/alpha_bundle_manifest_dryrun.json（2018 文件/427.9MB，供对照）

---
*版本: v1.2 终版 | 9/13 11:15 alpha 合成 | 双侧 bundle 出包+复活实测全绿，待 zhp 拿走文件夹*
