<template>
  <div>
    <section class="page-hero">
      <div>
        <h1 class="page-title">Services · 服务管理</h1>
        <p class="page-subtitle">服务型 agent 定义与运行实例 · def 清单 → 服务实例 → 运行详情</p>
      </div>
      <el-button @click="loadAll"><el-icon><Refresh /></el-icon>刷新</el-button>
    </section>

    <!-- L1: agent def 清单 -->
    <div class="neon-grid" style="grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); margin-bottom: var(--space-5);">
      <div
        class="neon-card def-card"
        :class="{ selected: !selectedDef }"
        style="cursor: pointer;"
        @click="selectDef('')"
      >
        <div style="font-family: var(--font-display); color: var(--text-secondary);">全部定义</div>
        <div style="margin-top: 8px; display: flex; gap: 8px;">
          <el-tag size="small" effect="plain">{{ defs.length }} def</el-tag>
          <el-tag size="small" effect="plain">{{ services.length }} 实例</el-tag>
        </div>
      </div>
      <div
        v-for="def in defs"
        :key="def.def_id"
        class="neon-card def-card"
        :class="{ selected: selectedDef === def.def_id }"
        style="cursor: pointer; position: relative;"
        @click="selectDef(def.def_id)"
      >
        <el-button
          class="def-config-btn"
          link
          type="primary"
          size="small"
          @click.stop="openDefConfig(def)"
        >配置</el-button>
        <div style="font-family: var(--font-display); color: var(--neon-cyan);">{{ def.display_name }}</div>
        <div class="brand-sub mono" style="font-size: 11px; color: var(--text-muted);">{{ def.def_id }}</div>
        <div style="margin-top: 8px; display: flex; gap: 8px;">
          <el-tag size="small" effect="plain">实例 {{ defCounts[def.def_id] || 0 }}</el-tag>
          <el-tag size="small" :type="(defActive[def.def_id] || 0) > 0 ? 'success' : 'info'" effect="plain">
            活跃 {{ defActive[def.def_id] || 0 }}
          </el-tag>
        </div>
      </div>
    </div>

    <!-- L2: 服务实例列表 -->
    <div class="neon-card">
      <div style="display: flex; align-items: center; margin-bottom: var(--space-3);">
        <h3 style="font-family: var(--font-display); color: var(--text-secondary); margin: 0;">
          实例列表{{ selectedDef ? ' · ' + selectedDefName : ' · 全部' }}
        </h3>
        <span class="brand-sub" style="margin-left: auto; color: var(--text-muted);">点击行查看运行详情</span>
      </div>
      <el-table :data="filteredServices" size="small" @row-click="openDetail" style="cursor: pointer;">
        <el-table-column prop="service_id" label="service_id" min-width="160" />
        <el-table-column prop="role" label="角色" min-width="110" />
        <el-table-column prop="agent_def_id" label="def" min-width="170" />
        <el-table-column label="项目" min-width="120">
          <template #default="{ row }">{{ row.project_name || '—' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="92">
          <template #default="{ row }">
            <el-tag size="small" :type="statusType(row.status)" effect="plain">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="订阅" min-width="150">
          <template #default="{ row }">
            <el-tag v-for="s in (row.subscriptions || [])" :key="s" size="small" effect="plain" style="margin-right: 4px;">{{ s }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="wakes" label="唤醒" width="66" />
        <el-table-column label="最近活动" width="108">
          <template #default="{ row }">{{ row.last_wake_at || '—' }}</template>
        </el-table-column>
        <el-table-column prop="open_events" label="待处理" width="72" />
      </el-table>
      <div v-if="!filteredServices.length" class="dev-placeholder" style="margin-top: var(--space-3);">
        <span class="mono">// no services</span>
      </div>
    </div>

    <!-- L3: 运行详情 drawer（六页签） -->
    <el-drawer v-model="drawer.open" :title="dash(drawer.service && (drawer.service.display_name || drawer.service.service_id))" size="58%">
      <template v-if="drawer.service">
        <el-tabs v-model="drawer.tab" @tab-change="onTabChange">
          <!-- 概览 -->
          <el-tab-pane label="概览" name="overview">
            <div style="display: flex; gap: 10px; margin-bottom: var(--space-4);">
              <el-tag effect="plain">{{ drawer.service.wakes ?? 0 }} 次唤醒</el-tag>
              <el-tag effect="plain" :type="(drawer.service.open_events || 0) > 0 ? 'warning' : 'info'">
                {{ drawer.service.open_events ?? 0 }} 待处理
              </el-tag>
              <el-tag effect="plain">{{ dash(drawer.service.last_wake_at) }} 最近活动</el-tag>
            </div>
            <el-descriptions :column="2" border size="small">
              <el-descriptions-item label="service_id">{{ drawer.service.service_id }}</el-descriptions-item>
              <el-descriptions-item label="agent def">{{ drawer.service.agent_def_id }}</el-descriptions-item>
              <el-descriptions-item label="状态">
                <el-tag size="small" :type="statusType(drawer.service.status)" effect="plain">{{ drawer.service.status }}</el-tag>
              </el-descriptions-item>
              <el-descriptions-item label="项目">{{ drawer.service.project_name || drawer.service.project_id || '—' }}</el-descriptions-item>
              <el-descriptions-item label="创建时间">{{ dash(drawer.service.created_at) }}</el-descriptions-item>
              <el-descriptions-item label="最近活动">{{ dash(drawer.service.last_wake_at) }}</el-descriptions-item>
            </el-descriptions>
          </el-tab-pane>

          <!-- 配置 -->
          <el-tab-pane label="配置" name="config">
            <el-descriptions :column="1" border size="small">
              <el-descriptions-item label="显示名">{{ dash(drawer.service.display_name) }}</el-descriptions-item>
              <el-descriptions-item label="角色">{{ dash(drawer.service.role) }}</el-descriptions-item>
              <el-descriptions-item label="订阅">
                <el-tag v-for="s in (drawer.service.subscriptions || [])" :key="s" size="small" effect="plain" style="margin-right: 4px;">{{ s }}</el-tag>
                <span v-if="!(drawer.service.subscriptions || []).length">—</span>
              </el-descriptions-item>
              <el-descriptions-item label="能力 capabilities">
                <el-tag v-for="c in capabilitiesList" :key="c" size="small" effect="plain" style="margin-right: 4px;">{{ c }}</el-tag>
                <span v-if="!capabilitiesList.length">—</span>
              </el-descriptions-item>
              <el-descriptions-item label="工具 tools">
                <span class="mono" style="font-size: 11px;">{{ toolsList.length ? toolsList.join(', ') : '—' }}</span>
              </el-descriptions-item>
            </el-descriptions>
            <div class="brand-sub" style="margin-top: var(--space-3); color: var(--text-muted); font-size: 11px;">
              配置编辑接口引擎侧已就绪（PATCH），前端编辑控件后续开放 · def 级 yaml 配置看列表卡片「配置」
            </div>
          </el-tab-pane>

          <!-- 会话 -->
          <el-tab-pane label="会话" name="sessions">
            <template v-if="tabState.sessions.error">
              <div class="dev-placeholder"><span class="mono">// 会话数据接口对接中（引擎侧随部署包上线）</span></div>
            </template>
            <template v-else>
              <el-table :data="tabState.sessions.items" size="small" v-loading="tabState.sessions.loading">
                <el-table-column label="会话" min-width="170">
                  <template #default="{ row }">{{ row.title || row.id }}</template>
                </el-table-column>
                <el-table-column prop="source" label="来源" width="90" />
                <el-table-column prop="message_count" label="消息" width="66" />
                <el-table-column label="tokens" width="110">
                  <template #default="{ row }">{{ tokensCell(row) }}</template>
                </el-table-column>
                <el-table-column label="开始" width="150">
                  <template #default="{ row }">{{ fmtTime(row.started_at) }}</template>
                </el-table-column>
                <el-table-column label="结束原因" width="100">
                  <template #default="{ row }">{{ dash(row.end_reason) }}</template>
                </el-table-column>
              </el-table>
              <div v-if="!tabState.sessions.loading && !tabState.sessions.items.length" class="dev-placeholder">
                <span class="mono">// no sessions</span>
              </div>
            </template>
          </el-tab-pane>

          <!-- 待办 -->
          <el-tab-pane label="待办" name="todos">
            <template v-if="tabState.todos.error">
              <div class="dev-placeholder"><span class="mono">// 待办数据接口对接中（引擎侧随部署包上线）</span></div>
            </template>
            <template v-else>
              <el-table :data="tabState.todos.items" size="small" v-loading="tabState.todos.loading">
                <el-table-column prop="goal" label="目标" min-width="220" />
                <el-table-column label="状态" width="90">
                  <template #default="{ row }">
                    <el-tag size="small" effect="plain" :type="todoStatusType(row.status)">{{ dash(row.status) }}</el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="priority" label="优先级" width="72" />
                <el-table-column label="截止" width="130">
                  <template #default="{ row }">{{ dash(row.deadline) }}</template>
                </el-table-column>
                <el-table-column label="更新于" width="130">
                  <template #default="{ row }">{{ dash(row.updated_at) }}</template>
                </el-table-column>
              </el-table>
              <div v-if="!tabState.todos.loading && !tabState.todos.items.length" class="dev-placeholder">
                <span class="mono">// no affairs</span>
              </div>
            </template>
          </el-tab-pane>

          <!-- 日程 -->
          <el-tab-pane label="日程" name="schedules">
            <template v-if="tabState.schedules.error">
              <div class="dev-placeholder"><span class="mono">// 日程数据接口对接中（引擎侧随部署包上线）</span></div>
            </template>
            <template v-else>
              <el-table :data="tabState.schedules.items" size="small" v-loading="tabState.schedules.loading">
                <el-table-column label="类型" width="120">
                  <template #default="{ row }">{{ row.event_kind || row.kind }}</template>
                </el-table-column>
                <el-table-column prop="fire_at" label="触发时间" width="150" />
                <el-table-column label="已触发" width="80">
                  <template #default="{ row }">{{ (row.fired_at || row.fired) ? '✓' : '—' }}</template>
                </el-table-column>
                <el-table-column label="内容" min-width="200">
                  <template #default="{ row }">
                    <span class="mono" style="font-size: 11px;">{{ briefPayload(row.payload_json ?? row.label) }}</span>
                  </template>
                </el-table-column>
              </el-table>
              <div v-if="!tabState.schedules.loading && !tabState.schedules.items.length" class="dev-placeholder">
                <span class="mono">// no timers</span>
              </div>
            </template>
          </el-tab-pane>

          <!-- 唤醒 -->
          <el-tab-pane label="唤醒" name="wakes">
            <el-table :data="drawer.wakes" size="small" v-loading="drawer.loading">
              <el-table-column prop="seq" label="#" width="52" />
              <el-table-column prop="session_id" label="session" min-width="190" />
              <el-table-column label="原因" min-width="120">
                <template #default="{ row }">{{ dash(row.reason) }}</template>
              </el-table-column>
              <el-table-column label="开始" width="150">
                <template #default="{ row }">{{ dash(row.started_at) }}</template>
              </el-table-column>
              <el-table-column label="结束" width="150">
                <template #default="{ row }">{{ dash(row.ended_at) }}</template>
              </el-table-column>
              <el-table-column label="tokens" width="90">
                <template #default="{ row }">{{ row.tokens == null ? '—' : Number(row.tokens).toLocaleString() }}</template>
              </el-table-column>
            </el-table>
            <div v-if="!drawer.loading && !(drawer.wakes || []).length" class="dev-placeholder">
              <span class="mono">// no wake records</span>
            </div>
          </el-tab-pane>
        </el-tabs>
      </template>
    </el-drawer>

    <!-- def 配置 dialog -->
    <el-dialog
      v-model="defDlg.open"
      :title="defDlg.def ? ('配置 · ' + defDlg.def.display_name) : '配置'"
      width="680px"
    >
      <div v-loading="defDlg.loading" style="min-height: 120px;">
        <pre v-if="defDlg.config" class="mono def-config-pre">{{ JSON.stringify(defDlg.config, null, 2) }}</pre>
        <template v-if="defDlg.persona">
          <div style="font-size: 12px; color: #909399; margin: 12px 0 6px;">persona（人设 prompt）</div>
          <pre class="mono def-config-pre" style="max-height: 320px; overflow: auto;">{{ defDlg.persona }}</pre>
        </template>
        <div v-else-if="defDlg.error" class="dev-placeholder">
          <span class="mono">读取失败：{{ defDlg.error }}</span>
        </div>
        <div v-else class="dev-placeholder">
          <span class="mono">// def 配置加载中…</span>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { adminApi } from '@/api/client'

const defs = ref([])
const services = ref([])
const selectedDef = ref('')
const drawer = reactive({ open: false, loading: false, service: null, wakes: [], tab: 'overview' })
const tabState = reactive({
  sessions:  { loading: false, loaded: false, error: '', items: [] },
  todos:     { loading: false, loaded: false, error: '', items: [] },
  schedules: { loading: false, loaded: false, error: '', items: [] },
})
const defDlg = reactive({ open: false, def: null, loading: false, error: '', config: null, persona: '' })

const defCounts = computed(() => countBy('agent_def_id', () => true))
const defActive = computed(() => countBy('agent_def_id', s => s.status === 'active'))
const selectedDefName = computed(() => {
  const d = defs.value.find(x => x.def_id === selectedDef.value)
  return d ? d.display_name : selectedDef.value
})
const filteredServices = computed(() =>
  selectedDef.value ? services.value.filter(s => s.agent_def_id === selectedDef.value) : services.value
)
const capabilitiesList = computed(() => parseList(drawer.service && drawer.service.capabilities))
const toolsList = computed(() => parseList(drawer.service && drawer.service.tools))

function countBy(key, pred) {
  const m = {}
  for (const s of services.value) {
    if (!pred(s)) continue
    m[s[key]] = (m[s[key]] || 0) + 1
  }
  return m
}

function dash(v) {
  return v === null || v === undefined || v === '' ? '—' : v
}

function parseList(v) {
  if (Array.isArray(v)) return v
  if (typeof v === 'string' && v.trim()) {
    try {
      const p = JSON.parse(v)
      return Array.isArray(p) ? p : [v]
    } catch { return [v] }
  }
  return []
}

function fmtInt(v) {
  return v == null ? '—' : Number(v).toLocaleString()
}

function fmtTime(v) {
  if (v == null || v === '') return '—'
  if (typeof v === 'number') {
    const d = new Date(v * (v < 1e12 ? 1000 : 1))
    const p = n => String(n).padStart(2, '0')
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
  }
  return v
}

function tokensCell(row) {
  if (row.input_tokens != null) return `${fmtInt(row.input_tokens)} / ${fmtInt(row.output_tokens)}`
  return fmtInt(row.tokens)
}

function briefPayload(v) {
  if (!v) return '—'
  let s = v
  if (typeof v === 'object') s = JSON.stringify(v)
  s = String(s)
  return s.length > 48 ? s.slice(0, 48) + '…' : s
}

function todoStatusType(st) {
  if (st === 'open' || st === 'active') return 'warning'
  if (st === 'done' || st === 'completed') return 'success'
  return 'info'
}

function statusType(st) {
  if (st === 'active') return 'success'
  if (st === 'paused') return 'warning'
  return 'info' // archived / unknown
}

async function loadAll() {
  const [d, s] = await Promise.all([adminApi.defs(), adminApi.services()])
  if (d.error) return ElMessage.error(d.error)
  if (s.error) return ElMessage.error(s.error)
  defs.value = d.defs || []
  services.value = s.services || []
}

function selectDef(id) {
  selectedDef.value = id
}

function resetTabs() {
  for (const k of Object.keys(tabState)) {
    Object.assign(tabState[k], { loading: false, loaded: false, error: '', items: [] })
  }
}

const tabApiMap = {
  sessions: adminApi.serviceSessions,
  todos: adminApi.serviceTodos,
  schedules: adminApi.serviceSchedules,
}

async function loadTab(key) {
  const t = tabState[key]
  if (!t || t.loaded || t.loading) return
  const sid = drawer.service && drawer.service.service_id
  if (!sid) return
  t.loading = true
  t.error = ''
  const d = await tabApiMap[key](sid)
  t.loading = false
  if (d.error) { t.error = d.error; return } // 接口未上线 → 页签内 placeholder
  t.items = d.items || d[key] || []
  t.loaded = true
}

function onTabChange(name) {
  syncQuery()
  if (tabApiMap[name]) loadTab(name)
}

async function openDetail(row) {
  drawer.open = true
  drawer.loading = true
  drawer.service = row
  drawer.wakes = []
  drawer.tab = 'overview'
  resetTabs()
  const d = await adminApi.serviceDetail(row.service_id)
  drawer.loading = false
  if (d.error) return ElMessage.error(d.error)
  drawer.service = d.service || row
  drawer.wakes = d.wakes || []
  syncQuery()
}

async function openDefConfig(def) {
  defDlg.open = true
  defDlg.def = def
  defDlg.config = null
  defDlg.persona = ''
  defDlg.error = ''
  defDlg.loading = true
  const d = await adminApi.defConfig(def.def_id)
  defDlg.loading = false
  if (d.error) { defDlg.error = d.error; return } // 接口未上线 → placeholder
  defDlg.config = d.config !== undefined ? d.config : d
  defDlg.persona = d.persona || ''

}

const route = useRoute()
const router = useRouter()

function syncQuery() {
  const q = {}
  if (drawer.open && drawer.service) q.svc = drawer.service.service_id
  if (drawer.open && drawer.tab !== 'overview') q.tab = drawer.tab
  router.replace({ query: q }).catch(() => {})
}

onMounted(async () => {
  await loadAll()
  const q = route.query || {}
  if (q.svc) {
    const row = services.value.find(s => s.service_id === q.svc)
    if (row) {
      await openDetail(row)
      if (q.tab && q.tab !== 'overview') { drawer.tab = String(q.tab); onTabChange(drawer.tab) }
    }
  }
})
</script>

<style scoped>
.def-card {
  transition: all 200ms;
}
.def-card:hover {
  transform: translateY(-2px);
  box-shadow: var(--shadow-glow-cyan);
}
.def-card.selected {
  box-shadow: var(--shadow-glow-cyan);
  border-color: var(--neon-cyan);
}
.def-config-btn {
  position: absolute;
  top: 8px;
  right: 8px;
  z-index: 2;
}
.def-config-pre {
  margin: 0;
  padding: 12px;
  max-height: 420px;
  overflow: auto;
  font-size: 11px;
  line-height: 1.6;
  background: var(--bg-deep, #0a0e14);
  border: 1px solid var(--border-color, rgba(255,255,255,0.08));
  border-radius: 6px;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
