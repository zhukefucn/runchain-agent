<script setup lang="ts">
import { ref } from "vue";
import { apiRequest } from "@/api/client";
import type { StableEvent } from "@/api/sse";
const props = defineProps<{ event: StableEvent }>();
const status = ref("");
const editing = ref(false);
const note = ref("");
const busy = ref(false);
async function decide(decision: "confirm" | "modify" | "cancel") {
  busy.value = true;
  try {
    const wireDecision = { confirm: "approve", modify: "modification", cancel: "reject" }[decision];
    await apiRequest(`/api/manager/hitl/${encodeURIComponent(String(props.event.data.request_id))}/decision`, { method: "POST", body: JSON.stringify({ decision: wireDecision, modifications: decision === "modify" ? { note: note.value } : null }) });
    status.value = decision === "confirm" ? "方案已确认" : decision === "modify" ? "修改意见已提交" : "方案已取消";
  } finally { busy.value = false; }
}
</script>
<template>
  <article class="hitl-card">
    <div class="hitl-title"><span>需要人工确认</span><strong>接待方案已汇总</strong></div>
    <p>主管已收齐接站、住宿和餐饮建议。请确认后继续执行。</p>
    <textarea v-if="editing" v-model="note" aria-label="方案修改意见" placeholder="输入需要调整的内容"></textarea>
    <p v-if="status" class="success-note">{{ status }}</p>
    <div v-else class="actions"><button class="button primary compact" :disabled="busy" @click="decide('confirm')">确认方案</button><button class="button secondary compact" @click="editing ? decide('modify') : editing = true">{{ editing ? '提交修改' : '修改' }}</button><button class="button ghost compact" @click="decide('cancel')">取消</button></div>
  </article>
</template>
