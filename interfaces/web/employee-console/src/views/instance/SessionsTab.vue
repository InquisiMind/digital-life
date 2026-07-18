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

          <!-- injections（注入的上下文块） -->
          <template v-if="injections.length">
            <details class="injection-block" open>
              <summary>上下文注入 ({{ injections.length }}) · 点击折叠</summary>
              <div v-for="inj in injections" :key="inj.id" class="injection-item">
                <div class="inj-head">
                  <span class="inj-source">{{ inj.sys_tool || 'unknown' }}</span>
                  <span class="inj-scope" v-if="inj.scope_id && inj.scope_id !== '*'">
                    @ {{ safeSlice(inj.scope_id, 0, 24) }}
                  </span>
                  <el-button size="small" text @click="copyText(JSON.stringify(inj, null, 2))">copy raw</el-button>
                </div>
                <div class="inj-content mono" v-html="renderMarkdown(inj.content)"></div>
              </div>
            </details>
          </template>

          <!-- turns (按 LLM call 分组显示) -->
          <div class="turns-stream">
            <div
              v-for="group in groupedTurns"
              :key="group.callSeq"
              class="call-group"
              :class="{ collapsed: !expandedCalls[group.callSeq] }"
            >
              <div class="call-group-head" @click="toggleCall(group.callSeq)">
                <span class="call-group-badge">🔄 Call #{{ group.callSeq }}</span>
                <span class="brand-sub mono call-group-summary">
                  {{ group.summary }}
                </span>
                <span v-if="group.tokenCount" class="brand-sub mono" style="margin-left: auto; color: var(--text-muted);">
                  {{ group.tokenCount }} tok
                </span>
                <el-icon class="expand-icon">
                  <ArrowDown v-if="!expandedCalls[group.callSeq]" />
                  <ArrowUp v-else />
                </el-icon>
              </div>

              <!-- 折叠时只显示 call head + one-line preview -->
              <div v-if="!expandedCalls[group.callSeq]" class="call-preview mono">
                {{ group.preview }}
              </div>

              <!-- 展开时:本 call 的所有 turn(input + output + tool result) -->
              <template v-else>
                <div
                  v-for="turn in group.turns"
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
                    <div v-for="(tc, i) in turn.tool_calls" :key="i" class="tool-call-card">
                      <div class="block-label">⚙ tool_call</div>
                      <strong style="color: var(--neon-pink);">{{ safeToolName(tc) }}</strong>
                      <pre class="mono tool-args">{{ safeToolArgs(tc) }}</pre>
                      <el-button size="small" text @click="copyText(safeToolArgs(tc))">copy args</el-button>
                    </div>
                  </div>

                  <div v-if="turn.error" class="turn-error mono">⚠ {{ turn.error }}</div>
                </div>

                <!-- 该 call 完整 LLM input JSON (按需展开) -->
                <div class="llm-call-input">
                  <el-button
                    v-if="!callInputs[callKeyForSeq(group.callSeq)]"
                    size="small"
                    :loading="callLoading[callKeyForSeq(group.callSeq)]"
                    @click="loadCallInputForSeq(group.callSeq)"
                  >
                    <el-icon><View /></el-icon> 查看完整 LLM 输入 JSON
                  </el-button>
                  <template v-else>
                    <div class="block-label">
                      📦 LLM Call #{{ group.callSeq }} input
                      ({{ callInputs[callKeyForSeq(group.callSeq)]?.length || 0 }} messages · model {{ callInputModels[callKeyForSeq(group.callSeq)] || '—' }})
                    </div>
                    <div class="call-input-list">
                      <details v-for="(m, mi) in callInputs[callKeyForSeq(group.callSeq)]" :key="mi" class="call-input-msg">
                        <summary>
                          <span class="msg-role" :class="'role-' + m.role">{{ m.role }}</span>
                          <span class="brand-sub mono">{{ safeSlice(typeof m.content === 'string' ? m.content : JSON.stringify(m.content), 0, 80) }}</span>
                        </summary>
                        <pre class="mono msg-json">{{ JSON.stringify(m, null, 2) }}</pre>
                      </details>
                    </div>
                    <el-button size="small" text @click="copyText(JSON.stringify(callInputs[callKeyForSeq(group.callSeq)], null, 2))">
                      copy 全部 input JSON
                    </el-button>
                  </template>
                </div>
              </template>
            </div>
          </div>
        </template>
      </main>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter, RouterLink } from 'vue-router'
import { Refresh, ArrowDown, ArrowUp, View, Document } from '@element-plus/icons-vue'
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
const callInputs = reactive({})      // {callKey: messages[]}
const callInputModels = reactive({})
const callLoading = reactive({})

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
  loadingDetail.value = true
  turns.value = []
  injections.value = []
  wakeMeta.value = null
  // 清理 call cache + expand state
  Object.keys(callInputs).forEach(k => delete callInputs[k])
  Object.keys(callInputModels).forEach(k => delete callInputModels[k])
  Object.keys(expandedTurns).forEach(k => delete expandedTurns[k])
  Object.keys(expandedCalls).forEach(k => delete expandedCalls[k])

  try {
    const d = await instanceApi(iid.value).wakeDetail(wakeId)
    if (d && !d.error) {
      wakeMeta.value = d.wake || null
      turns.value = Array.isArray(d.turns) ? d.turns : []
      injections.value = Array.isArray(d.injections) ? d.injections : []
      // 默认:所有 call 展开
      for (const g of groupedTurns.value) {
        expandedCalls[g.callSeq] = true
      }
    }
  } finally {
    loadingDetail.value = false
  }
}

// 按 llm_call_seq 聚合 turns (input = role in [user, system], output = assistant/tool)
const groupedTurns = computed(() => {
  if (!Array.isArray(turns.value) || !turns.value.length) return []
  const map = new Map()
  for (const t of turns.value) {
    const seq = t.llm_call_seq != null ? Number(t.llm_call_seq) : null
    if (seq == null) continue // 没 seq 的 turn(如审计记录)跳过
    if (!map.has(seq)) {
      map.set(seq, { callSeq: seq, turns: [], tokenCount: 0, summary: '', preview: '' })
    }
    const g = map.get(seq)
    g.turns.push(t)
    if (t.token_count) g.tokenCount += Number(t.token_count) || 0
  }
  const groups = Array.from(map.values()).sort((a, b) => a.callSeq - b.callSeq)
  // 给每组算大字 summary + preview(易扫读)
  for (const g of groups) {
    // summary: 找 assistant turn 的 tool_calls 名字列表(没就显示 content preview)
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
    // 也带上 user 事件触发源(role=user 的 content 头)
    const userTurn = g.turns.find(t => t.role === 'user')
    const userHead = userTurn ? String(userTurn.content || '').split('\n')[0].slice(0, 60) : ''
    if (toolNames.length) {
      g.summary = `${asstTurns.length} 个决策 → ${toolNames.join(', ')}`
    } else if (userHead) {
      g.summary = `事件: ${userHead}${userHead.length >= 60 ? '…' : ''}`
    } else {
      g.summary = `${g.turns.length} turns`
    }
    // preview = 第一条 user content 80 chars
    g.preview = userHead ? userHead.slice(0, 100) : (asstTurns[0]?.content || '').slice(0, 100) || ''
  }
  return groups
})

function toggleCall(callSeq) {
  expandedCalls[callSeq] = !expandedCalls[callSeq]
}
function toggleTurn(turn) {
  expandedTurns[turn.id] = !expandedTurns[turn.id]
}
function expandAll() {
  for (const g of groupedTurns.value) expandedCalls[g.callSeq] = true
}
function collapseAll() {
  for (const g of groupedTurns.value) expandedCalls[g.callSeq] = false
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

onMounted(load)
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
.injection-block summary { cursor: pointer; color: var(--text-muted); font-family: var(--font-mono); font-size: 12px; }
.injection-item {
  margin-top: 8px;
  padding: 6px 0;
  border-top: 1px solid var(--border-divider);
}
.inj-head { display: flex; align-items: center; gap: 8px; }
.inj-source { color: var(--neon-magenta); font-family: var(--font-mono); font-size: 11px; }
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
  background: rgba(255, 255, 255, 0.02);
  border-radius: 3px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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
