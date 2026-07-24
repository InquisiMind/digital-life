<template>
  <div>
    <!-- 工具栏 (无大标题, 嵌入 MemoriesTab 子 tab 时不需要再重复 "切片") -->
    <section class="chunks-toolbar">
      <div style="display: flex; gap: 8px; flex-wrap: wrap; align-items: center;">
        <el-input
          v-model="filters.q"
          placeholder="搜索切片内容"
          size="small"
          clearable
          style="width: 200px;"
          @keyup.enter="loadChunks(1)"
          @clear="loadChunks(1)"
        />
        <el-select
          v-model="filters.source"
          placeholder="来源 source"
          size="small"
          clearable
          filterable
          style="width: 180px;"
          @change="loadChunks(1)"
        >
          <el-option v-for="s in sourceOptions" :key="s" :label="s" :value="s" />
        </el-select>
        <el-radio-group v-model="filters.phase" size="small" @change="onLocalFilter">
          <el-radio-button label="">全部</el-radio-button>
          <el-radio-button label="experience">经历</el-radio-button>
          <el-radio-button label="cognition">认知</el-radio-button>
        </el-radio-group>
        <el-button size="small" type="primary" @click="loadChunks(1)">刷新</el-button>
      </div>
    </section>

    <!-- 切片层概览: phase / cognition_state / 取代诞生链 统计 -->
    <!-- 概览 stats panel 已移除: 总数与分页器底部"共 X 条"重复,
         phase 分布以前是基于当前页 30 条计算的假全库分布, 显示反而误导。
         phase 全库分布需要后端额外 GROUP BY 查询, 暂不维护——单 el-radio-button 过滤已够用 -->

    <el-table
      :data="filteredChunks"
      v-loading="loading"
      style="width: 100%;"
      @row-click="openDetail"
      row-key="id"
    >
      <el-table-column prop="id" label="#" width="60" sortable />
      <el-table-column label="phase" width="100">
        <template #default="{ row }">
          <el-tag :type="phaseTagType(row.phase)" size="small" effect="dark">
            {{ phaseLabel(row.phase) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="source" width="140">
        <template #default="{ row }">
          <span class="mono" style="color: var(--neon-cyan); font-size: 11px;">{{ row.source }}</span>
        </template>
      </el-table-column>
      <el-table-column label="text" min-width="320" show-overflow-tooltip>
        <template #default="{ row }">
          <span style="color: var(--text-secondary); font-size: 12px;">{{ row.text }}</span>
        </template>
      </el-table-column>
      <el-table-column label="认知状态" width="110">
        <template #default="{ row }">
          <el-tag v-if="row.cognition_state" :type="stateTagType(row.cognition_state)" size="small">
            {{ row.cognition_state }}
          </el-tag>
          <span v-else style="color: var(--text-muted); font-size: 11px;">—</span>
        </template>
      </el-table-column>
      <el-table-column label="authority" width="90">
        <template #default="{ row }">
          <span class="mono" :style="`color: ${authColor(row.authority)};`">{{ fmt(row.authority, 2) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="freshness" width="100">
        <template #default="{ row }">
          <el-progress :percentage="Math.round((row.freshness || 0) * 100)" :stroke-width="6" :color="freshColor(row.freshness)" :show-text="false" style="width: 70px;" />
          <span class="mono" style="color: var(--text-muted); font-size: 10px; margin-left: 4px;">{{ fmt(row.freshness, 2) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="created" width="140">
        <template #default="{ row }">
          <span style="color: var(--text-muted); font-size: 11px;">{{ fmtTime(row.created_at) }}</span>
        </template>
      </el-table-column>
    </el-table>

    <div style="margin-top: 12px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
      <span style="color: var(--text-muted); font-size: 12px;">共 {{ total }} 条</span>
      <el-pagination
        v-if="total > PAGE_SIZE"
        background
        layout="prev, pager, next"
        :total="total"
        :page-size="PAGE_SIZE"
        :current-page="page"
        :pager-count="7"
        @current-change="loadChunks"
      />
    </div>

    <!-- 详情抽屉 -->
    <el-drawer v-model="detailVisible" title="切片详情" size="60%" direction="rtl">
      <div v-if="detailLoading" v-loading="true" style="height: 200px;"></div>
      <div v-else-if="detail" class="detail-pane">
        <div class="detail-row">
          <span class="detail-key">id</span>
          <span class="mono">#{{ detail.id }}</span>
          <el-tag :type="phaseTagType(detail.phase)" size="small" effect="dark" style="margin-left: 8px;">
            {{ phaseLabel(detail.phase) }}
          </el-tag>
          <el-tag v-if="detail.cognition_state" :type="stateTagType(detail.cognition_state)" size="small" style="margin-left: 4px;">
            {{ detail.cognition_state }}
          </el-tag>
        </div>

        <div class="detail-row">
          <span class="detail-key">source</span>
          <span class="mono" style="color: var(--neon-cyan);">{{ detail.source }}</span>
          <span v-if="detail.source_kind" style="color: var(--text-muted); margin-left: 8px;">/ {{ detail.source_kind }}</span>
        </div>

        <div class="detail-block">
          <div class="detail-key">body(完整文本)</div>
          <pre class="detail-text">{{ detail.text }}</pre>
        </div>

        <div class="detail-grid">
          <div class="metric"><div class="metric-label">authority</div><div class="metric-value" :style="`color:${authColor(detail.authority)}`">{{ fmt(detail.authority) }}</div></div>
          <div class="metric"><div class="metric-label">permanence</div><div class="metric-value">{{ fmt(detail.permanence) }}</div></div>
          <div class="metric"><div class="metric-label">freshness</div><div class="metric-value">{{ fmt(detail.freshness) }}</div></div>
          <div class="metric"><div class="metric-label">activation</div><div class="metric-value">{{ fmt(detail.activation) }}</div></div>
          <div class="metric"><div class="metric-label">verification</div><div class="metric-value">{{ fmt(detail.verification) }}</div></div>
          <div class="metric"><div class="metric-label">evidence</div><div class="metric-value">{{ detail.evidence_count }}</div></div>
          <div class="metric"><div class="metric-label">challenge</div><div class="metric-value" :class="{ warn: detail.challenge_count > 0 }">{{ detail.challenge_count }}</div></div>
        </div>

        <div v-if="detail.session_id" class="detail-row">
          <span class="detail-key">session</span>
          <span class="mono" style="color: var(--neon-purple);">{{ detail.session_id }}</span>
          <span v-if="detail.segment_index !== null && detail.segment_index !== undefined" style="margin-left: 8px; color: var(--text-muted);">seg={{ detail.segment_index }}</span>
        </div>

        <div v-if="parseList(detail.derived_from).length" class="detail-block">
          <div class="detail-key">derived_from(诞生链上游)</div>
          <div class="chain-row">
            <el-tag v-for="src in parseList(detail.derived_from)" :key="src" type="success" size="small" effect="plain" style="margin: 0 4px 4px 0; cursor: pointer;" @click="jumpTo(src)">#{{ src }}</el-tag>
            <span style="color: var(--text-muted); font-size: 11px; margin-left: 8px;">kind: {{ detail.derive_kind || '—' }}</span>
          </div>
        </div>

        <div v-if="detail.supersede_by" class="detail-block warn">
          <div class="detail-key">↻ 已被取代</div>
          <div>本切片已被 #<el-link type="primary" @click="jumpTo(detail.supersede_by)">{{ detail.supersede_by }}</el-link> 取代(supersede_by 链)。</div>
        </div>

        <div v-if="parseList(detail.entity_links).length || parseList(detail.attention_tokens).length" class="detail-block">
          <div class="detail-key">导航骨架(entity_links / attention_tokens)</div>
          <div>
            <el-tag v-for="e in parseList(detail.entity_links)" :key="'e'+e" type="info" size="small" style="margin: 0 4px 4px 0;">{{ e }}</el-tag>
            <el-tag v-for="t in parseList(detail.attention_tokens)" :key="'t'+t" type="warning" size="small" effect="plain" style="margin: 0 4px 4px 0;">{{ t }}</el-tag>
          </div>
        </div>

        <div v-if="detail.linked && detail.linked.length" class="detail-block">
          <div class="detail-key">associations(共现联想)</div>
          <div class="chain-row">
            <el-tag v-for="lk in detail.linked" :key="lk.id" type="info" size="small" style="margin: 0 4px 4px 0; cursor: pointer;" @click="jumpTo(lk.id)">
              #{{ lk.id }} · w={{ fmt(lk.weight) }}
            </el-tag>
          </div>
        </div>

        <div v-if="detail.provenance" class="detail-row">
          <span class="detail-key">provenance</span>
          <span style="color: var(--text-muted); font-size: 11px;">{{ detail.provenance }}</span>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { instanceApi } from '@/api/client'
import { ElMessage } from 'element-plus'

const route = useRoute()
const iid = computed(() => route.params.iid)

const loading = ref(true)
const chunks = ref([])
const total = ref(0)
const sourceOptions = ref([])

const filters = ref({ q: '', source: '', phase: '' })

const overview = ref(null)

const detailVisible = ref(false)
const detailLoading = ref(false)
const detail = ref(null)

const PAGE_SIZE = 30
const page = ref(1)

async function loadChunks(targetPage) {
  // targetPage 可为数字(翻到指定页) 或未传(重置到第 1 页)
  const p = Number(targetPage) > 0 ? Number(targetPage) : 1
  page.value = p
  const offset = (p - 1) * PAGE_SIZE
  loading.value = true
  try {
    const res = await instanceApi(iid.value).chunks({
      q: filters.value.q || undefined,
      source: filters.value.source || undefined,
      limit: PAGE_SIZE,
      offset,
    })
    if (res.error) {
      ElMessage.error(res.error)
      return
    }
    chunks.value = res.chunks || []
    total.value = res.total || 0
    // 收集 source 选项
    const sset = new Set(chunks.value.map((c) => c.source).filter(Boolean))
    sourceOptions.value = [...sset].sort()
    // 概览统计(基于当前返回的 batch + 当前库的更大范围数,用 total 修一下)
    recomputeOverview(res)
  } catch (e) {
    ElMessage.error('加载切片失败: ' + e.message)
  } finally {
    loading.value = false
  }
}

function recomputeOverview(res) {
  const rows = res?.chunks || []
  const phase = {}
  const state = {}
  let superseded = 0
  let derived = 0
  for (const r of rows) {
    if (r.phase) phase[r.phase] = (phase[r.phase] || 0) + 1
    if (r.cognition_state) state[r.cognition_state] = (state[r.cognition_state] || 0) + 1
    if (r.supersede_by) superseded++
    if (r.derived_from && r.derived_from !== '[]') derived++
  }
  overview.value = {
    total: res?.total || rows.length,
    phase,
    state,
    superseded,
    derived,
  }
}

// 本地 phase 过滤(list endpoint 已返 phase 字段)
const filteredChunks = computed(() => {
  if (!filters.value.phase) return chunks.value
  return chunks.value.filter((c) => c.phase === filters.value.phase)
})

function onLocalFilter() {
  // 仅本地过滤,不重拉数据
}

async function openDetail(row) {
  detailVisible.value = true
  detailLoading.value = true
  detail.value = null
  try {
    const res = await instanceApi(iid.value).chunkDetail(row.id)
    if (res.error) {
      ElMessage.error(res.error)
      return
    }
    detail.value = res
  } catch (e) {
    ElMessage.error('加载详情失败: ' + e.message)
  } finally {
    detailLoading.value = false
  }
}

async function jumpTo(id) {
  if (!id) return
  // 切到指定 chunk 详情
  detailLoading.value = true
  detail.value = null
  try {
    const res = await instanceApi(iid.value).chunkDetail(id)
    if (res.error) {
      ElMessage.error(res.error)
      return
    }
    detail.value = res
  } catch (e) {
    ElMessage.error('跳转失败: ' + e.message)
  } finally {
    detailLoading.value = false
  }
}

// ───── helpers ─────
function parseList(v) {
  if (!v) return []
  if (Array.isArray(v)) return v
  try {
    const parsed = JSON.parse(v)
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []
  }
}

function fmt(v, d = 3) {
  if (v === null || v === undefined) return '—'
  const n = Number(v)
  if (Number.isNaN(n)) return String(v)
  return n.toFixed(d)
}

function fmtTime(unix) {
  if (!unix) return '—'
  const d = new Date(unix * 1000)
  return d.toLocaleString('zh-CN', { hour12: false })
}

function phaseLabel(p) {
  if (!p) return '?'
  if (p === 'cognition') return '认知'
  if (p === 'experience') return '经历'
  return p
}

function phaseTagType(p) {
  if (p === 'cognition') return 'danger'
  if (p === 'experience') return 'primary'
  return 'info'
}

function stateTagType(s) {
  switch (s) {
    case 'active': return 'success'
    case 'replaced': return 'info'
    case 'archived': return 'info'
    case 'challenged': return 'warning'
    case 'reinforced': return 'success'
    case 'higher': return 'danger'
    case 'nascent': return 'warning'
    case 'revising': return 'warning'
    default: return 'info'
  }
}

function authColor(a) {
  if (a === null || a === undefined) return 'var(--text-muted)'
  if (a >= 0.8) return 'var(--neon-pink)'
  if (a >= 0.5) return 'var(--neon-cyan)'
  return 'var(--text-muted)'
}

function freshColor(f) {
  if (f === null || f === undefined) return '#909399'
  if (f > 0.5) return '#67c23a'
  if (f > 0.2) return '#e6a23c'
  return '#909399'
}

onMounted(() => loadChunks(1))
</script>

<style scoped>
.stats-panel {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
  padding: 12px 16px;
  margin-bottom: 12px;
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid var(--border-color, rgba(255, 255, 255, 0.08));
  border-radius: 8px;
}
.stat-cell { display: flex; flex-direction: column; gap: 6px; }
.stat-label { color: var(--text-muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }
.stat-value { font-size: 22px; font-weight: 600; color: var(--neon-cyan); }
.stat-value-sm { font-size: 13px; color: var(--text-secondary); line-height: 1.6; }

.detail-pane { padding: 0 8px; }
.detail-row { margin-bottom: 12px; font-size: 13px; }
.detail-key {
  display: inline-block;
  min-width: 100px;
  color: var(--text-muted);
  font-size: 11px;
  text-transform: uppercase;
}
.detail-block {
  background: rgba(255, 255, 255, 0.02);
  border: 1px solid var(--border-color, rgba(255, 255, 255, 0.08));
  border-radius: 6px;
  padding: 10px;
  margin-bottom: 12px;
}
.detail-block.warn {
  border-color: var(--neon-pink);
  background: rgba(245, 108, 108, 0.05);
}
.detail-block .detail-key {
  display: block;
  margin-bottom: 8px;
}
.detail-text {
  font-family: 'JetBrains Mono', 'SF Mono', monospace;
  font-size: 12px;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
  max-height: 300px;
  overflow-y: auto;
}
.detail-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(80px, 1fr));
  gap: 8px;
  margin-bottom: 12px;
}
.metric {
  background: rgba(255, 255, 255, 0.02);
  border-radius: 4px;
  padding: 6px;
  text-align: center;
}
.metric-label { font-size: 10px; color: var(--text-muted); }
.metric-value { font-size: 14px; font-weight: 500; font-family: 'JetBrains Mono', monospace; }
.metric-value.warn { color: var(--neon-pink); }
.chain-row { line-height: 1.8; }

:deep(.el-table__row) { cursor: pointer; }
</style>
