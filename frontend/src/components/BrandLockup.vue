<script setup lang="ts">
import logoUrl from "@/assets/runchain-logo.png";

defineProps<{ subtitle: string; light?: boolean }>();
</script>

<template>
  <div class="brand-lockup" :class="{ light }">
    <img class="brand-logo" :src="logoUrl" alt="润辰科技" />
    <small>{{ subtitle }}</small>
  </div>
</template>
