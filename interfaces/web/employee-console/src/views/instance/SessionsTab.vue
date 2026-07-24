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
              <strong class="mono">{{ sessionKind(s) }}</strong>
              <span v-if="s.end_reason === 'error'" class="tag-error mono">!Error</span>
            </div>
            <div class="wake-meta">
              <span class="brand-sub mono" style="color: var(--text-muted); font-size: 11px;">
                {{ sessionTime(s) }} · {{ s.tool_call_count || 0 }} calls
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
              <span class="brand-sub mono">{{ totalCalls }} calls</span>
              <span class="session-id-chip mono" @click="copyText(selectedId)" :title="`点击复制完整 session_id:\n${selectedId}`">{{ shortSessionId }}</span>
            </div>
            <div class="tab-bar">
              <button class="tab-btn" :class="{ active: activeTab === 'flow' }" @click="activeTab = 'flow'">
                <span class="tab-icon">📜</span><span class="tab-text">流</span>
              </button>
              <button class="tab-btn" :class="{ active: activeTab === 'events' }" @click="activeTab = 'events'">
                <span class="tab-icon">📋</span><span class="tab-text">事件</span>
                <span v-if="eventsData.length" class="tab-count">{{ eventsData.length }}</span>
              </button>
              <el-button v-if="activeTab==='flow'" class="tab-action" size="small" text @click="toggleAllExpand">
                {{ allExpanded ? '收起' : '展开' }}
              </el-button>
            </div>
          </div>

          <div class="tab-content">
            <template v-if="activeTab === 'events'">
              <div class="events-grid">
                <div
                  v-for="ev in eventsData"
                  :key="ev.event_id"
                  class="event-card event-card-clickable"
                  :class="{ 'event-card-pending': !ev.consumed_at }"
                  :title="eventsData.length ? '点击跳到流' : ''"
                  @click="revealEvent(ev)"
                >
                  <div class="event-card-head">
                    <span class="event-kind" :class="'ev-' + ev.kind">{{ ev.kind }}</span>
                    <span v-if="ev.sender" class="brand-sub mono">{{ ev.sender }}</span>
                    <span class="event-id mono">#{{ ev.event_id }}</span>
                    <span v-if="ev.consumed_at" class="brand-sub mono" style="margin-left:auto;">{{ String(ev.created_at).slice(11,19) }}</span>
                    <span v-else class="event-pending">pending</span>
                  </div>
                  <div class="event-card-text mono">{{ ev.text || '(无内容)' }}</div>
                </div>
              </div>
            </template>

            <template v-if="activeTab === 'flow'">
              <div class="turns-stream">
                <template v-for="(item, idx) in timeline" :key="item.key">
                  <details v-if="item.type === 'injection'" class="injection-block" :class="{ 'injection-signal': isSignalInjection(item.inj) }" :data-flow-idx="idx" @click.stop>
                    <summary class="injection-head">
                      <span class="status-dot" :class="isSignalInjection(item.inj) ? 'live' : 'idle'"></span>
                      <strong class="turn-role">{{ isSignalInjection(item.inj) ? 'EVENT' : 'SYS' }}</strong>
                      <span class="tool-tag">{{ item.inj.sys_tool || 'unknown' }}</span>
                      <span class="brand-sub mono" style="margin-left:auto">{{ fmtTs(item.inj.injected_at) }}</span>
                      <el-button size="small" text @click.stop="copyText(JSON.stringify(item.inj, null, 2))" title="复制完整注入数据">copy</el-button>
                    </summary>
                    <div class="inj-content mono" v-html="renderMarkdown(item.inj.content)"></div>
                  </details>
                  <div v-else class="turn" :class="[msgClass(item.msg), turnAccentClass(item.msg)]" :data-flow-idx="idx">
                    <div class="turn-head" @click="toggleMsg(idx)">
                      <span class="status-dot" :class="msgDotClass(item.msg)"></span>
                      <strong class="turn-role">{{ msgLabel(item.msg) }}</strong>
                      <span v-if="item.msg.tool_name" class="tool-tag">{{ item.msg.tool_name }}</span>
                      <span v-if="firstActionTag(item.msg)" class="accent-tag" :class="firstActionTag(item.msg).cls">{{ firstActionTag(item.msg).text }}</span>
                      <span v-if="item.callSeq >= 0" class="call-seq-chip mono" :title="payloadDumpHint(item)" @click.stop="copyText(payloadDumpHint(item))">#{{ item.callSeq }}</span>
                      <span class="brand-sub mono" style="margin-left: auto;">{{ fmtTs(item.msg.ts) }}</span>
                      <el-icon class="expand-icon"><ArrowDown v-if="!openMsgs.has(idx)" /><ArrowUp v-else /></el-icon>
                    </div>
                    <div v-if="!openMsgs.has(idx)" class="turn-preview mono">{{ msgPreview(item.msg) }}</div>
                    <template v-else>
                      <div v-if="item.msg.reasoning" class="turn-reasoning mono">
                        <details open><summary class="block-label" style="cursor:pointer;">💭 thinking ({{ item.msg.reasoning.length }})</summary><div class="reasoning-text">{{ item.msg.reasoning }}</div></details>
                      </div>
                      <div v-if="item.msg.content" class="turn-body">
                        <div class="block-label" v-if="item.msg.role === 'user'">EVENT</div>
                        <div class="block-label" v-else-if="item.msg.role === 'assistant'">回复</div>
                        <div class="block-label" v-else-if="item.msg.role === 'tool'">工具返回</div>
                        <div v-html="renderMarkdown(item.msg.content)"></div>
                      </div>
                      <div v-if="Array.isArray(item.msg.tool_calls) && item.msg.tool_calls.length" class="tool-calls">
                        <div v-for="(tc,i) in item.msg.tool_calls" :key="'tc-'+i" class="tool-call-card" :class="toolCallClass(tc)">
                          <div class="tool-call-head">
                            <span class="block-label">{{ toolCallLabel(tc) }}</span>
                            <strong class="tool-call-name" :style="{ color: toolCallColor(tc) }">{{ safeToolName(tc) }}</strong>
                          </div>
                          <pre class="mono tool-args">{{ prettyToolArgs(tc) }}</pre>
                          <div v-if="item.msg.tool_responses && item.msg.tool_responses[i]" class="tool-response">
                            <div class="block-label">→ result</div>
                            <pre class="mono tool-result">{{ prettyResult(item.msg.tool_responses[i].content) }}</pre>
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
const loadingDetail = ref(true)
const messages = ref([])
const sessionMeta = ref(null)
const openMsgs = reactive(new Set())
const injectionsData = ref([])
const eventsData = ref([])
const activeTab = ref('flow')

// 从 session_id 拆出三段: kind(事件类型) / MMDD / HHMM
// 形如 tx_group_message_0721_2103_<uuid> → ("group_message", "07-21", "21:03")
// kind 可能含下划线, 所以从末尾定长拆
function _sessionParts(s) {
  if (!s) return { kind: '—', mmdd: '', hhmm: '' }
  const sid = s.id || ''
  const parts = sid.split('_')
  if (parts[0] !== 'tx' || parts.length < 5) return { kind: shortId(sid, 16), mmdd: '', hhmm: '' }
  const hhmm = parts[parts.length - 2] || ''
  const mmdd = parts[parts.length - 3] || ''
  const kind = parts.slice(1, -3).join('_')
  return { kind, mmdd, hhmm }
}
function sessionKind(s) { return _sessionParts(s).kind }
function sessionTime(s) {
  const { mmdd, hhmm } = _sessionParts(s)
  if (!mmdd || !hhmm) return ''
  const md = mmdd.replace(/(\d{2})(\d{2})/, '$1-$2')
  const hm = hhmm.replace(/(\d{2})(\d{2})/, '$1:$2')
  return `${md} ${hm}`
}
function sessionLabel(s) {
  const { kind, mmdd, hhmm } = _sessionParts(s)
  if (!mmdd || !hhmm) return kind
  const time = hhmm.replace(/(\d{2})(\d{2})/, '$1:$2')
  return `${mmdd} ${time} · ${kind}`
}
function fmtStarted(s) { return s && Number(s.started_at) ? fmtEpochTime(Number(s.started_at)) : '—' }
function fmtTs(ts) { return ts ? fmtEpochTime(Number(ts)) : '' }
function digestPreview(d) { const l = String(d||'').split('\n')[0]||''; return safeSlice(l,0,60)+(l.length>60?'…':'') }
const isSessionError = computed(() => sessionMeta.value?.end_reason === 'error')
const sessionError = computed(() => isSessionError.value ? sessionMeta.value.end_reason : '')
// 本 session 内所有 assistant.tool_calls 累加总数(= 真实 LLM 调用次数对外的"动作数")
const totalCalls = computed(() =>
  messages.value
    .filter(m => Array.isArray(m.tool_calls) && m.tool_calls.length)
    .reduce((n, m) => n + m.tool_calls.length, 0)
)
// session_id 顶部 chip 短码: 保留语义化前缀(tx_group_message_0721_2103),
// 省略尾部 uuid — 前缀信息量大(类型+日期+时间), 尾部仅作唯一性
const shortSessionId = computed(() => {
  const sid = String(selectedId.value || '')
  if (sid.length <= 30) return sid
  // 形如 tx_xxx_xxx_0721_2103_<6位hash><uuid> → 前缀到 2103 之后加 …
  // 用 time 部分(HHMM, 4 位)作为切点
  const m = sid.match(/^(tx_[a-z_]+_\d{4}_\d{4})_/i)
  if (m) return m[1] + '…'
  return sid.slice(0, 24) + '…'
})

function msgClass(m) {
  const role = m?.role || ''
  const base = { user:'role-user', assistant:'role-assistant', tool:'role-tool', system:'role-system' }[role] || ''
  // mid-session 信号类 sys_tool 显著标识 - 用 cyan 边框(与 user/EVENT 同色),
  // 体现"事件类型"语义, 与普通 sys_tool 注入(蓝色)视觉区分
  if (role === 'tool' && ['wake_signal','mid_session_event'].includes(m?.tool_name)) {
    return `${base} role-signal`
  }
  return base
}
// injection 块的"信号型注入"判定 - 与 turn.role-signal 同语义, 但走不同渲染路径
// wake_signal/mid_session_event 注入是 mid-session 事件到达的核心信号, 需要一眼可识别
function isSignalInjection(inj) {
  const s = inj?.sys_tool || ''
  return ['wake_signal','mid_session_event'].includes(s)
}
// timeline: 把 messages + injections 按 timestamp/injected_at 合并排序
// assistant 带 tool_calls 的消息 + 后续 tool response 合并为一组
const timeline = computed(() => {
  // 1. 先合并 messages: assistant+tool_calls 跟后续 tool response 合成一组
  //    例外: wake_signal / entity_recall 等 sys_tool 注入(由 _sys_tool_call 写入,
  //    session_db 单独 tool name 标记)是独立的"事件到达提示",不应被前 assistant 吞掉
  const SYS_TOOLS = new Set(['wake_signal', 'entity_recall', 'mid_session_event', 'sys_nudge'])
  const merged = []
  const msgs = messages.value
  for (let i = 0; i < msgs.length; i++) {
    const m = msgs[i]
    if (m.role === 'assistant' && Array.isArray(m.tool_calls) && m.tool_calls.length) {
      // assistant 带 tool_calls → 合并后续 tool responses (按 tool_call_id 匹配)
      const callIds = new Set(m.tool_calls.map(tc => tc.id || tc.function?.id).filter(Boolean))
      const tools = []
      for (let j = i + 1; j < msgs.length && msgs[j].role === 'tool'; j++) {
        const tj = msgs[j]
        // 该 tool response 必须属于本 assistant 的 tool_calls(用 tool_call_id 精确匹配)
        // 不属于本组的(如 wake_signal 独立注入)立即脱离合并, 不消耗它
        if (tj.tool_call_id && callIds.size && !callIds.has(tj.tool_call_id)) break
        // sys_tool 永不合并
        if (SYS_TOOLS.has(tj.tool_name || '')) break
        tools.push(tj)
        i = j // 跳过已合并的 tool messages
      }
      merged.push({ ...m, tool_responses: tools })
    } else if (m.role === 'tool') {
      // 孤立 tool message 分两类:
      //   (a) sys_tool 注入(wake_signal/entity_recall 等 mid-session 事件) → 独立项保留
      //   (b) assistant.tool_calls 的 response 但错过合并 → 跳过(已在前面组里)
      if (SYS_TOOLS.has(m.tool_name || '')) {
        merged.push(m)
      } else {
        const prev = merged[merged.length - 1]
        if (prev && prev.role === 'assistant' && prev.tool_responses) {
          // 已合并到前组, 跳过
        } else {
          merged.push(m)
        }
      }
    } else {
      merged.push(m)
    }
  }

  // 2. 转 timeline items — 同步给 assistant.tool_calls 消息打上 session 内 call_seq
  //    (按 messages 出现顺序累加；用于前端显示 call #N + 与 llm_payload_dumps 对齐)
  let callCounter = 0
  const msgItems = merged.map((m, i) => {
    const isAssistantCall = m.role === 'assistant' && Array.isArray(m.tool_calls) && m.tool_calls.length > 0
    const callSeq = isAssistantCall ? callCounter++ : -1
    return {
      type: 'message', msg: m, ts: Number(m.ts) || 0, idx: i,
      callSeq,  // -1 表示不是 call(纯 thinking / 纯回复)
      key: 'msg-' + i,
    }
  })
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

// msgDotClass see 30+ lines below (合并版本, mid-session signal 高亮)
// 给当前实例构造 llm_payload_dumps 目录路径 + 该 call 的文件名前缀(点击 chip 复制)
// 文件名约定(向后兼容老格式): {session_id}__wake_{N}__call_{M}__{ts_ms}.json
// 新格式(建议): {session_id}__call_{M}__{ts_ms}.json
// 这里给的是 shell 通配, 让用户/我们用 ls 直接定位
function payloadDumpHint(item) {
  const sid = String(selectedId.value || '')
  const iidStr = String(iid.value || '')
  const callSeq = item?.callSeq ?? -1
  if (!sid || callSeq < 0) return ''
  return `apps/${iidStr.slice(0,8)}…/data/llm_payload_dumps/${sid}__*call_${callSeq}__*.json`
}
function msgLabel(m) {
  // mid-session 注入类 sys_tool 用专属标签
  if (m?.role === 'tool' && ['wake_signal','entity_recall','mid_session_event','sys_nudge'].includes(m?.tool_name)) return 'EVENT'
  return { user:'EVENT', assistant:'AI', tool:'TOOL', system:'SYS' }[m.role||'']||m.role||''
}
function msgDotClass(m) {
  if (m.content && m.content.includes('"error"')) return 'down'
  // mid-session signal 用粉色高亮(显著事件)
  if (m?.role === 'tool' && ['wake_signal','mid_session_event','sys_nudge'].includes(m?.tool_name)) return 'live'
  return m.role === 'assistant' ? 'live' : 'idle'
}
function msgPreview(m) { const c=String(m.content||m.reasoning||'').replace(/\s+/g,' ').trim(); return safeSlice(c,0,100)+(c.length>100?'…':'') }
function toggleMsg(i) {
  if (openMsgs.has(i)) openMsgs.delete(i)
  else openMsgs.add(i)
}
function expandAll() {
  // 只展开 message 项 (injection 自带 details 折叠, 不混在一起)
  for (let i = 0; i < timeline.value.length; i++) {
    if (timeline.value[i].type === 'message') openMsgs.add(i)
  }
}
function collapseAll() { openMsgs.clear() }
// 全展开/全收起切换。allExpanded: 所有 message 项都已展开（injection 项不算）
const allExpanded = computed(() => {
  const msgIndices = timeline.value
    .map((item, i) => item.type === 'message' ? i : -1)
    .filter(i => i >= 0)
  if (!msgIndices.length) return false
  return msgIndices.every(i => openMsgs.has(i))
})
function toggleAllExpand() {
  if (allExpanded.value) collapseAll()
  else expandAll()
}

// 点击事件卡片 → 跳到流 tab 并展开/高亮对应项
// 匹配顺序(从精确到宽松):
//   1) sense_event_detail(event_id=XXX) 工具调用 — 模型"主动消费"事件的瞬间(最精确)
//   2) 时间邻近: events.consumed_at 与 session 第一个 user message 的 ts 接近
//      —— 这是 wake_signal 路径(scheduler.py auto-consume)的判定方式:
//      scheduler 在 session 第一条 user message 写入前后调 consume_event,
//      两者 ts 差几秒。取 |consumed_at - msg.ts| < 60s 且时间最近的 user msg。
//   3) 单事件模板: user message content 含事件正文头(模型第一次看到事件内容)
//   4) 多事件清单: user message content 含 [#eid] marker
// 找不到 → 提示"无对应 call"(不兜底到 idx=0, 那是 sys context)
function revealEvent(ev) {
  if (!ev || !timeline.value.length) return
  const eid = String(ev.event_id || '')
  const evText = String(ev.text || '')
  const evTextHead = evText.slice(0, 40).replace(/\s+/g, ' ').trim()
  // events 表里 consumed_at 是 epoch 字符串("1784638506.66"); messages ts 也是 epoch
  const consumedAt = Number(ev.consumed_at || 0)

  let targetIdx = -1

  // 1) sense_event_detail(event_id=XXX) — 真正消费事件的 call
  if (eid) {
    targetIdx = timeline.value.findIndex(item => {
      if (item.type !== 'message') return false
      const m = item.msg
      // (a) assistant 主动调 sense_event_detail(event_id)
      if (m.role === 'assistant' && Array.isArray(m.tool_calls)) {
        return m.tool_calls.some(tc => {
          const name = tc?.function?.name || tc?.name || ''
          if (!/sense_event_detail/.test(name)) return false
          let args = tc?.function?.arguments || tc?.arguments || ''
          if (typeof args === 'string') {
            try { args = JSON.parse(args) } catch { return false }
          }
          return String(args?.event_id || args?.eid || '') === eid
        })
      }
      // (b) mid-session 注入的 wake_signal/tool 直接含 [#eid] (格式: "[#3073 · ...]")
      if (m.role === 'tool' && ['wake_signal','mid_session_event'].includes(m.tool_name)) {
        const tcid = m.tool_call_id || ''
        if (tcid.includes(`event-${eid}`) || tcid.endsWith(`-${eid}`)) return true
        const c = String(m.content || '')
        // 匹配 [#3073] 或 [#3073 · xxx] (允许 eid 后跟 ] 或 空格)
        const re = new RegExp(`\\[#${eid}[\\]\\s]`)
        if (re.test(c)) return true
      }
      return false
    })
  }

  // 2) 时间邻近匹配 — wake_signal 自动消费场景
  // 选取所有 |user_msg.ts - consumed_at| < 60s 的 user message, 取最近的
  if (targetIdx < 0 && consumedAt > 0) {
    let best = -1
    let bestDelta = Infinity
    timeline.value.forEach((item, i) => {
      if (item.type !== 'message') return
      if (!item.msg || item.msg.role !== 'user') return
      const ts = Number(item.msg.ts || 0)
      if (!ts) return
      const delta = Math.abs(ts - consumedAt)
      if (delta < 60 && delta < bestDelta) {
        bestDelta = delta
        best = i
      }
    })
    if (best >= 0) targetIdx = best
  }

  // 3) 单事件 user message content 含事件正文头
  if (targetIdx < 0 && evTextHead.length >= 6) {
    targetIdx = timeline.value.findIndex(item => {
      if (item.type !== 'message' || item.msg.role !== 'user') return false
      const c = String(item.msg.content || '').replace(/\s+/g, ' ').trim()
      return c.includes(evTextHead)
    })
  }

  // 4) 多事件清单: [#eid] marker — user message (清单格式 - [#3061] 后跟 ])
  if (targetIdx < 0 && eid) {
    const re4 = new RegExp(`\\[#${eid}[\\]\\s]`)
    targetIdx = timeline.value.findIndex(item => {
      if (item.type !== 'message' || item.msg.role !== 'user') return false
      return re4.test(String(item.msg.content || ''))
    })
  }

  if (targetIdx < 0) {
    ElMessage.info('该事件在流中没有对应的 call（可能未被本轮主动消费）')
    return
  }

  // 切 flow tab + 展开 + 滚动 + 高亮
  activeTab.value = 'flow'
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      openMsgs.add(targetIdx)
      const el = document.querySelector(`[data-flow-idx="${targetIdx}"]`)
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' })
        el.classList.add('turn-flash')
        setTimeout(() => el.classList.remove('turn-flash'), 1500)
      }
    })
  })
}
function toolCallClass(tc) { const n=safeToolName(tc); if(n==='express_to_human')return 'tc-msg'; if(n==='rest')return 'tc-rest'; return '' }
// 给 turn 整体加 accent 修饰类(基于第一个 tool_call 类型) —— head 行也染色，扫视时一眼区分特殊 call
function turnAccentClass(m) {
  if (!Array.isArray(m?.tool_calls) || !m.tool_calls.length) return ''
  const n = safeToolName(m.tool_calls[0])
  if (n === 'express_to_human') return 'turn-accent-msg'
  if (n === 'rest') return 'turn-accent-rest'
  return ''
}
// turn-head 里的"动作标签"，与左边框呼应 —— 给"发消息/休息"打显眼 pill
function firstActionTag(m) {
  if (!Array.isArray(m?.tool_calls) || !m.tool_calls.length) return null
  const n = safeToolName(m.tool_calls[0])
  if (n === 'express_to_human') return { text: '发送', cls: 'at-msg' }
  if (n === 'rest') return { text: '休息', cls: 'at-rest' }
  return null
}
function toolCallLabel(tc) { const n=safeToolName(tc); if(n==='express_to_human')return '📤 发送消息'; if(n==='rest')return '😴 休息'; return '⚙ tool_call' }
function toolCallColor(tc) { const n=safeToolName(tc); if(n==='express_to_human')return '#ff9944'; if(n==='rest')return '#7ce07c'; return 'var(--neon-pink)' }
function safeToolName(tc) { return safeSlice((tc&&(tc.function?.name||tc.name))||'tool',0,40) }
function safeToolArgs(tc) { let a=(tc&&(tc.function?.arguments||tc.arguments))||''; if(typeof a!=='string'){try{a=JSON.stringify(a,null,2)}catch{a=String(a)}} return String(a) }
// pretty 化：JSON 串 → 美化输出；非 JSON → 原样返回
// 用于 tool-args 和 tool-result 的渲染，避免压缩 JSON 不换行难以阅读
function prettyToolArgs(tc) {
  let a = (tc && (tc.function?.arguments || tc.arguments)) || ''
  if (typeof a !== 'string') {
    try { return JSON.stringify(a, null, 2) } catch { return String(a) }
  }
  const trimmed = a.trim()
  if ((trimmed.startsWith('{') && trimmed.endsWith('}')) || (trimmed.startsWith('[') && trimmed.endsWith(']'))) {
    try { return JSON.stringify(JSON.parse(trimmed), null, 2) } catch { return a }
  }
  return a
}
function prettyResult(content) {
  const s = String(content || '').slice(0, 4000)
  const trimmed = s.trim()
  if ((trimmed.startsWith('{') && trimmed.endsWith('}')) || (trimmed.startsWith('[') && trimmed.endsWith(']'))) {
    try { return JSON.stringify(JSON.parse(trimmed), null, 2) } catch { return s }
  }
  return s
}

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
  openMsgs.clear()
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
      openMsgs.clear()
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
.sessions-root {
  --topbar-h: 56px;
  --app-main-pad: 32px;
  --app-main-pad-bottom: 32px;
  height: calc(100vh - var(--topbar-h) - var(--app-main-pad) - var(--app-main-pad-bottom));
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.session-layout { display:grid; grid-template-columns:240px 1fr; gap:var(--space-3); height:100%; overflow:hidden; }
.sidebar { display:flex; flex-direction:column; overflow:hidden; background:var(--bg-deep); border-radius:var(--radius); }
.sidebar-head { display:flex; justify-content:space-between; align-items:center; padding:4px 0; flex-shrink:0; }
.sidebar-list { overflow-y:auto; flex:1; display:flex; flex-direction:column; gap:6px; }
.section-label { letter-spacing:0.2em; color:var(--text-muted); }
.wake-card { background:var(--bg-panel); border:1px solid var(--border-line); border-radius:var(--radius); padding:8px 10px; cursor:pointer; transition:all 120ms ease; }
.wake-card:hover { border-color:var(--border-line-strong); }
.wake-card.active { border-color:var(--neon-cyan); background:var(--neon-cyan-soft); }
.wake-card.error { border-color:#ff5577; }
.wake-card-head { display:flex; align-items:center; gap:6px; }
.wake-card-head strong { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.wake-card-head .brand-sub { flex-shrink:0; }
.wake-meta { margin-top:2px; font-size:11px; }
.tag-error { background:rgba(255,85,119,.18); color:#ff5577; padding:0 4px; border-radius:2px; font-size:10px; }
.wake-detail { display:flex; flex-direction:column; overflow:hidden; }
.detail-topbar { flex-shrink:0; padding-bottom:6px; border-bottom:1px solid var(--border-line); }
.detail-topbar-info { display:flex; align-items:center; gap:6px; margin-bottom:6px; flex-wrap:wrap; }
.detail-topbar-info .session-id-chip { font-size:10px; padding:1px 6px; border-radius:3px; background:var(--bg-elevated); color:var(--text-muted); cursor:pointer; transition:all 120ms; }
.detail-topbar-info .session-id-chip:hover { color:var(--neon-cyan); background:var(--neon-cyan-soft); }
.tab-bar { display:flex; gap:4px; align-items:center; }
.tab-bar .tab-spacer { flex:1; }
.tab-bar .tab-action { margin-left:auto; }
.call-seq-chip { display:inline-block; padding:1px 6px; font-size:10px; border-radius:8px; background:rgba(255,85,153,.12); color:var(--neon-pink); cursor:copy; transition:all 120ms; min-width:22px; text-align:center; }
.call-seq-chip:hover { background:var(--neon-pink); color:#fff; }
.tab-btn { display:inline-flex; align-items:center; gap:4px; background:transparent; border:none; color:var(--text-secondary); padding:4px 10px; border-radius:var(--radius); cursor:pointer; font-size:12px; transition:all 120ms; }
.tab-btn:hover { color:var(--text-primary); background:var(--bg-overlay); }
.tab-btn.active { color:var(--neon-cyan); font-weight:600; }
.tab-count { font-size:10px; padding:0 6px; border-radius:8px; background:var(--bg-elevated); color:var(--text-secondary); line-height:14px; min-width:18px; text-align:center; }
.tab-btn.active .tab-count { background:rgba(0,200,255,.15); color:var(--neon-cyan); }
.tab-content { overflow-y:auto; flex:1; padding-top:8px; }
.turns-stream { display:flex; flex-direction:column; gap:4px; }
.turn { background:var(--bg-panel); border:1px solid var(--border-line); border-left:3px solid var(--text-muted); border-radius:var(--radius-sm); overflow:hidden; }
.turn.role-user { border-left-color:var(--neon-cyan); border-left-width:4px; background:rgba(0,200,255,.10); }
.turn.role-user .turn-head { background:rgba(0,200,255,.16); }
.turn.role-assistant { border-left-color:var(--neon-pink); }
.turn.role-tool { border-left-color:var(--text-muted); }
.turn.role-system { border-left-color:#6688ff; background:var(--bg-deep); }
.turn.role-system .turn-head { background:rgba(102,136,255,.06); }
/* mid-session 信号卡(wake_signal) — cyan 醒目边框, 高亮"事件到达"语义 */
.turn.role-signal { border-left-color:var(--neon-cyan); border-left-width:4px; background:rgba(0,200,255,.10); }
.turn.role-signal .turn-head { background:rgba(0,200,255,.16); }
.turn.role-signal .turn-role { color:var(--neon-cyan); letter-spacing:0.1em; }
.turn-head { display:flex; align-items:center; gap:6px; padding:6px 10px; cursor:pointer; font-size:12px; }
.turn-head:hover { background:var(--bg-overlay); }
.status-dot { display:inline-block; width:6px; height:6px; border-radius:50%; }
.status-dot.live { background:var(--neon-cyan); } .status-dot.down { background:#ff5577; } .status-dot.idle { background:var(--text-muted); }
.turn-role { font-size:11px; }
.tool-tag { background:var(--bg-elevated); padding:1px 4px; border-radius:2px; font-size:10px; color:var(--neon-magenta); }
.turn-preview { margin:0 10px 6px; padding:2px 6px; font-size:11px; color:var(--text-muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.turn-reasoning { padding:6px 10px; font-size:11px; border-bottom:1px dashed var(--border-line); }
.turn-reasoning summary { font-size:10px; color:var(--text-muted); }
.turn-reasoning .reasoning-text { margin-top:4px; padding:4px 8px; background:rgba(170,119,255,.06); border-left:2px solid var(--neon-magenta); border-radius:2px; font-size:11px; line-height:1.5; white-space:pre-wrap; word-break:break-word; color:var(--text-secondary); }
.turn-body { padding:8px 14px 10px; font-size:12px; line-height:1.6; }
.turn-body :deep(h1) { font-size:14px; font-weight:700; margin:8px 0 6px; color:var(--text-primary); }
.turn-body :deep(h2) { font-size:13px; font-weight:700; margin:8px 0 4px; color:var(--text-primary); }
.turn-body :deep(h3) { font-size:12px; font-weight:600; margin:6px 0 3px; color:var(--text-primary); }
.turn-body :deep(p) { margin:4px 0; }
.turn-body :deep(ul), .turn-body :deep(ol) { margin:4px 0; padding-left:20px; }
.turn-body :deep(li) { margin:2px 0; }
.turn-body :deep(code) { background:var(--bg-elevated); padding:1px 4px; border-radius:2px; font-family:var(--font-mono, monospace); font-size:11px; }
.turn-body :deep(pre) { background:var(--bg-elevated); padding:6px 10px; border-radius:3px; overflow-x:auto; margin:6px 0; }
.turn-body :deep(pre code) { background:transparent; padding:0; }
.turn-body :deep(blockquote) { border-left:2px solid var(--border-line-strong); padding-left:10px; color:var(--text-secondary); margin:6px 0; }
.turn-body :deep(table) { border-collapse:collapse; margin:6px 0; font-size:11px; }
.turn-body :deep(th), .turn-body :deep(td) { border:1px solid var(--border-line); padding:3px 6px; }
.turn-body :deep(hr) { border:0; border-top:1px dashed var(--border-line); margin:8px 0; }
.block-label { font-size:10px; color:var(--text-muted); }
.tool-calls { padding:2px 10px 6px; display:flex; flex-direction:column; gap:4px; }
.tool-call-card { background:var(--bg-deep); border-left:2px solid var(--neon-pink); padding:6px 8px; border-radius:var(--radius-sm); margin-top:3px; }
.tool-call-head { display:flex; align-items:baseline; gap:6px; margin-bottom:3px; }
.tool-call-name { font-size:11px; font-family:var(--font-mono, monospace); }
.tool-call-card.tc-msg { border-left-color:#ff9944; background:rgba(255,153,68,.06); }
.tool-call-card.tc-rest { border-left-color:#7ce07c; background:rgba(124,224,124,.06); }
/* 特殊 call 的整 turn 染色 —— 仅边框颜色，不破坏 bg 结构 */
.turn-accent-msg { border-left-color:#ff9944 !important; }
.turn-accent-rest { border-left-color:#7ce07c !important; }
.turn-accent-msg .turn-head { background:rgba(255,153,68,.05); }
.turn-accent-rest .turn-head { background:rgba(124,224,124,.05); }
.accent-tag { font-size:10px; padding:1px 6px; border-radius:8px; font-weight:600; line-height:14px; }
.accent-tag.at-msg { background:rgba(255,153,68,.18); color:#ff9944; }
.accent-tag.at-rest { background:rgba(124,224,124,.18); color:#7ce07c; }
.tool-args { margin:2px 0 0; padding:0; white-space:pre-wrap; word-break:break-word; overflow-wrap:anywhere; font-size:11px; line-height:1.5; background:transparent; }
.tool-response { margin-top:6px; border-top:1px dashed var(--border-line); padding-top:4px; }
.tool-result { margin:2px 0 0; padding:6px; background:var(--bg-panel); border-radius:3px; white-space:pre-wrap; word-break:break-word; overflow-wrap:anywhere; font-size:11px; line-height:1.5; }
.injection-block { background:var(--bg-panel); border:1px solid var(--border-line); border-left:3px solid #6688ff; border-radius:var(--radius-sm); overflow:hidden; }
.injection-block[open] { background:var(--bg-deep); }
/* mid-session 信号注入(wake_signal) — cyan 显眼边框 + 染色, 一眼可识别"事件到达" */
.injection-block.injection-signal { border-left-color:var(--neon-cyan); border-left-width:4px; background:rgba(0,200,255,.10); }
.injection-block.injection-signal[open] { background:rgba(0,200,255,.12); }
.injection-block.injection-signal .injection-head { background:rgba(0,200,255,.16); }
.injection-block.injection-signal .injection-head .turn-role { color:var(--neon-cyan); letter-spacing:0.1em; }
.injection-block.injection-signal .injection-head .tool-tag { background:rgba(0,200,255,.18); color:var(--neon-cyan); }
.injection-head { display:flex; align-items:center; gap:6px; padding:6px 10px; cursor:pointer; font-size:12px; list-style:none; }
.injection-head::-webkit-details-marker { display:none; }
.injection-head:hover { background:var(--bg-overlay); }
.injection-head .tool-tag { background:rgba(102,136,255,.15); color:#88aaff; }
.inj-content { padding:6px 14px 10px; font-size:11px; color:var(--text-secondary); background:rgba(0,0,0,.15); border-top:1px dashed var(--border-line); }
.inj-content :deep(h1) { font-size:13px; font-weight:700; margin:6px 0 4px; color:var(--text-primary); }
.inj-content :deep(h2) { font-size:12px; font-weight:700; margin:6px 0 3px; color:var(--text-primary); }
.inj-content :deep(h3) { font-size:11px; font-weight:600; margin:4px 0 2px; color:var(--text-primary); }
.inj-content :deep(p) { margin:3px 0; }
.inj-content :deep(ul), .inj-content :deep(ol) { margin:3px 0; padding-left:18px; }
.inj-content :deep(li) { margin:2px 0; }
.inj-content :deep(code) { background:var(--bg-elevated); padding:1px 3px; border-radius:2px; font-size:10px; }
.inj-content :deep(pre) { background:var(--bg-elevated); padding:4px 8px; border-radius:3px; overflow-x:auto; margin:4px 0; }
.inj-content :deep(pre code) { background:transparent; padding:0; }
.inj-content :deep(table) { border-collapse:collapse; margin:4px 0; font-size:10px; }
.inj-content :deep(th), .inj-content :deep(td) { border:1px solid var(--border-line); padding:2px 5px; }
.events-grid { display:flex; flex-direction:column; gap:4px; }
.event-card { background:var(--bg-panel); border:1px solid var(--border-line); border-left:3px solid; border-radius:var(--radius-sm); padding:6px 10px; }
.event-card-clickable { cursor:pointer; transition:all 120ms ease; }
.event-card-clickable:hover { border-color:var(--neon-cyan); background:var(--neon-cyan-soft); transform:translateX(2px); }
@keyframes turn-flash-anim {
  0%   { background:rgba(0,200,255,.25); box-shadow:0 0 0 2px rgba(0,200,255,.5); }
  100% { background:transparent; box-shadow:none; }
}
.turn-flash { animation:turn-flash-anim 1.5s ease-out; }
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
