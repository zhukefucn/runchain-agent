import { fireEvent, render, screen, waitFor } from "@testing-library/vue";
import { describe, expect, it, vi } from "vitest";
import ManagerView from "@/views/ManagerView.vue";
import { parseSseStream } from "@/api/sse";
import { createTestingApp, jsonResponse, sseResponse } from "./test-app";

describe("manager workspace", () => {
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
    await fireEvent.update(screen.getByLabelText("给接待主管发送消息"), "请安排周五晚到站的三位客人");
    await fireEvent.click(screen.getByRole("button", { name: /^发送/ }));
    await screen.findByText("正在统筹接待方案。");
    expect(screen.getByText("接站专家")).toBeInTheDocument();
    await fireEvent.click(await screen.findByRole("button", { name: "确认方案" }));
    await waitFor(() => expect(screen.getByText("方案已确认")).toBeInTheDocument());
    expect(requests.find((request) => request.url.includes("/hitl/"))?.body).toEqual({ decision: "approve", modifications: null });

    const chat = requests.find((request) => request.url.endsWith("/chat"));
    expect(chat?.body).toEqual({ prompt: "请安排周五晚到站的三位客人" });
    expect(chat?.headers.get("Authorization")).toBe("Bearer test-token");
    expect(JSON.stringify(requests)).not.toMatch(/owner_user_id|user_id|\"role\"/);
  });
});
