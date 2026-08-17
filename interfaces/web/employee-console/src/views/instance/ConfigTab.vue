<template>
  <div>
    <section class="page-hero">
      <h1 class="page-title">Instance Config</h1>
      <p class="page-subtitle">{{ shortId(iid, 8) }} 实例专属配置（messenger / 群聊 / 员工实例）</p>
    </section>

    <!-- 社交接管: 授权飞书让数字生命接管全部 IM 消息 -->
    <div class="neon-card" style="margin-bottom: var(--space-4); padding: var(--space-4);">
      <h3 class="page-title" style="font-size: 16px; margin: 0 0 var(--space-3);">社交接管</h3>
      <p class="brand-sub" style="color: var(--text-muted); margin-bottom: var(--space-3);">
        接管真人飞书账号后, 数字生命将以真人身份拉取全部群 + P2P 私聊消息入库。
      </p>

      <!-- 飞书接管 -->
      <div style="display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-bottom: var(--space-3);">
        <span class="brand-sub" style="min-width: 40px;">飞书</span>
        <div v-if="socialLoading" class="brand-sub mono" style="color: var(--text-muted);">loading…</div>
        <template v-else>
          <el-tag v-if="socialStatus.authorized" type="success" effect="dark">已接管</el-tag>
          <el-tag v-else type="info" effect="plain">未授权</el-tag>
          <a v-if="socialStatus.oauth_url" :href="socialStatus.oauth_url" target="_blank" rel="noopener">
            <el-button type="primary" size="small">{{ socialStatus.authorized ? '重新授权' : '⟶ 接管我的飞书' }}</el-button>
          </a>
          <el-button v-if="socialStatus.authorized" size="small" type="danger" plain :loading="socialRevoking" @click="revokeSocial">解除</el-button>
          <el-button size="small" plain @click="showTakeoverGuide = true">使用指南</el-button>
        </template>
      </div>

      <!-- 全接管引导弹窗 -->
      <el-dialog v-model="showTakeoverGuide" title="飞书全接管 · 使用指南" width="600px" style="max-width: 90vw;">
        <div style="line-height: 1.8; font-size: 14px;">
          <p><strong>什么是全接管？</strong></p>
          <p style="color: var(--text-muted);">授权后，数字生命以你的身份拉取飞书全部群聊和私聊消息（不只是被 @ 的），自动入库供它感知社交动态。它会像你一样"看到"所有消息，但不会替你回复——回复仍需通过它自己的飞书 Bot 身份。</p>

          <el-divider />

          <p><strong>前置条件</strong></p>
          <ol style="color: var(--text-muted); padding-left: 20px;">
            <li>已在 <a href="https://open.feishu.cn/app" target="_blank">飞书开放平台</a> 创建自建应用</li>
            <li>已导入基础权限（tenant）+ 事件订阅 + 机器人 + 发布（见 <a href="https://github.com/InquisiMind/digital-life/blob/main/docs/operations/feishu-setup.md" target="_blank">飞书配置指南</a>）</li>
            <li>已导入全接管 user 权限最小集（im:chat:read、im:message、get_as_user ×2、contact:user.base:readonly、offline_access——指南 1b 节，需管理员审批）</li>
          </ol>

          <el-divider />

          <p><strong>操作步骤</strong></p>
          <ol style="color: var(--text-muted); padding-left: 20px;">
            <li>点击「⟶ 接管我的飞书」按钮</li>
            <li>在弹出的飞书授权页面，选择你的企业 → 确认授权</li>
            <li>授权成功后状态变为「已接管」，数字生命开始拉取消息</li>
            <li>如需取消，点「解除」即可撤销授权</li>
          </ol>

          <el-divider />

          <p><strong>⚠️ 隐私说明</strong></p>
          <p style="color: var(--text-muted);">全接管后数字生命能读取你授权范围内的飞书消息。它有自主判断机制（只关注与工作相关的消息，忽略闲聊）。如需控制拉取范围，可在本页「社交接管范围」区（或 app.yaml 的 social.takeover 段）配置白名单 / 黑名单：allowlist 模式只拉指定群，blocklist 模式排除指定群（如家人群），默认 all 拉全部。</p>
        </div>
      </el-dialog>
    </div>

    <div v-if="loading" class="dev-placeholder"><span class="mono">loading…</span></div>
    <div v-else>
      <div v-for="section in instanceSections" :key="section.key" class="neon-card" style="margin-bottom: var(--space-4);">
        <h3 class="page-title" style="font-size: 16px; margin: 0 0 var(--space-3);">
          {{ section.label }}
        </h3>
        <p class="brand-sub" style="color: var(--text-muted); margin-top: -8px; margin-bottom: var(--space-4);">
          {{ section.description }}
        </p>

        <!-- 微信扫码登录（仅 wechat section） -->
        <div v-if="section.key === 'wechat'" style="display: flex; justify-content: flex-end; margin-bottom: var(--space-3);">
          <el-button type="success" :loading="wechatLoading" @click="doWechatLogin">
            {{ wechatLoading ? '等待扫码…(最多120s)' : '微信扫码登录' }}
          </el-button>
        </div>

        <div class="config-row" v-for="field in section.fields" :key="field.key">
          <div class="field-meta">
            <div class="field-title">{{ field.label }}</div>
            <div class="brand-sub mono" style="font-size: 11px; color: var(--text-muted);">{{ field.key }} · {{ field.origin }}</div>
            <p v-if="field.description" class="brand-sub" style="margin-top: 2px; color: var(--text-secondary); font-size: 12px;">{{ field.description }}</p>
          </div>
          <div class="field-control">
            <el-switch v-if="field.type === 'boolean'" v-model="draft[field.key]" />
            <el-input-number v-else-if="field.type === 'number'" v-model="draft[field.key]" />
            <el-select v-else-if="field.type === 'array'" v-model="draft[field.key]" multiple filterable allow-create />
            <el-select v-else-if="field.options && field.options.length" v-model="draft[field.key]" filterable>
              <el-option v-for="o in field.options" :key="o" :label="o" :value="o" />
            </el-select>
            <!-- 快捷键录制控件：点击后按下组合键自动捕获 -->
            <div v-else-if="field.type === 'hotkey'" class="hotkey-input-wrap">
              <button
                type="button"
                class="hotkey-record-btn"
                :class="{ recording: hotkeyRecording === field.key }"
                @click="startHotkeyRecord(field.key)"
                @keydown="hotkeyRecording === field.key ? onHotkeyKeydown($event, field.key) : null"
                @blur="hotkeyRecording === field.key ? cancelHotkeyRecord() : null"
                tabindex="0"
              >
                <template v-if="hotkeyRecording === field.key">按下组合键…</template>
                <template v-else-if="draft[field.key]">{{ formatHotkeyDisplay(draft[field.key]) }}</template>
                <template v-else>点击设置快捷键</template>
              </button>
              <span v-if="draft[field.key] && hotkeyRecording !== field.key" class="hotkey-hint mono">{{ draft[field.key] }}</span>
            </div>
            <el-input v-else v-model="draft[field.key]"
                      :type="field.secret ? 'password' : 'text'"
                      :show-password="field.secret"
                      :placeholder="field.secret && field.configured ? '留空保留当前密钥' : ''" />
          </div>
        </div>
      </div>

      <div style="display: flex; gap: 8px;">
        <el-button @click="load">还原</el-button>
        <el-button type="primary" :disabled="!dirty" :loading="saving" @click="save">
          保存 {{ dirty ? `(${Object.keys(changes).length})` : '' }}
        </el-button>
      </div>
    </div>

    <!-- 微信扫码 Dialog (接管 + ClawBot 共用) -->
    <el-dialog v-model="qrDialogVisible" title="微信扫码登录" width="360px" :close-on-click-modal="false">
      <div style="text-align: center; padding: 20px;">
        <!-- V6 接管模式: 直接用 base64 -->
        <img v-if="qrCodeUrl && qrCodeUrl.startsWith('data:')" :src="qrCodeUrl"
             alt="微信二维码"
             style="width: 240px; height: 240px; border-radius: var(--radius); margin-bottom: 16px; background: white;" />
        <!-- ClawBot 模式: 走代理 URL -->
        <img v-else-if="qrCodeUrl" :src="`/api/system/instances/${iid}/wechat-login/qr-page?qrcode_url=${encodeURIComponent(qrCodeUrl)}`"
             alt="微信二维码"
             style="width: 240px; height: 240px; border-radius: var(--radius); margin-bottom: 16px; background: white;" />
        <div v-else style="width: 240px; height: 240px; display: flex; align-items: center; justify-content: center; margin: 0 auto 16px; background: var(--bg-deep); border-radius: var(--radius);">
          <span class="brand-sub" style="color: var(--text-muted);">加载中…</span>
        </div>
        <p style="color: var(--text-secondary); font-size: 14px;">{{ qrStatus }}</p>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { instanceApi, systemApi, safeFetch } from '@/api/client'

const route = useRoute()
const iid = computed(() => String(route.params.iid || ''))
const loading = ref(true)
const saving = ref(false)
const showTakeoverGuide = ref(false)
const qrDialogVisible = ref(false)
const qrCodeUrl = ref('')
// 社交接管授权状态(ConfigTab 上方独立块)
const socialStatus = ref({ authorized: false, oauth_url: '' })
const socialLoading = ref(false)
const socialRevoking = ref(false)
// V6 微信全接管 (itchat)
const wechatTakeover = ref({ status: 'idle', qr_base64: '', my_nickname: '', error: '' })
const wechatTakeoverLoading = ref(false)
let wechatTakeoverPollTimer = null
const qrStatus = ref('')
const allSections = ref([])
const draft = ref({})
const baseline = ref({})

// 快捷键录制（hotkey 字段类型）
const hotkeyRecording = ref(null)  // 正在录制的 field.key，null = 未录制

const HOTKEY_MOD_LABELS = { meta: '⌘', cmd: '⌘', ctrl: '⌃', shift: '⇧', alt: '⌥', option: '⌥' }

function formatHotkeyDisplay(v) {
  if (!v) return ''
  return v.split('+').map(k => HOTKEY_MOD_LABELS[k.trim().toLowerCase()] || k.trim().toUpperCase()).join(' + ')
}

function startHotkeyRecord(fieldKey) {
  if (hotkeyRecording.value === fieldKey) {
    hotkeyRecording.value = null
    return
  }
  hotkeyRecording.value = fieldKey
}

function cancelHotkeyRecord() {
  hotkeyRecording.value = null
}

function onHotkeyKeydown(e, fieldKey) {
  // 阻止默认行为（避免浏览器快捷键触发）
  e.preventDefault()
  e.stopPropagation()
  const parts = []
  if (e.metaKey) parts.push('cmd')
  if (e.ctrlKey) parts.push('ctrl')
  if (e.shiftKey) parts.push('shift')
  if (e.altKey) parts.push('alt')
  // 单独按修饰键不算完成（等普通键）
  const keyName = e.key.toLowerCase()
  const isModOnly = ['meta', 'control', 'shift', 'alt'].includes(keyName)
  if (isModOnly && parts.length) return  // 等用户按普通键
  // 普通键：标准化键名
  let k = keyName
  if (k === ' ') k = 'space'
  if (k.length === 1) k = e.key  // 字母保留原样
  parts.push(k)
  draft.value[fieldKey] = parts.join('+')
  hotkeyRecording.value = null
}

// V6 微信全接管 (itchat)
async function startWechatTakeover() {
  if (wechatTakeoverLoading.value) return
  wechatTakeoverLoading.value = true
  wechatTakeover.value = { status: 'starting', qr_base64: '', my_nickname: '', error: '' }
  // 弹 QR 码 dialog
  qrDialogVisible.value = true
  qrStatus.value = '启动中…'

  try {
    // 调后端启动 daemon
    await safeFetch(`/api/system/instances/${iid.value}/wechat-takeover/start`, { method: 'POST', body: '{}' })
    // 轮询状态 + QR 码 (5s 间隔, 最多 24 次 = 120s)
    let polls = 0
    wechatTakeoverPollTimer = setInterval(async () => {
      polls++
      if (polls > 24) {
        clearInterval(wechatTakeoverPollTimer)
        qrStatus.value = '扫码超时, 请重新点击'
        wechatTakeoverLoading.value = false
        wechatTakeover.value.status = 'timeout'
        return
      }
      try {
        const d = await safeFetch(`/api/system/instances/${iid.value}/wechat-takeover/status`)
        wechatTakeover.value = {
          status: d.status || 'unknown',
          qr_base64: d.qr_base64 || '',
          my_nickname: d.my_nickname || '',
          error: d.error || '',
        }
        if (d.status === 'qr_ready' && d.qr_base64) {
          qrCodeUrl.value = d.qr_base64
          qrStatus.value = '请用手机微信扫码'
        } else if (d.status === 'logged_in') {
          clearInterval(wechatTakeoverPollTimer)
          qrDialogVisible.value = false
          wechatTakeoverLoading.value = false
          ElMessage.success(`✓ 微信接管成功 (${d.my_nickname || '已登录'})`)
        } else if (d.status === 'error') {
          clearInterval(wechatTakeoverPollTimer)
          qrStatus.value = `错误: ${d.error || '未知'}`
          wechatTakeoverLoading.value = false
        }
      } catch (e) { /* ignore poll errors */ }
    }, 2000)
  } catch (e) {
    qrStatus.value = `错误: ${e.message || e}`
    wechatTakeoverLoading.value = false
  }
}

async function stopWechatTakeover() {
  try {
    await safeFetch(`/api/system/instances/${iid.value}/wechat-takeover/stop`, { method: 'POST', body: '{}' })
    wechatTakeover.value = { status: 'stopped', qr_base64: '', my_nickname: '', error: '' }
    ElMessage.success('微信接管已解除')
  } catch (e) {
    ElMessage.error(`解除失败: ${e.message || e}`)
  }
}

async function doWechatLogin() {
  if (wechatLoading.value) return
  wechatLoading.value = true
  qrCodeUrl.value = ''
  qrDialogVisible.value = true
  qrStatus.value = '获取二维码…'
  try {
    const d = await systemApi.wechatQrcode(iid.value)
    if (d.error) {
      qrStatus.value = `获取二维码失败：${d.error}`
      return
    }
    qrCodeUrl.value = d.qrcode_url || ''
    qrStatus.value = '请用手机微信扫码'
    // 开始轮询（3s 间隔，最多 40 次 = 120s）
    let polls = 0
    const pollTimer = setInterval(async () => {
      polls++
      if (polls > 40) {
        clearInterval(pollTimer)
        qrStatus.value = '扫码超时，请重新点击'
        wechatLoading.value = false
        return
      }
      const st = await systemApi.wechatLoginStatus(iid.value)
      if (st.status === 'confirmed') {
        clearInterval(pollTimer)
        qrDialogVisible.value = false
        wechatLoading.value = false
        ElMessage.success(`✓ 微信登录成功（bot_id=${st.bot_id}），通道将在 30s 内自动生效`)
        await load()
      }
    }, 3000)
  } catch (e) {
    qrStatus.value = `错误：${e.message || e}`
  }
}

// 实例私有：身份 / 飞书凭证 / 模型 / 运行时（token/精力/心跳）/ 任务策略
const INSTANCE_SECTIONS = ['employee', 'model', 'feishu', 'wechat', 'behavior', 'runtime', 'tasks', 'perception']

const instanceSections = computed(() =>
  allSections.value.filter(s => INSTANCE_SECTIONS.includes(s.key))
)

const hasWechatChannel = computed(() =>
  instanceSections.value.some(s => s.key === 'wechat')
)

const changes = computed(() => {
  const out = {}
  for (const section of instanceSections.value) {
    for (const f of section.fields || []) {
      if (f.readonly) continue
      const a = draft.value[f.key]
      const b = baseline.value[f.key]
      if (f.secret && !a) continue
      if (JSON.stringify(a) !== JSON.stringify(b)) out[f.key] = a
    }
  }
  return out
})

const dirty = computed(() => Object.keys(changes.value).length > 0)

async function load() {
  loading.value = true
  const d = await instanceApi(iid.value).config()
  loading.value = false
  if (d.error) return ElMessage.error(d.error)
  allSections.value = d.sections || []
  const next = {}
  for (const s of instanceSections.value) {
    for (const f of s.fields || []) {
      if (f.readonly) continue
      next[f.key] = f.secret ? '' : (f.value ?? (f.type === 'array' ? [] : ''))
    }
  }
  draft.value = next
  baseline.value = { ...next }
}

async function save() {
  saving.value = true
  try {
    const d = await instanceApi(iid.value).updateConfig(changes.value)
    if (d.error) return ElMessage.error(d.error)
    ElMessage.success('实例配置已保存 · 重启网关后生效')
    await load()
  } finally { saving.value = false }
}

onMounted(() => {
  load()
  loadSocialStatus()
})

async function loadSocialStatus() {
  socialLoading.value = true
  try {
    const d = await instanceApi(iid.value).socialStatus()
    if (d && !d.error) socialStatus.value = { authorized: !!d.authorized, oauth_url: d.oauth_url || '' }
  } finally { socialLoading.value = false }
}

async function revokeSocial() {
  if (socialRevoking.value) return
  socialRevoking.value = true
  try {
    const d = await instanceApi(iid.value).socialRevoke()
    if (d && d.error) { ElMessage.error(d.error); return }
    ElMessage.success(d.note || '已解除授权')
    await loadSocialStatus()
  } finally { socialRevoking.value = false }
}
</script>

<style scoped>
.config-row {
  display: grid;
  grid-template-columns: 1fr 240px;
  gap: var(--space-4);
  padding: var(--space-3) 0;
  border-top: 1px solid var(--border-divider);
  align-items: center;
}
.field-title { color: var(--text-primary); font-size: 14px; }
.hotkey-input-wrap {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 4px;
}
.hotkey-record-btn {
  min-width: 160px;
  padding: 8px 16px;
  border: 1px solid var(--border-divider);
  border-radius: 6px;
  background: var(--bg-elevated, #1a1a2e);
  color: var(--text-primary);
  font-size: 14px;
  cursor: pointer;
  transition: border-color 0.15s, box-shadow 0.15s;
  font-family: inherit;
}
.hotkey-record-btn:hover { border-color: var(--accent, #00f0ff); }
.hotkey-record-btn.recording {
  border-color: var(--accent, #00f0ff);
  box-shadow: 0 0 0 2px rgba(0, 240, 255, 0.2);
  animation: hotkey-pulse 1s ease-in-out infinite;
}
@keyframes hotkey-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.6; }
}
.hotkey-hint { font-size: 11px; color: var(--text-muted); }
</style>
