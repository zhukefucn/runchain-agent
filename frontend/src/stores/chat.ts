import { computed, ref } from "vue";
import { defineStore } from "pinia";
import { apiRequest, apiStream } from "@/api/client";
import { parseSseStream, type StableEvent } from "@/api/sse";

export type Session = { id: string; agent_id: string; title: string; status: string; created_at?: string };
export type Message = { id: string; role: string; content: string; ordinal?: number };
export type Skill = { id: string; name: string; version: string; type: string; description?: string; warnings?: string[] };
export type WorkspaceFile = { id: string; session_id: string; relative_path: string; created_at?: string };

export const useChatStore = defineStore("chat", () => {
  const sessions = ref<Session[]>([]);
  const currentId = ref("");
  const messages = ref<Message[]>([]);
  const events = ref<StableEvent[]>([]);
  const skills = ref<Skill[]>([]);
  const files = ref<WorkspaceFile[]>([]);
  const streaming = ref(false);
  const error = ref("");
  const controller = ref<AbortController>();
  const assistantText = computed(() => events.value.filter((event) => event.type === "token").map((event) => String(event.data.text || "")).join(""));
  const hitl = computed(() => [...events.value].reverse().find((event) => event.type === "hitl_pending"));

  async function load() {
    error.value = "";
    try {
      const [sessionResult, skillResult, fileResult] = await Promise.all([
        apiRequest<{ items: Session[] }>("/api/manager/sessions"),
        apiRequest<{ items: Skill[] }>("/api/manager/skills"),
        apiRequest<{ items: WorkspaceFile[] }>("/api/manager/files"),
      ]);
      sessions.value = sessionResult.items;
      skills.value = skillResult.items;
      files.value = fileResult.items;
      if (!currentId.value && sessions.value[0]) await select(sessions.value[0].id);
    } catch (cause) { error.value = cause instanceof Error ? cause.message : "加载失败"; }
  }

  async function select(id: string) {
    currentId.value = id;
    events.value = [];
    const result = await apiRequest<{ items: Message[] }>(`/api/manager/sessions/${encodeURIComponent(id)}/messages`);
    messages.value = result.items;
  }

  async function create(title: string) {
    const session = await apiRequest<Session>("/api/manager/sessions", { method: "POST", body: JSON.stringify({ title, agent_id: "reception-leader" }) });
    sessions.value.unshift(session);
    await select(session.id);
  }

  async function send(prompt: string) {
    if (!currentId.value || streaming.value) return;
    streaming.value = true;
    error.value = "";
    events.value = [];
    messages.value.push({ id: `local-${Date.now()}`, role: "user", content: prompt });
    controller.value = new AbortController();
    try {
      const response = await apiStream(`/api/manager/sessions/${encodeURIComponent(currentId.value)}/chat`, {
        method: "POST",
        body: JSON.stringify({ prompt }),
        signal: controller.value.signal,
      });
      for await (const event of parseSseStream(response)) events.value.push(event);
    } catch (cause) {
      if (!(cause instanceof DOMException && cause.name === "AbortError")) error.value = cause instanceof Error ? cause.message : "流式执行失败";
    } finally { streaming.value = false; controller.value = undefined; }
  }

  function cancel() { controller.value?.abort(); }

  return { sessions, currentId, messages, events, skills, files, streaming, error, assistantText, hitl, load, select, create, send, cancel };
});
