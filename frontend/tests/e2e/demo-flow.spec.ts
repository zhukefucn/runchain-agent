import { expect, test, type Page } from "@playwright/test";
import { strToU8, zipSync } from "fflate";
import path from "node:path";

const PASSWORD = "12345678";

type Principal = { user_id: string; role: string };

async function login(page: Page, username: string, home: string) {
  await page.goto("/login");
  await page.locator('input[autocomplete="username"]').fill(username);
  await page.locator('input[autocomplete="current-password"]').fill(PASSWORD);
  await page.getByRole("button", { name: "安全登录" }).click();
  await expect(page).toHaveURL(new RegExp(`${home}$`));
  return api<Principal>(page, "/api/auth/me");
}

async function api<T>(
  page: Page,
  url: string,
  init: { method?: string; body?: unknown } = {},
): Promise<T> {
  const result = await page.evaluate(
    async ({ url, init }) => {
      const token = localStorage.getItem("runchain_token");
      const response = await fetch(url, {
        method: init.method,
        headers: {
          Authorization: `Bearer ${token}`,
          ...(init.body === undefined ? {} : { "Content-Type": "application/json" }),
        },
        body: init.body === undefined ? undefined : JSON.stringify(init.body),
      });
      return { status: response.status, body: await response.json() };
    },
    { url, init },
  );
  if (result.status >= 400) throw new Error(`${url} returned ${result.status}`);
  return result.body as T;
}

async function apiStatus(page: Page, url: string) {
  return page.evaluate(async (target) => {
    const token = localStorage.getItem("runchain_token");
    return (await fetch(target, { headers: { Authorization: `Bearer ${token}` } })).status;
  }, url);
}

function skillZip(name: string) {
  const root = `${name}-package`;
  const manifest = {
    id: name,
    name,
    version: "1.0.0",
    type: "python",
    entrypoint: "scripts/main.py",
    description: "Playwright local Skill installation fixture",
    parameters: { type: "object", properties: {}, additionalProperties: false },
  };
  return Buffer.from(zipSync({
    [`${root}/skill.json`]: strToU8(JSON.stringify(manifest)),
    [`${root}/SKILL.md`]: strToU8("# Browser-installed demo Skill\n\nDeterministic Phase 1 fixture."),
    [`${root}/scripts/main.py`]: strToU8('import json,sys\nsys.stdout.write(json.dumps({"mock": True}))\n'),
  }));
}

async function runReception(page: Page, username: string) {
  const principal = await login(page, username, "/manager");
  const [created] = await Promise.all([
    page.waitForResponse(
      (response) => response.url().endsWith("/api/manager/sessions")
        && response.request().method() === "POST",
    ),
    page.getByTitle("新建会话").click(),
  ]);
  expect(created.status()).toBe(201);
  const sessionId = (await created.json() as { id: string }).id;
  await page.getByLabel("给接待主管发送消息").fill(
    "接待 4 位远方客人，明天 18:00 到南京南站，安排接站、住宿和清淡餐饮。",
  );
  await page.getByRole("button", { name: /发送/ }).click();
  await expect(page.locator(".hitl-card")).toBeVisible({ timeout: 45_000 });
  await expect(page.locator(".agent-roster")).toContainText("接站");
  await expect(page.locator(".agent-roster")).toContainText("住宿");
  await expect(page.locator(".agent-roster")).toContainText("餐饮");
  await expect(page.locator(".timeline")).toContainText("调用工具");
  await page.getByRole("button", { name: "确认方案" }).click();
  await expect(page.getByLabel("最终接待方案")).toBeVisible();
  const publicSessions = await api<{ items: Array<{ title: string }> }>(
    page,
    "/api/manager/sessions",
  );
  expect(publicSessions.items.every((session) => !session.title.startsWith("team:"))).toBe(true);
  expect((await page.locator(".session-item").allTextContents()).join(" ")).not.toContain("team:");
  const finalPlan = page.getByLabel("最终接待方案");
  await expect(finalPlan).not.toContainText(principal.user_id);
  await expect(finalPlan).not.toContainText(/team_id|worker_ids|_agentscope/);
  return { principal, sessionId };
}

test("approved 11-step multi-tenant browser demonstration", async ({ browser }) => {
  const suffix = `${Date.now()}`.slice(-10);
  const skillName = `e2e-skill-${suffix}`;
  const mcpName = `e2e-mcp-${suffix}`;

  const manager1Context = await browser.newContext();
  const manager1Page = await manager1Context.newPage();
  const manager1 = await login(manager1Page, "manager0001", "/manager");
  await manager1Context.close();

  const manager2Context = await browser.newContext();
  const manager2Page = await manager2Context.newPage();
  const manager2 = await login(manager2Page, "manager0002", "/manager");
  await manager2Context.close();

  const businessContext = await browser.newContext();
  const business = await businessContext.newPage();
  await login(business, "business_admin01", "/business");
  await business.goto("/manager");
  await expect(business).toHaveURL(/\/business$/);
  expect(await apiStatus(business, "/api/manager/sessions")).toBe(403);

  await business.locator('input[type="file"]').setInputFiles({
    name: `${skillName}.zip`,
    mimeType: "application/zip",
    buffer: skillZip(skillName),
  });
  await business.getByRole("button", { name: "安装 Skill" }).click();
  const skillRow = business.locator("tbody tr", { hasText: skillName });
  await expect(skillRow).toBeVisible();
  await skillRow.getByRole("button", { name: "发布" }).click();
  await expect(skillRow).toContainText("已发布");
  const authorization = skillRow.getByLabel(`授权 ${skillName} 给经理`);
  await authorization.fill(manager1.user_id);
  await skillRow.getByRole("button", { name: "授权" }).click();

  await business.getByRole("button", { name: /MCP Server/ }).click();
  await business.getByPlaceholder("mock-reception").fill(mcpName);
  await business.getByPlaceholder("python").fill(
    path.resolve(process.cwd(), "..", ".venv", "Scripts", "python.exe"),
  );
  await business.getByPlaceholder("scripts/mock_mcp.py").fill("mock_pickup_server.py");
  await business.getByRole("button", { name: "登记 Server" }).click();
  const serverRow = business.locator(".server-row", { hasText: mcpName });
  await expect(serverRow).toBeVisible();
  await serverRow.getByRole("button", { name: "测试连接" }).click();
  await expect(serverRow).toContainText("healthy", { timeout: 30_000 });
  await expect(business.getByText("plan_pickup", { exact: true })).toBeVisible();
  await businessContext.close();

  const run1Context = await browser.newContext();
  const run1Page = await run1Context.newPage();
  const run1 = await runReception(run1Page, "manager0001");
  expect(run1.principal.user_id).toBe(manager1.user_id);
  await expect(run1Page.getByText(skillName, { exact: true })).toHaveCount(0);
  await run1Page.getByRole("button", { name: "资源" }).click();
  await expect(run1Page.getByText(skillName, { exact: true })).toBeVisible();
  await run1Context.close();

  const run2Context = await browser.newContext();
  const run2Page = await run2Context.newPage();
  const run2 = await runReception(run2Page, "manager0002");
  expect(run2.principal.user_id).toBe(manager2.user_id);
  expect(await apiStatus(run2Page, `/api/manager/sessions/${run1.sessionId}/messages`)).toBe(404);
  const manager2Skills = await api<{ items: Array<{ name: string }> }>(
    run2Page,
    "/api/manager/skills",
  );
  expect(manager2Skills.items.some((skill) => skill.name === skillName)).toBe(false);
  await run2Page.getByRole("button", { name: "资源" }).click();
  await expect(run2Page.getByText(skillName, { exact: true })).toHaveCount(0);
  await run2Page.goto("/system");
  await expect(run2Page).toHaveURL(/\/manager$/);
  expect(await apiStatus(run2Page, "/api/system/users")).toBe(403);
  await run2Context.close();

  const systemContext = await browser.newContext();
  const system = await systemContext.newPage();
  await login(system, "system_admin01", "/system");
  await expect(system.getByText("manager0001", { exact: true })).toBeVisible();
  await expect(system.getByText("manager0002", { exact: true })).toBeVisible();
  await system.getByRole("button", { name: "模型状态" }).click();
  await expect(system.getByText("step-3.7-flash", { exact: true })).toBeVisible();
  await system.getByRole("button", { name: "审计日志" }).click();
  await expect(system.getByText("manager.chat.start", { exact: true }).first()).toBeVisible();
  await system.goto("/business");
  await expect(system).toHaveURL(/\/system$/);
  expect(await apiStatus(system, "/api/business/skills")).toBe(403);
  await systemContext.close();
});
