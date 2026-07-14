<script setup lang="ts">
import { ref } from "vue";
import { useRouter } from "vue-router";
import { ApiError } from "@/api/client";
import { roleHome, useAuthStore } from "@/stores/auth";
import BrandLockup from "@/components/BrandLockup.vue";

const username = ref("");
const password = ref("");
const error = ref("");
const auth = useAuthStore();
const router = useRouter();

function fillManagerDemoCredentials() {
  username.value = "manager0001";
  password.value = "12345678";
}

async function submit() {
  error.value = "";
  try {
    const principal = await auth.login(username.value.trim(), password.value);
    await router.replace(roleHome[principal.role]);
  } catch (cause) {
    const request = cause instanceof ApiError && cause.requestId ? ` · 请求 ${cause.requestId}` : "";
    error.value = `${cause instanceof Error ? cause.message : "登录失败"}${request}`;
  }
}
</script>

<template>
  <main class="login-page">
    <section class="login-story">
      <BrandLockup subtitle="AgentScope 多租户智能体" light />
      <div class="story-copy"><p class="eyebrow">PHASE 1 · 通用技术底座</p><h1>每位客户经理，<br />拥有独立的智能工作空间。</h1><p>Agent、Skill、文件与运行记录按经理完全隔离；全局管理员仅执行治理职责。</p></div>
      <div class="architecture-strip"><span>Manager 隔离</span><span>Skill 治理</span><span>Multi-Agent</span></div>
    </section>
    <section class="login-panel">
      <form class="login-card" @submit.prevent="submit">
        <p class="eyebrow green">润辰智能体平台</p><h2>欢迎回来</h2><p class="muted">请使用 DEMO 账号进入对应工作台</p>
        <label>用户名<input v-model="username" aria-label="用户名" autocomplete="username" placeholder="manager0001" /></label>
        <label>密码<input v-model="password" aria-label="密码" autocomplete="current-password" type="password" placeholder="输入登录密码" /></label>
        <p v-if="error" class="alert error" role="alert">{{ error }}</p>
        <button class="button primary full" :disabled="auth.loading">{{ auth.loading ? '正在验证…' : '安全登录' }}</button>
        <div class="demo-accounts"><strong>演示账号</strong><p>客户经理：manager0001 / manager0002</p><p>业务管理员：business_admin01</p><p>系统管理员：system_admin01</p><small>以上账号初始密码均为 12345678</small><button type="button" class="button secondary compact full" @click="fillManagerDemoCredentials">填入 manager0001 演示账号</button></div>
      </form>
    </section>
  </main>
</template>
