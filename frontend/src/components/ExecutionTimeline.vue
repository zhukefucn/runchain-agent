<script setup lang="ts">
import type { StableEvent } from "@/api/sse";
defineProps<{ events: StableEvent[] }>();
const agents: Record<string, string> = { leader: "接待主管", pickup: "接站专家", lodging: "住宿专家", dining: "餐饮专家" };
const labels: Record<string, string> = { run_started: "任务已启动", agent_started: "开始执行", tool_call: "调用工具", tool_result: "工具返回", agent_completed: "任务完成", hitl_pending: "等待确认", complete: "执行完成", error: "执行异常" };
</script>
<template>
  <ol class="timeline" aria-label="执行时间线">
    <li v-for="(event, index) in events.filter((e) => e.type !== 'token')" :key="`${event.type}-${index}`" :class="{ danger: event.type === 'error' }">
      <span class="timeline-dot"></span><div><strong>{{ agents[String(event.data.agent_type || '')] || '接待主管' }}</strong><p>{{ labels[event.type] || event.type }}<template v-if="event.data.tool_name"> · {{ event.data.tool_name }}</template></p></div>
    </li>
    <li v-if="!events.some((e) => e.type !== 'token')" class="empty-compact">发起任务后，这里将展示各智能体与工具的执行轨迹。</li>
  </ol>
</template>
