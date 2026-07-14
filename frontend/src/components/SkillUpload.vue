<script setup lang="ts">
import { ref } from "vue";
defineProps<{ disabled?: boolean }>();
const emit = defineEmits<{ upload: [file: File] }>();
const file = ref<File>();
function choose(event: Event) { file.value = (event.target as HTMLInputElement).files?.[0]; }
</script>
<template>
  <div class="upload-panel"><label class="upload-target"><input type="file" accept=".zip,application/zip" aria-label="选择 Skill ZIP" :disabled="disabled" @change="choose" /><span class="upload-icon">⇧</span><strong>{{ file?.name || '选择本地 Skill ZIP' }}</strong><small>只接受本机 ZIP 文件，不提供互联网安装入口</small></label><button class="button primary" :disabled="!file || disabled" @click="file && emit('upload', file)">安装 Skill</button></div>
</template>
