<template>
  <RouterView />
</template>

<script setup>
import { RouterView } from 'vue-router'
// App 仅作为根容器；具体内容由 vue-router 决定：
//   /system/*     → SystemLayout + 全局台 views
//   /instance/*   → InstanceLayout (=SystemLayout) + 实例 views
</script>

<style>
/* 旧 App.vue 的全局 style（global block）已随 LegacyEmployee.vue 一起搬走。
   新 App 只做容器；所有样式统一在 src/theme/{tokens,layout}.css + Element Plus 默认主题。
*/
</style>
