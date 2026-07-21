<template>
  <div class="sessions-root">
    <div class="session-layout">
      <aside class="sidebar">
        <div class="sidebar-head">
          <span class="brand-sub section-label">SESSIONS</span>
          <el-button size="small" @click="load"><el-icon><Refresh /></el-icon></el-button>
        </div>
        <div class="sidebar-list">
          <div
            v-for="s in sessions"
            :key="s.id"
            class="wake-card"
            :class="{ active: selectedId === s.id, error: s.end_reason === 'error' }"
            @click="selectSession(s.id)"
          >
            <div class="wake-card-head">
              <strong class="mono">{{ sessionLabel(s) }}</strong>
              <span v-if="s.end_reason === 'error'" class="tag-error mono">!Error</span>
            </div>
            <div class="wake-meta">
              <span class="brand-sub mono" style="color: var(--text-muted); font-size: 11px;">
                {{ s.message_count || 0 }} msg · {{ s.tool_call_count || 0 }} calls
              </span>
            </div>
          </div>
          <div v-if="!sessions.length" class="dev-placeholder"><span class="mono">暂无</span></div>
        </div>
      </aside>

      <main class="wake-detail">
        <div v-if="loadingDetail" class="dev-placeholder"><span class="mono">loading…</span></div>
        <div v-else-if="!selectedId" class="dev-placeholder">
          <strong>// SELECT A SESSION</strong>
        </div>
        <template v-else>
          <div class="detail-topbar">
            <div class="detail-topbar-info">
              <span class="mono" style="font-weight:600;">{{ sessionLabel(sessionMeta) }}</span>
              <span class="brand-sub mono" style="margin-left:8px;">{{ messages.length }} msgs</span>
            </div>
            <div class="tab-bar">
              <button class="tab-btn" :class="{ active: activeTab === 'flow' }" @click="activeTab = 'flow'">📜 流</button>
              <button class="tab-btn" :class="{ active: activeTab === 'events' }" @click="activeTab = 'events'">📋 {{ eventsData.length }}</button>
              <el-button size="small" text @click="expandAll" v-if="activeTab==='flow'">展开</el-button>
              <el-button size="small" text @click="collapseAll" v-if="activeTab==='flow'">收起</el-button>
            </div>
          </div>

          <div class="tab-content">
            <template v-if="activeTab === 'events'">
              <div class="events-grid">
                <div
                  v-for="ev in eventsData"
                  :key="ev.event_id"
                  class="event-card"
                  :class="{ 'event-card-pending': !ev.consumed_at }"
                >
                  <div class="event-card-head">
                    <span class="event-kind" :class="'ev-' + ev.kind">{{ ev.kind }}</span>
                    <span v-if="ev.sender" class="brand-sub mono">{{ ev.sender }}</span>
                    <span class="event-id mono">#{{ ev.event_id }}</span>
                    <span v-if="ev.consumed_at" class="brand-sub mono" style="margin-left:auto;">✓{{ String(ev.consumed_at).slice(11,19) }}</span>
                    <span v-else class="event-pending">pending</span>
                  </div>
                  <div class="event-card-text mono">{{ ev.text || '(无内容)' }}</div>
                </div>
              </div>
            </template>

            <template v-if="activeTab === 'flow'">
              <div class="turns-stream">
                <template v-for="(item, idx) in timeline" :key="item.key">
                  <details v-if="item.type === 'injection'" class="injection-block" @click.stop>
                    <summary>
                      <span class="inj-source">{{ item.inj.sys_tool || 'unknown' }}</span>
                      <span class="brand-sub mono" style="margin-left:auto">{{ fmtTs(item.inj.injected_at) }}</span>
                      <el-button size="small" text @click.stop="copyText(JSON.stringify(item.inj, null, 2))">copy</el-button>
                    </summary>
                    <div class="inj-content mono" v-html="renderMarkdown(item.inj.content)"></div>
                  </details>
                  <div v-else class="turn" :class="msgClass(item.msg)">
                    <div class="turn-head" @click="toggleMsg(idx)">
                      <span class="status-dot" :class="msgDotClass(item.msg)"></span>
                      <strong class="turn-role">{{ msgLabel(item.msg) }}</strong>
                      <span v-if="item.msg.tool_name" class="tool-tag">{{ item.msg.tool_name }}</span>
                      <span class="brand-sub mono" style="margin-left: auto;">{{ fmtTs(item.msg.ts) }}</span>
                      <el-icon class="expand-icon"><ArrowDown v-if="!openMsgs[idx]" /><ArrowUp v-else /></el-icon>
                    </div>
                    <div v-if="!openMsgs[idx]" class="turn-preview mono">{{ msgPreview(item.msg) }}</div>
                    <template v-else>
                      <div v-if="item.msg.reasoning" class="turn-reasoning mono">
                        <details><summary class="block-label" style="cursor:pointer;">💭 ({{ item.msg.reasoning.length }})</summary><div>{{ item.msg.reasoning }}</div></details>
                      </div>
                      <div v-if="item.msg.content" class="turn-body">
                        <div class="block-label" v-if="item.msg.role === 'user'">⚡ EVENT</div>
                        <div class="block-label" v-else-if="item.msg.role === 'assistant'">🤖</div>
                        <div class="block-label" v-else-if="item.msg.role === 'tool'">⚙️</div>
                        <div v-html="renderMarkdown(item.msg.content)"></div>
                      </div>
                      <div v-if="Array.isArray(item.msg.tool_calls) && item.msg.tool_calls.length" class="tool-calls">
                        <div v-for="(tc,i) in item.msg.tool_calls" :key="'tc-'+i" class="tool-call-card" :class="toolCallClass(tc)">
                          <div class="block-label">{{ toolCallLabel(tc) }}</div>
                          <strong :style="{ color: toolCallColor(tc) }">{{ safeToolName(tc) }}</strong>
                          <pre class="mono tool-args">{{ safeToolArgs(tc) }}</pre>
                          <div v-if="item.msg.tool_responses && item.msg.tool_responses[i]" class="tool-response">
                            <div class="block-label">→ result</div>
                            <pre class="mono tool-result">{{ String(item.msg.tool_responses[i].content || '').slice(0, 2000) }}</pre>
                          </div>
                        </div>
                      </div>
                    </template>
                  </div>
                </template>
              </div>
            </template>
          </div>
        </template>
      </main>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Refresh, ArrowDown, ArrowUp, Document } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { instanceApi } from '@/api/client'
import { fmtEpochTime, safeSlice, shortId } from '@/composables/useFormat'
import { renderMarkdown } from '@/composables/useMarkdown'

const route = useRoute()
const iid = computed(() => String(route.params.iid || ''))

const sessions = ref([])
const selectedId = ref(null)
const loadingDetail = ref(false)
const messages = ref([])
const sessionMeta = ref(null)
const openMsgs = reactive({})
const injectionsData = ref([])
const eventsData = ref([])
const activeTab = ref('flow')

function sessionLabel(s) {
  if (!s) return '—'
  const sid = s.id || ''
  const parts = sid.split('_')
  if (parts[0] !== 'tx' || parts.length < 5) return shortId(sid, 16)
  // tx_{kind}_{MMDD}_{HHMM}_{uuid6} — kind 可能含下划线(group_message)
  // 从末尾定长拆: [-1]=uuid [-2]=HHMM [-3]=MMDD, 剩余=[1:-3]=kind
  const hhmm = parts[parts.length - 2] || ''
  const mmdd = parts[parts.length - 3] || ''
  const kind = parts.slice(1, -3).join('_')
  const time = hhmm.replace(/(\d{2})(\d{2})/, '$1:$2')
  return `${mmdd} ${time} · ${kind}`
}
function fmtStarted(s) { return s && Number(s.started_at) ? fmtEpochTime(Number(s.started_at)) : '—' }
function fmtTs(ts) { return ts ? fmtEpochTime(Number(ts)) : '' }
function digestPreview(d) { const l = String(d||'').split('\n')[0]||''; return safeSlice(l,0,60)+(l.length>60?'…':'') }
const isSessionError = computed(() => sessionMeta.value?.end_reason === 'error')
const sessionError = computed(() => isSessionError.value ? sessionMeta.value.end_reason : '')

function msgClass(m) { return { user:'role-user', assistant:'role-assistant', tool:'role-tool', system:'role-system' }[m.role||''] || '' }
// timeline: 把 messages + injections 按 timestamp/injected_at 合并排序
// assistant 带 tool_calls 的消息 + 后续 tool response 合并为一组
const timeline = computed(() => {
  // 1. 先合并 messages: assistant+tool_calls 跟后续 role=tool 合成一条
  const merged = []
  const msgs = messages.value
  for (let i = 0; i < msgs.length; i++) {
    const m = msgs[i]
    if (m.role === 'assistant' && Array.isArray(m.tool_calls) && m.tool_calls.length) {
      // assistant 带 tool_calls → 合并后续 tool responses
      const tools = []
      for (let j = i + 1; j < msgs.length && msgs[j].role === 'tool'; j++) {
        tools.push(msgs[j])
        i = j // 跳过已合并的 tool messages
      }
      merged.push({ ...m, tool_responses: tools })
    } else if (m.role === 'tool') {
      // 孤立的 tool message(前面没 assistant.tool_calls) → 跳过(已合并或无意义)
      // 但如果前一条 merged 也是 assistant 带 tool_responses, 不重复添加
      const prev = merged[merged.length - 1]
      if (prev && prev.role === 'assistant' && prev.tool_responses) {
        // 已经在 assistant 组里了
      } else {
        merged.push(m)
      }
    } else {
      merged.push(m)
    }
  }

  // 2. 转 timeline items
  const msgItems = merged.map((m, i) => ({
    type: 'message', msg: m, ts: Number(m.ts) || 0, idx: i,
    key: 'msg-' + i,
  }))
  const injItems = injectionsData.value.map((inj, i) => ({
    type: 'injection', inj, ts: Number(inj.injected_at) || 0,
    key: 'inj-' + (inj.id || i),
  }))
  const all = [...msgItems, ...injItems]
  all.sort((a, b) => {
    if (a.ts !== b.ts) return a.ts - b.ts
    if (a.type === 'injection' && b.type === 'message') return -1
    if (a.type === 'message' && b.type === 'injection') return 1
    return 0
  })
  // 重建 idx (排后序编号变了, openMsgs 需要跟着)
  all.forEach((item, i) => { if (item.type === 'message') item.idx = i })
  return all
})

function msgDotClass(m) { if(m.content&&m.content.includes('"error"'))return 'down'; return m.role==='assistant'?'live':'idle' }
function msgLabel(m) { return { user:'⚡ EVENT', assistant:'🤖 AI', tool:'⚙️ TOOL', system:'📋 SYS' }[m.role||'']||m.role||'' }
function msgPreview(m) { const c=String(m.content||m.reasoning||'').replace(/\s+/g,' ').trim(); return safeSlice(c,0,100)+(c.length>100?'…':'') }
function toggleMsg(i) { openMsgs[i] = !openMsgs[i] }
function expandAll() { for(let i=0;i<timeline.value.length;i++) openMsgs[i]=true }
function collapseAll() { for(let i=0;i<timeline.value.length;i++) openMsgs[i]=false }
function toolCallClass(tc) { const n=safeToolName(tc); if(n==='express_to_human')return 'tc-msg'; if(n==='rest')return 'tc-rest'; return '' }
function toolCallLabel(tc) { const n=safeToolName(tc); if(n==='express_to_human')return '📤 发送消息'; if(n==='rest')return '😴 休息'; return '⚙ tool_call' }
function toolCallColor(tc) { const n=safeToolName(tc); if(n==='express_to_human')return '#ff9944'; if(n==='rest')return '#ffb300'; return 'var(--neon-pink)' }
function safeToolName(tc) { return safeSlice((tc&&(tc.function?.name||tc.name))||'tool',0,40) }
function safeToolArgs(tc) { let a=(tc&&(tc.function?.arguments||tc.arguments))||''; if(typeof a!=='string'){try{a=JSON.stringify(a,null,2)}catch{a=String(a)}} return String(a) }

async function load() {
  const d = await instanceApi(iid.value).sessions()
  if (d && !d.error) {
    sessions.value = Array.isArray(d.sessions) ? d.sessions : []
    if (sessions.value.length) await selectSession(sessions.value[0].id)
  }
}
async function selectSession(sid) {
  selectedId.value = sid
  loadingDetail.value = true
  messages.value = []
  sessionMeta.value = null
  Object.keys(openMsgs).forEach(k => delete openMsgs[k])
  try {
    const d = await instanceApi(iid.value).sessionDetail(sid)
    if (d && !d.error) {
      messages.value = Array.isArray(d.messages) ? d.messages : []
      // injections 去重: 跟 agent._convert_user_to_tool 实际行为一致
      // 'latest' 策略的 sys_tool 只保留最后一条(injected_at 最大)
      // 'append' 策略的全部保留
      const LATEST_TOOLS = new Set(['system_context','session_digest','consciousness','social_context','task_skill','my_context','task_board','schedule','workspace','wake_signal'])
      const rawInj = Array.isArray(d.injections) ? d.injections : []
      const latestMap = new Map()   // sys_tool → 最新的那条
      const appendList = []
      for (const inj of rawInj) {
        const k = inj.sys_tool || 'unknown'
        if (LATEST_TOOLS.has(k)) {
          const prev = latestMap.get(k)
          if (!prev || Number(inj.injected_at) > Number(prev.injected_at)) {
            latestMap.set(k, inj)
          }
        } else {
          appendList.push(inj)
        }
      }
      injectionsData.value = [...latestMap.values(), ...appendList].sort((a,b) => (Number(a.injected_at)||0) - (Number(b.injected_at)||0))
      eventsData.value = Array.isArray(d.events) ? d.events : []
      sessionMeta.value = { id: sid, ...d }
      // 全部默认折叠
      for (let i = 0; i < timeline.value.length; i++) {
        openMsgs[i] = false
      }
    }
  } finally { loadingDetail.value = false }
}
function downloadSession() {
  const dump = JSON.stringify({session:sessionMeta.value, messages:messages.value}, null, 2)
  const blob = new Blob([dump], {type:'application/json'})
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url; a.download = `session-${selectedId.value}.json`; a.click()
  URL.revokeObjectURL(url)
}
watch(() => route.query.sid, (n) => { if(n){const f=sessions.value.find(s=>s.id===String(n));if(f&&f.id!==selectedId.value)selectSession(f.id)} })
function copyText(t) { navigator.clipboard.writeText(String(t)).then(()=>ElMessage.success('已复制'),()=>ElMessage.warning('复制失败')) }
onMounted(load)
</script>

<style scoped>
.sessions-root { height: calc(100vh - 60px); overflow: hidden; }
.session-layout { display:grid; grid-template-columns:240px 1fr; gap:var(--space-3); height:100%; overflow:hidden; }
.sidebar { display:flex; flex-direction:column; overflow:hidden; }
.sidebar-head { display:flex; justify-content:space-between; align-items:center; padding:4px 0; flex-shrink:0; }
.sidebar-list { overflow-y:auto; flex:1; display:flex; flex-direction:column; gap:6px; }
.section-label { letter-spacing:0.2em; color:var(--text-muted); }
.wake-card { background:var(--bg-panel); border:1px solid var(--border-line); border-radius:var(--radius); padding:8px 10px; cursor:pointer; transition:all 120ms ease; }
.wake-card:hover { border-color:var(--border-line-strong); }
.wake-card.active { border-color:var(--neon-cyan); background:var(--neon-cyan-soft); }
.wake-card.error { border-color:#ff5577; }
.wake-card-head { display:flex; justify-content:space-between; align-items:center; }
.wake-meta { margin-top:2px; font-size:11px; }
.tag-error { background:rgba(255,85,119,.18); color:#ff5577; padding:0 4px; border-radius:2px; font-size:10px; }
.wake-detail { display:flex; flex-direction:column; overflow:hidden; }
.detail-topbar { flex-shrink:0; padding-bottom:6px; border-bottom:1px solid var(--border-line); }
.detail-topbar-info { display:flex; align-items:center; margin-bottom:4px; }
.tab-bar { display:flex; gap:4px; align-items:center; }
.tab-btn { background:transparent; border:none; color:var(--text-secondary); padding:4px 10px; border-radius:var(--radius); cursor:pointer; font-size:12px; transition:all 120ms; }
.tab-btn:hover { color:var(--text-primary); background:var(--bg-overlay); }
.tab-btn.active { color:var(--neon-cyan); font-weight:600; }
.tab-content { overflow-y:auto; flex:1; padding-top:8px; }
.turns-stream { display:flex; flex-direction:column; gap:4px; }
.turn { background:var(--bg-panel); border:1px solid var(--border-line); border-left:3px solid var(--text-muted); border-radius:var(--radius-sm); overflow:hidden; }
.turn.role-user { border-left-color:var(--neon-cyan); }
.turn.role-assistant { border-left-color:var(--neon-pink); }
.turn-head { display:flex; align-items:center; gap:6px; padding:6px 10px; cursor:pointer; font-size:12px; }
.turn-head:hover { background:var(--bg-overlay); }
.status-dot { display:inline-block; width:6px; height:6px; border-radius:50%; }
.status-dot.live { background:var(--neon-cyan); } .status-dot.down { background:#ff5577; } .status-dot.idle { background:var(--text-muted); }
.turn-role { font-size:11px; }
.tool-tag { background:var(--bg-elevated); padding:1px 4px; border-radius:2px; font-size:10px; color:var(--neon-magenta); }
.turn-preview { margin:0 10px 6px; padding:2px 6px; font-size:11px; color:var(--text-muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.turn-reasoning { padding:6px 10px; font-size:11px; border-bottom:1px dashed var(--border-line); }
.turn-body { padding:6px 10px; font-size:12px; line-height:1.6; }
.block-label { font-size:10px; color:var(--text-muted); }
.tool-calls { padding:2px 10px 6px; }
.tool-call-card { background:var(--bg-deep); border-left:2px solid var(--neon-pink); padding:6px 8px; border-radius:var(--radius-sm); margin-top:3px; }
.tool-call-card.tc-msg { border-left-color:#ff9944; background:rgba(255,153,68,.06); }
.tool-call-card.tc-rest { border-left-color:#ffb300; background:rgba(255,179,0,.06); }
.tool-args { margin-top:2px; white-space:pre-wrap; word-break:break-all; font-size:11px; max-height:250px; overflow-y:auto; }
.tool-result { margin-top:4px; padding:4px; background:var(--bg-panel); border-radius:3px; font-size:11px; }
.injection-block { background:var(--bg-deep); border:1px dashed var(--border-line); border-radius:var(--radius-sm); padding:6px 10px; margin-bottom:3px; }
.injection-block summary { cursor:pointer; display:flex; align-items:center; gap:6px; font-size:11px; }
.inj-source { color:var(--neon-magenta); font-size:10px; }
.inj-content { margin-top:4px; font-size:11px; color:var(--text-secondary); white-space:pre-wrap; max-height:200px; overflow-y:auto; }
.events-grid { display:flex; flex-direction:column; gap:4px; }
.event-card { background:var(--bg-panel); border:1px solid var(--border-line); border-left:3px solid; border-radius:var(--radius-sm); padding:6px 10px; }
.event-card-pending { border-left-color:#ff5577; }
.event-card:not(.event-card-pending) { border-left-color:var(--text-muted); }
.event-card-head { display:flex; align-items:center; gap:6px; }
.event-card-text { font-size:11px; color:var(--text-secondary); white-space:pre-wrap; word-break:break-word; margin-top:2px; }
.event-kind { padding:0 4px; border-radius:2px; font-size:10px; }
.ev-group_message { background:rgba(68,204,119,.12); color:#44cc77; }
.ev-timer { background:rgba(0,200,255,.12); color:var(--neon-cyan); }
.ev-initiative { background:rgba(170,119,255,.12); color:#aa77ff; }
.ev-message { background:rgba(255,153,68,.12); color:#ff9944; }
.ev-awaiting_reply { background:rgba(150,150,150,.12); color:var(--text-muted); }
.ev-social_command { background:rgba(255,85,119,.12); color:#ff5577; }
.ev-routine { background:rgba(255,179,0,.12); color:#ffb300; }
.event-id { color:var(--text-muted); font-size:10px; }
.event-pending { color:#ff5577; font-size:10px; margin-left:auto; }
</style>
