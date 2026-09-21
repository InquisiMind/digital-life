<template>
  <div>
    <section class="page-hero">
      <div>
        <h1 class="page-title">Skill Market</h1>
        <p class="page-subtitle">系统内置 + 实例共享 + 实例自建 · 本页只管全局启停 · 实例级订阅请进实例详情 → 能力订阅</p>
      </div>
      <div style="display: flex; align-items: center; gap: 12px;">
        <el-tag effect="plain" type="info">全局 {{ enabledCount }}/{{ skills.length }} 启用</el-tag>
        <el-button @click="loadSkills"><el-icon><Refresh /></el-icon></el-button>
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
import { computed, onMounted, reactive, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { systemApi } from '@/api/client'
import { renderMarkdown } from '@/composables/useMarkdown'

const skills = ref([])
const globalToggling = reactive(new Set())

const docDrawer = reactive({ visible: false, title: '', path: '', content: '', key: '' })

const enabledCount = computed(() => skills.value.filter((s) => s.enabled !== false).length)

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

async function loadSkills() {
  const d = await systemApi.skills()
  if (d.error) return
  skills.value = d.skills || []
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

onMounted(loadSkills)
</script>

<style scoped>
.skill-disabled {
  opacity: 0.55;
}
</style>
