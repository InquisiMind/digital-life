import { createApp } from 'vue'
import App from './App.vue'
import ElementPlus from 'element-plus'
import { createRouter, createWebHistory } from 'vue-router'

import 'element-plus/dist/index.css'
import './theme/tokens.css'
import './theme/layout.css'

import SystemLayout from './layouts/SystemLayout.vue'
import InstanceLayout from './layouts/InstanceLayout.vue'
import PlaceholderView from './views/PlaceholderView.vue'
import LegacyEmployee from './views/LegacyEmployee.vue'

// 全局 helper 注册到 vue component context —— 避免 tree-shake 把 composable
// 当 unused import 删（template 表达式里的 helper 名 rollup 不识别为引用）
import * as formatHelpers from './composables/useFormat'
import * as markdownHelpers from './composables/useMarkdown'

// 全局台 view
import OverviewView from './views/system/OverviewView.vue'
import InstancesView from './views/system/InstancesView.vue'
import ProjectsView from './views/system/ProjectsView.vue'
import ProjectDetailView from './views/system/ProjectDetailView.vue'
import SkillsMarketView from './views/system/SkillsMarketView.vue'
import EventsView from './views/system/EventsView.vue'

// 实例台 view
import InstanceOverview from './views/instance/OverviewTab.vue'
import InstanceSessions from './views/instance/SessionsTab.vue'
import InstanceTodos from './views/instance/TodosTab.vue'
import InstanceCalendar from './views/instance/CalendarTab.vue'
import InstanceProjects from './views/instance/ProjectsTab.vue'
import InstanceSkills from './views/instance/SkillsTab.vue'
import InstanceMemories from './views/instance/MemoriesTab.vue'
import InstanceContacts from './views/instance/ContactsTab.vue'
import InstancePersona from './views/instance/PersonaTab.vue'
import InstanceConfig from './views/instance/ConfigTab.vue'

const routes = [
  // === 全局管理台 ===
  {
    path: '/system',
    component: SystemLayout,
    children: [
      { path: '', redirect: '/system/overview' },
      { path: 'overview', component: OverviewView, meta: { title: '系统实况', icon: 'DataAnalysis' } },
      { path: 'instances', component: InstancesView, meta: { title: '实例管理', icon: 'Cpu' } },
      { path: 'projects', component: ProjectsView, meta: { title: '项目', icon: 'Folder' } },
      { path: 'projects/:pid', component: ProjectDetailView, meta: { title: '项目详情', icon: 'Folder' } },
      { path: 'skills', component: SkillsMarketView, meta: { title: '技能市场', icon: 'MagicStick' } },
      { path: 'events', component: EventsView, meta: { title: '事件类型', icon: 'Bell' } },
    ],
  },

  // === 实例台 ===
  {
    path: '/instance/:iid',
    component: InstanceLayout,
    children: [
      { path: '', redirect: 'overview' },
      { path: 'overview', component: InstanceOverview, meta: { title: '概览', icon: 'Odometer' } },
      { path: 'sessions', component: InstanceSessions, meta: { title: '会话', icon: 'ChatDotRound' } },
      { path: 'todos', component: InstanceTodos, meta: { title: '待办', icon: 'List' } },
      { path: 'calendar', component: InstanceCalendar, meta: { title: '日程', icon: 'Calendar' } },
      { path: 'projects', component: InstanceProjects, meta: { title: '参与项目', icon: 'Folder' } },
      { path: 'skills', component: InstanceSkills, meta: { title: '能力订阅', icon: 'MagicStick' } },
      { path: 'memories', component: InstanceMemories, meta: { title: '记忆 / 联想', icon: 'Collection' } },
      { path: 'contacts', component: InstanceContacts, meta: { title: '社交关系', icon: 'User' } },
      { path: 'persona', component: InstancePersona, meta: { title: '人设 / 提示词', icon: 'Document' } },
      { path: 'config', component: InstanceConfig, meta: { title: '实例配置', icon: 'Setting' } },
    ],
  },

  // === 兼容入口 ===
  {
    path: '/employee/:iid',
    redirect: (to) => `/instance/${to.params.iid}/overview`,
  },
  {
    path: '/legacy/employee/:iid/:rest(.*)?',
    component: LegacyEmployee,
  },

  // === 默认入口 ===
  { path: '/', redirect: '/system' },
  { path: '/:pathMatch(.*)*', redirect: '/system' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior() {
    return { top: 0 }
  },
})

const app = createApp(App)
app.use(ElementPlus)
app.use(router)

// 全局 helpers：注册后模板 _ctx 自动有 safeSlice / shortId / fmtTs / renderMarkdown
// 无需每个 view 单独 import defineExpose —— 避免 rollup tree-shake 把 composable 当 unused 删
Object.assign(app.config.globalProperties, formatHelpers, markdownHelpers)

app.mount('#app')
