<script setup lang="ts">
import { inject, onMounted, ref } from "vue";
import { AdminApi } from "@aria/shared";

interface BriefItem {
  id: string;
  brief_date: string;
  text: string;
  status: string;
  delivered_at: string | null;
  channels: string[];
}

interface ReviewItem {
  id: string;
  review_date: string;
  text: string;
  status: string;
  delivered_at: string | null;
}

interface ButlerSummary {
  latest_brief_date: string | null;
  latest_review_date: string | null;
  briefs: number;
  reviews: number;
}

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

const briefs = ref<BriefItem[]>([]);
const reviews = ref<ReviewItem[]>([]);
const summary = ref<ButlerSummary | null>(null);
const loading = ref(false);

async function load() {
  loading.value = true;
  try {
    const [briefList, reviewList, summaryResult] = await Promise.all([
      api.request<BriefItem[]>("/api/v1/admin/butler/briefs?limit=14"),
      api.request<ReviewItem[]>("/api/v1/admin/butler/reviews?limit=14"),
      api.request<ButlerSummary>("/api/v1/admin/butler/summary"),
    ]);
    briefs.value = briefList;
    reviews.value = reviewList;
    summary.value = summaryResult;
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "简报与回顾加载失败", true);
  } finally {
    loading.value = false;
  }
}

onMounted(load);
</script>

<template>
  <section class="content">
    <div class="hero panel">
      <div>
        <div class="eyebrow">个人管家 · BRIEF-01 / REVIEW-01</div>
        <h2>简报与回顾</h2>
        <p>每日智能简报（默认 08:00）与晚间回顾（默认 21:30）的投递历史。内容为确定性模板拼装，每条事实带来源引用；修正入口在聊天端。</p>
      </div>
      <el-button :loading="loading" @click="load">刷新</el-button>
    </div>

    <div class="stats" v-if="summary">
      <article><span>最近简报</span><strong>{{ summary.latest_brief_date ?? "—" }}</strong><small>共 {{ summary.briefs }} 期</small></article>
      <article><span>最近回顾</span><strong>{{ summary.latest_review_date ?? "—" }}</strong><small>共 {{ summary.reviews }} 期</small></article>
    </div>

    <div class="columns">
      <div class="panel">
        <div class="panel-head"><h2>每日简报</h2><el-tag size="small" effect="plain">{{ briefs.length }} 条</el-tag></div>
        <el-empty v-if="!loading && !briefs.length" description="还没有简报" :image-size="60" />
        <div v-for="item in briefs" :key="item.id" class="digest-card">
          <div class="digest-head">
            <strong>{{ item.brief_date }}</strong>
            <span class="muted">{{ item.status === "delivered" ? "已投递" : "待投递" }}{{ item.channels.length ? ` · ${item.channels.join("/")}` : "" }}</span>
          </div>
          <p class="digest-text">{{ item.text }}</p>
        </div>
      </div>

      <div class="panel">
        <div class="panel-head"><h2>晚间回顾</h2><el-tag size="small" effect="plain">{{ reviews.length }} 条</el-tag></div>
        <el-empty v-if="!loading && !reviews.length" description="还没有回顾" :image-size="60" />
        <div v-for="item in reviews" :key="item.id" class="digest-card">
          <div class="digest-head">
            <strong>{{ item.review_date }}</strong>
            <span class="muted">{{ item.status === "delivered" ? "已投递" : "待投递" }}</span>
          </div>
          <p class="digest-text">{{ item.text }}</p>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.content { padding: 20px 24px 28px; display: grid; gap: 16px; align-content: start; overflow-y: auto; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 18px; }
.hero { display: flex; justify-content: space-between; align-items: center; gap: 24px; background: linear-gradient(135deg, #fff 0%, #f1f5ff 100%); }
.hero h2, .panel h2 { margin: 0; font-size: 16px; }
.hero p { margin: 7px 0 0; color: var(--muted); font-size: 12px; line-height: 1.6; max-width: 640px; }
.eyebrow { color: var(--accent); font-size: 11px; font-weight: 700; letter-spacing: .08em; margin-bottom: 7px; }
.muted { color: var(--muted); font-size: 11px; }
.stats { display: grid; grid-template-columns: repeat(2, minmax(160px, 1fr)); gap: 12px; }
.stats article { display: grid; gap: 4px; padding: 14px 16px; background: #fff; border: 1px solid var(--line); border-radius: 12px; }
.stats span { color: var(--muted); font-size: 11px; }
.stats strong { font-size: 20px; }
.stats small { color: var(--muted); font-size: 10px; }
.columns { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; align-items: start; }
.panel-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.digest-card { border: 1px solid #e8edf5; border-radius: 10px; padding: 12px 14px; margin-top: 10px; background: #fbfcff; }
.digest-head { display: flex; justify-content: space-between; align-items: center; }
.digest-text { margin: 8px 0 0; font-size: 12px; line-height: 1.7; white-space: pre-wrap; color: #303640; }
@media (max-width: 1100px) { .columns { grid-template-columns: 1fr; } }
</style>
