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
    [`${root}/scripts/main.py`]: strToU8(`import json,sys\npayload=json.load(sys.stdin)\nsys.stdout.write(json.dumps({"runner_marker":"${name}-executed","received_prompt":bool(payload.get("prompt"))}))\n`),
  }));
}

async function runReception(page: Page, username: string, sharedSkillName: string) {
  const principal = await login(page, username, "/manager");
  const [created] = await Promise.all([
    page.waitForResponse(
      (response) => response.url().endsWith("/api/manager/sessions")
        && response.request().method() === "POST",
    ),
    page.getByRole("button", { name: "接待专家团（演示）" }).click(),
  ]);
  expect(created.status()).toBe(201);
  const sessionId = (await created.json() as { id: string }).id;
  await page.getByLabel("给接待专家团发送消息").fill(
    "接待 4 位远方客人，明天 18:00 到南京南站，安排接站、住宿和清淡餐饮。",
  );
  const [chatResponse] = await Promise.all([
    page.waitForResponse(
      (response) => response.url().endsWith(`/api/manager/sessions/${sessionId}/chat`)
        && response.request().method() === "POST",
    ),
    page.getByRole("button", { name: /发送/ }).click(),
  ]);
  const chatRequestId = chatResponse.headers()["x-request-id"];
  expect(chatRequestId).toBeTruthy();
  await expect(page.locator(".hitl-card")).toBeVisible({ timeout: 45_000 });
  await expect(page.locator(".agent-roster")).toContainText("接站");
  await expect(page.locator(".agent-roster")).toContainText("住宿");
  await expect(page.locator(".agent-roster")).toContainText("餐饮");
  await page.getByRole("button", { name: "打开结果" }).click();
  await page.getByRole("button", { name: "执行详情" }).click();
  await expect(page.locator(".timeline")).toContainText("调用工具");
  await page.getByRole("button", { name: "确认方案" }).click();
  await page.getByRole("button", { name: "业务结果" }).click();
  await expect(page.getByLabel("最终接待方案")).toBeVisible();
  const publicSessions = await api<{ items: Array<{ title: string }> }>(
    page,
    "/api/manager/sessions",
  );
  expect(publicSessions.items.every((session) => !session.title.startsWith("team:"))).toBe(true);
  expect((await page.locator(".session-item").allTextContents()).join(" ")).not.toContain("team:");
  const finalPlan = page.getByLabel("最终接待方案");
  await expect(finalPlan).toContainText(`${sharedSkillName}-executed`);
  await expect(finalPlan).toContainText("七座商务车（Mock）");
  await expect(finalPlan).not.toContainText(principal.user_id);
  await expect(finalPlan).not.toContainText(/team_id|worker_ids|_agentscope/);
  return { principal, sessionId, chatRequestId };
}

test("approved 11-step multi-tenant browser demonstration", async ({ browser }) => {
  const suffix = `${Date.now()}`.slice(-10);
  const skillName = `shared-skill-${suffix}`;
  const privateSkillName = `private-skill-${suffix}`;
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
  await authorization.fill(manager2.user_id);
  await skillRow.getByRole("button", { name: "授权" }).click();

  await business.locator('input[type="file"]').setInputFiles({
    name: `${privateSkillName}.zip`,
    mimeType: "application/zip",
    buffer: skillZip(privateSkillName),
  });
  await business.getByRole("button", { name: "安装 Skill" }).click();
  const privateSkillRow = business.locator("tbody tr", { hasText: privateSkillName });
  await expect(privateSkillRow).toBeVisible();
  await privateSkillRow.getByRole("button", { name: "发布" }).click();
  const privateAuthorization = privateSkillRow.getByLabel(`授权 ${privateSkillName} 给经理`);
  await privateAuthorization.fill(manager1.user_id);
  await privateSkillRow.getByRole("button", { name: "授权" }).click();

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
  const mcpAuthorization = serverRow.getByLabel(`授权 ${mcpName} MCP 给经理`);
  for (const managerId of [manager1.user_id, manager2.user_id]) {
    await mcpAuthorization.fill(managerId);
    await serverRow.getByRole("button", { name: "授权" }).click();
  }
  await businessContext.close();

  const run1Context = await browser.newContext();
  const run1Page = await run1Context.newPage();
  const run1 = await runReception(run1Page, "manager0001", skillName);
  expect(run1.principal.user_id).toBe(manager1.user_id);
  await expect(run1Page.getByText(skillName, { exact: true })).toHaveCount(0);
  await run1Page.getByRole("button", { name: "文件" }).click();
  await expect(run1Page.getByText(skillName, { exact: true })).toBeVisible();
  const manager1Skills = await api<{ items: Array<{ id: string; name: string }> }>(run1Page, "/api/manager/skills");
  const privateSkillId = manager1Skills.items.find((skill) => skill.name === privateSkillName)!.id;
  const privateResult = await api<{ output: { runner_marker: string } }>(
    run1Page,
    `/api/manager/sessions/${run1.sessionId}/skills/${privateSkillId}/invoke`,
    { method: "POST", body: { input_data: { probe: true } } },
  );
  expect(privateResult.output.runner_marker).toBe(`${privateSkillName}-executed`);
  await run1Context.close();

  const run2Context = await browser.newContext();
  const run2Page = await run2Context.newPage();
  const run2 = await runReception(run2Page, "manager0002", skillName);
  expect(run2.principal.user_id).toBe(manager2.user_id);
  expect(await apiStatus(run2Page, `/api/manager/sessions/${run1.sessionId}/messages`)).toBe(404);
  expect(await run2Page.evaluate(async ({ sessionId, skillId }) => {
    const token = localStorage.getItem("runchain_token");
    return (await fetch(`/api/manager/sessions/${sessionId}/skills/${skillId}/invoke`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ input_data: { probe: true } }),
    })).status;
  }, { sessionId: run2.sessionId, skillId: privateSkillId })).toBe(404);
  const manager2Skills = await api<{ items: Array<{ name: string }> }>(
    run2Page,
    "/api/manager/skills",
  );
  expect(manager2Skills.items.some((skill) => skill.name === skillName)).toBe(true);
  expect(manager2Skills.items.some((skill) => skill.name === privateSkillName)).toBe(false);
  await run2Page.getByRole("button", { name: "文件" }).click();
  await expect(run2Page.getByText(skillName, { exact: true })).toBeVisible();
  await expect(run2Page.getByText(privateSkillName, { exact: true })).toHaveCount(0);
  await run2Page.goto("/system");
  await expect(run2Page).toHaveURL(/\/manager$/);
  expect(await apiStatus(run2Page, "/api/system/users")).toBe(403);
  await run2Context.close();

  const systemContext = await browser.newContext();
  const system = await systemContext.newPage();
  await login(system, "system_admin01", "/system");
  await expect(system.getByText("manager0001", { exact: true })).toBeVisible();
  await expect(system.getByText("manager0002", { exact: true })).toBeVisible();
  const manager2Row = system.locator("tbody tr", { hasText: "manager0002" });
  await manager2Row.getByRole("button", { name: "停用" }).click();
  await expect(manager2Row).toContainText("已停用");
  const disabledContext = await browser.newContext();
  const disabledPage = await disabledContext.newPage();
  await disabledPage.goto("/login");
  await disabledPage.locator('input[autocomplete="username"]').fill("manager0002");
  await disabledPage.locator('input[autocomplete="current-password"]').fill(PASSWORD);
  await disabledPage.getByRole("button", { name: "安全登录" }).click();
  await expect(disabledPage).toHaveURL(/\/login$/);
  await disabledContext.close();
  await manager2Row.getByRole("button", { name: "启用" }).click();
  await expect(manager2Row).toContainText("已启用");
  await system.getByRole("button", { name: "模型状态" }).click();
  await expect(system.getByText("step-3.7-flash", { exact: true })).toBeVisible();
  await system.getByRole("button", { name: "审计日志" }).click();
  await expect(system.getByText("manager.chat.start", { exact: true }).first()).toBeVisible();
  await expect(system.getByText("skill.invoke", { exact: true }).first()).toBeVisible();
  await expect(system.getByText("mcp.call", { exact: true }).first()).toBeVisible();
  await expect(system.getByText("manager.messages.read", { exact: true }).first()).toBeVisible();
  await expect(system.locator("tbody tr", { hasText: "skill.invoke" }).filter({ hasText: "failure" }).first()).toBeVisible();
  const currentAudits = await api<{
    items: Array<{ action: string; request_id: string | null }>;
  }>(system, "/api/system/audit-logs");
  expect(currentAudits.items.filter((row) => row.action === "system.user.patch")).toHaveLength(2);
  const mcpRequestIds = currentAudits.items
    .filter((row) => row.action === "mcp.call")
    .map((row) => row.request_id);
  expect(mcpRequestIds).toEqual(
    expect.arrayContaining([run1.chatRequestId, run2.chatRequestId]),
  );
  await system.goto("/business");
  await expect(system).toHaveURL(/\/system$/);
  expect(await apiStatus(system, "/api/business/skills")).toBe(403);
  await systemContext.close();
});
