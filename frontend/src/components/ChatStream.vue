<script setup lang="ts">
import type { AgentMode, Message } from "@/stores/chat";
defineProps<{ messages: Message[]; assistantText: string; streaming: boolean; mode: AgentMode }>();
</script>
<template>
  <div class="message-list" aria-live="polite">
    <div v-if="!messages.length && !assistantText" class="empty-state"><span class="empty-icon">✦</span><h3>{{ mode === 'reception-leader' ? '告诉接待主管你的需求' : '今天想让 Agent 帮你做什么？' }}</h3><p>{{ mode === 'reception-leader' ? '例如：三位专家周五 18:00 到站，请统筹接站、住宿和晚餐。' : '可以直接提问，也可以让 Agent 调用已授权的 Skill、MCP Tool 和文件能力。' }}</p></div>
    <article v-for="message in messages" :key="message.id" class="message" :class="message.role"><span>{{ message.role === 'user' ? '我' : '润' }}</span><p>{{ message.content }}</p></article>
    <article v-if="assistantText || streaming" class="message assistant"><span>润</span><p>{{ assistantText }}<i v-if="streaming" class="cursor"></i></p></article>
  </div>
</template>
