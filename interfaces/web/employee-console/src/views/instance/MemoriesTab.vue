<template>
  <div>
    <section class="page-hero">
      <div>
        <h1 class="page-title">Memory</h1>
        <p class="page-subtitle">数字生命的记忆沉淀 · 6 类文件 + 实体记忆（人 / 项目 / 概念）</p>
      </div>
      <el-button @click="reloadAll"><el-icon><Refresh /></el-icon></el-button>
    </section>

    <!-- 顶栏 tabs — 当前路由只显示该组的 kinds (持久化 / 短期暂存) -->
    <div class="kind-tabs">
      <button
        v-for="k in visibleKinds"
        :key="k.key"
        class="kind-tab"
        :class="{ active: active === k.key }"
        @click="active = k.key"
      >
        <span class="kind-icon">{{ k.icon }}</span>
        <span>{{ k.label }}</span>
        <span class="kind-count" v-if="counts[k.key] != null">{{ counts[k.key] }}</span>
        <span class="kind-empty" v-else-if="loaded[k.key] === true && counts[k.key] === 0">0</span>
      </button>
    </div>

    <!-- 文件型记忆 (按 ## 标题拆分, 折叠显示) -->
    <template v-if="active !== 'assoc'">
      <div v-if="loading" class="dev-placeholder"><span class="mono">loading…</span></div>
      <div v-else-if="!segments.length" class="dev-placeholder">
        <span class="mono">// 当前 {{ activeLabel }} 为空</span>
      </div>
      <div v-else class="segments-list">
        <!-- 总字数 / 章节统计 -->
        <div class="kind-summary brand-sub">
          {{ activeLabel }} · 共 {{ segments.length }} 段 · {{ totalChars }} 字 · 文件
          <code class="mono">{{ activeMeta.file }}</code>
        </div>

        <!-- 折叠 / 展开切换 -->
        <div class="seg-toolbar">
          <el-button size="small" text @click="collapseAll">全部折叠</el-button>
          <el-button size="small" text @click="expandAll">全部展开</el-button>
          <el-button v-if="visibleDaysCount < totalDaysCount" size="small" text @click="showMoreDays">
            加载更多日期 ({{ visibleDaysCount }}/{{ totalDaysCount }})
          </el-button>
        </div>

        <!-- 按天分组渲染, 每天一个折叠卡 -->
        <template v-for="group in visibleDayGroupsWithLabel" :key="group.dateKey">
          <details class="day-group" :open="group.isRecent">
            <summary>
              <strong class="mono">📅 {{ group.dateLabel }}</strong>
              <span class="brand-sub mono" style="margin-left: 8px;">{{ group.segs.length }} 段 · {{ group.totalChars }} 字</span>
            </summary>
            <details
              v-for="(seg, idx) in group.segs"
              :key="group.dateKey + '-' + idx"
              class="segment-card"
              :open="idx < 3"
            >
              <summary class="segment-head">
                <span class="segment-title mono" v-if="seg.title">{{ seg.title }}</span>
                <span class="segment-title-untagged" v-else>概览</span>
                <span class="brand-sub mono segment-size">{{ seg.body.length }} 字</span>
                <el-button size="small" text @click.prevent.stop="copyText(seg.body)">copy</el-button>
              </summary>
              <div class="segment-body" v-html="renderMarkdown(seg.body)"></div>
            </details>
          </details>
        </template>
      </div>
    </template>

    <!-- 联想:实体记忆视图(消费 /entities) -->
    <template v-else>
      <MemoryAdvisorTab :api-base="`/api/employee/${iid}`" />
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { instanceApi } from '@/api/client'
import { renderMarkdown } from '@/composables/useMarkdown'
import MemoryAdvisorTab from '@/components/MemoryAdvisorTab.vue'

const route = useRoute()
const iid = computed(() => String(route.params.iid || ''))

// 7 类:已退役 GOALS / HIM 删了对应 tab
// 按持久化级别分两组:持久化存储 (人/项目长期沉淀) vs 短期暂存 (工作内存/中间态)
const kinds = [
  // 持久化档案 - 永久保留 / 历史可追溯
  { key: 'consciousness', label: '意识流', icon: '🌀', file: 'CONSCIOUSNESS.md', group: 'persistent' },
  { key: 'consciousness_archive', label: '意识流·归档', icon: '🗃️', file: 'CONSCIOUSNESS.archive.md', group: 'persistent' },
  { key: 'assoc',         label: '实体',   icon: '👤', file: '/entities (人/项目/概念)', group: 'persistent' },
  { key: 'diary',         label: '日记',   icon: '📔', file: 'diary/', group: 'persistent' },
  { key: 'lessons',       label: '教训',   icon: '⚠️', file: 'LESSONS.md (按主题分节)', group: 'persistent' },
  // 短期暂存 - 工作内存 / 等待 self_review 消化 / 每天覆盖
  { key: 'scratchpad',    label: '草稿',   icon: '📝', file: 'SCRATCHPAD.md', group: 'ephemeral' },
  { key: 'context',       label: '上下文', icon: '🔗', file: 'CONTEXT.md', group: 'ephemeral' },
  { key: 'insights',      label: '洞察',   icon: '💡', file: 'INSIGHTS.md', group: 'ephemeral' },
]
// 路由 meta.group 决定显示哪组。两个 route(/memories persistent, /scratchpad ephemeral)
// 共用本组件, 按 meta 滤出对应 kinds。
const routeGroup = computed(() => String(route.meta?.group || 'persistent'))
const persistentKinds = computed(() => kinds.filter(k => k.group === 'persistent'))
const ephemeralKinds = computed(() => kinds.filter(k => k.group === 'ephemeral'))
// 当前页可见 kinds(persistent 或 ephemeral)
const visibleKinds = computed(() => routeGroup.value === 'ephemeral' ? ephemeralKinds.value : persistentKinds.value)
function setDefaultActive() {
  // 进入页时默认选第一个 tab(按路由组)
  active.value = visibleKinds.value[0]?.key || 'consciousness'
}
// 路由切过来时(同一组件复用)重置 active + reload
watch(() => route.meta?.group, () => {
  setDefaultActive()
  // 清缓存避免读到上一组的 segments
  segCache.value = {}
  loaded.value = {}
}, { immediate: true })
const active = ref('consciousness')

const loading = ref(false)
const currentContent = ref('')
const segments = ref([])
const loaded = ref({})  // {kind: bool} 哪些已加载
// 每种 kind 独立的 segments 缓存 —— 切 tab 时先从 cache 拿,空再走网络拉
// 之前 bug:insights 加载过 (loaded.insights=true),切回 context 时 loaded.context
// 也是 true 就跳过 loadMemory,但 segments 还是上一份 (insights 的空),显示为空
const segCache = ref({})

const counts = ref({})
const segRefs = ref([])

const activeLabel = computed(() => kinds.find(k => k.key === active.value)?.label || '—')
const activeMeta = computed(() => kinds.find(k => k.key === active.value) || {})
const totalChars = computed(() => segments.value.reduce((a, s) => a + (s.body || '').length, 0))

function copyText(text) {
  navigator.clipboard.writeText(String(text || '')).then(
    () => ElMessage.success('已复制'),
    () => ElMessage.warning('复制失败'),
  )
}

// 把 ## 标题分段:返回 [{title, body}]
function splitByChapters(content) {
  // 按 ## 切分章节; 对 INSIGHTS / 草稿 这种「一行一条 append 模式」额外按 - [xxx] 切。
  if (!content) return []
  const lines = String(content).split('\n')
  const out = []
  let current = null
  for (const line of lines) {
    if (/^##\s/.test(line)) {
      if (current) out.push(current)
      current = { title: line.replace(/^##\s*/, '').trim(), body: '' }
    } else if (current) {
      current.body += line + '\n'
    } else if (line.trim() && !line.startsWith('# ')) {
      current = { title: '', body: line + '\n' }
    }
  }
  if (current) out.push(current)
  let segs = out.map(s => ({ ...s, body: s.body.replace(/^\n+/, '').replace(/\n+$/, '') }))
    .filter(s => s.title || s.body.trim())
  // INSIGHTS 特殊处理: 若只有 1-2 段但 body 里含很多 "- [kind]" 行,
  // 按每行切成独立段,title 取 kind
  if (segs.length <= 2 && segs.some(s => /^- \[\w+\]/m.test(s.body))) {
    const flat = []
    for (const s of segs) {
      const bulletLines = s.body.split('\n').filter(l => /^- \[\w+\]/.test(l))
      if (bulletLines.length >= 2) {
        for (const bl of bulletLines) {
          const m = bl.match(/^-\s*\[(\w+)\]\s*([^\s]+)\s*(.*)$/)
          const titlePath = m ? `${m[1]} · ${m[2].slice(5, 16)}` : bl.slice(0, 40)
          flat.push({ title: titlePath, body: bl })
        }
      } else {
        flat.push(s)
      }
    }
    segs = flat
  }
  // 给每段抽 dateKey (YYYY-MM-DD) 给前端按天分组用
  for (const s of segs) {
    s.dateKey = extractDateKey(s.title, s.body)
  }
  return segs
}

function extractDateKey(title, body) {
  // 从 title `## 2026-07-15T10:00:00+08:00 [tag]` 或 body 首行 `- [kind] 2026-07-15T...`
  const sample = `${title}\n${body}`
  const m = sample.match(/(\d{4}-\d{2}-\d{2})/)
  return m ? m[1] : '未知日期'
}

// 按天分组 + 懒加载(初始显示最近 3 天)
const dayGroups = computed(() => {
  // 按 dateKey 分组 (segments 是 reverse 后的, 最近段在前)
  const map = new Map()  // dateKey -> {dateKey, segs, totalChars, isRecent}
  for (const s of segments.value) {
    const dk = s.dateKey || '未知日期'
    if (!map.has(dk)) map.set(dk, { dateKey: dk, segs: [], totalChars: 0, isRecent: false })
    const g = map.get(dk)
    g.segs.push(s)
    g.totalChars += (s.body || '').length
  }
  // 倒序(最近在前)+ 标 isRecent(头 2 篇 recent)
  const arr = Array.from(map.values())
  arr.forEach((g, idx) => { g.isRecent = idx < 2 })
  return arr
})
const totalDaysCount = computed(() => dayGroups.value.length)
const maxVisibleDays = ref(3)  // 默认显示最近 3 天, "加载更多" +5
const visibleDaysCount = computed(() => Math.min(maxVisibleDays.value, totalDaysCount.value))
const visibleDayGroups = computed(() => dayGroups.value.slice(0, maxVisibleDays.value))
function showMoreDays() {
  maxVisibleDays.value = Math.min(maxVisibleDays.value + 5, totalDaysCount.value)
}
// 切 tab 时重置 maxVisibleDays
watch(active, () => { maxVisibleDays.value = 3 })

const groupDateLabel = computed(() => {  // unused,保留扩展
  return (dk) => dk
})
const dayGroupsWithLabel = computed(() => {
  // 给每组算 dateLabel (如: "2026-07-15" + weekday)
  const wk = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
  return visibleDayGroups.value.map(g => {
    let label = g.dateKey
    const m = (g.dateKey || '').match(/^(\d{4})-(\d{2})-(\d{2})$/)
    if (m) {
      const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]))
      if (!isNaN(d.getTime())) label = `${g.dateKey} ${wk[d.getDay()] || ''}`
    }
    return { ...g, dateLabel: label }
  })
})
// 重新导出 visibleDayGroups 含 label
const visibleDayGroupsWithLabel = dayGroupsWithLabel

async function loadMemory(kind) {
  // cache hit:segCache[kind] 是 array(可能空 []=空内容文件)→ 直接复用,不延迟
  // ⚠️ 用 kind in segCache.value 判断 key 是否存在(空数组也算缓存),
  //    不能用 truthy([]是 truthy 但 'foo' in 对象判断更准)
  if (kind in segCache.value) {
    segments.value = segCache.value[kind]
    return
  }
  loading.value = true
  // cache miss:先清空,等网络回写
  segments.value = []
  try {
    const d = await instanceApi(iid.value).memories(kind)
    if (d && !d.error) {
      currentContent.value = String(d.content || '')
      const segs = splitByChapters(currentContent.value)
      segments.value = segs
      segCache.value[kind] = segs  // 缓存(可能空 [])
      counts.value = { ...counts.value, [kind]: segs.length }
    } else {
      counts.value = { ...counts.value, [kind]: 0 }
      segCache.value[kind] = []  // 缓存空,下次切回不再走网络
    }
    loaded.value[kind] = true
  } finally {
    loading.value = false
  }
}

function collapseAll() {
  segRefs.value.forEach(r => { if (r) r.removeAttribute('open') })
}
function expandAll() {
  segRefs.value.forEach(r => { if (r) r.setAttribute('open', '') })
}

async function reloadAll() {
  // 强制刷新:清 cache 让下次访问必走网络
  segCache.value = {}
  loaded.value = {}
  // 预加载当前路由组的首个 tab; 实体记忆由 MemoryAdvisorTab 自取, 无需父级处理
  if (active.value === 'assoc') return
  await loadMemory(active.value || visibleKinds.value[0]?.key || 'consciousness')
}

watch(active, (v) => {
  if (v === 'assoc') {
    // 实体记忆内嵌 MemoryAdvisorTab 组件,数据由组件 onMounted 自取,无需父级处理
    return
  }
  // 总是调 loadMemory:
  // - cache hit → segments=cache,瞬切
  // - cache miss → 走网络拉 + 拉完写 cache
  // 不能用 `if (!loaded.value[v])` 跳过 —— 切回时 segments 还是上一个的,内容错配
  loadMemory(v)
})

onMounted(() => {
  reloadAll()
})
</script>

<style scoped>
.kind-tabs {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
  margin-bottom: var(--space-4);
  border-bottom: 1px solid var(--border-line);
  padding-bottom: 8px;
  align-items: center;
}
.tab-group-label {
  font-size: 11px;
  letter-spacing: 0.15em;
  color: var(--text-muted);
  padding: 0 8px 0 4px;
  font-family: var(--font-mono);
  border-left: 1px solid var(--border-line-strong);
  margin-left: 4px;
}
.tab-group-label:first-child {
  border-left: none;
  margin-left: 0;
  padding-left: 0;
}
.kind-tab {
  background: transparent;
  border: 1px solid transparent;
  color: var(--text-secondary);
  padding: 8px 14px;
  border-radius: var(--radius);
  cursor: pointer;
  font-size: 13px;
  display: flex;
  align-items: center;
  gap: 6px;
  transition: all 160ms ease;
}
.kind-tab:hover { color: var(--text-primary); background: var(--bg-overlay); }
.kind-tab.active {
  color: var(--neon-cyan);
  border-color: var(--border-line-strong);
  background: var(--neon-cyan-soft);
  box-shadow: var(--shadow-glow-cyan);
}
/* 短期暂存类 tab: 视觉略低对比, 让用户一眼区分这是过渡性内容 */
.kind-tab-ephemeral {
  opacity: 0.75;
  font-style: italic;
}
.kind-tab-ephemeral:hover { opacity: 1; }
.kind-tab-ephemeral.active { opacity: 1; font-style: normal; }
.kind-icon { font-size: 16px; }
.kind-count {
  background: var(--bg-elevated);
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--neon-cyan);
}
.kind-empty {
  background: var(--bg-elevated);
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-muted);
  opacity: 0.6;
}

.segments-list { display: flex; flex-direction: column; gap: var(--space-3); }
.kind-summary {
  font-size: 12px;
  color: var(--text-muted);
  letter-spacing: 0.04em;
  margin-bottom: var(--space-2);
}
.seg-toolbar {
  display: flex;
  justify-content: flex-end;
  gap: 4px;
  margin-bottom: 4px;
  flex-wrap: wrap;
}

.day-group {
  border: 1px solid var(--border-line-strong);
  border-radius: var(--radius);
  padding: 8px 12px;
  margin-bottom: var(--space-3);
  background: rgba(0, 200, 255, 0.02);
}
.day-group > summary {
  cursor: pointer;
  padding: 4px 0;
  border-bottom: 1px dashed var(--border-line);
  margin-bottom: 6px;
}
.day-group[open] > summary {
  border-bottom-style: solid;
}

.segment-card {
  background: var(--bg-panel);
  border: 1px solid var(--border-line);
  border-left: 3px solid var(--neon-cyan);
  border-radius: var(--radius);
  padding: 0;
  overflow: hidden;
}
.segment-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: var(--bg-deep);
  border-bottom: 1px solid var(--border-divider);
  cursor: pointer;
  list-style: none;
}
.segment-head::-webkit-details-marker { display: none; }
.segment-title {
  flex: 1;
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--neon-cyan);
  letter-spacing: 0.04em;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.segment-title-untagged {
  flex: 1;
  font-family: var(--font-display);
  font-size: 12px;
  color: var(--text-muted);
}
.segment-size {
  color: var(--text-muted);
  font-size: 10px;
  opacity: 0.7;
}
.segment-body {
  padding: 10px 14px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-primary);
}
.segment-body :deep(h1),
.segment-body :deep(h2),
.segment-body :deep(h3) {
  font-family: var(--font-display);
  color: var(--neon-cyan);
  font-size: 14px;
  margin: 6px 0;
}
.segment-body :deep(pre) {
  background: var(--bg-deep);
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  font-family: var(--font-mono);
  font-size: 12px;
  overflow-x: auto;
}
.segment-body :deep(code) {
  background: var(--bg-deep);
  padding: 1px 4px;
  border-radius: var(--radius-sm);
  font-family: var(--font-mono);
  font-size: 12px;
}
.segment-body :deep(ul),
.segment-body :deep(ol) { margin: 6px 0; padding-left: 22px; }
.segment-body :deep(li) { margin: 3px 0; }
</style>
