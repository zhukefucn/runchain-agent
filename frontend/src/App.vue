<script setup lang="ts">
import { useRouter } from "vue-router";
import { roleName, useAuthStore } from "@/stores/auth";

const auth = useAuthStore();
const router = useRouter();
async function logout() {
  await auth.logout();
  await router.replace("/login");
}
</script>

<template>
  <RouterView v-if="$route.path === '/login'" />
  <div v-else class="app-frame">
    <header class="topbar">
      <div class="brand-lockup"><span class="brand-mark">R</span><div><strong>RunChain</strong><small>多租户智能体平台</small></div></div>
      <div class="identity" v-if="auth.principal">
        <span class="environment"><i></i> Windows DEMO</span>
        <span class="role-pill">{{ roleName[auth.principal.role] }}</span>
        <span>{{ auth.displayName || '已认证用户' }}</span>
        <button class="button ghost compact" @click="logout">退出</button>
      </div>
    </header>
    <RouterView />
  </div>
</template>
