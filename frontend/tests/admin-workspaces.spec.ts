import { fireEvent, render, screen } from "@testing-library/vue";
import { expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";
import BusinessAdminView from "@/views/BusinessAdminView.vue";
import SystemAdminView from "@/views/SystemAdminView.vue";
import { createTestingApp, jsonResponse } from "./test-app";

it("business admin uploads a local ZIP and publishes a skill", async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/skills/upload")) return jsonResponse({ id: "skill-1", name: "guest-plan", version: "1.0.0", status: "draft", type: "python", validation_warnings: ["入口使用受控 Python Runner"] }, 201);
    if (url.endsWith("/publish")) return jsonResponse({ id: "skill-1", name: "guest-plan", version: "1.0.0", status: "published", type: "python", validation_warnings: ["入口使用受控 Python Runner"] });
    if (url.endsWith("/skills")) return jsonResponse({ items: [] });
    if (url.endsWith("/mcp-servers") || url.endsWith("/skill-invocations")) return jsonResponse({ items: [] });
    throw new Error(`${init?.method ?? "GET"} ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  const { pinia } = createTestingApp({ username: "business_admin01", role: "business_admin", authenticated: true });
  render(BusinessAdminView, { global: { plugins: [pinia] } });
  const file = new File(["zip"], "guest-plan.zip", { type: "application/zip" });
  await userEvent.upload(screen.getByLabelText("选择 Skill ZIP"), file);
  await fireEvent.click(screen.getByRole("button", { name: "安装 Skill" }));
  await screen.findByText("guest-plan");
  expect(screen.getByText("入口使用受控 Python Runner")).toBeInTheDocument();
  await fireEvent.click(screen.getByRole("button", { name: "发布" }));
  await screen.findByText("已发布");
  expect(fetchMock).toHaveBeenCalledWith("/api/business/skills/upload", expect.objectContaining({ body: file }));
});

it("business admin cannot submit a second mutation while one is pending", async () => {
  let finishUpload!: (response: Response) => void;
  const pendingUpload = new Promise<Response>((resolve) => { finishUpload = resolve; });
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/skills/upload")) return pendingUpload;
    return jsonResponse({ items: [] });
  }));
  const { pinia } = createTestingApp({ username: "business_admin01", role: "business_admin", authenticated: true });
  render(BusinessAdminView, { global: { plugins: [pinia] } });
  const file = new File(["zip"], "pending.zip", { type: "application/zip" });
  await userEvent.upload(screen.getByLabelText("选择 Skill ZIP"), file);
  const install = screen.getByRole("button", { name: "安装 Skill" });
  await fireEvent.click(install);
  expect(install).toBeDisabled();
  expect(screen.getByLabelText("选择 Skill ZIP")).toBeDisabled();
  finishUpload(jsonResponse({ id: "skill-pending", name: "pending", version: "1.0.0", status: "draft", type: "python" }, 201));
  await screen.findByText("pending");
});

it("business admin shows tools returned by an MCP connectivity test", async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/skills") || url.endsWith("/skill-invocations")) return jsonResponse({ items: [] });
    if (url.endsWith("/mcp-servers")) return jsonResponse({ items: [{ id: "m1", name: "接站规划", transport: "stdio", status: "stopped" }] });
    if (url.endsWith("/mcp-servers/m1/test")) return jsonResponse({ server_id: "m1", healthy: true, tools: [{ name: "plan_pickup", description: "规划接站" }] });
    if (url.endsWith("/mcp-servers/m1/stop")) return jsonResponse({ server_id: "m1", status: "stopped" });
    if (url.endsWith("/mcp-servers/m1/authorizations")) return jsonResponse({ id: "a1", server_id: "m1", user_id: "manager-id" });
    throw new Error(url);
  });
  vi.stubGlobal("fetch", fetchMock);
  const { pinia } = createTestingApp({ username: "business_admin01", role: "business_admin", authenticated: true });
  render(BusinessAdminView, { global: { plugins: [pinia] } });
  await fireEvent.click(screen.getByRole("button", { name: /MCP Server/ }));
  await fireEvent.click(await screen.findByRole("button", { name: "测试连接" }));
  expect(await screen.findByText("plan_pickup")).toBeInTheDocument();
  await fireEvent.click(screen.getByRole("button", { name: "停止" }));
  expect(fetchMock).toHaveBeenCalledWith(
    "/api/business/mcp-servers/m1/stop",
    expect.objectContaining({ method: "POST" }),
  );
  expect(await screen.findByText(/stdio · stopped/)).toBeInTheDocument();
  await fireEvent.update(screen.getByLabelText("授权 接站规划 MCP 给经理"), "manager-id");
  await fireEvent.click(screen.getByRole("button", { name: "授权" }));
  expect(fetchMock).toHaveBeenCalledWith(
    "/api/business/mcp-servers/m1/authorizations",
    expect.objectContaining({ method: "POST" }),
  );
});

it("system admin creates a manager and sees only safe model metadata", async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/users") && init?.method === "POST") return jsonResponse({ id: "u2", username: "manager0003", role: "manager", is_active: true }, 201);
    if (url.endsWith("/users")) return jsonResponse({ items: [] });
    if (url.endsWith("/model/status")) return jsonResponse({ configured: true, model: "step-3.7-flash", base_url: "https://api.stepfun.com/step_plan/v1", connectivity: "not_checked" });
    if (url.endsWith("/audit-logs")) return jsonResponse({ items: [] });
    throw new Error(url);
  });
  vi.stubGlobal("fetch", fetchMock);
  const { pinia } = createTestingApp({ username: "system_admin01", role: "system_admin", authenticated: true });
  render(SystemAdminView, { global: { plugins: [pinia] } });
  await fireEvent.update(screen.getByLabelText("新用户名"), "manager0003");
  await fireEvent.update(screen.getByLabelText("初始密码"), "12345678");
  await fireEvent.click(screen.getByRole("button", { name: "创建用户" }));
  await screen.findByText("manager0003");
  await fireEvent.click(screen.getByRole("button", { name: /模型状态/ }));
  expect(screen.getByText("step-3.7-flash")).toBeInTheDocument();
  expect(document.body.textContent).not.toMatch(/api[_ -]?key|密钥/i);
});
