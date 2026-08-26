<template>
  <div>
    <section class="page-hero">
      <div>
        <h1 class="page-title">Social Relations</h1>
        <p class="page-subtitle">联系人管理 · 编辑 / 合并 / 拉黑</p>
      </div>
      <div style="display: flex; gap: 8px;">
        <el-input v-model="searchText" placeholder="搜索姓名/备注/ID" size="small" clearable
          style="width: 200px;" />
        <el-radio-group v-model="filter" size="small">
          <el-radio-button label="all">全部</el-radio-button>
          <el-radio-button label="named">已命名</el-radio-button>
          <el-radio-button label="stub">未命名</el-radio-button>
          <el-radio-button label="blocked">黑名单</el-radio-button>
        </el-radio-group>
        <el-button type="primary" size="small" @click="openCreate">+ 新增联系人</el-button>
      </div>
    </section>

    <el-table :data="pagedFilteredContacts" style="width: 100%;" v-loading="loading"
      @selection-change="onSelectionChange">
      <el-table-column type="selection" width="40" />
      <el-table-column label="姓名" min-width="140">
        <template #default="{ row }">
          <div style="display: flex; align-items: center; gap: 6px;">
            <strong :class="{ 'stub-name': !row.name }">{{ row.name || '(未命名)' }}</strong>
            <el-tag v-if="row.kind === 'bot'" size="small" type="warning">bot</el-tag>
            <el-tag v-else-if="row.kind === 'system'" size="small" type="info">system</el-tag>
            <el-tag v-else-if="row.kind === 'group'" size="small" type="primary">群</el-tag>
            <el-tag v-if="row.blocked" size="small" type="danger">blocked</el-tag>
          </div>
          <div class="brand-sub mono" style="color: var(--text-muted); font-size: 10px;">
            {{ row.id.slice(0, 8) }}
          </div>
        </template>
      </el-table-column>
      <el-table-column label="平台 ID" min-width="200">
        <template #default="{ row }">
          <div v-for="pid in (row.platform_ids || [])" :key="pid.platform + pid.platform_id"
            style="font-size: 11px; line-height: 1.6;">
            <span class="mono" style="color: var(--neon-cyan);">{{ pid.platform }}</span>
            <span style="color: var(--text-muted); margin: 0 4px;">·</span>
            <span class="mono">{{ shortText(pid.platform_id, 20) }}</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="备注" min-width="160">
        <template #default="{ row }">
          <span v-if="row.blocked && row.block_reason" style="color: var(--neon-pink); font-size: 12px;">
            拉黑: {{ row.block_reason }}
          </span>
          <span v-else style="color: var(--text-secondary); font-size: 12px;">{{ row.notes || '—' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="最近消息" min-width="280">
        <template #default="{ row }">
          <div v-if="row.last_message" style="display:flex; flex-direction:column; gap:3px;">
            <div style="display:flex; align-items:center; gap:6px;">
              <el-tag v-if="row.chat_kind" size="small" :type="row.chat_kind === 'dm' ? 'success' : 'info'">
                {{ row.chat_kind === 'dm' ? '私聊' : row.chat_kind === 'group' ? '群' : '会话' }}
              </el-tag>
              <span class="brand-sub mono" style="color: var(--text-muted); font-size: 10px;">
                {{ relTime(row.last_ts) }}
              </span>
            </div>
            <div class="brand-sub mono" style="color: var(--text-secondary); font-size: 11px; line-height: 1.4;">
              {{ shortText(row.last_message, 80) }}
            </div>
          </div>
          <span v-else class="brand-sub" style="color: var(--text-muted);">—</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="240" fixed="right">
        <template #default="{ row }">
          <el-button size="small" text @click="openEdit(row)">编辑</el-button>
          <el-button size="small" text :type="row.blocked ? 'success' : 'danger'"
            @click="toggleBlock(row)">{{ row.blocked ? '恢复' : '拉黑' }}</el-button>
          <el-popconfirm title="确认删除？删除后不可恢复。" @confirm="removeContact(row)">
            <template #reference>
              <el-button size="small" text type="danger">删除</el-button>
            </template>
          </el-popconfirm>
        </template>
      </el-table-column>
    </el-table>

    <!-- 窗口档案（自动建档，只读）：OC=窗口ID，群/私聊是类型 -->
    <section v-if="chats.length" style="margin-top: 24px;">
      <h3 class="page-title" style="font-size: 15px; margin-bottom: 8px;">窗口（自动建档）</h3>
      <p class="brand-sub" style="color: var(--text-muted); font-size: 12px; margin-bottom: 8px;">
        OC = 窗口 ID（群/私聊都是 oc_ 开头，回复消息填 chat_id 用）；OU = 用户 ID（@人/识人）。
        窗口由消息自动建档，无需手工维护。
      </p>
      <el-table :data="chats" size="small" style="width: 100%;">
        <el-table-column label="名称" min-width="140">
          <template #default="{ row }">{{ row.name || '(未命名)' }}</template>
        </el-table-column>
        <el-table-column label="类型" width="80">
          <template #default="{ row }">
            <el-tag size="small" :type="row.type === 'dm' ? 'success' : 'info'">
              {{ row.type === 'dm' ? '私聊' : row.type === 'group' ? '群' : '会话' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="窗口 ID (OC)" min-width="280">
          <template #default="{ row }">
            <span class="mono brand-sub" style="font-size: 11px;">{{ row.chat_id }}</span>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <div style="margin-top: 12px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
      <span style="color: var(--text-muted); font-size: 12px;">共 {{ filteredContacts.length }} 条</span>
      <el-pagination
        v-if="filteredContacts.length > contactPageSize"
        background
        layout="prev, pager, next"
        :total="filteredContacts.length"
        :page-size="contactPageSize"
        :current-page="contactPage"
        :pager-count="7"
        @current-change="(p) => contactPage = p"
      />
    </div>

    <!-- 合并操作栏（选中 2 条时显示） -->
    <div v-if="selectedRows.length === 2" style="margin-top: 12px; display: flex; align-items: center; gap: 12px;">
      <span class="brand-sub" style="color: var(--text-muted);">
        已选 2 条 → 合并「{{ selectedRows[0].name || '(未命名)' }}」到「{{ selectedRows[1].name || '(未命名)' }}」
      </span>
      <el-button size="small" @click="swapSelection">↔ 换方向</el-button>
      <el-button size="small" type="warning" :loading="merging" @click="doMerge">
        合并
      </el-button>
    </div>

    <!-- 新增/编辑 弹窗 -->
    <el-dialog v-model="dlg.open" :title="dlg.mode === 'edit' ? '编辑联系人' : '新增联系人'" width="600px">
      <el-form label-width="90px">
        <el-form-item label="姓名 *">
          <el-input v-model="dlg.form.name" placeholder="例：张浩普 / Alpha" />
        </el-form-item>
        <el-form-item label="类型">
          <el-select v-model="dlg.form.kind" style="width: 150px;">
            <el-option label="真人 (human)" value="human" />
            <el-option label="机器人 (bot)" value="bot" />
            <el-option label="系统 (system)" value="system" />
          </el-select>
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="dlg.form.notes" type="textarea" :rows="2"
            placeholder="关系说明、背景等" />
        </el-form-item>
        <el-form-item label="平台 ID">
          <div v-for="(pid, idx) in dlg.form.platform_ids" :key="idx"
            style="display: flex; gap: 8px; margin-bottom: 6px; width: 100%;">
            <el-input v-model="pid.platform" placeholder="feishu" style="width: 100px;" />
            <el-input v-model="pid.platform_id" placeholder="用户ID ou_… 或 窗口ID oc_…（群联系人填 oc_）" style="flex: 1;" />
            <el-button text type="danger" @click="dlg.form.platform_ids.splice(idx, 1)">✕</el-button>
          </div>
          <el-button text type="primary" @click="dlg.form.platform_ids.push({ platform: '', platform_id: '' })">
            + 添加平台 ID
          </el-button>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlg.open = false">取消</el-button>
        <el-button type="primary" :loading="dlg.loading" @click="submitDlg">保存</el-button>
      </template>
    </el-dialog>

    <!-- 拉黑弹窗 -->
    <el-dialog v-model="blockDlg.open" title="拉黑原因" width="400px">
      <el-input v-model="blockDlg.reason" type="textarea" :rows="2"
        placeholder="拉黑原因（可选，仅自己看）" />
      <template #footer>
        <el-button @click="blockDlg.open = false">取消</el-button>
        <el-button type="danger" :loading="blockDlg.loading" @click="confirmBlock">确认拉黑</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { instanceApi } from '@/api/client'

const route = useRoute()
const iid = computed(() => String(route.params.iid || ''))
const contacts = ref([])
const chats = ref([])
const loading = ref(true)
const searchText = ref('')
const filter = ref('all')
const selectedRows = ref([])
const merging = ref(false)
// 前端分页: 联系人通常数量不大 (几十-几百), 走前端 slice 即可
const contactPage = ref(1)
const contactPageSize = 20

// ── 计算属性 ──
const filteredContacts = computed(() => {
  let list = contacts.value
  // 筛选
  if (filter.value === 'named') list = list.filter(c => c.name && c.name.trim())
  else if (filter.value === 'stub') list = list.filter(c => !c.name || !c.name.trim())
  else if (filter.value === 'blocked') list = list.filter(c => c.blocked)
  // 搜索
  const q = searchText.value.trim().toLowerCase()
  if (q) {
    list = list.filter(c => {
      const ids = (c.platform_ids || []).map(p => `${p.platform} ${p.platform_id}`).join(' ')
      return (c.name || '').toLowerCase().includes(q)
        || (c.notes || '').toLowerCase().includes(q)
        || ids.toLowerCase().includes(q)
    })
  }
  return list
})
// 前端分页: 切当前 page 给 el-table
const pagedFilteredContacts = computed(() => {
  const start = (contactPage.value - 1) * contactPageSize
  return filteredContacts.value.slice(start, start + contactPageSize)
})
// 切 filter / search 时重置回第 1 页
watch([filter, searchText], () => { contactPage.value = 1 })

// ── 加载 ──
async function load() {
  loading.value = true
  const d = await instanceApi(iid.value).contacts()
  loading.value = false
  if (d.error) return ElMessage.error(d.error)
  contacts.value = d.contacts || []
  chats.value = d.chats || []
}

// ── 新增 / 编辑 ──
const dlg = reactive({
  open: false, mode: 'create', loading: false, editId: '',
  form: { name: '', kind: 'human', notes: '', platform_ids: [] },
})

function openCreate() {
  dlg.mode = 'create'
  dlg.editId = ''
  dlg.form = { name: '', kind: 'human', notes: '', platform_ids: [{ platform: '', platform_id: '' }] }
  dlg.open = true
}

function openEdit(row) {
  dlg.mode = 'edit'
  dlg.editId = row.id
  dlg.form = {
    name: row.name || '',
    kind: row.kind || 'human',
    notes: row.notes || '',
    platform_ids: (row.platform_ids || []).map(p => ({ ...p })),
  }
  // 至少留一个空行方便加新平台 ID
  if (!dlg.form.platform_ids.length) dlg.form.platform_ids.push({ platform: '', platform_id: '' })
  dlg.open = true
}

async function submitDlg() {
  if (!dlg.form.name.trim()) return ElMessage.error('姓名必填')
  // 清理空 platform_ids
  const pids = dlg.form.platform_ids
    .filter(p => p.platform.trim() && p.platform_id.trim())
    .map(p => ({ platform: p.platform.trim(), platform_id: p.platform_id.trim() }))
  if (dlg.mode === 'create' && !pids.length) {
    return ElMessage.error('至少填一个平台 ID')
  }
  dlg.loading = true
  const body = { name: dlg.form.name.trim(), kind: dlg.form.kind, notes: dlg.form.notes.trim(), platform_ids: pids }
  const d = dlg.mode === 'create'
    ? await instanceApi(iid.value).createContact(body)
    : await instanceApi(iid.value).updateContact(dlg.editId, body)
  dlg.loading = false
  if (d.error) return ElMessage.error(d.error)
  ElMessage.success(dlg.mode === 'create' ? '已创建' : '已更新')
  dlg.open = false
  await load()
}

// ── 删除 ──
async function removeContact(row) {
  const d = await instanceApi(iid.value).deleteContact(row.id)
  if (d.error) return ElMessage.error(d.error)
  ElMessage.success('已删除')
  await load()
}

// ── 拉黑 ──
const blockDlg = reactive({ open: false, loading: false, reason: '', contact: null })

function toggleBlock(row) {
  if (row.blocked) {
    // 直接恢复
    doBlock(row, false, '')
  } else {
    blockDlg.contact = row
    blockDlg.reason = ''
    blockDlg.open = true
  }
}

async function confirmBlock() {
  blockDlg.loading = true
  await doBlock(blockDlg.contact, true, blockDlg.reason)
  blockDlg.loading = false
  blockDlg.open = false
}

async function doBlock(row, blocked, reason) {
  const d = await instanceApi(iid.value).blockContact(row.id, blocked, reason)
  if (d.error) return ElMessage.error(d.error)
  ElMessage.success(blocked ? '已拉黑' : '已恢复')
  await load()
}

// ── 合并 ──
function onSelectionChange(rows) {
  // 只保留前 2 个
  selectedRows.value = rows.slice(0, 2)
}

function swapSelection() {
  if (selectedRows.value.length === 2) {
    selectedRows.value = [selectedRows.value[1], selectedRows.value[0]]
  }
}

async function doMerge() {
  if (selectedRows.value.length !== 2) return
  merging.value = true
  // source → target：第 1 个合并到第 2 个
  const [source, target] = selectedRows.value
  const d = await instanceApi(iid.value).mergeContacts(source.id, target.id)
  merging.value = false
  if (d.error) return ElMessage.error(d.error)
  ElMessage.success(`「${source.name || '(未命名)'}」已合并到「${target.name || '(未命名)'}」`)
  selectedRows.value = []
  await load()
}

// ── helpers ──
function shortText(s, n) {
  if (!s) return ''
  return s.length > n ? s.slice(0, n) + '…' : s
}
// iso 时间转相对时间: <1min "刚刚" / <60min "X 分钟前" / <24h "X 小时前"
// / <7d "X 天前" / ≥7d "MM-DD"
function relTime(ts) {
  if (!ts) return ''
  const t = Number(ts)
  if (!t) return ''
  const diff = Date.now() / 1000 - t
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} 天前`
  const d = new Date(t * 1000)
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

onMounted(load)
</script>

<style scoped>
.stub-name {
  color: var(--text-muted);
  font-style: italic;
}
</style>
