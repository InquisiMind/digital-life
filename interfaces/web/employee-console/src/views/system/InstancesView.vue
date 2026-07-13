<template>
  <div>
    <section class="page-hero">
      <div>
        <h1 class="page-title">Instance Registry</h1>
        <p class="page-subtitle">每个数字生命个体的入口 · 配置 avatar / accent / tagline</p>
      </div>
      <div style="display: flex; gap: 8px;">
        <el-button @click="load"><el-icon><Refresh /></el-icon>刷新</el-button>
        <el-button type="primary" @click="openCreate">
          <el-icon><Plus /></el-icon> 新建实例
        </el-button>
      </div>
    </section>

    <div class="neon-grid" style="grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));">
      <div
        v-for="inst in instances"
        :key="inst.id"
        class="neon-card accent-override"
        :style="{ '--instance-accent': inst.accent_color || '#00f0ff' }"
      >
        <div style="display: flex; align-items: center; gap: 14px;">
          <!-- avatar 区：若有 URL 显示 img，否则 fallback 字母圈 -->
          <div class="avatar-box" :style="{ '--accent': inst.accent_color || '#00f0ff' }">
            <img
              v-if="inst.avatar"
              :src="resolveAvatar(inst.id, inst.avatar)"
              :alt="inst.display_name"
              @error="onAvatarError(inst, $event)"
            />
            <span v-else class="avatar-glyph">{{ (inst.display_name || '?').slice(0,1).toUpperCase() }}</span>
          </div>
          <div style="flex: 1;">
            <div style="display: flex; align-items: center; gap: 8px;">
              <strong style="font-family: var(--font-display); color: var(--instance-accent);">
                {{ inst.display_name }}
              </strong>
              <span class="status-dot" :class="inst.status"></span>
            </div>
            <div class="brand-sub mono" style="color: var(--text-muted);">
              {{ inst.id.slice(0, 8) }}…
              <el-tag v-if="inst.active" size="small" type="success">active</el-tag>
              <el-tag v-else size="small" type="info">off</el-tag>
            </div>
            <div class="brand-sub" style="margin-top: 4px;">{{ inst.tagline || '—' }}</div>
            <!-- 通道连接状态微灯（cycles 自后端 instance.channels） -->
            <div class="channel-badges" v-if="Array.isArray(inst.channels) && inst.channels.length">
              <span
                v-for="ch in inst.channels"
                :key="ch.platform"
                class="channel-pill"
                :class="ch.status === 'connected' ? 'on' : 'off'"
                :title="`${ch.label}: ${ch.status === 'connected' ? '已连接' : '未配置'}${ch.identity ? ' / ' + ch.identity : ''}`"
              >
                {{ ch.label }}
                <span class="dot"></span>
              </span>
            </div>
          </div>
        </div>

        <el-divider />

        <!-- edit form -->
        <div class="edit-grid">
          <label>
            <span>Display Name</span>
            <el-input v-model="edits[inst.id].display_name" size="small" />
          </label>
          <label>
            <span>Avatar URL</span>
            <el-input
              v-model="edits[inst.id].avatar"
              size="small"
              placeholder="assets/avatar.gif 或绝对 URL"
            />
          </label>
          <label>
            <span>Accent Color</span>
            <el-color-picker v-model="edits[inst.id].accent_color" size="small" />
          </label>
          <label style="grid-column: 1/-1">
            <span>Tagline</span>
            <el-input v-model="edits[inst.id].tagline" size="small" />
          </label>
        </div>

        <div style="display: flex; gap: 8px; margin-top: 12px;">
          <el-button
            type="primary"
            size="small"
            :loading="saving === inst.id"
            @click="save(inst.id)"
          >保存</el-button>
          <el-button size="small" @click="enter(inst.id)">进入实例 →</el-button>
          <el-button size="small" type="info" plain @click="goConfig(inst.id)">配置</el-button>
          <el-button
            size="small"
            :type="inst.active ? 'danger' : 'success'"
            plain
            :loading="toggling === inst.id"
            @click="toggleActive(inst)"
          >{{ inst.active ? '离线' : '上线' }}</el-button>
          <el-button
            size="small"
            type="danger"
            text
            :loading="deleting === inst.id"
            @click="confirmDelete(inst)"
            style="margin-left: auto"
          >删除</el-button>
        </div>
      </div>
    </div>

    <!-- 新建实例 dialog -->
    <el-dialog v-model="createDlg.open" title="新建数字生命实例" width="640px">
      <p class="brand-sub" style="color: var(--text-muted); margin-bottom: 16px;">
        调用 <code class="mono">init_instance</code> 初始化：
        创建 apps/&lt;uuid&gt;/ 目录骨架、app.yaml、secrets.env、persona、state.db。
        创建后需重启网关 <code class="mono">digital-life restart</code>，实例才会被自动 spawn。
      </p>
      <el-form label-width="160px" :model="createDlg.form">
        <el-form-item label="Display Name *" required>
          <el-input v-model="createDlg.form.display_name" placeholder="例：zero / alpha / buyer" />
        </el-form-item>
        <el-form-item label="Tagline">
          <el-input v-model="createDlg.form.tagline" placeholder="一句话个性/职责描述" />
        </el-form-item>
        <el-form-item label="Accent Color">
          <el-color-picker v-model="createDlg.form.accent_color" />
          <span class="brand-sub" style="margin-left: 12px; color: var(--text-muted);">
            卡片主题色（建议和人格匹配）
          </span>
        </el-form-item>
        <el-form-item label="Avatar URL">
          <el-input v-model="createDlg.form.avatar" placeholder="可选，例：assets/avatar.gif" />
        </el-form-item>
        <el-divider>飞书 / 模型凭证（可选，可后续在配置中心填）</el-divider>
        <el-form-item label="飞书 App ID">
          <el-input v-model="createDlg.form.feishu_app_id" placeholder="cli_xxx" />
        </el-form-item>
        <el-form-item label="飞书 App Secret">
          <el-input v-model="createDlg.form.feishu_app_secret" type="password" show-password />
        </el-form-item>
        <el-form-item label="GLM API Key">
          <el-input v-model="createDlg.form.glm_api_key" type="password" show-password />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="createDlg.open = false">取消</el-button>
        <el-button type="primary" :loading="createDlg.loading" @click="create">
          创建并初始化
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Plus, Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { systemApi } from '@/api/client'

const router = useRouter()
const instances = ref([])
const edits = reactive({})
const saving = ref('')
const toggling = ref('')
const deleting = ref('')

const createDlg = reactive({
  open: false,
  loading: false,
  form: {
    display_name: '',
    tagline: '',
    accent_color: '#00f0ff',
    avatar: '',
    feishu_app_id: '',
    feishu_app_secret: '',
    glm_api_key: '',
  },
})

function openCreate() {
  createDlg.open = true
  // 复用上次输入的 accent_color + 凭证（方便连续创建）
  createDlg.form = {
    display_name: '',
    tagline: '',
    accent_color: createDlg.form.accent_color || '#ff2d9c',
    avatar: '',
    feishu_app_id: createDlg.form.feishu_app_id,
    feishu_app_secret: createDlg.form.feishu_app_secret,
    glm_api_key: createDlg.form.glm_api_key,
  }
}

async function create() {
  if (!createDlg.form.display_name) return ElMessage.error('display_name 必填')
  createDlg.loading = true
  try {
    const d = await systemApi.createInstance(createDlg.form)
    if (d.error) return ElMessage.error(d.error)
    createDlg.open = false
    await ElMessageBox.alert(
      `✅ 实例已创建：${d.instance.display_name}\n\nUUID: ${d.instance.id}\n\n` +
      `下一步：\n1. 重启网关（实例才会被自动 spawn）：digital-life restart\n` +
      `2. 检查 apps/${d.instance.id}/config/secrets.env 的 LLM_API_KEY + FEISHU_APP_SECRET\n` +
      `3. 访问 /instance/${d.instance.id}/overview`,
      '创建成功',
      { type: 'success', confirmButtonText: '知道了' }
    )
    await load()
  } finally {
    createDlg.loading = false
  }
}

function resolveAvatar(iid, avatar) {
  if (/^https?:/.test(avatar)) return avatar
  // 相对 apps/{iid}/assets/ 由后端 /employee/{iid}/assets/{file} 服务
  return `/employee/${iid}/assets/${avatar.replace(/^.*\//, '')}`
}

function onAvatarError(inst, ev) {
  // 隐藏 img，回退字母
  ev.target.style.display = 'none'
}

function enter(iid) {
  router.push(`/instance/${iid}/overview`)
}

function goConfig(iid) {
  router.push(`/instance/${iid}/config`)
}

async function load() {
  const d = await systemApi.instances()
  if (d.error) return ElMessage.error(d.error)
  instances.value = d.instances || []
  for (const i of instances.value) {
    edits[i.id] = {
      display_name: i.display_name,
      avatar: i.avatar || '',
      accent_color: i.accent_color || '#00f0ff',
      tagline: i.tagline || '',
    }
  }
}

async function save(iid) {
  saving.value = iid
  const payload = edits[iid]
  const d = await systemApi.patchInstance(iid, payload)
  saving.value = ''
  if (d.error) return ElMessage.error(d.error)
  ElMessage.success(`${d.instance.display_name} 已更新`)
  await load()
}

async function toggleActive(inst) {
  if (toggling.value) return
  const next = !inst.active
  const verb = next ? '上线' : '离线'
  try {
    await ElMessageBox.confirm(
      `${verb} 实例「${inst.display_name}」？\n\n`
      + (next
        ? '下次 master tick / gateway restart 后该实例子进程自动 spawn。'
        : '当前会停止 spawn；正在跑的会在自然生命周期结束。'),
      `确认${verb}`,
      { type: 'warning', confirmButtonText: verb, cancelButtonText: '取消' },
    )
  } catch { return }

  toggling.value = inst.id
  try {
    const d = await systemApi.setInstanceActive(inst.id, next, 'console toggle')
    if (d.error) return ElMessage.error(`操作失败：${d.error}`)
    ElMessage.success(`✓ ${verb}）已记录；gateway 下次 tick / restart 生效`)
    await load()
  } finally {
    toggling.value = ''
  }
}

async function confirmDelete(inst) {
  if (deleting.value) return
  // 简单二次确认：显示 display_name + 不可恢复提示
  try {
    await ElMessageBox.confirm(
      `彻底删除实例「${inst.display_name}」？\n\n`
      + `将停止实例子进程 + 物理删除 apps/${inst.id.slice(0, 8)}…/ 整个目录：\n`
      + `  · state.db（生命周期数据）\n`
      + `  · runtime_log.db（运行日志）\n`
      + `  · memories/、persona/、todos/\n`
      + `  · secrets.env（API 凭证）\n\n`
      + `此操作不可恢复。`,
      '删除实例',
      { type: 'error', confirmButtonText: '彻底删除', cancelButtonText: '取消' },
    )
  } catch { return }

  deleting.value = inst.id
  try {
    const d = await systemApi.deleteInstance(inst.id)
    if (d.error) return ElMessage.error(`删除失败：${d.error}`)
    ElMessage.success(`✓ ${inst.display_name} 已删除（${d.disk || ''}）`)
    await load()
  } finally {
    deleting.value = ''
  }
}

onMounted(load)
</script>

<style scoped>
.avatar-box {
  width: 56px;
  height: 56px;
  border-radius: var(--radius);
  background: color-mix(in oklab, var(--accent) 12%, var(--bg-elevated));
  border: 1px solid color-mix(in oklab, var(--accent) 50%, transparent);
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  box-shadow: 0 0 12px color-mix(in oklab, var(--accent) 30%, transparent);
}
.avatar-box img {
  width: 100%; height: 100%; object-fit: cover;
}
.avatar-glyph {
  font-family: var(--font-display);
  font-size: 24px;
  font-weight: 700;
  color: var(--accent);
}

.edit-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
.edit-grid label {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.edit-grid label > span {
  font-size: 11px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--text-muted);
}

/* 通道连接徽章 */
.channel-badges {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 6px;
}
.channel-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1px 7px;
  border-radius: 10px;
  font-size: 10px;
  font-family: var(--font-mono);
  letter-spacing: 0.04em;
  border: 1px solid;
}
.channel-pill .dot {
  width: 5px; height: 5px; border-radius: 50%;
}
.channel-pill.on {
  color: var(--neon-cyan);
  border-color: color-mix(in oklab, var(--neon-cyan) 50%, transparent);
  background: color-mix(in oklab, var(--neon-cyan) 10%, transparent);
}
.channel-pill.on .dot {
  background: var(--neon-cyan);
  box-shadow: 0 0 6px var(--neon-cyan);
}
.channel-pill.off {
  color: var(--text-muted);
  border-color: color-mix(in oklab, var(--text-muted) 35%, transparent);
  background: color-mix(in oklab, var(--text-muted) 8%, transparent);
}
.channel-pill.off .dot {
  background: var(--text-muted);
  opacity: 0.6;
}
</style>
