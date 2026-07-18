<template>
  <div>
    <section class="page-hero">
      <div>
        <h1 class="page-title">Memory</h1>
        <p class="page-subtitle">数字生命的记忆沉淀 · 6 类文件 + 实体记忆（人 / 项目 / 概念）</p>
      </div>
      <el-button @click="reloadAll"><el-icon><Refresh /></el-icon></el-button>
    </section>

    <!-- 顶栏 tabs — 分两组:持久化 / 短期暂存 -->
    <div class="kind-tabs">
      <span class="tab-group-label">📦 持久化</span>
      <button
        v-for="k in persistentKinds"
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

      <span class="tab-group-label">⌛ 短期暂存</span>
      <button
        v-for="k in ephemeralKinds"
        :key="k.key"
        class="kind-tab kind-tab-ephemeral"
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
        </div>

        <details
          v-for="(seg, idx) in segments"
          :key="idx"
          class="segment-card"
          :open="idx < 10"
          :ref="el => segRefs[idx] = el"
        >
          <summary class="segment-head">
            <span class="segment-title mono" v-if="seg.title">{{ seg.title }}</span>
            <span class="segment-title-untagged" v-else>概览</span>
            <span class="brand-sub mono segment-size">{{ seg.body.length }} 字</span>
            <el-button size="small" text @click.prevent.stop="copyText(seg.body)">copy</el-button>
          </summary>
          <div class="segment-body" v-html="renderMarkdown(seg.body)"></div>
        </details>
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
  { key: 'assoc',         label: '实体',   icon: '👤', file: '/entities (人/项目/概念)', group: 'persistent' },
  { key: 'diary',         label: '日记',   icon: '📔', file: 'diary/', group: 'persistent' },
  { key: 'lessons',       label: '教训',   icon: '⚠️', file: 'LESSONS.md (按主题分节)', group: 'persistent' },
  // 短期暂存 - 工作内存 / 等待 self_review 消化 / 每天覆盖
  { key: 'scratchpad',    label: '草稿',   icon: '📝', file: 'SCRATCHPAD.md', group: 'ephemeral' },
  { key: 'context',       label: '上下文', icon: '🔗', file: 'CONTEXT.md', group: 'ephemeral' },
  { key: 'insights',      label: '洞察',   icon: '💡', file: 'INSIGHTS.md', group: 'ephemeral' },
]
const persistentKinds = computed(() => kinds.filter(k => k.group === 'persistent'))
const ephemeralKinds = computed(() => kinds.filter(k => k.group === 'ephemeral'))
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
  // 是否所有内容都是"一行记录"模式(所有行匹配 - [...] 或 - xxxts)
  // INSIGHTS.md 这种:append 只有两段(header + 一大块 - [kind] ts text)
  // 把这种一条行单独切成段让折叠/展开更精准
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
        // 切成独立段
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
  return segs.reverse()
}

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
  // 预加载 consciousness(默认 tab);实体记忆由 MemoryAdvisorTab 自行拉取
  await loadMemory('consciousness')
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
