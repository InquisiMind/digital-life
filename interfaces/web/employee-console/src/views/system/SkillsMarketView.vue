<template>
  <div>
    <section class="page-hero">
      <div>
        <h1 class="page-title">Skill Market</h1>
        <p class="page-subtitle">系统内置 + 实例共享 + 实例自建 · 全局管启用 · 实例管挂载</p>
      </div>
      <div style="display: flex; align-items: center; gap: 12px;">
        <span class="brand-sub">为实例订阅：</span>
        <el-select v-model="selectedInstance" placeholder="选择实例…" filterable style="width: 180px;">
          <el-option v-for="i in instances" :key="i.id" :label="i.display_name" :value="i.id" />
        </el-select>
        <el-button @click="loadAll"><el-icon><Refresh /></el-icon></el-button>
      </div>
    </section>

    <div class="neon-grid" style="grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));">
      <div
        v-for="skill in skills"
        :key="skill.global_key || skill.name + '-' + skill.scope"
        class="neon-card"
        :class="[{ 'glow-pink': skill.scope === 'shared' }, { 'skill-disabled': skill.enabled === false }]"
      >
        <div style="display: flex; justify-content: space-between; align-items: flex-start;">
          <div>
            <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
              <strong style="font-family: var(--font-display); color: var(--neon-cyan);">
                {{ skill.name }}
              </strong>
              <el-tag size="small" :type="scopeTagType(skill.scope)" effect="plain">
                {{ scopeLabel(skill) }}
              </el-tag>
              <el-tag v-if="skill.enabled === false" size="small" type="danger" effect="dark">已停用</el-tag>
            </div>
            <p class="brand-sub" style="color: var(--text-secondary); margin-top: 6px; min-height: 32px;">
              {{ skill.description || '—' }}
            </p>
          </div>
          <el-switch
            :model-value="skill.enabled !== false"
            :loading="globalToggling.has(skill.global_key)"
            active-text="启用"
            inactive-text="停用"
            @change="(v) => toggleGlobal(skill, v)"
          />
        </div>

        <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 12px; gap: 8px;">
          <div style="display: flex; align-items: center; gap: 6px; min-width: 0;">
            <span class="brand-sub mono" style="color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
              {{ skill.path }}
            </span>
            <el-button size="small" text @click="openDoc(skill)">文档</el-button>
            <el-button size="small" text @click="revealLocal(skill)" title="在访达中定位">本地</el-button>
          </div>
          <el-switch
            v-if="selectedInstance && skill.enabled !== false"
            :model-value="!!skill.subscribed"
            :loading="toggling.has(skill.name)"
            active-text="订阅"
            inactive-text=""
            @change="(v) => toggleSubscribe(skill, v)"
          />
          <span v-else-if="skill.enabled === false" class="brand-sub" style="color: var(--neon-red);">全局停用中</span>
          <span v-else class="brand-sub" style="color: var(--text-muted);">选实例…</span>
        </div>
      </div>
    </div>

    <el-drawer v-model="docDrawer.visible" :title="docDrawer.title" size="46%">
      <div style="padding: 0 8px;">
        <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 12px;">
          <span class="brand-sub mono" style="color: var(--text-muted);">{{ docDrawer.path }}</span>
          <el-button size="small" text @click="copyText(docDrawer.path)">复制路径</el-button>
          <el-button size="small" text @click="revealLocal({ global_key: docDrawer.key })">在访达中打开</el-button>
        </div>
        <div class="md-body" v-html="renderMarkdown(docDrawer.content)"></div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref, watch } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { systemApi } from '@/api/client'
import { renderMarkdown } from '@/composables/useMarkdown'

const instances = ref([])
const skills = ref([])
const selectedInstance = ref('')
const toggling = reactive(new Set())
const globalToggling = reactive(new Set())

const docDrawer = reactive({ visible: false, title: '', path: '', content: '', key: '' })

async function openDoc(skill) {
  const d = await systemApi.skillContent(skill.global_key)
  if (d.error) { ElMessage.error(d.error); return }
  docDrawer.title = `${skill.name} · SKILL.md`
  docDrawer.path = d.path
  docDrawer.content = d.content || '（无 SKILL.md）'
  docDrawer.key = skill.global_key
  docDrawer.visible = true
}

async function revealLocal(skill) {
  const d = await systemApi.skillReveal(skill.global_key)
  if (d.error) { ElMessage.error(d.error); return }
  ElMessage.success(`已在访达定位：${d.path}`)
}

function copyText(t) {
  navigator.clipboard?.writeText(t)
  ElMessage.success('已复制')
}

function scopeLabel(skill) {
  if (skill.scope === 'personal') return `${skill.owner_name || '实例'} · 自建`
  return skill.scope
}
function scopeTagType(scope) {
  return { system: 'info', shared: 'warning', personal: 'success' }[scope] || 'info'
}

async function loadInstances() {
  const d = await systemApi.instances()
  if (d.error) return
  instances.value = d.instances || []
  if (!selectedInstance.value && instances.value.length) {
    selectedInstance.value = instances.value[0].id
  }
}

async function loadSkills() {
  const d = await systemApi.skills(selectedInstance.value)
  if (d.error) return
  skills.value = d.skills || []
}

async function loadAll() {
  await loadInstances()
  await loadSkills()
}

async function toggleGlobal(skill, enabled) {
  if (!skill.global_key) return
  globalToggling.add(skill.global_key)
  const d = await systemApi.toggleSkillGlobal(skill.global_key, enabled)
  globalToggling.delete(skill.global_key)
  if (d.error) {
    ElMessage.error(d.error)
    return
  }
  ElMessage.success(`${skill.name} 全局${enabled ? '启用' : '停用'}`)
  await loadSkills()
}

async function toggleSubscribe(skill, subscribed) {
  if (!selectedInstance.value) return
  toggling.add(skill.name)
  const d = await systemApi.subscribeSkill(selectedInstance.value, skill.name, subscribed)
  toggling.delete(skill.name)
  if (d.error) {
    ElMessage.error(d.error)
    return
  }
  ElMessage.success(`${skill.name} ${subscribed ? '已订阅' : '已退订'} · ${d.skills.length} 项`)
  await loadSkills()
}

watch(selectedInstance, loadSkills)
onMounted(loadAll)
</script>

<style scoped>
.skill-disabled {
  opacity: 0.55;
}
</style>
