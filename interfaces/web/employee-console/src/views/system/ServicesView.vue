<template>
  <div>
    <section class="page-hero">
      <div>
        <h1 class="page-title">Services · 服务管理</h1>
        <p class="page-subtitle">服务型 agent 定义与运行实例 · def 清单 → 服务实例 → 运行记录</p>
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
        style="cursor: pointer;"
        @click="selectDef(def.def_id)"
      >
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

    <!-- L3: 运行详情 drawer -->
    <el-drawer v-model="drawer.open" :title="dash(drawer.service && (drawer.service.display_name || drawer.service.service_id))" size="58%">
      <template v-if="drawer.service">
        <el-descriptions :column="2" border size="small" style="margin-bottom: var(--space-4);">
          <el-descriptions-item label="service_id">{{ drawer.service.service_id }}</el-descriptions-item>
          <el-descriptions-item label="agent def">{{ drawer.service.agent_def_id }}</el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag size="small" :type="statusType(drawer.service.status)" effect="plain">{{ drawer.service.status }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="项目">{{ drawer.service.project_name || drawer.service.project_id || '—' }}</el-descriptions-item>
          <el-descriptions-item label="订阅" :span="2">
            <el-tag v-for="s in (drawer.service.subscriptions || [])" :key="s" size="small" effect="plain" style="margin-right: 4px;">{{ s }}</el-tag>
            <span v-if="!(drawer.service.subscriptions || []).length">—</span>
          </el-descriptions-item>
          <el-descriptions-item label="工具" :span="2">
            <span class="mono" style="font-size: 11px;">{{ (drawer.service.tools && drawer.service.tools.length) ? drawer.service.tools.join(', ') : '—' }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="创建时间">{{ dash(drawer.service.created_at) }}</el-descriptions-item>
          <el-descriptions-item label="最近活动">{{ dash(drawer.service.last_wake_at) }}</el-descriptions-item>
        </el-descriptions>

        <h4 style="font-family: var(--font-display); color: var(--text-secondary); margin: 0 0 var(--space-2);">唤醒记录</h4>
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
      </template>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { adminApi } from '@/api/client'

const defs = ref([])
const services = ref([])
const selectedDef = ref('')
const drawer = reactive({ open: false, loading: false, service: null, wakes: [] })

const defCounts = computed(() => countBy('agent_def_id', () => true))
const defActive = computed(() => countBy('agent_def_id', s => s.status === 'active'))
const selectedDefName = computed(() => {
  const d = defs.value.find(x => x.def_id === selectedDef.value)
  return d ? d.display_name : selectedDef.value
})
const filteredServices = computed(() =>
  selectedDef.value ? services.value.filter(s => s.agent_def_id === selectedDef.value) : services.value
)

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

async function openDetail(row) {
  drawer.open = true
  drawer.loading = true
  drawer.service = row
  drawer.wakes = []
  const d = await adminApi.serviceDetail(row.service_id)
  drawer.loading = false
  if (d.error) return ElMessage.error(d.error)
  drawer.service = d.service || row
  drawer.wakes = d.wakes || []
}

onMounted(loadAll)
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
</style>
