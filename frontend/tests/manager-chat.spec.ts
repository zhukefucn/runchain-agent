import { fireEvent, render, screen, waitFor } from "@testing-library/vue";
import { describe, expect, it, vi } from "vitest";
import ManagerView from "@/views/ManagerView.vue";
import { parseSseStream } from "@/api/sse";
import { createTestingApp, jsonResponse, sseResponse } from "./test-app";
import { RECEPTION_AGENT_ID, useChatStore } from "@/stores/chat";
import FinalPlan from "@/components/FinalPlan.vue";

describe("manager workspace", () => {
  it("renders the ordinary Agent workbench with an explicit expert-team entry and result drawer", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/sessions") || url.endsWith("/skills") || url.endsWith("/files")) return jsonResponse({ items: [] });
      throw new Error(url);
    }));
    const { pinia } = createTestingApp({ username: "manager0001", role: "manager", authenticated: true });
    render(ManagerView, { global: { plugins: [pinia] } });

    expect(await screen.findByRole("heading", { name: "润辰智能助手" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "新建普通对话" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "接待专家团（演示）" })).toBeInTheDocument();
    await fireEvent.click(screen.getByRole("button", { name: "打开结果" }));
    expect(screen.getByRole("complementary", { name: "结果展示" })).toHaveClass("result-open");
    expect(screen.getByRole("button", { name: "业务结果" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "文件" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "执行详情" })).toBeInTheDocument();
  });

  it("creates expert mode explicitly and keeps result visibility session-scoped", async () => {
    const bodies: unknown[] = [];
    vi.stubGlobal("fetch", vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body));
      bodies.push(body);
      return jsonResponse({ id: `s-${bodies.length}`, title: body.title, status: "active", agent_id: body.agent_id });
    }));
    const { pinia } = createTestingApp({ username: "manager0001", role: "manager", authenticated: true });
    const store = useChatStore(pinia);

    await store.createForMode(RECEPTION_AGENT_ID);
    store.setResultOpen(true);
    expect(store.mode).toBe(RECEPTION_AGENT_ID);
    expect(store.resultOpen).toBe(true);
    expect(bodies[0]).toEqual({ title: "接待专家团 1", agent_id: "reception-leader" });

    await store.createForMode("general-assistant");
    expect(store.mode).toBe("general-assistant");
    expect(store.resultOpen).toBe(false);
  });

  it("creates the first ordinary-Agent session automatically when the manager sends from an empty workspace", async () => {
    const requests: Array<{ url: string; body?: unknown }> = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
      requests.push({ url, body });
      if (url.endsWith("/api/manager/sessions")) {
        return jsonResponse({ id: "s-new", title: "新对话 1", status: "active", agent_id: "general-assistant" });
      }
      if (url.endsWith("/chat")) {
        return sseResponse([
          'data: {"type":"token","request_id":"req-new","session_id":"s-new","run_id":"r-new","data":{"text":"收到"}}\n\n',
        ]);
      }
      throw new Error(`unexpected ${url}`);
    }));
    const { pinia } = createTestingApp({ username: "manager0001", role: "manager", authenticated: true });
    const store = useChatStore(pinia);

    await store.send("请安排接待");

    expect(requests.map((request) => request.url)).toEqual([
      "/api/manager/sessions",
      "/api/manager/sessions/s-new/chat",
    ]);
    expect(requests[0].body).toEqual({ title: "新对话 1", agent_id: "general-assistant" });
    expect(requests[1].body).toEqual({ prompt: "请安排接待" });
    expect(store.currentId).toBe("s-new");
    expect(store.assistantText).toBe("收到");
  });

  it("renders operational plan fields without internal owner or AgentScope IDs", () => {
    render(FinalPlan, {
      props: {
        plan: {
          owner: "dcdd9434-55c3-4aea-b5d4-2223c5c0a904",
          pickup: { vehicle: "商务车", team_id: "team-secret" },
          lodging: { hotel: "演示酒店", worker_ids: ["worker-secret"] },
          _agentscope: {
            team_id: "internal-team",
            worker_ids: ["internal-worker"],
          },
        },
      },
    });

    expect(screen.getByText("商务车")).toBeInTheDocument();
    expect(screen.getByText("演示酒店")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(
      /dcdd9434|owner|team_id|worker_ids|team-secret|worker-secret|internal-team/,
    );
  });
  it("parses split SSE frames and preserves the stable event timeline", async () => {
    const response = sseResponse([
      'event: token\ndata: {"type":"token","request_id":"req-1","session_id":"s1","run_id":"r1","data":{"text":"主管已拆解任务"}}\n\n',
      'event: tool_call\ndata: {"type":"tool_call","request_id":"req-1","session_id":"s1","run_id":"r1","data":{"agent_type":"pickup","tool_name":"plan_pickup"}}\n',
      '\nevent: hitl_pending\ndata: {"type":"hitl_pending","request_id":"req-1","session_id":"s1","run_id":"r1","data":{"request_id":"hitl-1","summary":{"pickup":{"vehicle":"商务车"}}}}\n\n',
    ]);
    const events = [];
    for await (const event of parseSseStream(response)) events.push(event);
    expect(events.map((event) => event.type)).toEqual(["token", "tool_call", "hitl_pending"]);
    expect(events[1].data.agent_type).toBe("pickup");
  });

  it("parses CRLF and multiline data when delimiters split across chunks", async () => {
    const payload = '{"type":"run_started","request_id":"req-2","session_id":"s2","run_id":"r2",\n"data":{}}';
    const wire = `: heartbeat\r\nevent: run_started\r\ndata: ${payload.split("\n")[0]}\r\ndata: ${payload.split("\n")[1]}\r\n\r\n`;
    const response = sseResponse([...wire]);
    const events = [];
    for await (const event of parseSseStream(response)) events.push(event);
    expect(events).toHaveLength(1);
    expect(events[0].type).toBe("run_started");
  });

  it("sends only prompt authority data, renders timeline, and confirms HITL", async () => {
    const requests: Array<{ url: string; body?: unknown; headers: Headers }> = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const headers = new Headers(init?.headers);
      const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
      requests.push({ url, body, headers });
      if (url.endsWith("/api/manager/sessions")) return jsonResponse({ items: [{ id: "s1", title: "远方专家团接待", status: "active", agent_id: "reception-leader" }] });
      if (url.includes("/messages")) return jsonResponse({ items: [] });
      if (url.endsWith("/skills") || url.endsWith("/files")) return jsonResponse({ items: [] });
      if (url.endsWith("/chat")) return sseResponse([
        'data: {"type":"agent_started","request_id":"req-1","session_id":"s1","run_id":"r1","data":{"agent_type":"pickup"}}\n\n',
        'data: {"type":"token","request_id":"req-1","session_id":"s1","run_id":"r1","data":{"text":"正在统筹接待方案。"}}\n\n',
        'data: {"type":"hitl_pending","request_id":"req-1","session_id":"s1","run_id":"r1","data":{"request_id":"hitl-1","summary":{}}}\n\n',
      ]);
      if (url.includes("/hitl/")) return jsonResponse({ request_id: "hitl-1", status: "approved", events: [] });
      throw new Error(`unexpected ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { pinia } = createTestingApp({ username: "manager0001", role: "manager", authenticated: true });
    render(ManagerView, { global: { plugins: [pinia] } });

    expect((await screen.findAllByText("远方专家团接待")).length).toBeGreaterThan(0);
    await fireEvent.update(screen.getByLabelText("给接待专家团发送消息"), "请安排周五晚到站的三位客人");
    await fireEvent.click(screen.getByRole("button", { name: /^发送/ }));
    await screen.findByText("正在统筹接待方案。");
    await fireEvent.click(screen.getByRole("button", { name: "打开结果" }));
    await fireEvent.click(screen.getByRole("button", { name: "执行详情" }));
    expect(screen.getByText("接站专家")).toBeInTheDocument();
    await fireEvent.click(await screen.findByRole("button", { name: "确认方案" }));
    await waitFor(() => expect(screen.getByText("方案已确认")).toBeInTheDocument());
    expect(requests.find((request) => request.url.includes("/hitl/"))?.body).toEqual({ decision: "approve", modifications: null });

    const chat = requests.find((request) => request.url.endsWith("/chat"));
    expect(chat?.body).toEqual({ prompt: "请安排周五晚到站的三位客人" });
    expect(chat?.headers.get("Authorization")).toBe("Bearer test-token");
    expect(JSON.stringify(requests)).not.toMatch(/owner_user_id|user_id|\"role\"/);
    expect(screen.getByRole("complementary", { name: "结果展示" })).toHaveClass("result-open");
  });

  it("reconstructs a persisted pending HITL event and human transcript", async () => {
    const terminal = { type: "hitl_pending", request_id: "req-p", session_id: "s1", run_id: "r1", data: { request_id: "hitl-p", summary: { pickup: { vehicle: "商务车" } } } };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/sessions")) return jsonResponse({ items: [{ id: "s1", title: "持久会话", status: "active", agent_id: "reception-leader" }] });
      if (url.endsWith("/messages")) return jsonResponse({ items: [{ id: "m1", session_id: "s1", role: "user", content: "安排接待" }, { id: "m2", session_id: "s1", role: "assistant", content: JSON.stringify(terminal) }] });
      if (url.endsWith("/skills") || url.endsWith("/files")) return jsonResponse({ items: [] });
      throw new Error(url);
    }));
    const { pinia } = createTestingApp({ username: "manager0001", role: "manager", authenticated: true });
    render(ManagerView, { global: { plugins: [pinia] } });
    await screen.findByText("安排接待");
    expect(screen.getByText("接待方案已汇总，等待你的确认。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确认方案" })).toBeInTheDocument();
  });

  it("reconstructs a later persisted decision and renders the structured final reception plan", async () => {
    const pending = { type: "hitl_pending", request_id: "req-p", session_id: "s1", run_id: "r1", data: { request_id: "hitl-p", summary: {} } };
    const resumed = { type: "token", request_id: "hitl-p", session_id: "s1", run_id: "r1", data: { phase: "resumed", decision: "approve" } };
    const complete = { type: "complete", request_id: "hitl-p", session_id: "s1", run_id: "r1", data: { decision: "approve", status: "approved", plan: { pickup: { vehicle: "商务车", time: "18:00" }, lodging: { hotel: "演示酒店" }, dining: { restaurant: "迎宾餐厅" } } } };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/sessions")) return jsonResponse({ items: [{ id: "s1", title: "已确认接待", status: "active", agent_id: "reception-leader" }] });
      if (url.endsWith("/messages")) return jsonResponse({ items: [pending, resumed, complete].map((event, index) => ({ id: `m${index}`, role: "assistant", content: JSON.stringify(event) })) });
      if (url.endsWith("/skills") || url.endsWith("/files")) return jsonResponse({ items: [] });
      throw new Error(url);
    }));
    const { pinia } = createTestingApp({ username: "manager0001", role: "manager", authenticated: true });
    render(ManagerView, { global: { plugins: [pinia] } });
    expect(await screen.findByRole("heading", { name: "最终接待方案" })).toBeInTheDocument();
    expect(screen.getByText("商务车")).toBeInTheDocument();
    expect(screen.getByText("演示酒店")).toBeInTheDocument();
    expect(screen.getByText("迎宾餐厅")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "确认方案" })).not.toBeInTheDocument();
  });

  it.each([
    ["confirm", "approve", "方案已确认"],
    ["cancel", "reject", "方案已取消"],
    ["modify", "modification", "修改意见已提交"],
  ] as const)("consumes %s HITL response events", async (decision, wireDecision, label) => {
    const terminal = { type: "hitl_pending", request_id: "req-p", session_id: "s1", run_id: "r1", data: { request_id: "hitl-p", summary: {} } };
    const complete = { type: "complete", request_id: "hitl-p", session_id: "s1", run_id: "r1", data: { decision: wireDecision, status: "completed", plan: { hotel: "演示酒店" } } };
    const calls: unknown[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/sessions")) return jsonResponse({ items: [{ id: "s1", title: "持久会话", status: "active", agent_id: "reception-leader" }] });
      if (url.endsWith("/messages")) return jsonResponse({ items: [{ id: "m2", session_id: "s1", role: "assistant", content: JSON.stringify(terminal) }] });
      if (url.endsWith("/skills") || url.endsWith("/files")) return jsonResponse({ items: [] });
      if (url.includes("/hitl/")) { calls.push(JSON.parse(String(init?.body))); return jsonResponse({ request_id: "hitl-p", status: "completed", events: [{ ...complete, type: "token", data: { phase: "resumed", decision: wireDecision } }, complete] }); }
      throw new Error(url);
    }));
    const { pinia } = createTestingApp({ username: "manager0001", role: "manager", authenticated: true });
    const store = useChatStore();
    await store.load();
    await store.decideHitl(store.hitl!, decision, decision === "modify" ? { note: "靠窗" } : null);
    expect(calls[0]).toEqual({ decision: wireDecision, modifications: decision === "modify" ? { note: "靠窗" } : null });
    expect(store.hitl).toBeUndefined();
    expect(store.events[store.events.length - 1]?.type).toBe("complete");
    expect(store.messages[store.messages.length - 1]?.content).toContain(label);
    expect(store.finalPlan).toEqual({ hotel: "演示酒店" });
  });
});
