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

interface ActivitySummary {
  id: string;
  period_start: string;
  period_end: string;
  timezone: string;
  text: string;
}
const activityKind = ref<"daily" | "weekly">("daily");
const activityItems = ref<ActivitySummary[]>([]);
const activityPage = ref(0);
const activityLoading = ref(false);
const activityError = ref("");
const hasMore = ref(false);
let activityRequest = 0;

async function loadActivity(page = 0) {
  const request = ++activityRequest;
  activityLoading.value = true;
  activityError.value = "";
  try {
    const items = await api.request<ActivitySummary[]>(
      `/api/v1/admin/butler/activity-summaries?kind=${activityKind.value}&limit=11&offset=${page * 10}`,
    );
    if (request !== activityRequest) return;
    activityItems.value = items.slice(0, 10);
    hasMore.value = items.length > 10;
    activityPage.value = page;
  } catch (error) {
    if (request !== activityRequest) return;
    activityError.value = error instanceof Error ? error.message : "总结加载失败，请重试";
    activityItems.value = [];
  } finally {
    if (request === activityRequest) activityLoading.value = false;
  }
}

function periodLabel(item: ActivitySummary) {
  const end = new Date(`${item.period_end}T00:00:00Z`);
  end.setUTCDate(end.getUTCDate() - 1);
  return activityKind.value === "daily" ? item.period_start : `${item.period_start} 至 ${end.toISOString().slice(0, 10)}`;
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

onMounted(() => { void load(); void loadActivity(); });
</script>

<template>
  <section class="content">
    <div class="hero panel">
      <div>
        <div class="eyebrow">个人管家 · BRIEF-01 / REVIEW-01</div>
        <h2>简报与回顾</h2>
        <p>每日智能简报（默认 08:00）与晚间回顾（默认 21:30）的投递历史。内容为确定性模板拼装，每条事实带来源引用；修正入口在聊天端。</p>
      </div>
      <el-button :loading="loading" @click="load(); loadActivity(activityPage)">刷新</el-button>
    </div>

    <div class="stats" v-if="summary">
      <article><span>最近简报</span><strong>{{ summary.latest_brief_date ?? "—" }}</strong><small>共 {{ summary.briefs }} 期</small></article>
      <article><span>最近回顾</span><strong>{{ summary.latest_review_date ?? "—" }}</strong><small>共 {{ summary.reviews }} 期</small></article>
    </div>

    <div class="panel" aria-label="综合活动总结">
      <div class="panel-head">
        <h2>综合活动总结</h2>
        <el-radio-group v-model="activityKind" @change="loadActivity(0)">
          <el-radio-button value="daily">日总结</el-radio-button>
          <el-radio-button value="weekly">周总结</el-radio-button>
        </el-radio-group>
      </div>
      <p class="muted">汇总聊天、浏览、屏幕活动、任务和日程。日总结覆盖前一天，周总结覆盖上周一至周日。</p>
      <p v-if="activityLoading" role="status">正在加载总结…</p>
      <el-alert v-else-if="activityError" :title="activityError" type="error" :closable="false">
        <el-button size="small" @click="loadActivity(activityPage)">重试</el-button>
      </el-alert>
      <template v-else>
        <el-empty v-if="!activityItems.length" description="暂无已保存的总结，自动生成后会显示在这里" :image-size="60" />
        <details v-for="item in activityItems" :key="item.id" class="digest-card">
          <summary class="activity-title">{{ periodLabel(item) }} <span class="muted">· {{ item.timezone }} · 展开查看</span></summary>
          <p class="digest-text">{{ item.text }}</p>
        </details>
        <div class="activity-pages" v-if="activityItems.length || activityPage">
          <el-button :disabled="activityPage === 0" @click="loadActivity(activityPage - 1)">上一页</el-button>
          <span>第 {{ activityPage + 1 }} 页</span>
          <el-button :disabled="!hasMore" @click="loadActivity(activityPage + 1)">下一页</el-button>
        </div>
      </template>
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
.activity-title { cursor: pointer; line-height: 1.8; }
.activity-pages { display: flex; justify-content: center; align-items: center; gap: 12px; margin-top: 16px; }
.digest-text { overflow-wrap: anywhere; margin: 8px 0 0; font-size: 12px; line-height: 1.7; white-space: pre-wrap; color: #303640; }
@media (max-width: 1100px) { .columns { grid-template-columns: 1fr; } }
</style>
