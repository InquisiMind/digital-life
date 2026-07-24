<template>
  <div v-loading="loading" style="min-height: 200px;">
    <section class="page-hero">
      <div>
        <h1 class="page-title">{{ instanceName }}</h1>
        <p class="page-subtitle">{{ instanceTagline }}</p>
      </div>
      <div style="display: flex; gap: 16px;">
        <div class="neon-card" style="padding: 8px 16px; min-width: 120px;">
          <div class="brand-sub">Energy</div>
          <div style="font-family: var(--font-display); font-size: 24px; color: var(--neon-cyan);">
            {{ Math.round(Number(energy) || 0) }}%
          </div>
          <!-- 加鸡腿 / 投喂能量按钮：nurture_energy 事件 -->
          <div style="display: flex; gap: 4px; margin-top: 6px;">
            <button class="nurture-btn" :disabled="nurturing" @click="nurture(30, '加了鸡腿🍗')" title="加鸡腿 +30">🍗</button>
            <button class="nurture-btn" :disabled="nurturing" @click="nurture(60, '投喂能量包⚡')" title="能量包 +60">⚡</button>
            <button class="nurture-btn" :disabled="nurturing" @click="nurture(100, '满血复活💯')" title="满血 +100">💯</button>
          </div>
          <div v-if="nurtureHint" class="brand-sub" style="font-size: 11px; margin-top: 4px; color: var(--neon-cyan);">
            {{ nurtureHint }}
          </div>
        </div>
        <div class="neon-card" style="padding: 8px 16px; min-width: 100px;">
          <div class="brand-sub">status</div>
          <div style="display: flex; align-items: center; gap: 6px; margin-top: 4px;">
            <span class="status-dot" :class="String(status || 'idle')"></span>
            <strong>{{ statusLabel }}</strong>
          </div>
        </div>
        <div class="neon-card" style="padding: 8px 16px; min-width: 100px;">
          <div class="brand-sub">mode</div>
          <div style="margin-top: 4px; font-family: var(--font-mono); font-size: 13px;"
               :style="{ color: isAbnormal ? 'var(--neon-red)' : 'var(--text-primary)' }">
            {{ modeLabel || (mode || '—') }}
          </div>
        </div>
      </div>
    </section>

    <!-- 异常 banner（仅当 status=error） -->
    <div v-if="isAbnormal" class="blocked-banner">
      <div>
        <strong>⚠ 数字生命异常</strong>
        <div class="brand-sub mono error-reason" v-if="healthReason"
             style="margin-top: 6px;">
          {{ healthReason }}
        </div>
        <div class="brand-sub" style="margin-top: 8px; color: var(--text-muted);">
          异常基于真实事件源判断：
          <code>turn.error</code>（模型调用失败）/ <code>flow_event severity=error</code>（事件流严重错误）。
          <br>
          自动恢复路径：下一次模型调用成功（任意无 error 的 role=assistant turn）自动恢复 ok；
          或者 reset 按钮仅 abort 卡住的 wake（不动 lifecycle RUNNING/BLOCKED，模型继续自治）。
        </div>
      </div>
      <el-button
        type="danger"
        :loading="resettingAffair"
        @click="resetAffair"
      >立即解卡 / reset</el-button>
    </div>

    <!-- Token 消耗趋势（明细拆分：主调用 / 摘要 / 429 频次） -->
    <div class="neon-card" style="margin-bottom: var(--space-5);">
      <h3 style="font-family: var(--font-display); color: var(--text-secondary); margin: 0 0 var(--space-3);">
        Token 消耗趋势
      </h3>
      <div style="margin-bottom: var(--space-3); display: flex; gap: 8px; align-items: center;">
        <button class="chart-tab" :class="{ active: tokenMode === 'hour' }" @click="switchTokenMode('hour')">今日 24h</button>
        <button class="chart-tab" :class="{ active: tokenMode === 'day' }" @click="switchTokenMode('day')">近 30 天</button>
        <span class="brand-sub" style="margin-left: auto; color: var(--text-muted);">
          {{ tokenMode === 'hour' ? '按小时聚合（北京时间，每天 00:00 重置）' : '按天聚合（每日累计 token）' }}
        </span>
      </div>
      <div ref="tokenChartEl" style="height: 240px;"></div>
      <!-- 今日 24h 模式下累计统计(紧跟图下) -->
      <div v-if="tokenMode === 'hour'" style="display: flex; gap: var(--space-4); margin-top: var(--space-3); flex-wrap: wrap;">
        <span class="brand-sub" style="color: var(--text-muted);">
          今日累计 <strong style="color: var(--neon-cyan, #00f0ff);">{{ Number(tokenSeries.day_total_used || 0).toLocaleString() }}</strong> tokens
        </span>
        <span class="brand-sub" style="color: var(--text-muted);">
          本时已用 <strong style="color: var(--neon-cyan, #00f0ff);">{{ Number(tokenSeries.hour_used || 0).toLocaleString() }}</strong>
        </span>
      </div>
      <!-- 近 30 天模式下: 30 天总量 + 日均 -->
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
    <div class="neon-card" style="margin-bottom: var(--space-5);">
      <h3 style="font-family: var(--font-display); color: var(--text-secondary); margin: 0 0 var(--space-4);">
        精力值波动
      </h3>
      <p class="brand-sub" style="color: var(--text-muted); margin-bottom: var(--space-3);">
        ⚡ 当前精力 {{ Math.round(Number(energy) || 0) }}% —— 折线为精力值变化（上涨=自然恢复，下跌=消耗）。
      </p>
      <div ref="energyChartEl" style="height: 240px;"></div>
    </div>

    <!-- 通道连接状态 -->
    <div class="neon-card" style="margin-bottom: var(--space-5);">
      <h3 style="font-family: var(--font-display); color: var(--text-secondary); margin: 0 0 var(--space-4);">
        通道连接状态
      </h3>
      <div v-if="Array.isArray(channels) && channels.length" class="channel-grid">
        <div
          v-for="ch in channels"
          :key="ch.platform"
          class="channel-card"
          :class="ch.status === 'connected' ? 'on' : 'off'"
        >
          <div class="ch-head">
            <span class="ch-label">{{ ch.label }}</span>
            <span class="ch-status">
              <span class="status-dot" :class="ch.status === 'connected' ? 'live' : 'idle'"></span>
              {{ ch.status === 'connected' ? '已连接' : '未配置' }}
            </span>
          </div>
          <div class="ch-identity mono" v-if="ch.identity">{{ ch.identity }}</div>
          <div class="ch-identity brand-sub" v-else style="color: var(--text-muted);">
            （未识别身份）
          </div>
          <!-- 微信专属：未连接时显示重扫按钮 -->
          <div v-if="ch.platform === 'wechat'" class="ch-actions">
            <el-button
              size="small"
              :loading="qrloading"
              @click="startWechatLogin"
            >
              {{ ch.status === 'connected' ? '重新扫码' : '扫码登录' }}
            </el-button>
            <span v-if="qrerror" class="brand-sub" style="color: var(--neon-red);">
              {{ qrerror }}
            </span>
            <span v-if="qrok" class="brand-sub" style="color: var(--neon-cyan);">
              ✓ {{ qrok }}
            </span>
            <span v-if="qrUrl && !qrok" class="brand-sub" style="color: var(--text-muted);">
              {{ qrStatus }}
            </span>
          </div>
          <!-- 飞书专属：未连接时提示去 Config 配 app_id/secret -->
          <div v-else-if="ch.status !== 'connected'" class="ch-actions">
            <el-button
              size="small"
              type="info"
              plain
              @click="router.push(`/instance/${iid}/config`)"
            >去配置 →</el-button>
          </div>
        </div>
      </div>
      <p v-else class="brand-sub" style="color: var(--text-muted);">
        尚未读取到通道信息。
      </p>
    </div>

    <!-- 三栏快照 -->
    <div class="neon-grid" style="grid-template-columns: repeat(3, 1fr);">
      <div class="neon-card">
        <h3>最近唤醒</h3>
        <template v-if="recentWakes.length">
          <div v-for="w in recentWakes" :key="w.id" class="entity-row wake-row" @click="goSessions">
            <span class="status-dot" :class="String(w.status) === 'running' ? 'live' : 'idle'"></span>
            <div style="flex: 1;">
              <div class="mono" style="font-size: 12px;">
                #{{ w.id }} · {{ wakeTriggerLabel(w) }}
              </div>
              <div class="brand-sub mono" style="color: var(--text-muted); font-size: 11px;">
                {{ safeFmtShort(w.started_at) }}
                · 耗时 {{ wakeDuration(w) }}
                <span v-if="w.message_count"> · {{ w.message_count }} 条</span>
              </div>
            </div>
            <span class="brand-sub" style="color: var(--neon-cyan);">→</span>
          </div>
        </template>
        <div v-else class="brand-sub" style="color: var(--text-muted);">无最近唤醒</div>
        <div style="margin-top: 8px;">
          <RouterLink to="sessions" class="brand-sub" style="font-size: 11px; color: var(--neon-cyan);">
            查看全部 wakes →
          </RouterLink>
        </div>
      </div>

      <div class="neon-card">
        <h3>待办 Top</h3>
        <template v-if="recentTodos.length">
          <div v-for="t in recentTodos" :key="t.id" class="entity-row">
            <span class="mono" style="color: var(--neon-pink);">●</span>
            <span>{{ String(t.title || '(无标题)') }}</span>
            <el-tag v-if="t.priority" size="small" :type="priorityTag(String(t.priority))">
              {{ t.priority }}
            </el-tag>
          </div>
        </template>
        <div v-else class="brand-sub" style="color: var(--text-muted);">无待办</div>
      </div>

      <div class="neon-card">
        <h3>记账 / budget</h3>
        <div v-if="budgetHour" class="entity-row">
          <span>Token / hour</span>
          <span class="mono" style="color: var(--neon-cyan);">
            {{ formatTokens(Number(budgetHour?.used) || 0) }} / {{ formatTokens(Number(budgetHour?.limit) || 0) }}
          </span>
        </div>
        <div v-if="budgetDay" class="entity-row">
          <span>Token / day</span>
          <span class="mono" style="color: var(--neon-cyan);">
            {{ formatTokens(Number(budgetDay?.used) || 0) }} / {{ formatTokens(Number(budgetDay?.limit) || 0) }}
          </span>
        </div>
        <div v-if="budgetEnergy" class="entity-row">
          <span>energy segment</span>
          <span class="mono">{{ String(budgetEnergy?.segment || '—') }}</span>
        </div>
        <div v-if="!budgetHour && !budgetDay && !budgetEnergy"
             class="brand-sub" style="color: var(--text-muted);">
          budget 数据未就绪
        </div>
      </div>
    </div>
    <!-- 微信扫码 Dialog（内嵌，避免 popup blocker + 安全） -->
    <el-dialog v-model="qrDialogVisible" title="微信扫码登录" width="360px" :close-on-click-modal="false">
      <div style="text-align: center; padding: 20px;">
        <img v-if="qrUrl"
             :src="`/api/system/instances/${iid}/wechat-login/qr-page?qrcode_url=${encodeURIComponent(qrUrl)}`"
             alt="微信二维码"
             style="width: 240px; height: 240px; border-radius: var(--radius); margin-bottom: 16px; background: white;" />
        <div v-else style="width: 240px; height: 240px; display: flex; align-items: center; justify-content: center; margin: 0 auto 16px; background: var(--bg-deep); border-radius: var(--radius);">
          <span class="brand-sub" style="color: var(--text-muted);">加载中…</span>
        </div>
        <p style="color: var(--text-secondary); font-size: 14px;">{{ qrStatus }}</p>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter, RouterLink } from 'vue-router'
import { ElMessage } from 'element-plus'
import { instanceApi, systemApi, safeFetch } from '@/api/client'
import { fmtTs } from '@/composables/useFormat'
import { createChart, disposeChart, NEON_PALETTE } from '@/composables/useEcharts'

const route = useRoute()
const router = useRouter()
const iid = computed(() => String(route.params.iid || ''))

const meta = ref({})
const energy = ref(0)
// 全页 loading: 加载完成前不渲染主结构, 避免出现 "0% energy / 无待办" 等假空状态闪烁
const loading = ref(true)
// nurture_energy（加鸡腿）按钮状态
const nurturing = ref(false)
const nurtureHint = ref('')
let nurtureHintTimer = null
const status = ref('idle')
const mode = ref('')
const modeLabel = ref('')
const affairStatus = ref('')
const affairGoal = ref('')
const healthReason = ref('')
const resettingAffair = ref(false)
const wakes = ref([])
const todos = ref([])
const budget = ref({})

// 图表：token 消耗趋势 + 精力值波动
const tokenChartEl = ref(null)
const energyChartEl = ref(null)
let tokenChartHandle = null
let energyChartHandle = null
// token 走势图模式：今日 24h(按小时聚合) / 近 30 天(按天聚合)
const tokenMode = ref('hour')
// 缓存两种 bucket 的数据, 切换时不重发请求
const tokenBucketCache = { hour: null, day: null }
const tokenSeries = ref({})
const vitalsSeries = ref({ samples: [], events: [] })
// 近 30 天 buckets 缓存的派生统计 (切到 day mode 时 loadCharts 写入 tokenBucketCache.day)
// 由 day mode buckets 计算 30天总量 / 近7天日均 / 有数据天数
const tokenMonthTotal = computed(() => {
  const buckets = tokenBucketCache.day || []
  return buckets.reduce((s, b) => s + (Number(b.input) || 0) + (Number(b.output) || 0) + (Number(b.total_summary) || 0), 0)
})
const tokenDayCount = computed(() => {
  // 至少有一次非零 token 使用的天数 (避免空天数干扰"日均")
  const buckets = tokenBucketCache.day || []
  return buckets.filter(b => (Number(b.input) || 0) + (Number(b.output) || 0) + (Number(b.total_summary) || 0) > 0).length
})
const tokenWeekAvg = computed(() => {
  // 取 day mode buckets 的最后 7 个(按 at_iso 升序), 求平均
  const buckets = (tokenBucketCache.day || []).slice().sort((a, b) => String(a.at_iso).localeCompare(String(b.at_iso)))
  const last7 = buckets.slice(-7)
  if (!last7.length) return 0
  const total = last7.reduce((s, b) => s + (Number(b.input) || 0) + (Number(b.output) || 0) + (Number(b.total_summary) || 0), 0)
  return Math.round(total / last7.length)
})
let chartTimer = null
// 通道状态（从 meta.channels 推出）
const channels = ref([])
const qrloading = ref(false)
const qrerror = ref('')
const qrok = ref('')
const qrUrl = ref('')   // 后端 qrcode_url（图片 src 用）
const qrStatus = ref('')  // 给用户看的提示文字
const qrDialogVisible = ref(false)
let poller = null

const instanceName = computed(() => {
  const m = meta.value || {}
  return String(m.display_name || '').trim() || String(iid.value || '').slice(0, 8) || '—'
})
const instanceTagline = computed(() => String((meta.value || {}).tagline || ''))

const recentWakes = computed(() => (Array.isArray(wakes.value) ? wakes.value : []).slice(0, 5))
const recentTodos = computed(() => (Array.isArray(todos.value) ? todos.value : []).slice(0, 5))

const budgetHour = computed(() => (budget.value && budget.value.hour) || null)
const budgetDay = computed(() => (budget.value && budget.value.day) || null)
const budgetEnergy = computed(() => (budget.value && budget.value.energy) || null)

const statusLabel = computed(() => {
  // 与后端 5 复合状态对齐：offline / error / resting / working / idle
  // BLOCKED affair 不算异常——它是数字生命主动 wait（休息中），由后端映射成 resting
  const map = {
    offline: '离线',
    error: '异常',
    resting: '休息中',
    working: '工作中',
    idle: '待命',
    // 向后兼容老字段
    blocked: '休息中',
    low_energy: '休息中',
    live: '工作中',
    down: '离线',
  }
  return map[String(status.value)] || String(status.value || '—')
})
// 只前端真异常（心跳死/wake 卡住）才显示 reset 按钮；resting 不显示
const isAbnormal = computed(() =>
  String(status.value).toLowerCase() === 'error'
)

function safeFmtShort(iso) {
  try {
    if (!iso) return '—'
    return fmtTs(String(iso)).slice(5, 16)
  } catch {
    return '—'
  }
}

function formatTokens(n) {
  if (!n) return '0'
  if (n > 1e6) return (n / 1e6).toFixed(1) + 'M'
  if (n > 1e3) return (n / 1e3).toFixed(1) + 'K'
  return String(n)
}
function priorityTag(p) {
  return { high: 'danger', medium: 'warning', low: 'info' }[p] || ''
}

function goSessions() {
  router.push(`/instance/${iid.value}/sessions`)
}

function wakeTriggerLabel(w) {
  // wake meta_json.trigger_type 字段 —— 从 /wakes 列表项里抽取
  const meta = w.meta_json || {}
  const type = typeof meta === 'string' ? (() => { try { return JSON.parse(meta) } catch { return {} } })() : meta
  const t = type.trigger_type || w.trigger_type || ''
  return ({
    message: '💬 消息',
    awaiting_reply: '⏰ 等回复',
    routine: '🕐 例行',
    timer: '⏲️ 定时',
    condition: '⚙️ 条件',
    initiative: '✨ 主动',
    external: '📨 外部',
    project_created: '📁 项目',
  })[String(t)] || (t ? `#${t}` : '—')
}

function wakeDuration(w) {
  const s = Number(w.started_at)
  const e = Number(w.ended_at)
  if (!s || !e || e < s) return '—'
  const sec = e - s
  if (sec < 60) return sec.toFixed(1) + 's'
  const m = Math.floor(sec / 60), r = Math.round(sec % 60)
  return `${m}m${r}s`
}

async function loadMeta() {
  try {
    const d = await systemApi.instances()
    if (d && !d.error) {
      const list = Array.isArray(d.instances) ? d.instances : []
      meta.value = (list.find(i => i && String(i.id) === String(iid.value)) || {})
      energy.value = Number(meta.value?.energy) || 0
      status.value = String(meta.value?.status || 'idle')
      healthReason.value = String(meta.value?.health_reason || '')
      // 同步通道状态：后端 instance.channels 是权威来源
      channels.value = Array.isArray(meta.value?.channels) ? meta.value.channels : []
    }
  } catch {}
}

// 微信扫码登录：内嵌 dialog 显示二维码（避免 popup blocker），后端 poll 完成自动关
async function startWechatLogin() {
  qrerror.value = ''
  qrok.value = ''
  qrUrl.value = ''
  qrStatus.value = '获取二维码…'
  qrloading.value = true
  qrDialogVisible.value = true
  try {
    const d = await systemApi.wechatQrcode(iid.value)
    if (d.error) {
      qrerror.value = d.error
      qrloading.value = false
      return
    }
    if (!d.qrcode_url) {
      qrerror.value = '后端未返回 qrcode_url'
      qrloading.value = false
      return
    }
    // 后端 /qr-page?qrcode_url=xxx 用 Python qrcode 把 ClawBot 链接渲染成 PNG
    qrUrl.value = d.qrcode_url
    qrStatus.value = '请用手机微信扫码'
    // 前端轮询扫码状态（3s 间隔，最多 100s）
    const deadline = Date.now() + 100000
    const iv = setInterval(async () => {
      if (Date.now() > deadline) {
        clearInterval(iv)
        qrloading.value = false
        qrerror.value = '扫码超时，请重新点击'
        qrUrl.value = ''
        return
      }
      try {
        const s = await systemApi.wechatLoginStatus(iid.value)
        // 后端 status 端点返 'ok' 或 'confirmed'，两种都算成功
        if (s && (s.status === 'ok' || s.status === 'confirmed')) {
          clearInterval(iv)
          qrloading.value = false
          qrDialogVisible.value = false
          qrok.value = `微信连接成功（bot_id=${s.bot_id ? s.bot_id.slice(0, 16) : '—'}…）`
          qrUrl.value = ''
          await loadMeta()
        }
      } catch {}
    }, 3000)
  } catch (e) {
    qrerror.value = String(e?.message || e)
    qrloading.value = false
  }
}

async function loadStatus() {
  try {
    const d = await instanceApi(iid.value).status()
    if (!d || d.error) return
    const e = (d.runtime && d.runtime.energy) ?? (d.vitals && d.vitals.energy)
    if (e != null) energy.value = Math.round(Number(e) || 0)
    const rt = d.runtime || {}
    mode.value = String(rt.mode || '')
    modeLabel.value = String(rt.mode_label || rt.mode || '')
    const aff = d.affair || {}
    affairStatus.value = String(aff.status || '')
    affairGoal.value = String(aff.goal || '')
  } catch {}
}

async function resetAffair() {
  if (resettingAffair.value) return
  resettingAffair.value = true
  try {
    // 默认只 abort stuck wakes，不动 lifecycle affairs.status（模型保留 BLOCKED 自决策）
    const d = await systemApi.resetAffair(iid.value, { abort_stuck_wakes: true })
    if (d && d.error) {
      ElMessage.error(`reset 失败：${d.error}`)
      return
    }
    ElMessage.success(`✓ abort ${d.wakes_aborted} 个卡住的 wake（不动 lifecycle RUNNING/BLOCKED）`)
    await loadStatus()
    await loadSummary()
  } finally {
    resettingAffair.value = false
  }
}

async function loadSummary() {
  try {
    const [w, t, b] = await Promise.all([
      instanceApi(iid.value).wakeSnapshot(),
      instanceApi(iid.value).todos(),
      instanceApi(iid.value).budget(),
    ])
    if (w && !w.error) wakes.value = Array.isArray(w.wakes) ? w.wakes : (Array.isArray(w.sessions) ? w.sessions : [])
    if (t && !t.error) todos.value = Array.isArray(t.todos) ? t.todos : []
    if (b && !b.error) budget.value = b || {}
  } catch {}
}

async function loadAll() {
  try {
    await Promise.all([loadMeta(), loadStatus(), loadSummary()])
  } finally {
    loading.value = false
  }
}

// ── 图表：拉 token 序列 + 精力序列，渲染两张图 ──
// 60s 拉一次（比 15s poller 慢，降低开销；曲线粒度本身是分钟/小时级）
async function loadCharts() {
  try {
    const tkHours = tokenMode.value === 'day' ? 720 : 24
    const tkBucket = tokenMode.value
    const [ts, vs] = await Promise.all([
      instanceApi(iid.value).budgetSeries(tkHours, tkBucket),
      instanceApi(iid.value).vitalsSeries(24),
    ])
    if (ts && !ts.error) {
      tokenSeries.value = ts
      tokenBucketCache[tokenMode.value] = ts.buckets || []
      renderTokenChart(ts.buckets || [])
    }
    if (vs && !vs.error) {
      vitalsSeries.value = vs
      renderEnergyChart(vs.samples || [])
    }
  } catch {}
}

// 切换 token 图模式: 今日 24h(按小时) / 近 30 天(按天)
function switchTokenMode(mode) {
  if (mode === tokenMode.value) return
  tokenMode.value = mode
  const cached = tokenBucketCache[mode]
  if (cached && cached.length) {
    renderTokenChart(cached)
  } else {
    loadCharts()
  }
}

function renderTokenChart(buckets) {
  if (!tokenChartEl.value || !buckets.length) return
  // hour 模式 label: HH:MM; day 模式 label: MM-DD
  const isDay = tokenMode.value === 'day'
  const labels = buckets.map(b => {
    const iso = b.at_iso || ''
    return isDay ? iso.slice(5, 10) : iso.slice(11, 16)
  })
  const mainInput = buckets.map(b => Number(b.input) || 0)
  const mainOutput = buckets.map(b => Number(b.output) || 0)
  const summaryTotal = buckets.map(b => (Number(b.total_summary) || 0))
  // day 模式累计 total 更直观(看每日总规模), hour 模式保持输入/输出分离看趋势
  const mainTotal = buckets.map(b => (Number(b.input) || 0) + (Number(b.output) || 0))
  // 取消 429 series (历史数据全是 0, 干扰判断)
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

function renderEnergyChart(samples) {
  if (!energyChartEl.value) return
  // 单一精力值折线：涨=自然恢复，跌=消耗。
  const lineData = samples.map(s => [Number(s.at_unix) * 1000, Number(s.energy).toFixed(1)]).filter(p => p[0])
  const option = {
    backgroundColor: 'transparent',
    color: [NEON_PALETTE[0]],
    grid: { top: 20, left: 40, right: 20, bottom: 30, containLabel: true },
    tooltip: { trigger: 'axis', backgroundColor: 'rgba(10,14,36,0.95)', borderColor: 'rgba(0,240,255,0.32)', textStyle: { color: '#e8ecff' }, valueFormatter: v => `${v}%` },
    xAxis: { type: 'time', axisLabel: { color: '#7a85ad' }, axisLine: { lineStyle: { color: '#2a3358' } } },
    yAxis: { type: 'value', name: '精力%', min: 0, max: 100, axisLabel: { color: '#7a85ad' }, splitLine: { lineStyle: { color: 'rgba(42,51,88,0.4)' } } },
    series: [
      { name: '精力值', type: 'line', smooth: true, showSymbol: false, data: lineData, areaStyle: { opacity: 0.12 } },
    ],
  }
  if (energyChartHandle) disposeChart(energyChartHandle)
  energyChartHandle = createChart(energyChartEl.value, option)
}

onMounted(() => {
  loadAll()
  loadCharts()  // 首次拉曲线
  poller = setInterval(() => { loadStatus(); loadSummary() }, 15000)
  chartTimer = setInterval(loadCharts, 60000)  // 曲线 60s 刷新一次
})

// 加鸡腿 / 能量包：调 /api/employee/<iid>/nurture-energy，注入 energize 事件。
// 后端返 {added, energy}（见 employee_console_routes.py:_handle_console_nurture_energy）
async function nurture(amount, label) {
  if (nurturing.value) return
  nurturing.value = true
  try {
    const d = await safeFetch(`/api/employee/${iid.value}/nurture-energy`, {
      method: 'POST',
      body: JSON.stringify({ amount, label }),
    })
    if (d.error) {
      ElMessage.error(d.error)
      return
    }
    nurtureHint.value = `${label} +${d.added} → ${d.energy}%`
    energy.value = Number(d.energy) || energy.value
    // 4s 后清提示
    if (nurtureHintTimer) clearTimeout(nurtureHintTimer)
    nurtureHintTimer = setTimeout(() => { nurtureHint.value = '' }, 4000)
    // 立即也强制 reload 一次 status，确保精力数值真同步（防止 nurture 后状态机 reset 改回去）
    loadStatus()
  } catch (e) {
    ElMessage.error(String(e?.message || e))
  } finally {
    nurturing.value = false
  }
}

onUnmounted(() => {
  if (poller) { clearInterval(poller); poller = null }
  if (chartTimer) { clearInterval(chartTimer); chartTimer = null }
  if (tokenChartHandle) { disposeChart(tokenChartHandle); tokenChartHandle = null }
  if (energyChartHandle) { disposeChart(energyChartHandle); energyChartHandle = null }
  if (nurtureHintTimer) { clearTimeout(nurtureHintTimer); nurtureHintTimer = null }
})
watch(iid, loadAll)
</script>

<style scoped>
.entity-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 0;
  border-bottom: 1px solid var(--border-divider);
  font-size: 13px;
}
.entity-row:last-child { border: none; }
h3 { margin: 0 0 var(--space-3); font-size: 14px; }
.blocked-banner {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--space-4);
  padding: var(--space-4) var(--space-5);
  margin-bottom: var(--space-5);
  background: rgba(255, 77, 106, 0.08);
  border: 1px solid rgba(255, 77, 106, 0.4);
  border-left: 4px solid var(--neon-red);
  border-radius: var(--radius);
  box-shadow: 0 0 24px rgba(255, 77, 106, 0.12);
}
.blocked-banner strong { color: var(--neon-red); font-family: var(--font-display); }
.error-reason {
  color: var(--neon-red);
  background: rgba(255,77,106,0.06);
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  font-size: 12px;
  border-left: 2px solid var(--neon-red);
}

/* 通道连接面板 */
.channel-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: var(--space-3);
}
.channel-card {
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius);
  border: 1px solid;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.channel-card.on {
  border-color: color-mix(in oklab, var(--neon-cyan) 50%, transparent);
  background: color-mix(in oklab, var(--neon-cyan) 8%, var(--bg-elevated));
  box-shadow: 0 0 16px color-mix(in oklab, var(--neon-cyan) 15%, transparent);
}
.channel-card.off {
  border-color: var(--border-divider);
  background: var(--bg-elevated);
  opacity: 0.85;
}
.channel-card .ch-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.channel-card .ch-label {
  font-family: var(--font-display);
  font-size: 14px;
  color: var(--text-primary);
}
.channel-card .ch-status {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: var(--text-muted);
}
.channel-card .ch-identity {
  font-size: 11px;
  color: var(--text-muted);
  word-break: break-all;
}
.channel-card .ch-actions {
  margin-top: 4px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

/* 加鸡腿 / 能量包按钮（Energy 卡） */
.nurture-btn {
  background: color-mix(in oklab, var(--neon-cyan) 8%, var(--bg-elevated));
  border: 1px solid color-mix(in oklab, var(--neon-cyan) 35%, transparent);
  border-radius: var(--radius-sm);
  padding: 2px 8px;
  font-size: 16px;
  line-height: 1.4;
  cursor: pointer;
  transition: all 0.15s ease;
}
.nurture-btn:hover:not(:disabled) {
  background: color-mix(in oklab, var(--neon-cyan) 18%, var(--bg-elevated));
  box-shadow: 0 0 8px color-mix(in oklab, var(--neon-cyan) 30%, transparent);
}
.nurture-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
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
