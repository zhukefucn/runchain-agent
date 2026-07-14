<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useChatStore } from "@/stores/chat";
import ChatStream from "@/components/ChatStream.vue";
import ExecutionTimeline from "@/components/ExecutionTimeline.vue";
import HitlCard from "@/components/HitlCard.vue";
const chat = useChatStore();
const prompt = ref("");
const tab = ref<"timeline" | "resources">("timeline");
async function submit() { const value = prompt.value.trim(); if (!value) return; prompt.value = ""; await chat.send(value); }
async function addSession() { await chat.create(`新接待任务 ${chat.sessions.length + 1}`); }
onMounted(() => chat.load());
</script>
<template>
  <main class="manager-layout">
    <aside class="session-sidebar"><div class="side-heading"><div><p class="eyebrow green">MANAGER SPACE</p><h2>我的会话</h2></div><button class="icon-button" title="新建会话" @click="addSession">＋</button></div><button v-for="session in chat.sessions" :key="session.id" class="session-item" :class="{ active: session.id === chat.currentId }" @click="chat.select(session.id)"><span class="session-glyph">⌁</span><span><strong>{{ session.title || '未命名会话' }}</strong><small>多智能体接待主管</small></span></button><p v-if="!chat.sessions.length" class="empty-compact">还没有会话，点击右上角新建。</p><div class="isolation-note"><span>◉</span><p><strong>租户空间已隔离</strong><small>会话、文件与执行记录仅你可见</small></p></div></aside>
    <section class="chat-panel"><header class="workspace-title"><div><p class="eyebrow green">专家团模式 · MOCK 演示</p><h1>{{ chat.sessions.find((s) => s.id === chat.currentId)?.title || '接待专家团' }}</h1></div><div class="agent-roster"><span>主管</span><span>接站</span><span>住宿</span><span>餐饮</span></div></header><ChatStream :messages="chat.messages" :assistant-text="chat.assistantText" :streaming="chat.streaming" /><HitlCard v-if="chat.hitl" :event="chat.hitl" /><p v-if="chat.error" class="alert error">{{ chat.error }}</p><form class="composer" @submit.prevent="submit"><textarea v-model="prompt" aria-label="给接待主管发送消息" placeholder="描述客人的抵达时间、人数和接待偏好…" @keydown.enter.exact.prevent="submit"></textarea><button v-if="chat.streaming" type="button" class="button secondary" @click="chat.cancel">停止</button><button v-else class="button primary">发送 <span>↗</span></button></form></section>
    <aside class="context-panel"><div class="context-tabs"><button :class="{ active: tab === 'timeline' }" @click="tab = 'timeline'">执行轨迹</button><button :class="{ active: tab === 'resources' }" @click="tab = 'resources'">资源</button></div><template v-if="tab === 'timeline'"><div class="panel-heading"><h3>实时协作</h3><span v-if="chat.streaming" class="live-dot">执行中</span></div><ExecutionTimeline :events="chat.events" /></template><template v-else><section class="resource-group"><h3>已授权 Skill <span>{{ chat.skills.length }}</span></h3><div v-for="skill in chat.skills" :key="skill.id" class="resource-row"><span>Py</span><p><strong>{{ skill.name }}</strong><small>{{ skill.version }} · {{ skill.type }}</small></p></div><p v-if="!chat.skills.length" class="empty-compact">暂无生效 Skill</p></section><section class="resource-group"><h3>我的文件 <span>{{ chat.files.length }}</span></h3><div v-for="file in chat.files" :key="file.id" class="resource-row"><span>↳</span><p><strong>{{ file.relative_path }}</strong><small>当前经理空间</small></p></div><p v-if="!chat.files.length" class="empty-compact">暂无运行产物</p></section></template></aside>
  </main>
</template>
