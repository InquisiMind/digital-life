<template>
  <div>
    <section class="page-hero">
      <div>
        <h1 class="page-title">System Overview</h1>
        <p class="page-subtitle">{{ subtitle || '所有实例运行状态聚合 · 数字生命的全貌' }}</p>
      </div>
      <div class="brand-sub mono">{{ nowTs }}</div>
    </section>

    <!-- 顶部 stats -->
    <div class="neon-grid" style="grid-template-columns: repeat(4, 1fr); margin-bottom: var(--space-5);">
      <div class="neon-card" v-for="stat in stats" :key="stat.label">
        <div class="brand-sub">{{ stat.label }}</div>
        <div style="font-family: var(--font-display); font-size: 28px; color: var(--neon-cyan); margin-top: 4px;">
          {{ stat.value }}
        </div>
        <div class="brand-sub" style="margin-top: 4px; color: var(--text-muted);">{{ stat.hint }}</div>
      </div>
    </div>

    <!-- 跨实例 token 消耗走势 (汇总所有实例) -->
    <div class="neon-card" style="margin-bottom: var(--space-5);">
      <h3 style="font-family: var(--font-display); color: var(--text-secondary); margin: 0 0 var(--space-3);">
        Token 消耗走势 · 汇总全部实例
      </h3>
      <div style="margin-bottom: var(--space-3); display: flex; gap: 8px; align-items: center;">
        <button class="chart-tab" :class="{ active: tokenMode === 'hour' }" @click="switchTokenMode('hour')">今日 24h</button>
        <button class="chart-tab" :class="{ active: tokenMode === 'day' }" @click="switchTokenMode('day')">近 30 天</button>
        <span class="brand-sub" style="margin-left: auto; color: var(--text-muted);">
          {{ tokenMode === 'hour' ? '按小时聚合（北京时间，每天 00:00 重置）· 跨实例累加' : '按天聚合（每日累计 token）· 跨实例累加' }}
        </span>
      </div>
      <div ref="tokenChartEl" style="height: 240px;"></div>
      <div v-if="tokenMode === 'hour'" style="display: flex; gap: var(--space-4); margin-top: var(--space-3); flex-wrap: wrap;">
        <span class="brand-sub" style="color: var(--text-muted);">
          今日累计 <strong style="color: var(--neon-cyan, #00f0ff);">{{ Number(tokenSeries.day_total_used || 0).toLocaleString() }}</strong> tokens
        </span>
        <span class="brand-sub" style="color: var(--text-muted);">
          本时已用 <strong style="color: var(--neon-cyan, #00f0ff);">{{ Number(tokenSeries.hour_used || 0).toLocaleString() }}</strong>
        </span>
      </div>
      <div v-else style="display: flex; gap: var(--space-4); margin-top: var(--space-3); flex-wrap: wrap;">
        <span class="brand-sub" style="color: var(--text-muted);">
          30 天累计 <strong style="color: var(--neon-cyan, #00f0ff);">{{ Number(tokenMonthTotal).toLocaleString() }}</strong> tokens
        </span>
        <span class="brand-sub" style="color: var(--text-muted);">
          日均 <strong style="color: var(--neon-cyan, #00f0ff);">{{ Number(Math.round(tokenMonthTotal / Math.max(1, tokenDayCount))).toLocaleString() }}</strong>
        </span>
        <span class="brand-sub" style="color: var(--text-muted);">
          近 7 天日均 <strong style="color: var(--neon-cyan, #00f0ff);">{{ Number(tokenWeekAvg).toLocaleString() }}</strong>
        </span>
      </div>
    </div>

    <!-- 双栏：实时实例 + 项目列表 -->
    <div class="neon-grid" style="grid-template-columns: 1.4fr 1fr; gap: var(--space-5);">
      <div>
        <h2 class="page-title" style="font-size: 18px; margin-bottom: var(--space-3);">Instances · 运行中</h2>
        <div class="neon-grid" style="grid-template-columns: repeat(2, 1fr);">
          <div
            v-for="inst in instances"
            :key="inst.id"
            class="neon-card accent-override instance-card"
            :style="{ '--instance-accent': inst.accent_color || '#00f0ff' }"
            @click="enter(inst.id)"
          >
            <div class="instance-header">
              <span class="avatar-glyph" :style="{ background: inst.accent_color || '#00f0ff' }">
                {{ (inst.display_name || '?').slice(0, 1).toUpperCase() }}
              </span>
              <div>
                <div class="display-name" :style="{ color: inst.accent_color || 'var(--neon-cyan)' }">
                  {{ inst.display_name }}
                </div>
                <div class="brand-sub">{{ inst.tagline || '—' }}</div>
              </div>
              <span class="status-dot" :class="inst.status"></span>
            </div>

            <div class="energy-bar">
              <div class="energy-fill" :style="{ width: inst.energy + '%', background: inst.accent_color || 'var(--neon-cyan)' }"></div>
              <span class="energy-label">{{ Math.round(inst.energy) }}% energy</span>
            </div>

            <div class="mono id-line">{{ safeSlice(inst.id, 0, 8) }}…</div>

            <!-- 离线/上线开关：stop 阻止冒泡到卡片本身的 enter 跳转 -->
            <div class="card-actions" @click.stop>
              <el-button
                size="small"
                :type="inst.active ? 'danger' : 'success'"
                plain
                :loading="toggling === inst.id"
                @click="toggleActive(inst)"
              >{{ inst.active ? '离线' : '上线' }}</el-button>
            </div>
          </div>
        </div>
      </div>

      <div>
        <h2 class="page-title" style="font-size: 18px; margin-bottom: var(--space-3);">Active Projects</h2>
        <div
          v-for="p in projects"
          :key="p.id"
          class="neon-card project-row"
        >
          <div style="display: flex; justify-content: space-between; align-items: center;">
            <strong>{{ p.name }}</strong>
            <el-tag size="small" effect="plain">{{ p.status }}</el-tag>
          </div>
          <div class="brand-sub" style="margin-top: 4px;">{{ p.description || '—' }}</div>
          <div class="tag-row" style="margin-top: 8px;">
            <el-tag v-for="pos in p.positions" :key="pos.id" size="small" type="info" effect="plain">
              {{ pos.name }} ({{ pos.assignees.length }})
            </el-tag>
          </div>
        </div>
        <div v-if="!projects.length" class="dev-placeholder">
          <span class="mono">No projects</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { systemApi } from '@/api/client'
import { fmtTs } from '@/composables/useFormat'
import { createChart, disposeChart, NEON_PALETTE } from '@/composables/useEcharts'
// template 用 safeSlice 通过 app.config.globalProperties 注入；script 内用 fmtTs import

const router = useRouter()
const subtitle = computed(() => '')

const instances = ref([])
const projects = ref([])
const toggling = ref('') // 正在切换 active 的实例 id
const refreshTimer = ref(null)
const now = ref(Date.now())
const nowTs = computed(() => fmtTs(new Date(now.value).toISOString()))

// ── 跨实例 token 走势图 (汇总) ──
const tokenChartEl = ref(null)
let tokenChartHandle = null
const tokenMode = ref('hour')
const tokenSeries = ref({})
const tokenBucketCache = { hour: null, day: null }

const tokenMonthTotal = computed(() => {
  const buckets = tokenBucketCache.day || []
  return buckets.reduce((s, b) => s + (Number(b.input) || 0) + (Number(b.output) || 0) + (Number(b.total_summary) || 0), 0)
})
const tokenDayCount = computed(() => {
  const buckets = tokenBucketCache.day || []
  return buckets.filter(b => (Number(b.input) || 0) + (Number(b.output) || 0) + (Number(b.total_summary) || 0) > 0).length
})
const tokenWeekAvg = computed(() => {
  const buckets = (tokenBucketCache.day || []).slice().sort((a, b) => String(a.at_iso).localeCompare(String(b.at_iso)))
  const last7 = buckets.slice(-7)
  if (!last7.length) return 0
  const total = last7.reduce((s, b) => s + (Number(b.input) || 0) + (Number(b.output) || 0) + (Number(b.total_summary) || 0), 0)
  return Math.round(total / last7.length)
})

async function loadChart() {
  try {
    const tkHours = tokenMode.value === 'day' ? 720 : 24
    const d = await systemApi.systemBudgetSeries(tkHours, tokenMode.value)
    if (d && !d.error) {
      tokenSeries.value = d
      tokenBucketCache[tokenMode.value] = d.buckets || []
      renderTokenChart(d.buckets || [])
    }
  } catch {}
}

function switchTokenMode(mode) {
  if (mode === tokenMode.value) return
  tokenMode.value = mode
  const cached = tokenBucketCache[mode]
  if (cached && cached.length) {
    renderTokenChart(cached)
  } else {
    loadChart()
  }
}

function renderTokenChart(buckets) {
  if (!tokenChartEl.value || !buckets.length) return
  const isDay = tokenMode.value === 'day'
  const labels = buckets.map(b => {
    const iso = b.at_iso || ''
    return isDay ? iso.slice(5, 10) : iso.slice(11, 16)
  })
  const mainInput = buckets.map(b => Number(b.input) || 0)
  const mainOutput = buckets.map(b => Number(b.output) || 0)
  const summaryTotal = buckets.map(b => Number(b.total_summary) || 0)
  const mainTotal = buckets.map(b => (Number(b.input) || 0) + (Number(b.output) || 0))
  const series = isDay ? [
    { name: '主用量', type: 'bar', stack: 'main', data: mainTotal, itemStyle: { color: NEON_PALETTE[0] } },
    { name: '摘要', type: 'bar', stack: 'main', data: summaryTotal, itemStyle: { color: NEON_PALETTE[4] } },
  ] : [
    { name: '主输入', type: 'line', smooth: true, data: mainInput },
    { name: '主输出', type: 'line', smooth: true, data: mainOutput },
    { name: '摘要', type: 'line', smooth: true, data: summaryTotal, lineStyle: { type: 'dashed' } },
  ]
  const legendData = isDay ? ['主用量', '摘要'] : ['主输入', '主输出', '摘要']
  const option = {
    backgroundColor: 'transparent',
    color: [NEON_PALETTE[0], NEON_PALETTE[1], NEON_PALETTE[4]],
    grid: { top: 30, left: 50, right: 50, bottom: 30, containLabel: true },
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'rgba(10,14,36,0.95)',
      borderColor: 'rgba(0,240,255,0.32)',
      textStyle: { color: '#e8ecff' },
      valueFormatter: v => Number(v).toLocaleString(),
    },
    legend: { data: legendData, textStyle: { color: '#9aa4cf' }, top: 0 },
    xAxis: { type: 'category', data: labels, axisLabel: { color: '#7a85ad' }, axisLine: { lineStyle: { color: '#2a3358' } } },
    yAxis: [
      { type: 'value', name: 'tokens', axisLabel: { color: '#7a85ad' }, splitLine: { lineStyle: { color: 'rgba(42,51,88,0.4)' } } },
    ],
    series,
  }
  if (tokenChartHandle) disposeChart(tokenChartHandle)
  tokenChartHandle = createChart(tokenChartEl.value, option)
}

const stats = computed(() => [
  { label: '总实例数', value: instances.value.length, hint: 'registered' },
  { label: '运行中', value: instances.value.filter(i => i.process_state === 'online').length, hint: 'process online' },
  { label: '健康实例', value: instances.value.filter(i => i.health_state === 'ok' && i.process_state === 'online').length, hint: 'health ok' },
  { label: '异常实例', value: instances.value.filter(i => i.health_state === 'error').length, hint: 'health error' },
  { label: '活跃项目', value: projects.value.length, hint: 'ongoing' },
])

function enter(iid) {
  router.push(`/instance/${iid}/overview`)
}

async function toggleActive(inst) {
  if (toggling.value) return
  const next = !inst.active
  const verb = next ? '上线' : '离线'
  try {
    await ElMessageBox.confirm(
      `${verb} 实例「${inst.display_name}」？\n\n`
      + (next
        ? '下次 master tick / gateway restart 后该实例子进程自动 spawn。'
        : '当前会停止 spawn；正在跑的会在自然生命周期结束。'),
      `确认${verb}`,
      { type: 'warning', confirmButtonText: verb, cancelButtonText: '取消' },
    )
  } catch { return }

  toggling.value = inst.id
  try {
    const d = await systemApi.setInstanceActive(inst.id, next, 'overview toggle')
    if (d.error) return ElMessage.error(`操作失败：${d.error}`)
    ElMessage.success(`✓ ${verb}）已记录；gateway 下次 tick / restart 生效`)
    await load()
  } finally {
    toggling.value = ''
  }
}

async function load() {
  const d = await systemApi.overview()
  if (d.error) return
  instances.value = d.instances || []
  projects.value = d.projects || []
}

onMounted(() => {
  load()
  loadChart()
  refreshTimer.value = setInterval(load, 10000)
  setInterval(() => { now.value = Date.now() }, 30000)
})

onUnmounted(() => {
  clearInterval(refreshTimer.value)
  if (tokenChartHandle) disposeChart(tokenChartHandle)
})
</script>

<style scoped>
.instance-card {
  cursor: pointer;
  transition: all 200ms;
}
.instance-card:hover {
  transform: translateY(-2px);
  box-shadow: var(--shadow-glow-cyan);
}

.instance-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: var(--space-3);
}
.avatar-glyph {
  width: 36px;
  height: 36px;
  border-radius: var(--radius);
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: var(--font-display);
  font-weight: 700;
  color: #050714;
  font-size: 18px;
  box-shadow: 0 0 12px currentColor;
}
.display-name {
  font-family: var(--font-display);
  letter-spacing: 0.06em;
  font-weight: 700;
}

.energy-bar {
  position: relative;
  height: 6px;
  background: var(--bg-elevated);
  border-radius: 3px;
  margin: var(--space-3) 0;
  overflow: hidden;
}
.energy-fill {
  height: 100%;
  border-radius: 3px;
  box-shadow: 0 0 8px currentColor;
  transition: width 800ms ease;
}
.energy-label {
  position: absolute;
  right: 0;
  top: -16px;
  font-size: 11px;
  color: var(--text-muted);
  font-family: var(--font-mono);
}

.id-line {
  font-size: 11px;
  color: var(--text-muted);
  opacity: 0.7;
}

/* 离线/上线开关容器：右对齐，与 id-line 保持间距 */
.card-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: var(--space-3);
}

.project-row {
  margin-bottom: var(--space-3);
  padding: var(--space-4);
}

/* token 走势图切换 tab */
.chart-tab {
  background: var(--bg-elevated);
  border: 1px solid var(--border-line);
  color: var(--text-secondary);
  padding: 4px 12px;
  border-radius: var(--radius);
  font-size: 12px;
  cursor: pointer;
  transition: all 120ms;
}
.chart-tab:hover { color: var(--text-primary); border-color: var(--neon-cyan); }
.chart-tab.active {
  background: color-mix(in oklab, var(--neon-cyan) 15%, var(--bg-elevated));
  color: var(--neon-cyan);
  border-color: var(--neon-cyan);
}
</style>
