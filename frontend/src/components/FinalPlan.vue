<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{ plan: unknown }>();
const labels: Record<string, string> = {
  pickup: "接站安排", lodging: "住宿安排", dining: "餐饮安排",
  vehicle: "车辆", time: "时间", route: "路线", hotel: "酒店",
  rooms: "房间", restaurant: "餐厅", menu: "餐饮偏好",
};
const ignored = new Set([
  "status", "decision", "owner", "owner_user_id", "team_id", "worker_ids",
  "agent_id", "session_id", "run_id", "request_id",
]);
function isPublicKey(key: string) { return !key.startsWith("_") && !ignored.has(key); }
const sections = computed(() => {
  if (!props.plan || typeof props.plan !== "object" || Array.isArray(props.plan)) return [];
  return Object.entries(props.plan as Record<string, unknown>).filter(([key]) => isPublicKey(key));
});
function title(key: string) { return labels[key] || key; }
function fields(value: unknown) {
  return value && typeof value === "object" && !Array.isArray(value)
    ? Object.entries(value as Record<string, unknown>).filter(([key]) => isPublicKey(key))
    : [["detail", value] as [string, unknown]];
}
function display(value: unknown): string {
  if (Array.isArray(value)) return value.join("、");
  if (value && typeof value === "object") return Object.entries(value as Record<string, unknown>)
    .filter(([key]) => isPublicKey(key)).map(([, detail]) => display(detail)).join(" · ");
  return value == null ? "—" : String(value);
}
</script>

<template>
  <article v-if="sections.length" class="final-plan" aria-label="最终接待方案">
    <header><p class="eyebrow green">CONFIRMED RECEPTION</p><h2>最终接待方案</h2></header>
    <div class="plan-grid">
      <section v-for="([key, value]) in sections" :key="key" class="plan-section">
        <h3>{{ title(key) }}</h3>
        <dl><div v-for="([field, detail]) in fields(value)" :key="field"><dt>{{ field === 'detail' ? '安排' : title(field) }}</dt><dd>{{ display(detail) }}</dd></div></dl>
      </section>
    </div>
  </article>
</template>
