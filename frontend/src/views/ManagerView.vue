<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import ChatStream from "@/components/ChatStream.vue";
import HitlCard from "@/components/HitlCard.vue";
import ManagerSidebar from "@/components/ManagerSidebar.vue";
import ResultPanel from "@/components/ResultPanel.vue";
import {
  GENERAL_AGENT_ID,
  RECEPTION_AGENT_ID,
  useChatStore,
} from "@/stores/chat";

const chat = useChatStore();
const prompt = ref("");
const currentSession = computed(() =>
  chat.sessions.find((session) => session.id === chat.currentId),
);
const expertMode = computed(() => chat.mode === RECEPTION_AGENT_ID);

async function submit() {
  const value = prompt.value.trim();
  if (!value) return;
  prompt.value = "";
  await chat.send(value);
}

async function newGeneral() {
  await chat.createForMode(GENERAL_AGENT_ID);
}

async function newExpert() {
  await chat.createForMode(RECEPTION_AGENT_ID);
}

onMounted(() => chat.load());
onBeforeUnmount(() => chat.stopActive());
</script>

<template>
  <main class="manager-layout" :class="{ 'result-expanded': chat.resultOpen }">
    <ManagerSidebar
      :sessions="chat.sessions"
      :current-id="chat.currentId"
      :current-mode="chat.mode"
      @select="chat.select"
      @new-general="newGeneral"
      @new-expert="newExpert"
    />

    <section class="chat-panel">
      <header class="workspace-title">
        <div>
          <p class="eyebrow green">
            {{ expertMode ? "专家团模式 · MOCK 演示" : "GENERAL AGENT" }}
          </p>
          <h1>{{ currentSession?.title || (expertMode ? "接待专家团" : "润辰智能助手") }}</h1>
        </div>
        <div class="workspace-actions">
          <button class="button ghost compact" aria-label="打开结果" @click="chat.setResultOpen(true)">
            结果
          </button>
          <div v-if="expertMode" class="agent-roster" aria-label="专家团成员">
            <span>统筹</span><span>接站</span><span>住宿</span><span>餐饮</span>
          </div>
        </div>
      </header>

      <ChatStream
        :messages="chat.messages"
        :assistant-text="chat.assistantText"
        :streaming="chat.streaming"
        :mode="chat.mode"
      />
      <HitlCard v-if="chat.hitl" :event="chat.hitl" />
      <p v-if="chat.error" class="alert error">{{ chat.error }}</p>

      <form class="composer" @submit.prevent="submit">
        <textarea
          v-model="prompt"
          :aria-label="expertMode ? '给接待专家团发送消息' : '给润辰智能助手发送消息'"
          :placeholder="expertMode
            ? '描述客人的抵达时间、人数和接待偏好…'
            : '输入问题，或让 Agent 调用已授权的 Skill、MCP Tool…'"
          @keydown.enter.exact.prevent="submit"
        />
        <button v-if="chat.streaming" type="button" class="button secondary" @click="chat.cancel">停止</button>
        <button v-else class="button primary">发送 <span>→</span></button>
      </form>
    </section>

    <ResultPanel
      :open="chat.resultOpen"
      :plan="chat.finalPlan"
      :files="chat.currentFiles"
      :events="chat.events"
      :skills="chat.skills"
      :streaming="chat.streaming"
      @close="chat.setResultOpen(false)"
    />
  </main>
</template>
