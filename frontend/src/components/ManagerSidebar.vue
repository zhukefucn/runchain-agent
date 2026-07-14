<script setup lang="ts">
import type { AgentMode, Session } from "@/stores/chat";

defineProps<{ sessions: Session[]; currentId: string; currentMode: AgentMode }>();
defineEmits<{ select: [id: string]; newGeneral: []; newExpert: [] }>();
</script>

<template>
  <aside class="session-sidebar manager-sidebar">
    <div class="side-heading"><div><p class="eyebrow green">MANAGER SPACE</p><h2>智能工作台</h2></div></div>
    <button class="button primary full new-chat" aria-label="新建普通对话" @click="$emit('newGeneral')">＋ 新建对话</button>
    <section class="capability-menu" aria-label="能力模式">
      <p class="sidebar-label">能力</p>
      <button class="capability-item" :class="{ active: currentMode === 'general-assistant' }" aria-label="普通 Agent" @click="$emit('newGeneral')"><span>✦</span><span><strong>普通 Agent</strong><small>默认 · 模型与授权工具</small></span></button>
      <button class="capability-item" :class="{ active: currentMode === 'reception-leader' }" aria-label="接待专家团（演示）" @click="$emit('newExpert')"><span>⌘</span><span><strong>接待专家团</strong><small>多 Agent · MOCK 演示</small></span></button>
    </section>
    <section class="session-history">
      <p class="sidebar-label">历史会话</p>
      <button v-for="session in sessions" :key="session.id" class="session-item" :class="{ active: session.id === currentId }" @click="$emit('select', session.id)">
        <span class="session-glyph">{{ session.agent_id === 'reception-leader' ? '⌘' : '✦' }}</span>
        <span><strong>{{ session.title || '未命名会话' }}</strong><small>{{ session.agent_id === 'reception-leader' ? '专家团演示' : '普通 Agent' }}</small></span>
      </button>
      <p v-if="!sessions.length" class="empty-compact">发送第一条消息即可自动创建会话。</p>
    </section>
    <div class="isolation-note"><span>◉</span><p><strong>经理空间完全隔离</strong><small>会话、文件与执行记录仅你可见</small></p></div>
  </aside>
</template>
