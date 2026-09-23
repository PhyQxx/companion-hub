<script setup lang="ts">
import { ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import ConflictsView from "./ConflictsView.vue";
import QualityView from "./QualityView.vue";

const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

const route = useRoute();
const router = useRouter();
// 单 Tab 内的「质量分析 / 冲突处理」切换，section 持久化在 URL 上
type QualitySection = "quality" | "conflicts";
const section = ref<QualitySection>(route.query.section === "conflicts" ? "conflicts" : "quality");

function switchSection(next: string | number | boolean | undefined) {
  section.value = next === "conflicts" ? "conflicts" : "quality";
}

watch(section, (value) => {
  void router.replace({ query: { ...route.query, section: value === "conflicts" ? "conflicts" : undefined } });
});
</script>

<template>
  <section class="quality-wrapper">
    <div class="section-bar">
      <el-radio-group :model-value="section" size="small" @change="switchSection">
        <el-radio-button value="quality">质量分析</el-radio-button>
        <el-radio-button value="conflicts">冲突处理</el-radio-button>
      </el-radio-group>
    </div>
    <QualityView v-if="section === 'quality'" @status="(text: string, error?: boolean) => emit('status', text, error)" />
    <ConflictsView v-else @status="(text: string, error?: boolean) => emit('status', text, error)" />
  </section>
</template>

<style scoped>
.quality-wrapper { display: flex; flex-direction: column; min-height: 0; height: 100%; overflow: auto; }
.section-bar { display: flex; padding: 14px 22px 0; background: transparent; flex: none; }
</style>
