<template>
  <div>
    <section class="page-hero">
      <div>
        <h1 class="page-title">Memory</h1>
        <p class="page-subtitle">数字生命的记忆沉淀 · 6 类文件 + 实体记忆（人 / 项目 / 概念）</p>
      </div>
      <el-button @click="reloadAll"><el-icon><Refresh /></el-icon></el-button>
    </section>

    <!-- 两级导航:
         一级 = 组 (持久化经历 / 短期暂存 / 切片认知)
         二级 = 当前组内的 tab (持久化经历 4 个 / 短期暂存 3 个)
         切片认知组只有切片一个内容, 不显示二级 tab 直接进入 -->
    <div class="group-nav">
      <button
        v-for="g in groups"
        :key="g.key"
        class="group-tab"
        :class="{ active: activeGroup === g.key }"
        @click="switchGroup(g.key)"
      >{{ g.label }}</button>
    </div>
    <div v-if="activeGroup !== 'chunks'" class="kind-tabs">
      <button
        v-for="k in currentGroupKinds"
        :key="k.key"
        class="kind-tab"
        :class="[{ active: active === k.key }, k.section === 'ephemeral' ? 'kind-tab-ephemeral' : '']"
        @click="active = k.key"
      >
        <span class="kind-icon">{{ k.icon }}</span>
        <span>{{ k.label }}</span>
        <span class="kind-count" :class="{ 'kind-count-zero': !tabCount(k.key) }">{{ tabCount(k.key) }}</span>
      </button>
    </div>

    <!-- 文件型记忆 (按 ## 标题拆分, 折叠显示) -->
    <template v-if="active !== 'assoc' && active !== 'chunks'">
      <div v-if="loading && !segments.length" class="dev-placeholder"><span class="mono">loading…</span></div>
      <div v-else-if="!segments.length" class="dev-placeholder">
        <span class="brand-sub mono" style="color: var(--text-muted);">(空)</span>
      </div>
      <div v-else class="segments-list">
        <!-- 工具栏: 段/字数 + 仅在段数较多时显示折叠/展开 + 仍有日期未展示时显示加载更多 -->
        <div class="seg-toolbar">
          <span class="brand-sub mono" style="color: var(--text-muted); font-size: 11px;">
            {{ segments.length }} 段 · {{ totalChars }} 字
          </span>
          <span style="flex:1"></span>
          <template v-if="segments.length > 5">
            <el-button size="small" text @click="collapseAll">全部折叠</el-button>
            <el-button size="small" text @click="expandAll">全部展开</el-button>
          </template>
          <el-button v-if="totalDaysCount > 0 && visibleDaysCount < totalDaysCount" size="small" text @click="showMoreDays">
            加载更多日期 ({{ visibleDaysCount }}/{{ totalDaysCount }})
          </el-button>
        </div>

        <!-- 按天分组渲染, 每天一个折叠卡 (day 卡默认收起, 段也默认收起) -->
        <template v-for="group in visibleDayGroupsWithLabel" :key="group.dateKey">
          <details class="day-group">
            <summary>
              <strong class="mono">📅 {{ group.dateLabel }}</strong>
              <span class="brand-sub mono" style="margin-left: 8px;">{{ group.segs.length }} 段 · {{ group.totalChars }} 字</span>
            </summary>
            <details
              v-for="(seg, idx) in group.segs"
              :key="group.dateKey + '-' + idx"
              class="segment-card"
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

    <!-- 实体联想:实体记忆视图 -->
    <template v-else-if="active === 'assoc'">
      <MemoryAdvisorTab :api-base="`/api/employee/${iid}`" />
    </template>

    <!-- 认知层切片:嵌入式引用 ChunksTab -->
    <template v-else-if="active === 'chunks'">
      <ChunksTab />
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
import ChunksTab from '@/views/instance/ChunksTab.vue'

const route = useRoute()
const iid = computed(() => String(route.params.iid || ''))

// 7 类:已退役 GOALS / HIM 删了对应 tab
// 按持久化级别分两组:持久化存储 (人/项目长期沉淀) vs 短期暂存 (工作内存/中间态)
// 持久化: 按使用频率排序 (意识流最高频)
// 历史上"意识流·归档"已删除 —— 意识流文件本身足够且按天分组能浏览更早日期
const kinds = [
  // 第一组 持久化经历 - 原始写入的记忆 + 手工维护的实体联想
  { key: 'consciousness', label: '意识流',    icon: '🌀', file: 'CONSCIOUSNESS.md', section: 'persistent' },
  { key: 'lessons',       label: '教训',      icon: '⚠️', file: 'LESSONS.md', section: 'persistent' },
  { key: 'assoc',         label: '实体联想',  icon: '👤', file: '/entities', section: 'persistent' },
  { key: 'diary',         label: '日记',      icon: '📔', file: 'diary/', section: 'persistent' },
  // 第二组 短期暂存 - 工作内存 / 等待 self_review 消化 / 每天覆盖
  { key: 'insights',      label: '洞察',      icon: '💡', file: 'INSIGHTS.md', section: 'ephemeral' },
  { key: 'scratchpad',    label: '草稿',      icon: '📝', file: 'SCRATCHPAD.md', section: 'ephemeral' },
  { key: 'context',       label: '上下文',    icon: '🔗', file: 'CONTEXT.md', section: 'ephemeral' },
]
const persistentKinds = computed(() => kinds.filter(k => k.section === 'persistent'))
const ephemeralKinds  = computed(() => kinds.filter(k => k.section === 'ephemeral'))
// groups 一级组的展示顺序 + label
// 三组: 持久化经历(含原料+实体手工维护) / 短期暂存 / 切片认知(自动建索引)
const groups = [
  { key: 'persistent', label: '持久化经历' },
  { key: 'ephemeral',  label: '短期暂存' },
  { key: 'chunks',     label: '切片认知' },
]
// active 跟随 activeGroup 自动调整: 切组 → 跳到该组第一个 kind
// 默认组 = persistent, 默认 tab = consciousness (意识流)
const active = ref('consciousness')
const activeGroup = ref('persistent')
const currentGroupKinds = computed(() => {
  if (activeGroup.value === 'chunks') {
    // 切片认知组: 只有切片一个 tab (独立组件, 不在 kinds 数组里)
    return [{ key: 'chunks', label: '切片', icon: '📊', section: 'chunks' }]
  }
  return kinds.filter(k => k.section === activeGroup.value)
})
function switchGroup(groupKey) {
  if (groupKey === activeGroup.value) return
  activeGroup.value = groupKey
  // 切组自动跳到该组第一个 tab
  if (groupKey === 'chunks') {
    active.value = 'chunks'
  } else {
    const first = kinds.find(k => k.section === groupKey)
    if (first) active.value = first.key
  }
}

const loading = ref(true)
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
  // 倒序: 文件通常是正序写入(旧的在前), 但 UI 展示应该最新的在最上面
  // 让用户进入意识流先看到最近的写入, 不必滚到底
  segs.reverse()
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
  // 按 dateKey 分组
  const map = new Map()  // dateKey -> {dateKey, segs, totalChars, isRecent}
  for (const s of segments.value) {
    const dk = s.dateKey || '未知日期'
    if (!map.has(dk)) map.set(dk, { dateKey: dk, segs: [], totalChars: 0, isRecent: false })
    const g = map.get(dk)
    g.segs.push(s)
    g.totalChars += (s.body || '').length
  }
  // 按 dateKey DESC 排序(最新日期在前), 不依赖上游 segments 顺序
  // "未知日期" 因为字符串比较最大, 会浮到最前; 压到最后避免遮蔽正常日期
  const arr = Array.from(map.values()).sort((a, b) => {
    const ka = a.dateKey === '未知日期' ? '' : a.dateKey
    const kb = b.dateKey === '未知日期' ? '' : b.dateKey
    return kb.localeCompare(ka)
  })
  arr.forEach((g, idx) => { g.isRecent = idx < 2 })
  return arr
})
const totalDaysCount = computed(() => dayGroups.value.length)
// 默认展示近 30 天, 全部 collapsed 状态. 用户进入 -> 看到所有日期 (折叠) -> 按需展开
// 不再用 +5 step, 老 UX 让人只看到 "最近 3 天" 就要点加载, 不够方便
const maxVisibleDays = ref(30)
const visibleDaysCount = computed(() => Math.min(maxVisibleDays.value, totalDaysCount.value))
const visibleDayGroups = computed(() => dayGroups.value.slice(0, maxVisibleDays.value))
function showMoreDays() {
  maxVisibleDays.value = Math.min(maxVisibleDays.value + 30, totalDaysCount.value)
}
// 切 tab 时重置到近 30 天
watch(active, () => { maxVisibleDays.value = 30 })

// 给每组算 dateLabel (如: "2026-07-15" + weekday)
const visibleDayGroupsWithLabel = computed(() => {
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
  counts.value = {}
  // 预拉所有 kind 的 count —— 避免 nav 数字徽章点击后才出现, 消除宽度跳变
  // 实体联想(assoc) 不走 loadMemory, 单独走 /entities API 拿 count
  // chunks 自带 count 在 ChunksTab 内部 loadChunks, 父级不预拉
  const fileKinds = kinds.filter(k => k.key !== 'assoc').map(k => k.key)
  const promises = [
    ...fileKinds.map(k => loadMemory(k).catch(() => {})),
    // 实体联想 count: 调 /entities 拿 total 字段(返回 capped 至 200 条, total 是真实计数)
    (async () => {
      try {
        const r = await fetch(`/api/employee/${iid.value}/entities`)
        const d = await r.json().catch(() => ({}))
        counts.value = { ...counts.value, assoc: d.total ?? (d.entities || []).length ?? 0 }
      } catch {}
    })(),
  ]
  await Promise.all(promises)
  // 再把当前 active 拉到位 (preload 已 cache 命中)
  if (active.value === 'assoc' || active.value === 'chunks') return
  await loadMemory(active.value || 'consciousness')
}
// nav badge 可以稳定显示的 count helper
function tabCount(key) {
  return counts.value[key] ?? 0
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
/* 两级导航: 顶行组别(大), 第二行组内 tab(小) */
.group-nav {
  display: flex;
  gap: 4px;
  align-items: center;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--border-line);
  margin-bottom: 6px;
}
.group-tab {
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  color: var(--text-muted);
  padding: 4px 12px 6px;
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.05em;
  cursor: pointer;
  transition: all 120ms;
  margin-bottom: -1px;
}
.group-tab:hover { color: var(--text-primary); }
.group-tab.active {
  color: var(--text-primary);
  border-bottom-color: var(--neon-cyan);
}
/* 二级 tab (组内) */
.kind-tabs {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  align-items: center;
  margin-bottom: var(--space-4);
}
.kind-tabs-divider {
  width: 1px;
  height: 18px;
  background: var(--border-line-strong);
  margin: 0 6px;
}
.kind-tab {
  background: transparent;
  border: 1px solid transparent;
  color: var(--text-secondary);
  padding: 5px 12px;
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
  min-width: 18px;
  text-align: center;
  display: inline-block;
}
/* 数字 0/未加载时用 muted, 不抢视觉 (避免暂存类 "0 0 0" 太吵) */
.kind-count.kind-count-zero {
  color: var(--text-muted);
}
.kind-empty {
  display: none;
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
