<script setup lang="ts">
import { ref } from "vue";
import type { StableEvent } from "@/api/sse";
import { useChatStore } from "@/stores/chat";
const props = defineProps<{ event: StableEvent }>();
const chat = useChatStore();
const status = ref("");
const error = ref("");
const editing = ref(false);
const note = ref("");
const busy = ref(false);
async function decide(decision: "confirm" | "modify" | "cancel") {
  busy.value = true;
  error.value = "";
  try {
    status.value = await chat.decideHitl(props.event, decision, decision === "modify" ? { note: note.value } : null);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "提交失败";
  } finally { busy.value = false; }
}
</script>
<template>
  <article class="hitl-card">
    <div class="hitl-title"><span>需要人工确认</span><strong>接待方案已汇总</strong></div>
    <p>主管已收齐接站、住宿和餐饮建议。请确认后继续执行。</p>
    <textarea v-if="editing" v-model="note" aria-label="方案修改意见" placeholder="输入需要调整的内容"></textarea>
    <p v-if="error" class="alert error" role="alert">{{ error }}</p>
    <p v-if="status" class="success-note">{{ status }}</p>
    <div v-else class="actions"><button class="button primary compact" :disabled="busy" @click="decide('confirm')">确认方案</button><button class="button secondary compact" :disabled="busy" @click="editing ? decide('modify') : editing = true">{{ editing ? '提交修改' : '修改' }}</button><button class="button ghost compact" :disabled="busy" @click="decide('cancel')">取消</button></div>
  </article>
</template>
