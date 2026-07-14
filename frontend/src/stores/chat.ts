import { computed, reactive, ref } from "vue";
import { defineStore } from "pinia";
import { apiRequest, apiStream } from "@/api/client";
import { registerStreamCanceller } from "@/api/streams";
import { parseSseStream, type StableEvent } from "@/api/sse";

export type Session = { id: string; agent_id: string; title: string; status: string; created_at?: string };
export type Message = { id: string; role: string; content: string; ordinal?: number };
export type Skill = { id: string; name: string; version: string; type: string; description?: string; warnings?: string[] };
export type WorkspaceFile = { id: string; session_id: string; relative_path: string; created_at?: string };
type SessionState = { messages: Message[]; events: StableEvent[]; pending?: StableEvent; finalPlan?: unknown; liveText: string; loaded: boolean };
type HitlUiDecision = "confirm" | "modify" | "cancel";
type HitlResult = { request_id: string; status: string; events: StableEvent[] };

function blankState(): SessionState { return { messages: [], events: [], liveText: "", loaded: false }; }

function parseStableEvent(content: string): StableEvent | undefined {
  try {
    const value = JSON.parse(content) as Partial<StableEvent>;
    return typeof value.type === "string" && typeof value.session_id === "string" && typeof value.run_id === "string" && value.data && typeof value.data === "object" ? value as StableEvent : undefined;
  } catch { return undefined; }
}

function decisionLabel(decision: unknown) {
  return decision === "approve" ? "方案已确认" : decision === "reject" ? "方案已取消" : decision === "modification" ? "修改意见已提交" : "方案执行完成";
}

function eventText(event: StableEvent) {
  if (event.type === "hitl_pending") return "接待方案已汇总，等待你的确认。";
  if (event.type === "complete") return `${decisionLabel(event.data.decision)}。`;
  if (event.type === "error") return `执行异常：${String(event.data.message || "请稍后重试")}`;
  return "";
}

export const useChatStore = defineStore("chat", () => {
  const sessions = ref<Session[]>([]);
  const currentId = ref("");
  const states = reactive<Record<string, SessionState>>({});
  const skills = ref<Skill[]>([]);
  const files = ref<WorkspaceFile[]>([]);
  const streaming = ref(false);
  const error = ref("");
  let activeController: AbortController | undefined;
  let activeTask: Promise<void> | undefined;
  let generation = 0;

  const current = computed(() => states[currentId.value] || blankState());
  const messages = computed(() => current.value.messages);
  const events = computed(() => current.value.events);
  const assistantText = computed(() => current.value.liveText);
  const hitl = computed(() => current.value.pending);
  const finalPlan = computed(() => current.value.finalPlan);

  function stateFor(id: string) { return states[id] ||= blankState(); }

  function ingest(state: SessionState, event: StableEvent, transcript = true) {
    state.events.push(event);
    if (event.type === "token" && typeof event.data.text === "string") state.liveText += event.data.text;
    if (event.type === "hitl_pending") state.pending = event;
    if (event.type === "complete" || event.type === "error") state.pending = undefined;
    if (event.type === "complete" && "plan" in event.data) state.finalPlan = event.data.plan;
    if (transcript && ["hitl_pending", "complete", "error"].includes(event.type)) {
      const streamedText = state.liveText.trim();
      const terminalText = eventText(event);
      if (streamedText) state.messages.push({ id: `stream-${event.run_id}-${state.messages.length}`, role: "assistant", content: streamedText });
      if (terminalText) state.messages.push({ id: `event-${event.run_id}-${state.messages.length}`, role: "assistant", content: terminalText });
      state.liveText = "";
    }
  }

  function reconstruct(rows: Message[], sessionId: string) {
    const state = blankState();
    for (const row of rows) {
      const event = row.role === "assistant" ? parseStableEvent(row.content) : undefined;
      if (event && event.session_id === sessionId) ingest(state, event);
      else state.messages.push(row);
    }
    state.loaded = true;
    states[sessionId] = state;
  }

  async function stopActive() {
    generation += 1;
    activeController?.abort();
    const task = activeTask;
    activeController = undefined;
    if (task) await task.catch(() => undefined);
    activeTask = undefined;
    streaming.value = false;
  }
  registerStreamCanceller(stopActive);

  async function load() {
    error.value = "";
    try {
      const [sessionResult, skillResult, fileResult] = await Promise.all([
        apiRequest<{ items: Session[] }>("/api/manager/sessions"),
        apiRequest<{ items: Skill[] }>("/api/manager/skills"),
        apiRequest<{ items: WorkspaceFile[] }>("/api/manager/files"),
      ]);
      skills.value = skillResult.items;
      files.value = fileResult.items;
      if (!currentId.value && sessionResult.items[0]) await select(sessionResult.items[0].id);
      sessions.value = sessionResult.items;
    } catch (cause) { error.value = cause instanceof Error ? cause.message : "加载失败"; }
  }

  async function select(id: string) {
    await stopActive();
    const selectGeneration = generation;
    currentId.value = id;
    const result = await apiRequest<{ items: Message[] }>(`/api/manager/sessions/${encodeURIComponent(id)}/messages`);
    if (generation !== selectGeneration || currentId.value !== id) return;
    reconstruct(result.items, id);
  }

  async function create(title: string) {
    await stopActive();
    const session = await apiRequest<Session>("/api/manager/sessions", { method: "POST", body: JSON.stringify({ title, agent_id: "reception-leader" }) });
    sessions.value.unshift(session);
    states[session.id] = { ...blankState(), loaded: true };
    currentId.value = session.id;
  }

  async function send(prompt: string) {
    await stopActive();
    const sessionId = currentId.value;
    if (!sessionId) return;
    const state = stateFor(sessionId);
    state.messages.push({ id: `local-${Date.now()}`, role: "user", content: prompt });
    state.liveText = "";
    state.pending = undefined;
    error.value = "";
    streaming.value = true;
    const controller = new AbortController();
    activeController = controller;
    const runGeneration = ++generation;
    const consume = async () => {
      try {
        const response = await apiStream(`/api/manager/sessions/${encodeURIComponent(sessionId)}/chat`, { method: "POST", body: JSON.stringify({ prompt }), signal: controller.signal });
        for await (const event of parseSseStream(response)) {
          if (generation !== runGeneration || currentId.value !== sessionId || event.session_id !== sessionId) continue;
          ingest(state, event);
        }
      } catch (cause) {
        if (!(cause instanceof DOMException && cause.name === "AbortError") && generation === runGeneration) error.value = cause instanceof Error ? cause.message : "流式执行失败";
      } finally {
        if (generation === runGeneration) { streaming.value = false; activeController = undefined; activeTask = undefined; }
      }
    };
    activeTask = consume();
    await activeTask;
  }

  async function decideHitl(event: StableEvent, decision: HitlUiDecision, modifications: Record<string, unknown> | null) {
    const wireDecision = { confirm: "approve", modify: "modification", cancel: "reject" }[decision];
    error.value = "";
    const result = await apiRequest<HitlResult>(`/api/manager/hitl/${encodeURIComponent(String(event.data.request_id))}/decision`, { method: "POST", body: JSON.stringify({ decision: wireDecision, modifications }) });
    const state = stateFor(event.session_id);
    let terminalSeen = false;
    for (const responseEvent of result.events) {
      if (responseEvent.session_id === event.session_id && responseEvent.run_id === event.run_id) {
        ingest(state, responseEvent);
        terminalSeen ||= responseEvent.type === "complete" || responseEvent.type === "error";
      }
    }
    if (!terminalSeen) state.messages.push({ id: `decision-${event.run_id}-${state.messages.length}`, role: "assistant", content: decisionLabel(wireDecision) });
    state.pending = undefined;
    return decisionLabel(wireDecision);
  }

  return { sessions, currentId, messages, events, skills, files, streaming, error, assistantText, hitl, finalPlan, load, select, create, send, cancel: stopActive, stopActive, decideHitl };
});
