<template>
  <div>
    <section class="page-hero">
      <h1 class="page-title">Persona · Prompts</h1>
      <p class="page-subtitle">人设核心(LIFE_PERSONA) + 系统提示词覆盖(L4_LIFECYCLE_PROMPT 等)</p>
    </section>

    <div class="persona-layout">
      <!-- 左 list -->
      <aside class="prompt-list">
        <div class="brand-sub section-label">PROMPTS ({{ prompts.length }})</div>
        <button
          v-for="p in prompts"
          :key="p.key || p.name"
          class="prompt-card"
          :class="{ active: isSelected(p) }"
          @click="selectPrompt(p)"
        >
          <div class="prompt-card-head">
            <strong>{{ p.key || p.name }}</strong>
            <el-tag size="small" effect="plain" :type="p.is_default ? 'info' : 'success'">
              {{ p.is_default ? '内置' : '已覆盖' }}
            </el-tag>
          </div>
          <div class="brand-sub mono prompt-card-meta">
            {{ p.layer || '系统 Prompt' }}
          </div>
          <div class="brand-sub mono prompt-card-meta" v-if="p.file">
            📄 {{ safeSlice(p.file, -50) }}
          </div>
        </button>
        <div v-if="!prompts.length" class="dev-placeholder">
          <span class="mono">// 暂无 prompts</span>
        </div>
      </aside>

      <!-- 右 editor -->
      <main class="prompt-editor">
        <div v-if="!selectedPrompt" class="dev-placeholder">
          <strong>// SELECT A PROMPT</strong>
          <span>左侧任选一条 prompt 编辑</span>
        </div>
        <template v-else>
          <div class="editor-head">
            <div>
              <h2 class="page-title" style="font-size: 18px;">
                {{ selectedPrompt.key || selectedPrompt.name }}
              </h2>
              <div class="brand-sub mono">
                {{ selectedPrompt.layer || '系统 Prompt' }}
                <span v-if="selectedPrompt.trigger"> · {{ selectedPrompt.trigger }}</span>
              </div>
            </div>
            <div class="tag-row">
              <el-button size="small" @click="resetPrompt" title="还原成内置默认值">
                <el-icon><RefreshLeft /></el-icon> 还原默认
              </el-button>
              <el-button type="primary" :loading="saving" @click="save">
                <el-icon><Check /></el-icon> 保存
              </el-button>
            </div>
          </div>

          <!-- 差异提示(只对 override 路径有效) -->
          <div v-if="selectedPrompt.original && selectedPrompt.overridden" class="diff-banner mono">
            ⓘ <strong>已覆盖内置默认值</strong>。内置版本可点"还原默认"恢复;<br>
            保存后下次 wake 时通过 prompts_override 注入。
          </div>

          <!-- 写入路径提示(Q3: 让用户看到保存到了哪) -->
          <div v-if="savedFile" class="file-banner mono">
            ✅ 上次保存到: <code>{{ savedFile }}</code>
            <span v-if="savedAt"> · {{ savedAt }}</span>
            <div class="brand-sub" style="margin-top: 4px;">
              下次 wake 时此处读取生效。如需立即生效:
              <code>digital-life restart</code>
            </div>
          </div>

          <el-input
            v-model="editingText"
            type="textarea"
            :rows="22"
            :placeholder="`编辑 ${selectedPrompt.key || selectedPrompt.name}...`"
            class="prompt-textarea"
          />
          <div class="brand-sub mono" style="margin-top: 6px; color: var(--text-muted); font-size: 11px;">
            {{ (editingText || '').length }} 字符 · 保存后下次唤醒生效
          </div>
        </template>
      </main>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { RefreshLeft, Check } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { instanceApi } from '@/api/client'
import { safeSlice } from '@/composables/useFormat'

const route = useRoute()
const iid = computed(() => String(route.params.iid || ''))
const prompts = ref([])
const selectedKey = ref(null)       // 选中的 prompt key
const editingText = ref('')         // 编辑框内容
const saving = ref(false)
const savedFile = ref('')           // 上次保存写的 file path(Q3)
const savedAt = ref('')             // 上次保存时间

const selectedPrompt = computed(() =>
  prompts.value.find(p => (p.key || p.name) === selectedKey.value)
)

function isSelected(p) {
  return (p.key || p.name) === selectedKey.value
}

function selectPrompt(p) {
  selectedKey.value = p.key || p.name
  editingText.value = String(p.content || '')
  savedFile.value = p.file || ''
  savedAt.value = ''
}

async function reload() {
  const d = await instanceApi(iid.value).prompts()
  if (d.error) return
  prompts.value = d.prompts || []
  // 默认选 LIFE_PERSONA(若没则第一条)
  if (!selectedKey.value) {
    const life = prompts.value.find(p => (p.key || p.name) === 'LIFE_PERSONA')
    if (life) selectPrompt(life)
    else if (prompts.value[0]) selectPrompt(prompts.value[0])
  } else {
    // 已选中: 刷新 content 保持编辑框与后端一致(若用户没改则覆盖, 若改了也以最新后端为准,这是 "reload" 语义)
    const cur = prompts.value.find(p => (p.key || p.name) === selectedKey.value)
    if (cur) editingText.value = String(cur.content || '')
  }
}

async function save() {
  if (!selectedPrompt.value) return
  saving.value = true
  try {
    const name = selectedPrompt.value.key || selectedPrompt.value.name
    const d = await instanceApi(iid.value).updatePrompt(name, { content: editingText.value })
    if (d.error) return ElMessage.error(d.error)
    // Q3: 显示后端返回的 file 路径, 让用户能定位写入位置
    savedFile.value = d.file || '(写入了 prompts_override 配置)'
    savedAt.value = new Date().toLocaleTimeString()
    ElMessage.success(`已保存 ${name} · 下次 wake 生效`)
    await reload()
  } finally { saving.value = false }
}

async function resetPrompt() {
  if (!selectedPrompt.value) return
  if (!selectedPrompt.value.original) {
    ElMessage.info('此 prompt 无内置默认值, 无法还原')
    return
  }
  // 把编辑框还原成 original (用户可再点保存写回; 不强制保存)
  editingText.value = String(selectedPrompt.value.original || '')
  ElMessage.info('已还原内置默认值(需点保存才生效)')
}

onMounted(reload)
</script>

<style scoped>
.persona-layout {
  display: grid;
  grid-template-columns: 300px 1fr;
  gap: var(--space-4);
  min-height: 60vh;
}
.prompt-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.section-label {
  letter-spacing: 0.2em;
  color: var(--text-muted);
  margin-bottom: 4px;
}
.prompt-card {
  background: var(--bg-panel);
  border: 1px solid var(--border-line);
  border-radius: var(--radius);
  padding: 10px 12px;
  cursor: pointer;
  transition: all 160ms ease;
  text-align: left;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.prompt-card:hover { border-color: var(--border-line-strong); }
.prompt-card.active {
  border-color: var(--neon-cyan);
  background: var(--neon-cyan-soft);
  box-shadow: var(--shadow-glow-cyan);
}
.prompt-card-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.prompt-card-meta {
  font-size: 11px;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.prompt-editor {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.editor-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  padding-bottom: var(--space-3);
  border-bottom: 1px solid var(--border-line);
}
.diff-banner {
  background: rgba(255, 165, 0, 0.08);
  border-left: 3px solid #ffa500;
  padding: 8px 12px;
  border-radius: 4px;
  font-size: 12px;
  color: #ffcc66;
  line-height: 1.6;
}
.file-banner {
  background: rgba(0, 200, 100, 0.08);
  border-left: 3px solid #00c864;
  padding: 8px 12px;
  border-radius: 4px;
  font-size: 12px;
  color: #66ddaa;
  word-break: break-all;
}
.prompt-textarea :deep(.el-textarea__inner) {
  font-family: var(--font-mono);
  font-size: 13px;
  line-height: 1.6;
}
</style>
