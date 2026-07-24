<template>
  <div>
    <section class="page-hero">
      <div>
        <h1 class="page-title">Todos</h1>
        <p class="page-subtitle">{{ iid.slice(0,8) }} 数字生命的待办 · 按 project / type / status 分组</p>
      </div>
      <div style="display: flex; gap: 8px;">
        <el-button @click="load"><el-icon><Refresh /></el-icon>刷新</el-button>
        <el-button type="primary" @click="openCreate"><el-icon><Plus /></el-icon>新建</el-button>
      </div>
    </section>

    <!-- 顶部分类筛选 -->
    <div class="filter-row">
      <el-radio-group v-model="filterStatus" @change="filterChange">
        <el-radio-button label="all">全部 ({{ allCount }})</el-radio-button>
        <el-radio-button label="active">进行中 ({{ activeCount }})</el-radio-button>
        <el-radio-button label="done">已完成 ({{ doneCount }})</el-radio-button>
        <el-radio-button label="cancelled">已取消 ({{ cancelledCount }})</el-radio-button>
      </el-radio-group>
    </div>

    <!-- 按 source / project 分组 -->
    <div v-if="loading" class="dev-placeholder"><span class="mono">loading…</span></div>
    <template v-else>
      <div v-for="(group, sourceKey) in groupedTodos" :key="sourceKey" class="group-section">
        <h3 class="group-title">
          {{ sourceLabel(sourceKey) }}
          <span class="brand-sub" style="font-size: 11px; color: var(--text-muted);">
            ({{ group.length }})
          </span>
        </h3>
        <div class="neon-card">
          <div v-for="t in group" :key="t.id" class="todo-row">
            <div
              class="done-dot"
              :class="{ 'is-done': t.status === 'done' || t.status === 'completed', 'is-cancelled': t.status === 'cancelled' }"
              :title="(t.status === 'done' || t.status === 'completed') ? '已完成' : (t.status === 'cancelled' ? '已取消' : '点击标记完成')"
              @click="() => complete(t)"
            >
              <span v-if="t.status === 'done' || t.status === 'completed'" class="done-check">✓</span>
              <span v-else-if="t.status === 'cancelled'" class="cancelled-mark">×</span>
            </div>
            <div style="flex: 1; min-width: 0;">
              <div :class="{ 'todo-done': t.status === 'done' || t.status === 'completed' || t.status === 'cancelled' }">
                {{ t.title || '(无标题)' }}
              </div>
              <div class="brand-sub mono" style="color: var(--text-muted); font-size: 11px;">
                #{{ t.id?.slice(0, 8) }} · {{ t.priority || 'med' }}
                <span v-if="t.type"> · {{ t.type }}</span>
                <span v-if="t.deadline"> · 截止 {{ String(t.deadline).slice(0, 10) }}</span>
                <span v-if="t.assignee_kind === 'human'"> · 人类</span>
              </div>
              <!-- 详情记忆:行内简略 -->
              <div v-if="t.detail" class="todo-detail mono">
                📝 {{ String(t.detail).slice(0, 80) }}<span v-if="t.detail.length > 80">…</span>
              </div>
            </div>
            <el-tag v-if="t.status" size="small" :type="statusTag(t.status)">{{ statusLabel(t.status) }}</el-tag>
            <el-button size="small" plain @click="openEdit(t)">编辑</el-button>
            <el-button size="small" type="danger" plain @click="remove(t)">×</el-button>
          </div>
        </div>
      </div>
      <div v-if="!filteredTodos.length" class="dev-placeholder">
        <span class="mono">// 暂无待办</span>
      </div>
    </template>

    <!-- 新建 dialog（含 detail） -->
    <el-dialog v-model="dlg.open" title="新建待办" width="560px">
      <el-form label-width="80px">
        <el-form-item label="标题" required>
          <el-input v-model="dlg.title" placeholder="任务标题" @keyup.enter="create" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="dlg.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="详情">
          <el-input
            v-model="dlg.detail"
            type="textarea"
            :rows="4"
            placeholder="详情记忆：这条待办的上下文 / 进展 / 卡点。可后续编辑（增删改）。"
          />
        </el-form-item>
        <el-form-item label="优先级">
          <el-select v-model="dlg.priority" style="width: 100%;">
            <el-option label="高" value="high" />
            <el-option label="中" value="medium" />
            <el-option label="低" value="low" />
          </el-select>
        </el-form-item>
        <el-form-item label="来源">
          <el-input v-model="dlg.source" placeholder="personal / project:xxx（可空）" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlg.open = false">取消</el-button>
        <el-button type="primary" :loading="dlg.loading" @click="create">创建</el-button>
      </template>
    </el-dialog>

    <!-- 编辑 dialog（detail 为主,可增删改;其他字段也支持） -->
    <el-dialog v-model="editDlg.open" title="编辑待办" width="560px">
      <el-form label-width="80px">
        <el-form-item label="标题">
          <el-input v-model="editDlg.title" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="editDlg.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="详情">
          <el-input
            v-model="editDlg.detail"
            type="textarea"
            :rows="8"
            placeholder="详情记忆：填入会覆盖现有内容。留空保存 = 清空详情。"
          />
          <div class="brand-sub" style="font-size: 11px; color: var(--text-muted); margin-top: 4px;">
            详情字段 = 整段替换。每次写入以新内容覆盖旧版；想增删改某段,自己读旧文 + 编辑后整体再提交。模型 sense_todos 时会看到。
          </div>
        </el-form-item>
        <el-form-item label="优先级">
          <el-select v-model="editDlg.priority" style="width: 100%;">
            <el-option label="高" value="high" />
            <el-option label="中" value="medium" />
            <el-option label="低" value="low" />
          </el-select>
        </el-form-item>
        <el-form-item label="状态">
          <el-select v-model="editDlg.status" style="width: 100%;">
            <el-option label="构思" value="idea" />
            <el-option label="计划" value="planned" />
            <el-option label="执行" value="in_progress" />
            <el-option label="暂停" value="paused" />
            <el-option label="完成" value="done" />
            <el-option label="取消" value="cancelled" />
          </el-select>
          <div class="brand-sub" style="font-size: 11px; color: var(--text-muted); margin-top: 4px;">
            从「完成」切回「计划」= 重新激活（恢复任务）。
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editDlg.open = false">取消</el-button>
        <el-button type="primary" :loading="editDlg.loading" @click="saveEdit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { Plus, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { instanceApi } from '@/api/client'

const route = useRoute()
const iid = computed(() => String(route.params.iid || ''))
const todos = ref([])
const loading = ref(true)
const filterStatus = ref('all')

const dlg = reactive({
  open: false, loading: false, title: '', description: '', detail: '',
  priority: 'medium', source: '',
})

const editDlg = reactive({
  open: false, loading: false,
  id: '', title: '', description: '', detail: '',
  priority: 'medium', status: 'planned',
})

const allCount = computed(() => todos.value.length)
// 进行中 = 仍在动的（不含 done / cancelled 终态）
const activeCount = computed(() => todos.value.filter(t => !['done', 'completed', 'cancelled'].includes(t.status)).length)
const doneCount = computed(() => todos.value.filter(t => ['done', 'completed'].includes(t.status)).length)
const cancelledCount = computed(() => todos.value.filter(t => t.status === 'cancelled').length)

const filteredTodos = computed(() => {
  if (filterStatus.value === 'active') {
    return todos.value.filter(t => !['done', 'completed', 'cancelled'].includes(t.status))
  }
  if (filterStatus.value === 'done') {
    return todos.value.filter(t => ['done', 'completed'].includes(t.status))
  }
  if (filterStatus.value === 'cancelled') {
    return todos.value.filter(t => t.status === 'cancelled')
  }
  return todos.value
})

// 按 source 分组（personal / project:xxx / null）
const groupedTodos = computed(() => {
  const groups = {}
  for (const t of filteredTodos.value) {
    const key = t.source || 'personal'
    if (!groups[key]) groups[key] = []
    groups[key].push(t)
  }
  return groups
})

function sourceLabel(source) {
  if (!source || source === 'personal') return '📋 Personal'
  if (source.startsWith('project:')) {
    const pid = source.slice('project:'.length)
    return `📁 Project ${shortId(pid, 12)}`
  }
  return `📁 ${source}`
}

function shortId(value, n) {
  try { return String(value || '').slice(0, n) } catch { return String(value) }
}

function statusLabel(s) {
  return {
    planned: '计划',
    in_progress: '执行',
    idea: '构思',
    done: '完成',
    completed: '完成',
    cancelled: '取消',
    pending: '待定',
  }[String(s)] || s
}
function statusTag(s) {
  return {
    planned: 'info',
    in_progress: 'warning',
    idea: 'info',
    done: 'success',
    completed: 'success',
    cancelled: 'danger',
    pending: '',
  }[String(s)] || ''
}

async function load() {
  loading.value = true
  try {
    const d = await instanceApi(iid.value).todos()
    if (!d.error) todos.value = d.todos || []
  } finally { loading.value = false }
}

function filterChange() {}

function openCreate() {
  dlg.open = true
  dlg.title = ''
  dlg.description = ''
  dlg.detail = ''
  dlg.priority = 'medium'
  dlg.source = ''
}

async function create() {
  if (!dlg.title) return ElMessage.error('请填标题')
  dlg.loading = true
  try {
    const body = {
      title: dlg.title,
      description: dlg.description,
      detail: dlg.detail,
      priority: dlg.priority,
    }
    if (dlg.source) body.source = dlg.source
    const d = await instanceApi(iid.value).createTodo(body)
    if (d.error) {
      ElMessage.error(d.error)
      return
    }
    dlg.open = false
    await load()
  } finally { dlg.loading = false }
}

function openEdit(t) {
  editDlg.open = true
  editDlg.id = t.id
  editDlg.title = t.title || ''
  editDlg.description = t.description || ''
  editDlg.detail = t.detail || ''
  editDlg.priority = t.priority || 'medium'
  editDlg.status = t.status || 'planned'
}

async function saveEdit() {
  if (!editDlg.id) return
  editDlg.loading = true
  try {
    // detail 显式传(即使 '' 也要发,覆盖语义)
    const body = {
      title: editDlg.title,
      description: editDlg.description,
      detail: editDlg.detail,
      priority: editDlg.priority,
      status: editDlg.status,
    }
    const d = await instanceApi(iid.value).updateTodo(editDlg.id, body)
    if (d.error) {
      ElMessage.error(d.error)
      return
    }
    ElMessage.success('已保存')
    editDlg.open = false
    await load()
  } finally { editDlg.loading = false }
}

// 状态点点击 = 单向标记完成。done/cancelled 状态不响应点击（复活走编辑按钮）。
async function complete(t) {
  const doneSet = ['done', 'completed', 'cancelled']
  if (doneSet.includes(t.status)) return  // 终态不可反向点
  try {
    const d = await instanceApi(iid.value).updateTodo(t.id, { status: 'done' })
    if (d.error) {
      ElMessage.error(`标记完成失败：${d.error}`)
      return
    }
    t.status = 'done'
  } catch (e) {
    ElMessage.error(String(e.message || e))
  }
}

async function remove(t) {
  const d = await instanceApi(iid.value).deleteTodo(t.id)
  if (d.error) return ElMessage.error(`删除失败：${d.error}`)
  todos.value = todos.value.filter(x => x.id !== t.id)
}

onMounted(load)
</script>

<style scoped>
.filter-row { margin-bottom: var(--space-4); }
.todo-row {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 10px 0;
  border-bottom: 1px solid var(--border-divider);
}
.todo-row:last-child { border: none; }
.todo-done { text-decoration: line-through; opacity: 0.5; }

/* 状态点（○ → ✓ 发亮）。单向语义:dodo/cancelled 不可反向点击 */
.done-dot {
  flex-shrink: 0;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  border: 1.5px solid var(--text-muted);
  background: transparent;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-top: 2px;
  transition: all 0.2s ease;
}
.done-dot:hover {
  border-color: var(--neon-cyan);
  box-shadow: 0 0 6px color-mix(in oklab, var(--neon-cyan) 35%, transparent);
}
.done-dot.is-done {
  background: color-mix(in oklab, var(--neon-cyan) 18%, var(--bg-elevated));
  border-color: var(--neon-cyan);
  cursor: default;  /* 终态,不可反向点 */
  box-shadow: 0 0 8px color-mix(in oklab, var(--neon-cyan) 50%, transparent);
}
.done-dot.is-cancelled {
  background: color-mix(in oklab, var(--neon-red) 12%, var(--bg-elevated));
  border-color: var(--neon-red);
  cursor: default;
  opacity: 0.7;
}
.done-check {
  color: var(--neon-cyan);
  font-size: 12px;
  line-height: 1;
  font-weight: bold;
}
.cancelled-mark {
  color: var(--neon-red);
  font-size: 14px;
  line-height: 1;
  font-weight: bold;
}

.todo-detail {
  margin-top: 4px;
  font-size: 11px;
  color: var(--neon-cyan);
  opacity: 0.85;
  white-space: pre-wrap;
  word-break: break-word;
}
.group-section { margin-bottom: var(--space-5); }
.group-title {
  font-family: var(--font-display);
  font-size: 14px;
  color: var(--neon-cyan);
  letter-spacing: 0.05em;
  margin: 0 0 var(--space-2);
}
</style>
