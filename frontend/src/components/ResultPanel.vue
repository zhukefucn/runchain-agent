<script setup lang="ts">
import { ref } from "vue";
import type { StableEvent } from "@/api/sse";
import type { Skill, WorkspaceFile } from "@/stores/chat";
import ExecutionTimeline from "@/components/ExecutionTimeline.vue";
import FinalPlan from "@/components/FinalPlan.vue";

defineProps<{ open: boolean; plan?: unknown; files: WorkspaceFile[]; events: StableEvent[]; skills: Skill[]; streaming: boolean }>();
defineEmits<{ close: [] }>();
const tab = ref<"result" | "files" | "execution">("result");
</script>

<template>
  <aside aria-label="结果展示" class="result-panel" :class="{ 'result-open': open }">
    <header class="result-header"><div><p class="eyebrow green">BUSINESS OUTPUT</p><h2>结果展示</h2></div><button class="icon-button" aria-label="关闭结果" @click="$emit('close')">×</button></header>
    <div class="context-tabs result-tabs">
      <button :class="{ active: tab === 'result' }" @click="tab = 'result'">业务结果</button>
      <button :class="{ active: tab === 'files' }" @click="tab = 'files'">文件</button>
      <button :class="{ active: tab === 'execution' }" @click="tab = 'execution'">执行详情</button>
    </div>
    <div class="result-body">
      <template v-if="tab === 'result'"><FinalPlan v-if="plan" :plan="plan" /><div v-else class="result-empty"><span>◇</span><h3>暂无业务结果</h3><p>Agent 产生结构化数据后将在这里展示。</p></div></template>
      <template v-else-if="tab === 'files'"><section class="resource-group"><h3>当前会话文件 <span>{{ files.length }}</span></h3><div v-for="file in files" :key="file.id" class="resource-row"><span>↳</span><p><strong>{{ file.relative_path }}</strong><small>经理隔离空间</small></p></div><p v-if="!files.length" class="empty-compact">暂无文件产物</p></section><section class="resource-group"><h3>已授权 Skill <span>{{ skills.length }}</span></h3><div v-for="skill in skills" :key="skill.id" class="resource-row"><span>Py</span><p><strong>{{ skill.name }}</strong><small>{{ skill.version }} · {{ skill.type }}</small></p></div><p v-if="!skills.length" class="empty-compact">暂无生效 Skill</p></section></template>
      <template v-else><div class="panel-heading"><h3>Agent 执行轨迹</h3><span v-if="streaming" class="live-dot">执行中</span></div><ExecutionTimeline :events="events" /></template>
    </div>
  </aside>
</template>
