<template>
  <div>
    <section class="page-hero">
      <div>
        <h1 class="page-title">Sessions</h1>
        <p class="page-subtitle">数字生命的最近唤醒 · 每次唤醒 = 一轮自由行动</p>
      </div>
      <div style="display: flex; gap: 6px;">
        <el-button size="small" @click="expandAll">全部展开</el-button>
        <el-button size="small" @click="collapseAll">全部收起</el-button>
        <el-button @click="load"><el-icon><Refresh /></el-icon>刷新</el-button>
      </div>
    </section>

    <div class="session-layout">
      <!-- 左：wake 列表 -->
      <aside class="wake-list">
        <div class="brand-sub section-label">WAKES ({{ wakes.length }})</div>
        <div
          v-for="w in wakes"
          :key="w.id"
          class="wake-card"
          :class="{ active: selectedId === w.id, error: isWakeError(w) }"
          @click="selectWake(w.id)"
        >
          <div class="wake-card-head">
            <strong class="mono">#{{ shortId(w.id, 6) }}</strong>
            <span class="wake-card-tags">
              <span v-if="metaCallCount(w)" class="tag-callcount mono">🔄 {{ metaCallCount(w) }}</span>
              <span v-if="isWakeError(w)" class="tag-error mono">Error</span>
              <span class="brand-sub mono">{{ triggerLabel(metaTrigger(w)) }}</span>
            </span>
          </div>
          <div class="wake-meta">
            <span class="mono">{{ fmtEpochTime(w.started_at) }}</span>
            <span class="brand-sub mono" style="color: var(--text-muted);">
              · {{ fmtRelativeEpoch(w.started_at) }}
            </span>
          </div>
          <div class="wake-meta">
            <span class="brand-sub mono" style="color: var(--text-muted); font-size: 11px;">
              ⏱ {{ fmtDuration(w.started_at, w.ended_at) }}
              <span v-if="metaChat(w)"> · {{ safeSlice(metaChat(w), 0, 10) }}…</span>
            </span>
          </div>
        </div>
        <div v-if="!wakes.length" class="dev-placeholder"><span class="mono">暂无唤醒记录</span></div>

        <!-- 分页:加载更多 -->
        <div v-if="hasMore" style="text-align: center; padding: 8px 0;">
          <el-button size="small" plain :loading="loadingMore" @click="loadMore">
            加载更多 · 已加载 {{ wakes.length }} / {{ totalWakes }}
          </el-button>
        </div>
        <div v-else-if="wakes.length" class="brand-sub" style="font-size: 11px; color: var(--text-muted); text-align: center; padding: 8px 0;">
          共 {{ totalWakes }} 条已全部加载
        </div>
      </aside>

      <!-- 右：单 wake 详情 -->
      <main class="wake-detail">
        <div v-if="loadingDetail" class="dev-placeholder"><span class="mono">loading turns…</span></div>
        <div v-else-if="!selectedId" class="dev-placeholder">
          <strong>// SELECT A WAKE</strong>
          <span>左侧任选一个唤醒查看完整对话 + JSON</span>
        </div>
        <template v-else>
          <div class="detail-head" :class="{ 'detail-error-header': isCurrentWakeError }">
            <div>
              <h2 class="page-title" style="font-size: 18px;">
                Wake #{{ shortId(selectedId, 6) }}
                <span v-if="isCurrentWakeError" class="tag-error" style="margin-left: 8px;">Error</span>
              </h2>
              <div class="brand-sub mono">
                {{ triggerLabel(detailTrigger) }}
                · {{ fmtEpoch(detailStarted) }}
                · {{ fmtDuration(detailStarted, detailEnded) }}
                · {{ turns.length }} turns
                · 🔄 {{ detailCallCount }} calls
              </div>
            </div>
            <div class="tag-row">
              <el-button size="small" @click="openRawPayload" title="导出整 wake 完整 JSON">
                <el-icon><Document /></el-icon> Raw JSON
              </el-button>
            </div>
          </div>

          <!-- 错误详情(本 wake 任一 turn 有 error) -->
          <div v-if="detailError" class="wake-error-banner mono">
            ⚠️ <strong>本 wake 报错(非自然结束):</strong> {{ detailError }}
          </div>

          <!-- 初始注入(所有 call 之前) -->
          <template v-if="initialInjections.length">
            <div class="brand-sub section-label" style="margin-top: 4px;">CONTEXT INJECTIONS</div>
            <details
              v-for="inj in initialInjections"
              :key="'init-'+inj.id"
              class="injection-block"
              :class="injectionStyleClass(inj)"
              :open="!!injectionOpen[inj.id]"
              @toggle="injectionOpen[inj.id] = $event.target.open"
            >
              <summary>
                <span class="inj-status-dot" :class="injectionStyleClass(inj)"></span>
                <span class="inj-source">{{ inj.sys_tool || 'unknown' }}</span>
                <el-button size="small" text @click.stop="copyText(JSON.stringify(inj, null, 2))">copy raw</el-button>
              </summary>
              <div class="inj-content mono" v-html="renderMarkdown(inj.content)"></div>
            </details>
          </template>

          <!-- timeline: injections + calls 按实际时间排序 -->
          <div class="turns-stream">
            <template v-for="item in timeline" :key="item.type === 'call' ? 'call-'+item.callSeq : 'inj-'+item.inj.id">
              <!-- injection item -->
              <details
                v-if="item.type === 'injection'"
                class="injection-block injection-mid"
                :class="injectionStyleClass(item.inj)"
                :open="!!injectionOpen[item.inj.id]"
                @toggle="injectionOpen[item.inj.id] = $event.target.open"
                @click.stop
              >
                <summary>
                  <span class="inj-status-dot" :class="injectionStyleClass(item.inj)"></span>
                  <span class="inj-source">{{ item.inj.sys_tool || 'unknown' }}</span>
                  <el-button size="small" text @click.stop="copyText(JSON.stringify(item.inj, null, 2))">copy raw</el-button>
                </summary>
                <div class="inj-content mono" v-html="renderMarkdown(item.inj.content)"></div>
              </details>

              <!-- call item -->
              <div
                v-else
                class="call-group"
                :class="{ collapsed: !expandedCalls[item.callSeq] }"
                @click="toggleCall(item.callSeq)"
              >
              <div class="call-group-head">
                <span class="call-group-badge">🔄 Call #{{ item.callSeq }}</span>
                <span class="brand-sub mono call-group-summary">
                  {{ item.summary }}
                </span>
                <span v-if="item.tokenCount" class="brand-sub mono" style="margin-left: auto; color: var(--text-muted);">
                  {{ item.tokenCount }} tok
                </span>
                <el-icon class="expand-icon">
                  <ArrowDown v-if="!expandedCalls[item.callSeq]" />
                  <ArrowUp v-else />
                </el-icon>
              </div>

              <!-- 折叠时只显示 call head + one-line preview -->
              <div v-if="!expandedCalls[item.callSeq]" class="call-preview mono">
                {{ item.preview }}
              </div>

              <!-- 展开时:本 call 的所有 turn(input + output + tool result) -->
              <div v-else class="call-expanded-body" @click.stop>
                <div
                  v-for="turn in item.turns"
                  :key="turn.id"
                  class="turn"
                  :class="turnClass(turn)"
                >
                  <div class="turn-head">
                    <span class="status-dot" :class="turnDotClass(turn)"></span>
                    <strong class="turn-role">{{ roleLabel(turn.role) }}</strong>
                    <span v-if="turn.tool_name" class="tool-tag">{{ turn.tool_name }}</span>
                    <span class="brand-sub mono" style="margin-left: auto;">
                      {{ fmtEpochTime(turn.timestamp) }}
                    </span>
                  </div>

                  <div v-if="turn.reasoning" class="turn-reasoning mono">
                    <div class="block-label">💭 reasoning</div>
                    <details>
                      <summary class="brand-sub" style="cursor: pointer; font-size: 11px;">展开 ({{ String(turn.reasoning).length }} chars)</summary>
                      <div>{{ String(turn.reasoning) }}</div>
                    </details>
                  </div>

                  <div v-if="turn.content" class="turn-body">
                    <div class="block-label" v-if="turn.role === 'user'">⚡ EVENT PAYLOAD · 输入</div>
                    <div class="block-label" v-else-if="turn.role === 'assistant'">🤖 AI 响应 · 输出</div>
                    <div class="block-label" v-else-if="turn.role === 'tool'">⚙️ 工具结果</div>
                    <div v-html="renderMarkdown(turn.content)"></div>
                  </div>

                  <!-- tool_calls (assistant 发起) -->
                  <div v-if="Array.isArray(turn.tool_calls) && turn.tool_calls.length" class="tool-calls">
                    <div v-for="(tc, i) in turn.tool_calls" :key="i" class="tool-call-card" :class="toolCallClass(tc)">
                      <div class="block-label">{{ toolCallLabel(tc) }}</div>
                      <strong :style="{ color: toolCallColor(tc) }">{{ safeToolName(tc) }}</strong>
                      <pre class="mono tool-args">{{ safeToolArgs(tc) }}</pre>
                      <el-button size="small" text @click="copyText(safeToolArgs(tc))">copy args</el-button>
                    </div>
                  </div>

                  <div v-if="turn.error" class="turn-error mono">⚠ {{ turn.error }}</div>
                </div>

                <!-- 该 call 完整 LLM input JSON — 不在页面内渲染所有 messages, 直接下载/打开本地文件 -->
                <div class="llm-call-input">
                  <div class="block-label">
                    📦 LLM Call #{{ item.callSeq }} input ({{ callInputs[callKeyForSeq(item.callSeq)]?.length || '?' }} messages)
                  </div>
                  <div class="call-input-actions">
                    <el-button size="small" @click="downloadCallInput(item.callSeq)" title="下载完整 LLM 输入 JSON 文件(可用编辑器或新 tab 打开)">
                      <el-icon><Download /></el-icon> 打开本地 JSON
                    </el-button>
                    <el-button
                      v-if="!callInputs[callKeyForSeq(item.callSeq)]"
                      size="small" text
                      :loading="callLoading[callKeyForSeq(item.callSeq)]"
                      @click="loadCallInputForSeq(item.callSeq)"
                      title="在面板内分组加载各 message(压栈高,不推荐)"
                    >
                      或在面板内展开
                    </el-button>
                    <el-button
                      v-if="callInputs[callKeyForSeq(item.callSeq)]"
                      size="small" text
                      @click="copyText(JSON.stringify(callInputs[callKeyForSeq(item.callSeq)], null, 2))"
                    >
                      copy 全部
                    </el-button>
                  </div>
                  <!-- 内联展开的 messages 列表(手动点开"在面板内展开"后才有, 默认隐藏) -->
                  <div v-if="callInputs[callKeyForSeq(item.callSeq)]" class="call-input-list">
                    <details v-for="(m, mi) in callInputs[callKeyForSeq(item.callSeq)]" :key="mi" class="call-input-msg">
                      <summary>
                        <span class="msg-role" :class="'role-' + m.role">{{ m.role }}</span>
                        <span class="brand-sub mono">{{ safeSlice(typeof m.content === 'string' ? m.content : JSON.stringify(m.content), 0, 80) }}</span>
                      </summary>
                      <pre class="mono msg-json">{{ JSON.stringify(m, null, 2) }}</pre>
                    </details>
                  </div>
                </div>
              </div>
            </div>
            </template>
          </div>
        </template>
      </main>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter, RouterLink } from 'vue-router'
import { Refresh, ArrowDown, ArrowUp, View, Document, Download } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { instanceApi } from '@/api/client'
import {
  fmtEpoch, fmtEpochTime, fmtDuration, fmtRelative, triggerLabel,
  safeSlice, shortId,
} from '@/composables/useFormat'
import { renderMarkdown } from '@/composables/useMarkdown'

const route = useRoute()
const router = useRouter()
const iid = computed(() => String(route.params.iid || ''))

const wakes = ref([])
const selectedId = ref(null)
const loadingDetail = ref(false)
const turns = ref([])
const injections = ref([])
const wakeMeta = ref(null)
const expandedTurns = reactive({})  // {turnId: bool}
const expandedCalls = reactive({})  // {callSeq: bool} — 按 LLM call 折叠, 默认全展开
const injectionOpen = reactive({})  // {injId: bool} injection 块独立折叠(Vue state 控制, 让 collapseAll 能管到)
const callInputs = reactive({})      // {callKey: messages[]}
const callInputModels = reactive({})
const callLoading = reactive({})
let wakeStream = null                // EventSource — 连接到 /wakes/{id}/stream

// 分页
const PAGE_SIZE = 30
const totalWakes = ref(0)
const hasMore = computed(() => wakes.value.length < totalWakes.value)
const loadingMore = ref(false)

// 帮助方法：trigger / chat 从 meta_json 提取
function metaTrigger(w) {
  const meta = w && w.meta_json
  if (typeof meta === 'string') { try { return JSON.parse(meta || '{}').trigger_type } catch { return '' } }
  return meta && meta.trigger_type
}
function metaChat(w) {
  const meta = w && w.meta_json
  if (typeof meta === 'string') { try { return JSON.parse(meta || '{}').trigger_chat_id } catch { return '' } }
  return meta && meta.trigger_chat_id
}
function metaCallCount(w) {
  const meta = w && w.meta_json
  let m = meta
  if (typeof m === 'string') { try { m = JSON.parse(m || '{}') } catch { return 0 } }
  if (!m) return 0
  return Number(m.llm_call_count) || 0
}
function isWakeError(w) {
  let meta = w && w.meta_json
  if (typeof meta === 'string') { try { meta = JSON.parse(meta || '{}') } catch { return false } }
  return meta && meta.end_reason === 'error'
}
// 找本 wake 里第一条有 error 字段的 turn, 用于详情头展示详情
const detailError = computed(() => {
  if (!Array.isArray(turns.value)) return ''
  for (const t of turns.value) {
    if (t && t.error) return String(t.error)
  }
  return ''
})
const detailCallCount = computed(() => metaCallCount(wakeMeta.value) || turns.value.length || 0)
const isCurrentWakeError = computed(() => isWakeError(wakeMeta.value))

const detailTrigger = computed(() => metaTrigger(wakeMeta.value) || '—')
const detailStarted = computed(() => wakeMeta.value?.started_at)
const detailEnded = computed(() => wakeMeta.value?.ended_at)

// 相对时间 helper（epoch 版）
function fmtRelativeEpoch(ep) {
  if (ep == null) return '—'
  try { return fmtRelative(new Date(Number(ep) * 1000).toISOString()) }
  catch { return '—' }
}

async function load() {
  const d = await instanceApi(iid.value).wakeSnapshot(PAGE_SIZE, 0)
  if (!d.error) {
    wakes.value = Array.isArray(d.wakes) ? d.wakes : []
    totalWakes.value = Number(d.total) || wakes.value.length
    // 自动选:route query.wake_id(数字) 优先,其次第一个
    const qSid = route.query.wake_id || route.query.sid
    const initial = (qSid && wakes.value.find(w => String(w.id) === String(qSid)))
      || wakes.value[0]
    if (initial) {
      await selectWake(initial.id)
    }
  }
}

// 跨 tab 跳过来时:组件已 mount 但 route.query 变了 —— 监听重选 wake
watch(
  () => route.query.wake_id || route.query.sid,
  (newSid) => {
    if (!newSid) return
    const found = wakes.value.find(w => String(w.id) === String(newSid))
    if (found && found.id !== selectedId.value) {
      selectWake(found.id)
    }
  }
)

async function loadMore() {
  if (!hasMore.value || loadingMore.value) return
  loadingMore.value = true
  try {
    const d = await instanceApi(iid.value).wakeSnapshot(PAGE_SIZE, wakes.value.length)
    if (!d.error) {
      const more = Array.isArray(d.wakes) ? d.wakes : []
      // 去重(理论上 offset 模式不会重复,防御 merge)
      const seen = new Set(wakes.value.map(w => w.id))
      for (const w of more) {
        if (!seen.has(w.id)) wakes.value.push(w)
      }
      totalWakes.value = Number(d.total) || totalWakes.value
    }
  } finally { loadingMore.value = false }
}

async function selectWake(wakeId) {
  selectedId.value = wakeId
  // 切换 wake 前:关掉老的 SSE
  closeWakeStream()
  loadingDetail.value = true
  turns.value = []
  injections.value = []
  wakeMeta.value = null
  // 清理 call cache + expand state
  Object.keys(callInputs).forEach(k => delete callInputs[k])
  Object.keys(callInputModels).forEach(k => delete callInputModels[k])
  Object.keys(expandedTurns).forEach(k => delete expandedTurns[k])
  Object.keys(expandedCalls).forEach(k => delete expandedCalls[k])
  Object.keys(injectionOpen).forEach(k => delete injectionOpen[k])

  try {
    const d = await instanceApi(iid.value).wakeDetail(wakeId)
    if (d && !d.error) {
      wakeMeta.value = d.wake || null
      turns.value = Array.isArray(d.turns) ? d.turns : []
      injections.value = Array.isArray(d.injections) ? d.injections : []
      // 默认:所有 call 展开
      // 默认所有 call 折叠, 让用户根据按钮提示主动点开
      for (const g of groupedTurns.value) {
        expandedCalls[g.callSeq] = false
      }
      // init injection 按 isInjectionDefaultOpen 设默认值
      for (const inj of injections.value) {
        injectionOpen[inj.id] = isInjectionDefaultOpen(inj)
      }
    }
  } finally {
    loadingDetail.value = false
    // ⚠️ SSE 暂时禁用 — 之前导致页面疯狂抖动
    // (根因待定位:snapshot/x-polling 引起的 v-for 重渲还是 SSE error 重连)
    // 改用手动 刷新按钮,等定位后再开。
    // if (selectedId.value && wakeMeta.value && !wakeMeta.value.ended_at) {
    //   startWakeStream(selectedId.value)
    // }
  }
}

// 把 injections + turns 合并成一条时间线: 按 timestamp 排序。
// injection 作为独立的 timeline item(类似 call 但 sys_tool 标记),
// 它出现在哪个 call 之间由 injected_at 跟 turns 的 timestamp 决定。
// 这样不需要 injected_before_call 硬匹配, 完全按实际顺序渲染。

const timeline = computed(() => {
  if (!Array.isArray(turns.value) || !turns.value.length) return []
  // 1. 按 callSeq 聚合 turns (跟之前 groupedTurns 一样)
  const callMap = new Map()
  for (const t of turns.value) {
    const seq = t.llm_call_seq != null ? Number(t.llm_call_seq) : null
    if (seq == null) continue
    if (!callMap.has(seq)) {
      callMap.set(seq, {
        type: 'call',
        callSeq: seq,
        timestamp: Number(t.timestamp) || 0,
        turns: [], tokenCount: 0, summary: '', preview: '',
      })
    }
    const g = callMap.get(seq)
    g.turns.push(t)
    if (t.token_count) g.tokenCount += Number(t.token_count) || 0
    // call 的 timestamp = 第一条 turn 的时间
    const ts = Number(t.timestamp) || 0
    if (g.timestamp === 0 || ts < g.timestamp) g.timestamp = ts
  }

  // 给 call 算 summary + preview
  for (const g of callMap.values()) {
    const asstTurns = g.turns.filter(t => t.role === 'assistant')
    const toolNames = []
    for (const at of asstTurns) {
      if (Array.isArray(at.tool_calls)) {
        for (const tc of at.tool_calls) {
          const n = safeToolName(tc)
          if (n) toolNames.push(n)
        }
      }
    }
    const userTurn = g.turns.find(t => t.role === 'user')
    const userHead = userTurn ? String(userTurn.content || '').split('\n')[0].slice(0, 60) : ''
    if (toolNames.length) {
      g.summary = `${asstTurns.length} 个决策 → ${toolNames.join(', ')}`
    } else if (userHead) {
      g.summary = `事件: ${userHead}${userHead.length >= 60 ? '…' : ''}`
    } else {
      g.summary = `${g.turns.length} turns`
    }
    g.preview = userHead ? userHead.slice(0, 100) : (asstTurns[0]?.content || '').slice(0, 100) || ''
  }

  // 2. injections 去重: agent 的 _convert_user_to_tool 会替换旧版本, 同一 sys_tool 只保留最新。
  // audit DB 两条都存了, 前端只展示最新的(=injected_at 最大的)那条。
  const injMap = new Map()
  for (const inj of (injections.value || [])) {
    const k = inj.sys_tool || 'unknown'
    const ts = Number(inj.injected_at) || 0
    const prev = injMap.get(k)
    if (!prev || ts > (Number(prev.injected_at) || 0)) {
      injMap.set(k, inj)
    }
  }
  const injItems = Array.from(injMap.values()).map(inj => ({
    type: 'injection',
    timestamp: Number(inj.injected_at) || 0,
    inj,
  }))

  // 3. 合并 + 按 timestamp 排序
  // 注意: 初始注入(injected 在第一个 call 之前)在顶部单独渲染,
  // timeline 里只放 mid-session 注入(在第一个 call 之后的)
  const calls = Array.from(callMap.values())
  const firstCallTs = calls.length ? Math.min(...calls.map(c => c.timestamp || 0)) : 0
  const midInjItems = injItems.filter(item => item.timestamp > firstCallTs)
  const all = [...calls, ...midInjItems]
  all.sort((a, b) => {
    const ta = a.timestamp || 0
    const tb = b.timestamp || 0
    if (ta !== tb) return ta - tb
    // 同一时刻: injection 放前面(注入在模型看到之前)
    if (a.type === 'injection' && b.type === 'call') return -1
    if (a.type === 'call' && b.type === 'injection') return 1
    return 0
  })

  return all
})

// 兼容旧引用(groupedTurns 保留, 从 timeline 提取 call 类型)
const groupedTurns = computed(() => timeline.value.filter(t => t.type === 'call'))

// 初始注入(injected 在第一个 call 之前) — 从 timeline 去重后的 injections 取
const initialInjections = computed(() => {
  const calls = groupedTurns.value
  const firstCallTs = calls.length ? (calls[0].timestamp || 0) : 0
  return timeline.value
    .filter(t => t.type === 'injection' && t.timestamp <= firstCallTs)
    .map(t => t.inj)
})

// 是否有 mid-session 注入
const midSessionInjectionCount = computed(() => {
  const init = initialInjections.value.length
  return (injections.value || []).length - init
})

function isInjectionDefaultOpen(inj) {
  // 所有注入默认折叠, 用户主动点开看
  return false
}
// injection 全部同色 — 对用户来说都是"系统背景注入", 没有谁比谁特殊。
// 真正需要高亮的是 turn 区里的工具调用(发消息/rest 等), 那里有独立配色。
function injectionStyleClass(inj) {
  return 'inj-default'
}

function toggleCall(callSeq) {
  expandedCalls[callSeq] = !expandedCalls[callSeq]
}
function toggleTurn(turn) {
  expandedTurns[turn.id] = !expandedTurns[turn.id]
}
function expandAll() {
  for (const g of groupedTurns.value) expandedCalls[g.callSeq] = true
  for (const inj of injections.value) injectionOpen[inj.id] = true
}
function collapseAll() {
  for (const g of groupedTurns.value) expandedCalls[g.callSeq] = false
  for (const inj of injections.value) injectionOpen[inj.id] = false
}

function callKeyForSeq(callSeq) {
  return `${selectedId.value}:${callSeq}`
}
async function loadCallInputForSeq(callSeq) {
  const key = callKeyForSeq(callSeq)
  callLoading[key] = true
  try {
    const d = await instanceApi(iid.value).wakeCallInput(selectedId.value, callSeq)
    if (d && !d.error) {
      callInputs[key] = Array.isArray(d.messages) ? d.messages : []
      callInputModels[key] = d.model || '—'
    } else if (d && d.error) {
      ElMessage.error(d.error)
    }
  } finally {
    callLoading[key] = false
  }
}
async function downloadCallInput(callSeq) {
  // 先把 call input 拉到内存(已有 cache 时直接用), 然后浏览器下载
  const key = callKeyForSeq(callSeq)
  let msgs = callInputs[key]
  if (!msgs) {
    await loadCallInputForSeq(callSeq)
    msgs = callInputs[key]
  }
  if (!msgs || !Array.isArray(msgs)) {
    ElMessage.warning('未拉取到该 call 的 input data')
    return
  }
  const dump = JSON.stringify({
    wake_id: selectedId.value,
    call_seq: callSeq,
    messages: msgs,
    model: callInputModels[key] || null,
  }, null, 2)
  const blob = new Blob([dump], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `wake-${selectedId.value}__call-${callSeq}__input.json`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

function callKey(turn) {
  return `${turn.wake_id || selectedId.value}:${turn.llm_call_seq}`
}

async function loadCallInput(turn) {
  const key = callKey(turn)
  callLoading[key] = true
  try {
    const d = await instanceApi(iid.value).wakeCallInput(turn.wake_id || selectedId.value, turn.llm_call_seq)
    if (d && !d.error) {
      callInputs[key] = Array.isArray(d.messages) ? d.messages : []
      callInputModels[key] = d.model || '—'
    } else if (d && d.error) {
      ElMessage.error(d.error)
    }
  } finally {
    callLoading[key] = false
  }
}

function openRawPayload() {
  const dump = JSON.stringify({
    wake: wakeMeta.value,
    turns: turns.value,
    injections: injections.value,
  }, null, 2)
  // 直接 download
  const blob = new Blob([dump], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `wake-${selectedId.value}.json`
  a.click()
  URL.revokeObjectURL(url)
}

function copyText(text) {
  navigator.clipboard.writeText(String(text)).then(
    () => ElMessage.success('已复制'),
    () => ElMessage.warning('复制失败，请手动选中'),
  )
}

function previewText(turn) {
  const c = turn.content || turn.reasoning || turn.error || ''
  const s = String(c).replace(/\s+/g, ' ').trim()
  return safeSlice(s, 0, 100) + (s.length > 100 ? '…' : '')
}

function turnClass(turn) {
  const r = String(turn.role || '')
  return {
    user: 'role-user',
    assistant: 'role-assistant',
    tool: 'role-tool',
    system: 'role-system',
  }[r] || ''
}
function turnDotClass(turn) {
  if (turn.error) return 'down'
  const r = String(turn.role || '')
  if (r === 'assistant') return 'live'
  return 'idle'
}
function roleLabel(role) {
  return {
    user: '⚡ EVENT · 触发',
    assistant: '🤖 AI · 决策',
    tool: '⚙️ TOOL · 结果',
    system: '📋 SYSTEM',
  }[String(role)] || String(role || '—')
}
function safeToolName(tc) {
  return safeSlice((tc && (tc.function?.name || tc.name)) || 'tool', 0, 40)
}
function safeToolArgs(tc) {
  let args = (tc && (tc.function?.arguments || tc.arguments)) || ''
  if (typeof args !== 'string') {
    try { args = JSON.stringify(args, null, 2) } catch { args = String(args) }
  }
  return String(args)
}

// tool_call 按工具名精确分类标识, 关键动作高亮:
// 📤 橙 = express_to_human (模型发消息给人类)
// 😴 黄 = rest (模型决定休息)
// ⚙ 默认灰 = 其他工具
function toolCallClass(tc) {
  const n = safeToolName(tc)
  if (n === 'express_to_human') return 'tc-msg'
  if (n === 'rest') return 'tc-rest'
  return ''
}
function toolCallLabel(tc) {
  const n = safeToolName(tc)
  if (n === 'express_to_human') return '📤 发送消息'
  if (n === 'rest') return '😴 进入休息'
  return '⚙ tool_call'
}
function toolCallColor(tc) {
  const n = safeToolName(tc)
  if (n === 'express_to_human') return '#ff9944'
  if (n === 'rest') return '#ffb300'
  return 'var(--neon-pink)'  // 默认
}

function startWakeStream(wakeId) {
  closeWakeStream()
  if (!wakeId) return
  try {
    const url = `/api/employee/${iid.value}/wakes/${wakeId}/stream`
    wakeStream = new EventSource(url)
    let snapshotReceived = false  // 防止 snapshot 重复刷新
    wakeStream.addEventListener('snapshot', (e) => {
      // snapshot 只接收一次 = 初始化, 之后只增量接 turn 事件, 避免页面跳动
      if (snapshotReceived) return
      snapshotReceived = true
      try {
        const d = JSON.parse(e.data)
        if (d.wake) wakeMeta.value = d.wake
        if (Array.isArray(d.turns)) {
          // 直接设, 不要每次 sort(SSE 收到的 turns 已经是按 id 排序的)
          turns.value = d.turns
        }
        if (Array.isArray(d.injections)) injections.value = d.injections
        // 默认所有 call 折叠, 用户主动点开
        for (const g of groupedTurns.value) {
          expandedCalls[g.callSeq] = false
        }
        for (const inj of injections.value) {
          injectionOpen[inj.id] = isInjectionDefaultOpen(inj)
        }
      } catch (err) { /* ignore parse err */ }
    })
    wakeStream.addEventListener('turn', (e) => {
      try {
        const d = JSON.parse(e.data)
        if (d.turn) appendTurn(d.turn)
      } catch (err) { /* ignore */ }
    })
    wakeStream.addEventListener('end', (e) => {
      try {
        const d = JSON.parse(e.data)
        if (d.reason) {
          ElMessage.info(`本轮 wake 已结束 (${d.reason})`)
        }
      } catch (err) { /* ignore */ }
      closeWakeStream()
    })
    wakeStream.addEventListener('error', () => {
      // EventSource 自带重连, 通常网络抖动; 不主动 close
    })
  } catch (err) {
    console.warn('startWakeStream failed', err)
  }
}

function appendTurn(turn) {
  // 增量追加一条 turn, 不动现有顺序; 去重 by id
  if (!turn || !turn.id) return
  const exists = turns.value.find(t => t.id === turn.id)
  if (exists) return  // 已有 → 忽略, 不动
  // 追加在末尾(假设后端按 id 顺序推)
  turns.value.push(turn)
  // 自动展开它所在的 call
  const seq = Number(turn.llm_call_seq)
  if (seq != null && !isNaN(seq)) expandedCalls[seq] = true
}

function closeWakeStream() {
  if (wakeStream) {
    try { wakeStream.close() } catch (e) { /* ignore */ }
    wakeStream = null
  }
}

onMounted(load)
onUnmounted(() => {
  closeWakeStream()
})
</script>

<style scoped>
.session-layout {
  display: grid;
  grid-template-columns: 320px 1fr;
  gap: var(--space-4);
  min-height: 60vh;
}

.wake-list { display: flex; flex-direction: column; gap: 8px; }
.section-label { letter-spacing: 0.2em; color: var(--text-muted); margin-bottom: 4px; }
.wake-card {
  background: var(--bg-panel);
  border: 1px solid var(--border-line);
  border-radius: var(--radius);
  padding: 10px 12px;
  cursor: pointer;
  transition: all 160ms ease;
}
.wake-card:hover { border-color: var(--border-line-strong); }
.wake-card.active {
  border-color: var(--neon-cyan);
  background: var(--neon-cyan-soft);
  box-shadow: var(--shadow-glow-cyan);
}
.wake-card.error {
  border-color: #ff5577;
  background: rgba(255, 85, 119, 0.06);
}
.wake-card.error.active {
  border-color: #ff5577;
  background: rgba(255, 85, 119, 0.14);
  box-shadow: 0 0 12px rgba(255, 85, 119, 0.4);
}
.wake-card-tags {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.tag-callcount {
  background: rgba(0, 200, 255, 0.12);
  color: #6cf;
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 11px;
}
.tag-error {
  background: rgba(255, 85, 119, 0.18);
  color: #ff5577;
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 11px;
  font-weight: 600;
}
.wake-error-banner {
  background: rgba(255, 85, 119, 0.1);
  border-left: 3px solid #ff5577;
  padding: 8px 12px;
  border-radius: 4px;
  font-size: 13px;
  color: #ff7799;
  word-break: break-all;
}
.detail-error-header {
  border-color: #ff5577;
}
.wake-card-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.wake-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 4px;
  font-size: 12px;
  color: var(--text-secondary);
}

.wake-detail { display: flex; flex-direction: column; gap: var(--space-3); }
.detail-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  padding-bottom: var(--space-3);
  border-bottom: 1px solid var(--border-line);
}

.injection-block {
  background: var(--bg-deep);
  border: 1px dashed var(--border-line);
  border-radius: var(--radius);
  padding: 10px 12px;
}
.injection-block summary {
  cursor: pointer;
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 12px;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  margin: -6px -8px;
  /* list-style: \25BE 标记默认在左侧 */
  user-select: none;
}
.injection-block summary:hover {
  background: var(--bg-overlay);
  border-radius: var(--radius);
}
.injection-item {
  margin-top: 8px;
  padding: 6px 0;
  border-top: 1px solid var(--border-divider);
}
.inj-head { display: flex; align-items: center; gap: 8px; }
.inj-source { color: var(--neon-magenta); font-family: var(--font-mono); font-size: 11px; }
.inj-status-dot {
  display: inline-block;
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--text-muted);
  margin-right: 6px;
  vertical-align: middle;
}
/* 颜色扎染: injection 全部紫色统一(tool_call 在 turn 区按工具名分色) */
.inj-default {
  border-left: 3px solid #aa77ff !important;
  background: rgba(170, 119, 255, 0.04) !important;
}
.inj-default .inj-source { color: #aa77ff; }
.inj-default .inj-status-dot { background: #aa77ff; box-shadow: 0 0 6px #aa77ff; }
.call-input-actions {
  display: flex;
  gap: 6px;
  align-items: center;
  flex-wrap: wrap;
  margin: 4px 0;
}
.inj-scope { color: var(--text-muted); font-size: 11px; }
.inj-content { margin-top: 6px; font-size: 12px; color: var(--text-secondary); white-space: pre-wrap; max-height: 240px; overflow-y: auto; }

.turns-stream { display: flex; flex-direction: column; gap: var(--space-3); }
.call-group {
  border: 1px solid var(--border-line-strong);
  border-radius: var(--radius);
  padding: 10px 12px;
  background: rgba(0, 200, 255, 0.03);
  cursor: pointer;
}
.call-group:hover { border-color: var(--neon-cyan); }
.call-group.collapsed { padding: 8px 12px; background: var(--bg-panel); }
.call-group-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  padding: 4px 2px;
  user-select: none;
}
.call-group-head:hover {
  color: var(--neon-cyan);
}
.call-group-badge {
  background: var(--neon-cyan-soft);
  color: var(--neon-cyan);
  padding: 2px 8px;
  border-radius: 3px;
  font-weight: 600;
}
.call-group-summary { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.call-preview {
  margin-top: 6px;
  padding: 4px 8px;
  font-size: 12px;
  color: var(--text-muted);
  cursor: pointer;
  border-radius: 3px;
}
.call-preview:hover {
  color: var(--neon-cyan);
  background: rgba(0, 200, 255, 0.04);
}
.turn {
  background: var(--bg-panel);
  border: 1px solid var(--border-line);
  border-radius: var(--radius);
  padding: 0;
  border-left: 3px solid transparent;
  overflow: hidden;
}
.turn.role-user { border-left-color: var(--neon-cyan); }
.turn.role-assistant { border-left-color: var(--neon-pink); }
.turn.role-tool { border-left-color: var(--text-muted); }
.turn.role-system { border-left-color: var(--neon-magenta); opacity: 0.85; }

.turn.collapsed { padding-bottom: 0; }
.turn-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  cursor: pointer;
  user-select: none;
}
.turn-head:hover { background: var(--bg-overlay); }
.turn-role {
  font-family: var(--font-display);
  letter-spacing: 0.05em;
  color: var(--text-primary);
  font-size: 12px;
}
.tool-tag {
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  background: var(--neon-pink-soft);
  color: var(--neon-pink);
  font-family: var(--font-mono);
  font-size: 11px;
}
.call-seq-tag {
  padding: 2px 6px;
  border-radius: var(--radius-sm);
  background: var(--bg-elevated);
  color: var(--neon-cyan);
  font-size: 10px;
}
.expand-icon { color: var(--text-muted); }

.turn-preview {
  padding: 0 12px 10px;
  font-size: 12px;
  color: var(--text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.block-label {
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.2em;
  color: var(--text-muted);
  text-transform: uppercase;
  margin-bottom: 4px;
}

.turn-reasoning {
  padding: 10px 12px 10px 32px;
  font-size: 12px;
  color: var(--text-muted);
  background: var(--bg-deep);
  border-top: 1px solid var(--border-divider);
  max-height: 200px;
  overflow-y: auto;
}
.turn-body {
  padding: 10px 12px 10px 32px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-primary);
}
.turn-body :deep(pre),
.turn-body :deep(code) {
  background: var(--bg-deep);
  border-radius: var(--radius-sm);
  padding: 2px 6px;
  font-family: var(--font-mono);
  font-size: 12px;
}
.turn-body :deep(pre) { padding: 8px 10px; overflow-x: auto; margin: 6px 0; }
.turn-body :deep(h1),
.turn-body :deep(h2),
.turn-body :deep(h3) { font-family: var(--font-display); color: var(--neon-cyan); margin: 8px 0 4px; font-size: 15px; }
.turn-body :deep(p) { margin: 6px 0; }
.turn-body :deep(ul),
.turn-body :deep(ol) { margin: 6px 0; padding-left: 24px; }

.tool-calls { display: flex; flex-direction: column; gap: 8px; padding: 10px 12px 10px 32px; }
.tool-call-card {
  background: var(--bg-deep);
  border-left: 2px solid var(--neon-pink);
  padding: 8px 10px;
  border-radius: var(--radius-sm);
}
/* 关键动作 tool_call 高亮 */
.tool-call-card.tc-msg {
  border-left-color: #ff9944;
  background: rgba(255, 153, 68, 0.06);
}
.tool-call-card.tc-rest {
  border-left-color: #ffb300;
  background: rgba(255, 179, 0, 0.06);
}
.tool-args {
  margin-top: 4px;
  font-size: 11px;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-wrap: break-word;
  max-height: 220px;
  overflow-y: auto;
}

.llm-call-input {
  padding: 10px 12px 10px 32px;
  border-top: 1px solid var(--border-divider);
}
.call-input-list {
  margin-top: 6px;
  max-height: 400px;
  overflow-y: auto;
  border-left: 2px solid var(--neon-magenta);
  padding-left: 6px;
}
.call-input-msg { margin-bottom: 4px; border-bottom: 1px solid var(--border-divider); padding: 4px 0; }
.call-input-msg summary { cursor: pointer; font-size: 11px; }
.msg-role {
  display: inline-block;
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  font-family: var(--font-mono);
  font-size: 10px;
  margin-right: 6px;
}
.msg-role.role-system { background: rgba(193,43,255,0.15); color: var(--neon-magenta); }
.msg-role.role-user { background: rgba(0,240,255,0.15); color: var(--neon-cyan); }
.msg-role.role-assistant { background: rgba(255,45,156,0.15); color: var(--neon-pink); }
.msg-role.role-tool { background: rgba(120,130,200,0.15); color: var(--text-secondary); }
.msg-json {
  margin-top: 6px;
  font-size: 11px;
  color: var(--text-secondary);
  white-space: pre-wrap;
  max-height: 200px;
  overflow-y: auto;
}

.turn-error {
  margin: 6px 12px;
  padding: 6px 10px;
  background: rgba(255, 77, 106, 0.1);
  border-radius: var(--radius-sm);
  color: var(--neon-red);
  font-size: 12px;
}

.turn-raw { margin: 4px 12px 10px 32px; }
.turn-raw summary { cursor: pointer; font-size: 11px; color: var(--text-muted); font-family: var(--font-mono); }
.raw-json {
  margin-top: 6px;
  font-size: 11px;
  color: var(--text-secondary);
  white-space: pre-wrap;
  max-height: 280px;
  overflow-y: auto;
}
</style>
