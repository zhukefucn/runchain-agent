import { createRouter, createWebHistory, type RouteRecordRaw } from "vue-router";
import { useAuthStore, roleHome, type Role } from "@/stores/auth";
import { getToken, setUnauthorizedHandler } from "@/api/client";

const routes: RouteRecordRaw[] = [
  { path: "/login", component: () => import("@/views/LoginView.vue"), meta: { public: true } },
  { path: "/manager", component: () => import("@/views/ManagerView.vue"), meta: { role: "manager" } },
  { path: "/business", component: () => import("@/views/BusinessAdminView.vue"), meta: { role: "business_admin" } },
  { path: "/system", component: () => import("@/views/SystemAdminView.vue"), meta: { role: "system_admin" } },
  { path: "/:pathMatch(.*)*", redirect: "/login" },
];

export function createAppRouter() {
  const router = createRouter({ history: createWebHistory(), routes });
  setUnauthorizedHandler(() => {
    const auth = useAuthStore();
    auth.expire();
    if (router.currentRoute.value.path !== "/login") void router.replace("/login");
  });
  router.beforeEach(async (to) => {
    const auth = useAuthStore();
    if (!to.meta.public && (!auth.principal || !getToken())) await auth.restore();
    if (to.path === "/login" && auth.authenticated && auth.principal) return roleHome[auth.principal.role];
    if (!to.meta.public && !auth.authenticated) return "/login";
    const required = to.meta.role as Role | undefined;
    if (required && auth.principal?.role !== required) return roleHome[auth.principal!.role];
    return true;
  });
  return router;
}
