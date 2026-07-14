import { computed, ref } from "vue";
import { defineStore } from "pinia";
import { apiRequest, clearToken, getToken, setToken } from "@/api/client";

export type Role = "manager" | "business_admin" | "system_admin";
export type Principal = { user_id: string; role: Role; tenant_id: string; username?: string };

export const roleHome: Record<Role, string> = {
  manager: "/manager",
  business_admin: "/business",
  system_admin: "/system",
};

export const roleName: Record<Role, string> = {
  manager: "客户经理",
  business_admin: "业务管理员",
  system_admin: "系统管理员",
};

export const useAuthStore = defineStore("auth", () => {
  const principal = ref<Principal | null>(null);
  const displayName = ref("");
  const loading = ref(false);
  const authenticated = computed(() => Boolean(principal.value && getToken()));

  async function login(username: string, password: string) {
    loading.value = true;
    try {
      const token = await apiRequest<{ access_token: string }>("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });
      setToken(token.access_token);
      principal.value = await apiRequest<Principal>("/api/auth/me");
      displayName.value = username;
      return principal.value;
    } catch (error) {
      clearToken();
      principal.value = null;
      throw error;
    } finally {
      loading.value = false;
    }
  }

  async function restore() {
    if (!getToken()) return null;
    try {
      principal.value = await apiRequest<Principal>("/api/auth/me");
      return principal.value;
    } catch {
      logout();
      return null;
    }
  }

  function logout() {
    clearToken();
    principal.value = null;
    displayName.value = "";
  }

  function hydrateForTest(token: string, value: Principal) {
    setToken(token);
    principal.value = value;
    displayName.value = value.username || "";
  }

  return { principal, displayName, loading, authenticated, login, restore, logout, hydrateForTest };
});
