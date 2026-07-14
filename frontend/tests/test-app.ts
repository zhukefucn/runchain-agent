import { createPinia, setActivePinia } from "pinia";
import { vi } from "vitest";
import { createAppRouter } from "@/router";
import { useAuthStore, type Role } from "@/stores/auth";
import App from "@/App.vue";

export function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", "X-Request-ID": "req-test" } });
}

export function sseResponse(chunks: string[]) {
  const encoder = new TextEncoder();
  return new Response(new ReadableStream({ start(controller) { chunks.forEach((chunk) => controller.enqueue(encoder.encode(chunk))); controller.close(); } }), { headers: { "Content-Type": "text/event-stream" } });
}

export function createTestingApp(options: { username: string; role: Role; authenticated?: boolean }) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const auth = useAuthStore();
  if (options.authenticated) auth.hydrateForTest("test-token", { user_id: "server-id", username: options.username, role: options.role, tenant_id: "bank_demo" });
  if (!options.authenticated) {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/auth/login")) return jsonResponse({ access_token: "test-token", token_type: "bearer" });
      if (url.endsWith("/api/auth/me")) return jsonResponse({ user_id: "server-id", username: options.username, role: options.role, tenant_id: "bank_demo" });
      throw new Error(`unexpected ${url}`);
    }));
  }
  const router = createAppRouter();
  router.push("/login");
  return { pinia, router, auth, App };
}
