<script setup lang="ts">
import { computed } from "vue";
import { renderMarkdown } from "./markdown";

const props = defineProps<{ content: string }>();

const rendered = computed(() => renderMarkdown(props.content));
</script>

<template>
  <!-- html=false 会转义消息中的原始 HTML；markdown-it 同时过滤危险链接协议。 -->
  <div class="markdown-content" v-html="rendered"></div>
</template>

<style scoped>
.markdown-content { min-width:0; overflow-wrap:anywhere; }
.markdown-content :deep(> :first-child) { margin-top:0; }
.markdown-content :deep(> :last-child) { margin-bottom:0; }
.markdown-content :deep(p) { margin:.45em 0; }
.markdown-content :deep(h1),
.markdown-content :deep(h2),
.markdown-content :deep(h3),
.markdown-content :deep(h4),
.markdown-content :deep(h5),
.markdown-content :deep(h6) { margin:.8em 0 .35em; line-height:1.3; }
.markdown-content :deep(h1) { font-size:1.35em; }
.markdown-content :deep(h2) { font-size:1.22em; }
.markdown-content :deep(h3) { font-size:1.12em; }
.markdown-content :deep(ul),
.markdown-content :deep(ol) { margin:.45em 0; padding-left:1.5em; }
.markdown-content :deep(li + li) { margin-top:.2em; }
.markdown-content :deep(blockquote) { margin:.55em 0; padding:.1em 0 .1em .8em; border-left:3px solid var(--accent); color:var(--muted); }
.markdown-content :deep(code) { padding:.12em .35em; border-radius:5px; background:var(--panel2); font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace; font-size:.9em; }
.markdown-content :deep(pre) { max-width:100%; margin:.6em 0; overflow:auto; padding:.75em .9em; border:1px solid var(--line); border-radius:9px; background:var(--panel2); line-height:1.45; }
.markdown-content :deep(pre code) { padding:0; background:transparent; white-space:pre; }
.markdown-content :deep(a) { color:var(--accent); text-decoration:underline; text-underline-offset:2px; }
.markdown-content :deep(hr) { margin:.8em 0; border:0; border-top:1px solid var(--line); }
.markdown-content :deep(table) { display:block; max-width:100%; overflow-x:auto; border-collapse:collapse; }
.markdown-content :deep(th),
.markdown-content :deep(td) { padding:.35em .55em; border:1px solid var(--line); text-align:left; }
</style>
