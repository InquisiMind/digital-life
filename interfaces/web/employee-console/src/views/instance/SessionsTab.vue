<template>
  <div>
    <section class="page-hero">
      <div>
        <h1 class="page-title">Sessions</h1>
        <p class="page-subtitle">数字生命的会话 · 每个 session = 一段连续的对话</p>
      </div>
      <div style="display: flex; gap: 6px;">
        <el-button size="small" @click="expandAll">全部展开</el-button>
        <el-button size="small" @click="collapseAll">全部收起</el-button>
        <el-button @click="load"><el-icon><Refresh /></el-icon>刷新</el-button>
      </div>
    </section>

    <div class="session-layout">
      <aside class="wake-list">
        <div class="brand-sub section-label">SESSIONS ({{ sessions.length }})</div>
        <div
          v-for="s in sessions"
          :key="s.id"
          class="wake-card"
          :class="{ active: selectedId === s.id, error: s.end_reason === 'error' }"
          @click="selectSession(s.id)"
        >
          <div class="wake-card-head">
            <strong class="mono">{{ sessionLabel(s) }}</strong>
            <span v-if="s.end_reason === 'error'" class="tag-error mono">Error</span>
          </div>
          <div class="wake-meta">
            <span class="mono">{{ fmtStarted(s) }}</span>
            <span class="brand-sub mono" style="color: var(--text-muted);">
              · {{ s.message_count || 0 }} msg · {{ s.tool_call_count || 0 }} calls
            </span>
          </div>
          <div v-if="s.digest" class="wake-meta">
            <span class="brand-sub mono" style="color: var(--text-muted); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
              {{ digestPreview(s.digest) }}
            </span>
          </div>
        </div>
        <div v-if="!sessions.length" class="dev-placeholder"><span class="mono">暂无会话</span></div>
      </aside>

      <main class="wake-detail">
        <div v-if="loadingDetail" class="dev-placeholder"><span class="mono">loading…</span></div>
        <div v-else-if="!selectedId" class="dev-placeholder">
          <strong>// SELECT A SESSION</strong>
          <span>左侧任选一个会话查看完整对话</span>
        </div>
        <template v-else>
          <div class="detail-head" :class="{ 'detail-error-header': isSessionError }">
            <div>
              <h2 class="page-title" style="font-size: 16px;">{{ sessionLabel(sessionMeta) }}</h2>
              <div class="brand-sub mono">
                {{ fmtStarted(sessionMeta) }} · {{ messages.length }} msgs
                <span v-if="sessionMeta?.input_tokens"> · {{ Number(sessionMeta.input_tokens).toLocaleString() }} tok in</span>
                <span v-if="sessionMeta?.health_label"> · {{ sessionMeta.health_label }}</span>
              </div>
            </div>
            <div class="tag-row">
              <el-button size="small" @click="downloadSession">
                <el-icon><Document /></el-icon> Raw JSON
              </el-button>
            </div>
          </div>

          <div v-if="sessionError" class="wake-error-banner mono">
            ⚠️ <strong>本 session 报错:</strong> {{ sessionError }}
          </div>

          <div class="turns-stream">
            <template v-for="(msg, idx) in messages" :key="idx">
              <div class="turn" :class="msgClass(msg)">
                <div class="turn-head" @click="toggleMsg(idx)">
                  <span class="status-dot" :class="msgDotClass(msg)"></span>
                  <strong class="turn-role">{{ msgLabel(msg) }}</strong>
                  <span v-if="msg.tool_name" class="tool-tag">{{ msg.tool_name }}</span>
                  <span class="brand-sub mono" style="margin-left: auto;">{{ fmtTs(msg.ts) }}</span>
                  <el-icon class="expand-icon">
                    <ArrowDown v-if="!openMsgs[idx]" /><ArrowUp v-else />
                  </el-icon>
                </div>
                <div v-if="!openMsgs[idx]" class="turn-preview mono">{{ msgPreview(msg) }}</div>
                <template v-else>
                  <div v-if="msg.reasoning" class="turn-reasoning mono">
                    <details><summary class="block-label" style="cursor:pointer;">💭 reasoning ({{ msg.reasoning.length }} chars)</summary><div>{{ msg.reasoning }}</div></details>
                  </div>
                  <div v-if="msg.content" class="turn-body">
                    <div class="block-label" v-if="msg.role === 'user'">⚡ EVENT</div>
                    <div class="block-label" v-else-if="msg.role === 'assistant'">🤖 AI</div>
                    <div class="block-label" v-else-if="msg.role === 'tool'">⚙️ TOOL</div>
                    <div v-html="renderMarkdown(msg.content)"></div>
                  </div>
                  <div v-if="Array.isArray(msg.tool_calls) && msg.tool_calls.length" class="tool-calls">
                    <div v-for="(tc,i) in msg.tool_calls" :key="i" class="tool-call-card" :class="toolCallClass(tc)">
                      <div class="block-label">{{ toolCallLabel(tc) }}</div>
                      <strong :style="{ color: toolCallColor(tc) }">{{ safeToolName(tc) }}</strong>
                      <pre class="mono tool-args">{{ safeToolArgs(tc) }}</pre>
                      <el-button size="small" text @click="copyText(safeToolArgs(tc))">copy args</el-button>
                    </div>
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

function sessionLabel(s) {
  if (!s) return '—'
  const parts = (s.id || '').split('_')
  if (parts.length >= 5 && parts[0] === 'tx') return `${parts[2]||''} ${(parts[3]||'').replace(/(\d{2})(\d{2})/,'$1:$2')} · ${parts[1]||''}`
  return shortId(s.id || '', 16)
}
function fmtStarted(s) { return s && Number(s.started_at) ? fmtEpochTime(Number(s.started_at)) : '—' }
function fmtTs(ts) { return ts ? fmtEpochTime(Number(ts)) : '' }
function digestPreview(d) { const l = String(d||'').split('\n')[0]||''; return safeSlice(l,0,60)+(l.length>60?'…':'') }
const isSessionError = computed(() => sessionMeta.value?.end_reason === 'error')
const sessionError = computed(() => isSessionError.value ? sessionMeta.value.end_reason : '')

function msgClass(m) { return { user:'role-user', assistant:'role-assistant', tool:'role-tool', system:'role-system' }[m.role||''] || '' }
function msgDotClass(m) { if(m.content&&m.content.includes('"error"'))return 'down'; return m.role==='assistant'?'live':'idle' }
function msgLabel(m) { return { user:'⚡ EVENT', assistant:'🤖 AI', tool:'⚙️ TOOL', system:'📋 SYS' }[m.role||'']||m.role||'' }
function msgPreview(m) { const c=String(m.content||m.reasoning||'').replace(/\s+/g,' ').trim(); return safeSlice(c,0,100)+(c.length>100?'…':'') }
function toggleMsg(i) { openMsgs[i] = !openMsgs[i] }
function expandAll() { for(let i=0;i<messages.value.length;i++) openMsgs[i]=true }
function collapseAll() { for(let i=0;i<messages.value.length;i++) openMsgs[i]=false }
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
      sessionMeta.value = { id: sid, ...d }
      for (let i = 0; i < messages.value.length; i++) {
        const r = messages.value[i].role
        openMsgs[i] = r === 'user' || r === 'assistant'
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
.session-layout { display:grid; grid-template-columns:320px 1fr; gap:var(--space-4); min-height:60vh; }
.wake-list { display:flex; flex-direction:column; gap:8px; }
.section-label { letter-spacing:0.2em; color:var(--text-muted); margin-bottom:4px; }
.wake-card { background:var(--bg-panel); border:1px solid var(--border-line); border-radius:var(--radius); padding:10px 12px; cursor:pointer; transition:all 160ms ease; }
.wake-card:hover { border-color:var(--border-line-strong); }
.wake-card.active { border-color:var(--neon-cyan); background:var(--neon-cyan-soft); box-shadow:var(--shadow-glow-cyan); }
.wake-card.error { border-color:#ff5577; background:rgba(255,85,119,.06); }
.wake-card-head { display:flex; justify-content:space-between; align-items:center; }
.wake-meta { display:flex; align-items:center; gap:6px; margin-top:4px; font-size:12px; color:var(--text-secondary); }
.tag-error { background:rgba(255,85,119,.18); color:#ff5577; padding:1px 6px; border-radius:3px; font-size:11px; }
.wake-detail { display:flex; flex-direction:column; gap:var(--space-3); }
.detail-head { display:flex; justify-content:space-between; align-items:flex-start; padding-bottom:var(--space-3); border-bottom:1px solid var(--border-line); }
.tag-row { display:flex; gap:6px; }
.turns-stream { display:flex; flex-direction:column; gap:var(--space-2); }
.turn { background:var(--bg-panel); border:1px solid var(--border-line); border-left:3px solid var(--text-muted); border-radius:var(--radius-sm); overflow:hidden; }
.turn.role-user { border-left-color:var(--neon-cyan); }
.turn.role-assistant { border-left-color:var(--neon-pink); }
.turn-head { display:flex; align-items:center; gap:8px; padding:8px 12px; cursor:pointer; font-size:13px; }
.turn-head:hover { background:var(--bg-overlay); }
.status-dot { display:inline-block; width:6px; height:6px; border-radius:50%; }
.status-dot.live { background:var(--neon-cyan); box-shadow:0 0 6px var(--neon-cyan); }
.status-dot.down { background:#ff5577; }
.status-dot.idle { background:var(--text-muted); }
.turn-role { font-size:12px; }
.tool-tag { background:var(--bg-elevated); padding:1px 6px; border-radius:3px; font-size:11px; color:var(--neon-magenta); }
.turn-preview { margin:0 12px 8px; padding:4px 8px; font-size:12px; color:var(--text-muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.turn-body { padding:8px 12px; font-size:13px; line-height:1.6; }
.block-label { font-size:11px; color:var(--text-muted); margin-bottom:4px; }
.tool-calls { padding:4px 12px 8px; }
.tool-call-card { background:var(--bg-deep); border-left:2px solid var(--neon-pink); padding:8px 10px; border-radius:var(--radius-sm); margin-top:4px; }
.tool-call-card.tc-msg { border-left-color:#ff9944; background:rgba(255,153,68,.06); }
.tool-call-card.tc-rest { border-left-color:#ffb300; background:rgba(255,179,0,.06); }
.tool-args { margin-top:4px; white-space:pre-wrap; word-break:break-all; font-size:12px; max-height:300px; overflow-y:auto; }
</style>
