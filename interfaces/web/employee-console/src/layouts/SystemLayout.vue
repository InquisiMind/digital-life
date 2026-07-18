<template>
  <div class="app-shell">
    <!-- 顶部栏 -->
    <header class="app-topbar">
      <div class="topbar-brand" @click="goSystem" style="cursor: pointer">
        <span class="brand-glyph">◇</span>
        <span>DIGITAL LIFE</span>
        <span class="brand-sub">数字生命控制台</span>
      </div>

      <nav class="topbar-nav">
        <RouterLink class="topbar-link" :class="{ active: !isInstanceScope }" to="/system">
          全局台
        </RouterLink>
        <el-divider direction="vertical" />
        <!-- 当前实例徽章 + 切换器合并到右侧，实例域内才会显示，左侧保持干净 -->
        <div class="instance-switcher" v-if="currentInstance">
          <span class="status-dot" :class="statusClass(currentInstance.status)"></span>
          <span class="ci-name">{{ currentInstance.display_name }}</span>
        </div>
        <el-select
          v-model="currentIid"
          placeholder="进入实例…"
          filterable
          style="width: 200px"
          @change="enterInstance"
        >
          <el-option
            v-for="inst in instanceList"
            :key="inst.id"
            :label="inst.display_name"
            :value="inst.id"
            @click="enterInstance(inst.id)"
          >
            <span>
              <span class="status-dot" :class="statusClass(inst.status)"></span>
              {{ inst.display_name }}
              <span class="brand-sub" style="font-size: 11px">{{ inst.tagline }}</span>
            </span>
          </el-option>
        </el-select>
        <el-divider direction="vertical" />
        <el-button
          type="warning"
          plain
          size="small"
          :icon="RefreshRight"
          :loading="restarting"
          @click="confirmRestart"
          title="重启 gateway master 进程（含所有实例子进程）"
        >重启</el-button>
      </nav>
    </header>

    <div class="app-body">
      <!-- 侧栏 -->
      <aside class="app-sidebar">
        <template v-if="isSystemRoute">
          <div class="sidebar-section">
            <div class="sidebar-section-title">SYSTEM</div>
            <RouterLink
              v-for="item in systemNav"
              :key="item.path"
              :to="item.path"
              class="sidebar-link"
              active-class="active"
            >
              <el-icon><component :is="item.icon" /></el-icon>
              <span>{{ item.label }}</span>
            </RouterLink>
          </div>
        </template>
        <template v-else>
          <div class="sidebar-section">
            <div class="sidebar-section-title">返回</div>
            <RouterLink to="/system" class="sidebar-link">
              <el-icon><Back /></el-icon>
              <span>全局台</span>
            </RouterLink>
          </div>
          <div class="sidebar-section" v-if="currentInstance">
            <div class="sidebar-section-title">{{ currentInstance.display_name }}</div>
            <RouterLink
              v-for="item in instanceNav"
              :key="item.path"
              :to="`/instance/${iid}${item.path}`"
              class="sidebar-link"
              active-class="active"
            >
              <el-icon><component :is="item.icon" /></el-icon>
              <span>{{ item.label }}</span>
            </RouterLink>
          </div>
        </template>

        <div class="sidebar-section" style="margin-top: auto">
          <div class="sidebar-section-title">LEGACY</div>
          <RouterLink v-if="iid" :to="`/legacy/employee/${iid}/`" class="sidebar-link">
            <el-icon><Back /></el-icon>
            <span>旧版控制台</span>
          </RouterLink>
        </div>
      </aside>

      <!-- 主内容 + 路由出口 -->
      <main class="app-main">
        <RouterView v-slot="{ Component }">
          <transition name="route" mode="out-in">
            <component :is="Component" />
          </transition>
        </RouterView>
      </main>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter, RouterLink, RouterView } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Back, Odometer, ChatDotRound, List, Calendar, Folder, MagicStick,
  Collection, User, Document, Setting, DataAnalysis, Cpu, Bell, RefreshRight,
  Histogram, Memo,
} from '@element-plus/icons-vue'
import { systemApi } from '../api/client'

const route = useRoute()
const router = useRouter()

const instanceList = ref([])
const currentIid = ref('')
const restarting = ref(false)

const iid = computed(() => route.params.iid || '')
const isSystemRoute = computed(() => route.path.startsWith('/system'))
const isInstanceScope = computed(() => route.path.startsWith('/instance'))

const currentInstance = computed(() =>
  instanceList.value.find((i) => i.id === iid.value)
)

const systemNav = [
  { path: '/system/overview', label: '系统实况', icon: DataAnalysis },
  { path: '/system/instances', label: '实例管理', icon: Cpu },
  { path: '/system/projects', label: '项目', icon: Folder },
  { path: '/system/skills', label: '技能市场', icon: MagicStick },
  { path: '/system/events', label: '事件类型', icon: Bell },
]

const instanceNav = [
  { path: '/overview', label: '概览', icon: Odometer },
  { path: '/sessions', label: '会话', icon: ChatDotRound },
  { path: '/todos', label: '待办', icon: List },
  { path: '/calendar', label: '日程', icon: Calendar },
  { path: '/projects', label: '参与项目', icon: Folder },
  { path: '/skills', label: '能力订阅', icon: MagicStick },
  { path: '/memories', label: '记忆 / 联想', icon: Collection },
  { path: '/scratchpad', label: '短期暂存', icon: Memo },
  { path: '/chunks', label: '切片 / 认知层', icon: Histogram },
  { path: '/contacts', label: '社交关系', icon: User },
  { path: '/persona', label: '人设 / 提示词', icon: Document },
  { path: '/config', label: '实例配置', icon: Setting },
]

function goSystem() {
  router.push('/system')
}

function enterInstance(selectedIid) {
  router.push(`/instance/${selectedIid}/overview`)
}

async function confirmRestart() {
  if (restarting.value) return
  try {
    await ElMessageBox.confirm(
      '重启 gateway？所有实例子进程会被回收并重新 spawn（等同执行 digital-life restart）。\n\n'
      + '约 5-10 秒完成；期间页面请求会短暂失败，请勿关闭浏览器。',
      '重启网关',
      { type: 'warning', confirmButtonText: '立即重启', cancelButtonText: '取消' },
    )
  } catch { return }

  restarting.value = true
  const startedAt = Date.now()
  try {
    const d = await systemApi.gatewayRestart('console manual reset')
    if (d.error) {
      ElMessage.error(`重启失败：${d.error}`)
      return
    }
    ElMessage.info('重启请求已发出，等待新 master 复活…')

    // gateway 重启期间请求会短暂失败；用指数 backoff 轮询 overview，
    // 直到新 master 复活（最多 ~20s）。复活后立即 reload instances 顶栏数据。
    const backoffMs = [1000, 1500, 2000, 2000, 2000, 3000, 3000, 5000]
    let alive = false
    let attempts = 0
    for (const wait of backoffMs) {
      await new Promise((r) => setTimeout(r, wait))
      attempts++
      try {
        const resp = await systemApi.overview()
        if (!resp.error) {
          alive = true
          await loadInstances()
          break
        }
      } catch {
        // 还没复活，继续等
      }
    }
    const elapsed = Math.round((Date.now() - startedAt) / 1000)
    if (alive) {
      ElMessage.success(`✓ 网关已重启完成（${elapsed}s, ${attempts} 次轮询）`)
    } else {
      ElMessage.warning(`⚠ 网关可能仍在重启（${elapsed}s 无响应）。请刷新页面确认；若长期无响应请检查日志。`)
    }
  } finally {
    // 给一点缓冲让用户看到结果
    setTimeout(() => { restarting.value = false }, 1500)
  }
}

function statusClass(status) {
  return status || 'idle'
}

async function loadInstances() {
  const d = await systemApi.instances()
  if (!d.error) {
    instanceList.value = d.instances || []
    // 顶部切换器只跟随路由 iid，不做"默认选 active 实例"预选 ——
    // 否则用户在全局台或刚进页面时，右上角总是显示 zero，造成"当前是 zero"的错觉。
    // currentIid 严格由 watch(iid) 从路由同步；无 iid 时保持空(显示"进入实例…")。
  }
}

// 严格跟随路由：进入 /instance/:iid 时同步到顶部切换器；离开实例域时清空
watch(iid, (v) => { currentIid.value = v || '' }, { immediate: true })

onMounted(loadInstances)
</script>
