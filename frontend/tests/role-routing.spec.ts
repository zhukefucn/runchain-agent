import { fireEvent, render, screen, waitFor } from "@testing-library/vue";
import { createTestingApp, jsonResponse } from "./test-app";
import { expect, it } from "vitest";
import { apiRequest } from "@/api/client";
import { useChatStore } from "@/stores/chat";
import { vi } from "vitest";

const cases = [
  ["manager0001", "manager", "/manager"],
  ["business_admin01", "business_admin", "/business"],
  ["system_admin01", "system_admin", "/system"],
] as const;

it.each(cases)("routes %s to its server-verified workspace", async (username, role, path) => {
  const { router, pinia, App } = createTestingApp({ username, role });
  await router.isReady();
  render(App, { global: { plugins: [pinia, router] } });

  await fireEvent.update(screen.getByLabelText("用户名"), username);
  await fireEvent.update(screen.getByLabelText("密码"), "12345678");
  await fireEvent.click(screen.getByRole("button", { name: "安全登录" }));

  await waitFor(() => expect(router.currentRoute.value.path).toBe(path));
  expect(localStorage.getItem("runchain_token")).toBe("test-token");
  expect(localStorage.getItem("role")).toBeNull();
  expect(localStorage.getItem("user_id")).toBeNull();
});

it("blocks a manager from navigating to either admin workspace", async () => {
  const { router, auth } = createTestingApp({ username: "manager0001", role: "manager", authenticated: true });
  await router.push("/system");
  await router.isReady();
  expect(router.currentRoute.value.path).toBe("/manager");
  expect(auth.principal?.role).toBe("manager");
});

it("clears the complete auth session and returns to login on API 401", async () => {
  const { router, auth } = createTestingApp({ username: "manager0001", role: "manager", authenticated: true });
  await router.push("/manager");
  vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ code: "TOKEN_INVALID", message: "expired" }, 401)));
  await expect(apiRequest("/api/manager/sessions")).rejects.toThrow("expired");
  await waitFor(() => expect(router.currentRoute.value.path).toBe("/login"));
  expect(auth.principal).toBeNull();
  expect(localStorage.getItem("runchain_token")).toBeNull();
});

it("does not deadlock when the active chat stream returns 401", async () => {
  const { router, auth } = createTestingApp({ username: "manager0001", role: "manager", authenticated: true });
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/sessions")) return jsonResponse({ items: [{ id: "s1", title: "stream", status: "active", agent_id: "reception-leader" }] });
    if (url.endsWith("/messages") || url.endsWith("/skills") || url.endsWith("/files")) return jsonResponse({ items: [] });
    if (url.endsWith("/chat")) return jsonResponse({ code: "TOKEN_INVALID", message: "expired" }, 401);
    throw new Error(url);
  }));
  const store = useChatStore();
  await store.load();
  await Promise.race([
    store.send("test"),
    new Promise<never>((_, reject) => setTimeout(() => reject(new Error("stream 401 deadlocked")), 500)),
  ]);
  await waitFor(() => expect(router.currentRoute.value.path).toBe("/login"));
  expect(auth.principal).toBeNull();
});
