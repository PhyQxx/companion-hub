<script setup lang="ts">
import type { ToolPresentation } from "@aria/shared";

defineProps<{ result: ToolPresentation }>();
const emit = defineEmits<{ chooseLocation: [name: string] }>();

const modeLabels: Record<string, string> = { walking: "步行", driving: "驾车", transit: "公交" };
const basisLabels: Record<string, string> = {
  straight_line: "直线距离",
  straight_line_fallback: "直线距离（路网不可用）",
  walking_route: "步行距离",
  driving_route: "驾车距离",
};

function distance(value?: number): string {
  if (typeof value !== "number") return "距离未知";
  return value >= 1000 ? `${(value / 1000).toFixed(value >= 10_000 ? 0 : 1)} 公里` : `${value} 米`;
}

function duration(value?: number | null): string {
  if (typeof value !== "number") return "";
  const minutes = Math.max(1, Math.round(value / 60));
  return minutes >= 60 ? `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分` : `${minutes} 分钟`;
}

function sourceLabel(cacheHit: boolean): string {
  return `高德${cacheHit ? " · 缓存命中" : " · 实时查询"}`;
}
</script>

<template>
  <section class="tool-card">
    <template v-if="result.kind === 'location_ambiguous'">
      <header><strong>请选择具体地点</strong></header>
      <div class="candidate-list">
        <button
          v-for="candidate in result.candidates"
          :key="`${candidate.adcode}-${candidate.name}`"
          type="button"
          @click="emit('chooseLocation', candidate.name)"
        >
          {{ candidate.name }}<small v-if="candidate.adcode">{{ candidate.adcode }}</small>
        </button>
      </div>
    </template>

    <template v-else-if="result.kind === 'weather'">
      <header>
        <strong>{{ result.location?.name || "天气" }}</strong>
        <small>{{ sourceLabel(result.cache_hit) }}</small>
      </header>
      <div v-if="result.current" class="weather-current">
        <b>{{ result.current.temperature_c ?? "--" }}℃</b>
        <span>{{ result.current.weather || "天气未知" }}</span>
        <span v-if="result.current.humidity_percent != null">湿度 {{ result.current.humidity_percent }}%</span>
        <span v-if="result.current.wind">{{ result.current.wind }}</span>
      </div>
      <div v-if="result.forecast?.length" class="forecast-list">
        <div v-for="day in result.forecast" :key="day.date">
          <small>{{ day.date }}</small>
          <span>{{ day.day_weather }} / {{ day.night_weather }}</span>
          <b>{{ day.low_c ?? "--" }}～{{ day.high_c ?? "--" }}℃</b>
        </div>
      </div>
    </template>

    <template v-else-if="result.kind === 'nearby'">
      <header>
        <strong>{{ result.origin?.name || "附近地点" }}</strong>
        <small>{{ sourceLabel(result.cache_hit) }}</small>
      </header>
      <ol class="poi-list">
        <li v-for="item in result.results" :key="`${item.name}-${item.address}`">
          <div><b>{{ item.name }}</b><small>{{ item.address }}</small></div>
          <div class="poi-meta">
            <span>{{ distance(item.distance_m) }}</span>
            <span v-if="item.duration_s">约 {{ duration(item.duration_s) }}</span>
            <span>{{ basisLabels[item.distance_basis || ''] || item.distance_basis }}</span>
            <a v-if="item.navigation_uri" :href="item.navigation_uri" target="_blank" rel="noopener noreferrer">高德打开</a>
          </div>
        </li>
      </ol>
    </template>

    <template v-else-if="result.kind === 'route'">
      <header>
        <strong>{{ modeLabels[result.mode || ''] || "路线" }}方案</strong>
        <small>{{ sourceLabel(result.cache_hit) }}</small>
      </header>
      <div class="route-main">
        <span>{{ result.origin || "当前位置" }} → {{ result.destination }}</span>
        <b>{{ distance(result.distance_m) }}<template v-if="result.duration_s"> · 约 {{ duration(result.duration_s) }}</template></b>
      </div>
      <ol v-if="result.steps?.length" class="steps">
        <li v-for="step in result.steps.slice(0, 4)" :key="step">{{ step }}</li>
      </ol>
      <a v-if="result.navigation_uri" class="nav-button" :href="result.navigation_uri" target="_blank" rel="noopener noreferrer">在高德中开始导航</a>
    </template>
  </section>
</template>

<style scoped>
.tool-card { margin-top:10px; padding:12px; border:1px solid var(--line); border-radius:12px; background: var(--panel2); white-space:normal; }
header { display:flex; justify-content:space-between; align-items:center; gap:10px; margin-bottom:9px; }
header small, .tool-card small { color:var(--muted); font-size:11px; }
.weather-current { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }
.weather-current b { font-size:25px; }
.weather-current span { color:var(--muted); font-size:13px; }
.forecast-list { display:grid; grid-template-columns:repeat(auto-fit,minmax(110px,1fr)); gap:7px; margin-top:10px; }
.forecast-list div { display:grid; gap:2px; padding:7px; border-radius:8px; background:var(--panel); }
.candidate-list { display:grid; gap:7px; }
.candidate-list button { display:flex; justify-content:space-between; text-align:left; }
.poi-list, .steps { margin:0; padding-left:20px; }
.poi-list li { margin:8px 0; }
.poi-list li > div:first-child { display:grid; }
.poi-meta { display:flex; gap:8px; flex-wrap:wrap; color:var(--muted); font-size:12px; }
a { color:var(--accent); }
.route-main { display:grid; gap:5px; }
.steps { margin-top:8px; color:var(--muted); font-size:12px; }
.nav-button { display:inline-block; margin-top:10px; padding:6px 10px; border:1px solid var(--accent); border-radius:8px; text-decoration:none; }
</style>
