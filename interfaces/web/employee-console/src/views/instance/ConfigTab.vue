<template>
  <div>
    <section class="page-hero">
      <h1 class="page-title">Instance Config</h1>
      <p class="page-subtitle">{{ shortId(iid, 8) }} 实例专属配置（messenger / 群聊 / 员工实例）</p>
    </section>

    <!-- 社交接管: 授权飞书 + 微信 让数字生命接管全部 IM 消息 -->
    <div class="neon-card" style="margin-bottom: var(--space-4); padding: var(--space-4);">
      <h3 class="page-title" style="font-size: 16px; margin: 0 0 var(--space-3);">社交接管</h3>
      <p class="brand-sub" style="color: var(--text-muted); margin-bottom: var(--space-3);">
        接管真人 IM 账号后, 数字生命将以真人身份拉取全部群 + P2P 私聊消息萹库。
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
        </template>
      </div>

      <!-- 微信接管 (V6 itchat Web 协议) -->
      <div style="display: flex; gap: 12px; align-items: center; flex-wrap: wrap;">
        <span class="brand-sub" style="min-width: 40px;">微信</span>
        <el-tag v-if="wechatTakeover.status === 'logged_in'" type="success" effect="dark">已接管 ({{ wechatTakeover.my_nickname }})</el-tag>
        <el-tag v-else-if="wechatTakeover.status === 'qr_ready' || wechatTakeover.status === 'logging_in'" type="warning" effect="dark">等待扫码…</el-tag>
        <el-tag v-else-if="wechatTakeover.status === 'error'" type="danger" effect="dark">错误</el-tag>
        <el-tag v-else type="info" effect="plain">未接管</el-tag>
        <el-button type="success" size="small" :loading="wechatTakeoverLoading" @click="startWechatTakeover">
          {{ wechatTakeover.status === 'logged_in' ? '重新接管' : '⟶ 接管我的微信' }}
        </el-button>
        <el-button v-if="wechatTakeover.status === 'logged_in'" size="small" type="danger" plain @click="stopWechatTakeover">解除</el-button>
      </div>
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
const wechatLoading = ref(false)
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
const INSTANCE_SECTIONS = ['employee', 'model', 'feishu', 'wechat', 'behavior', 'runtime', 'tasks']

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
</style>
