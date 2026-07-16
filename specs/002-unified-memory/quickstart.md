# Quickstart: 统一记忆体系

本地最快验证当前阶段成果的路径。每阶段更新。

## 准备（所有阶段共用）

```bash
# 在实例数据目录上验证
cd /Users/zhanghaopu/Documents/项目材料/探索项目/数字生命

# 确认实例库存在
ls apps/*/data/memories/memory_vectors.db
ls apps/*/data/memories/memory_layers.db
ls apps/*/data/memories/entity_index.json
```

## P1 验证（缺口补丁）

### 1. 嵌入客户端改造

```bash
python3 -m pytest tests/ -k "embed_texts or embed_partial or embed_timeout" -v
```

手动验证（不依赖网络）：

```bash
python3 -c "
from unittest.mock import patch, MagicMock
from domain.memory.memory.recall.vector import _embed_texts

# 模拟 429
with patch('urllib.request.urlopen', side_effect=Exception('HTTP 429')):
    r = _embed_texts(['hello'])
    print('returns:', r)  # 应 None 或 []，且 stderr 有 warning

# 模拟部分成功（API 返回 2/3）
fake_resp = MagicMock()
fake_resp.read.return_value = b'{\"data\":[{\"index\":0,\"embedding\":[0.1]*2048},{\"index\":2,\"embedding\":[0.2]*2048}]}'
with patch('urllib.request.urlopen', return_value=fake_resp):
    r = _embed_texts(['a','b','c'])
    print('len:', len(r) if r else 'None', 'partials:', [bool(x) for x in r] if r else 'N/A')
"
```

预期：`len` 是 3、第 1 项 None、其它有值（留成功项）。

### 2. segment 索引

```bash
python3 -m pytest tests/ -k "segment_narrative_indexed or digest_segment" -v
```

手动验证——找最近一个会话的 segment narrative：

```bash
sqlite3 apps/c2a5c8e8-*/data/memories/memory_layers.db \
  "SELECT period, substr(llm_summary,1,80) FROM memory_layers WHERE layer='segment' ORDER BY created_at DESC LIMIT 5;"
```

确认这些 `llm_summary` 也在向量库里：

```bash
sqlite3 apps/c2a5c8e8-*/data/memories/memory_vectors.db \
  "SELECT source, COUNT(*) FROM chunks WHERE source='digest_segment' GROUP BY source;"
# 改造前应为空或 source 缺；改造后应有行
```

跑一次 consolidate 触发回填：

```bash
python3 -c "
from infrastructure.config import set_runtime_instance_id  # 或你的实例设置函数
from domain.memory.memory.summaries.consolidation_runtime import backfill_existing_sessions
import sqlite3
sess_db = sqlite3.connect('apps/c2a5c8e8-*/data/state.db')
backfill_existing_sessions(sess_db, limit=5)
"
```

### 3. `update_entity_index_from_narrative` 修复

```bash
python3 -m pytest tests/ -k "update_entity_index_from_narrative or sync_entity_from_narrative" -v
```

手动验证——feeding 一段含已知实体的 narrative：

```bash
python3 -c "
from domain.memory.memory.summaries.consolidation_runtime import update_entity_index_from_narrative
# 替换为实例里已存在的实体名
update_entity_index_from_narrative('今天和 ZHP 聊了 A+ 策略，效果不错')
# 检查 entity_index 是否有新条目
import json
data = json.load(open('apps/c2a5c8e8-*/data/memories/entity_index.json'))
print('ZHP' in data.get('entities', {}), 'A+' in data.get('entities', {}))
"
```

预期：两者都 True（或至少进入 _pending 同步队列）。

### 4. 基线评估

```bash
python3 scripts/eval_memory_recall.py --output /tmp/recall_p1.json
cat /tmp/recall_p1.json | python3 -m json.tool | head -20
```

与 `apps/c2a5c8e8-*/data/memories/recall_eval_report.json`（改前基线）对比召回率。

## P2 验证（统一检索面）

```bash
# facade 融合测
python3 -m pytest tests/ -k "unified_recall or rrf_fusion or fts_search or attention_boost" -v

# 三 sense 工具走 facade 的静态断言
python3 -m pytest tests/ -k "sense_tools_facade" -v

# 检索硬上限
python3 -m pytest tests/ -k "unified_recall_hard_timeout" -v
```

手动验证 facade：

```bash
python3 -c "
from domain.memory.recall.unified import unified_recall
r = unified_recall('A+ 策略效果', attention_tokens=['A+', '策略'])
print(f'n_results={len(r)}')
for x in r[:5]: print(' ', x['kind_label'], x['body'][:80])
"
```

嵌入 API 不可达（断网验证）：

```bash
python3 -c "
import os
os.environ['LLM_API_KEY'] = ''  # 强制无 key
from domain.memory.recall.unified import unified_recall
r = unified_recall('A+ 策略效果')  # 应仍非空(FTS5 兜底)
print(f'without embedding: n_results={len(r)}')
"
```

## P3 验证（切片层重构 + 参数引擎）

```bash
python3 -m pytest tests/ -k "schema_migration or slice_defaults or slice_dynamics or slice_backfill" -v

# 验证迁移幂等（跑两次应一致）
python3 -c "
from domain.memory.recall.vector import _get_db
db1 = _get_db(); db1.close()
db2 = _get_db(); db2.close()  # 二次打开应无 ALTER 报错
print('migration idempotent')
"
```

监控页：打开控制台 chunks 列表，确认能看到 phase/source_kind/authority 等新字段。

新加记忆源演练（验证 FR-303 可扩展性）：

```bash
# 假设 P3 提供 register_normalizer(name, fn, defaults) 接口
python3 -c "
from domain.memory.recall.unified.slice import register_normalizer, Slice
register_normalizer('project', lambda p: Slice(body=p.name+p.description, phase='cognition', authority=0.8), {})
print('extended without touching retrieval code')
"
```

## P4 验证（认知演化）

```bash
python3 -m pytest tests/ -k "cognition_state_machine or supersede_chain or access_does_not_reinforce or cluster_born" -v
```

铁律一验证：

```bash
python3 -c "
from domain.memory.recall.unified import cognition
slice = cognition.create(authority=0.5)
for _ in range(100):
    cognition.on_access(slice)  # 想 100 次
print('authority after 100 accesses:', slice.authority)  # 应仍 0.5
"
```

supersede 链验证：

```bash
python3 -c "
from domain.memory.recall.unified import cognition
old = cognition.create(authority=0.7)
new = cognition.create(authority=0.9)
cognition.supersede(old, new)  # 旧标 supersede_by=新, 新 derived_from += [旧]
print('old.supersede_by:', old.supersede_by, '=', new.id)
print('new.derived_from:', old.id in new.derived_from)
"
```
