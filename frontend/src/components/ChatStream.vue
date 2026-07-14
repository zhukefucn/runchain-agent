<script setup lang="ts">
import type { Message } from "@/stores/chat";
defineProps<{ messages: Message[]; assistantText: string; streaming: boolean }>();
</script>
<template>
  <div class="message-list" aria-live="polite">
    <div v-if="!messages.length && !assistantText" class="empty-state"><span class="empty-icon">✦</span><h3>告诉接待主管你的需求</h3><p>例如：三位专家周五 18:00 到站，请统筹接站、住宿和晚餐。</p></div>
    <article v-for="message in messages" :key="message.id" class="message" :class="message.role"><span>{{ message.role === 'user' ? '我' : 'R' }}</span><p>{{ message.content }}</p></article>
    <article v-if="assistantText || streaming" class="message assistant"><span>R</span><p>{{ assistantText }}<i v-if="streaming" class="cursor"></i></p></article>
  </div>
</template>
