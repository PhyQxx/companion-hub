<script setup lang="ts">
import { computed } from "vue";
import { adminModules } from "../admin-navigation";

const props = defineProps<{ module: string; mode?: string }>();
const currentModule = computed(() => adminModules.find((item) => item.key === props.module) ?? adminModules[0]);
const currentTab = computed(() => currentModule.value.tabs.find((item) => item.key === props.mode) ?? currentModule.value.tabs[0]);
const moduleNotes: Record<string, string[]> = {
  overview: ["复用现有健康检查和运行指标", "只展示可行动异常，不制造无基线趋势", "告警必须关联可下钻 Trace"],
  memory: ["质量指标必须来自固定回归集", "冲突裁决产生新版本和审计记录", "不在质量页面复制敏感正文"],
  devices: ["L3 原始遥测不进入历史图", "设备权限取声明与授权的交集", "健康诊断只展示安全聚合指标"],
  logs: ["以 trace_id / correlation_id 为查询主线", "禁止读取 Prompt、消息正文和密钥", "普通日志默认保留 14 天"],
  privacy: ["按采集到删除的数据生命周期组织", "安全审计追加写且不保存正文", "任何 L3 Canary 命中立即进入 P0"],
  system: ["区分热更新和必须重启的设置", "高风险变更要求重新验证管理员身份", "密钥只显示引用和配置状态"],
};
</script>

<template>
  <section class="content">
    <article class="state-card">
      <div class="state-head">
        <div>
          <span class="eyebrow">{{ currentModule.label }}</span>
          <h2>{{ currentTab.label }}</h2>
          <p>{{ currentTab.description }}</p>
        </div>
        <el-tag type="info" effect="plain">待接入真实数据</el-tag>
      </div>
      <el-alert title="导航和页面边界已经生效；该 Tab 所需的查询接口仍在后续实现批次。" type="info" :closable="false" show-icon />
      <div class="scope-grid">
        <section>
          <h3>实现边界</h3>
          <ul><li v-for="item in moduleNotes[module] ?? []" :key="item">{{ item }}</li></ul>
        </section>
        <section>
          <h3>当前状态</h3>
          <dl>
            <div><dt>页面路由</dt><dd>已完成</dd></div>
            <div><dt>Tab 状态</dt><dd>URL 可恢复</dd></div>
            <div><dt>后端查询</dt><dd>待实现</dd></div>
            <div><dt>权限边界</dt><dd>{{ module === "logs" || module === "devices" ? "operator" : module === "overview" ? "viewer" : "admin" }}</dd></div>
          </dl>
        </section>
      </div>
    </article>
  </section>
</template>

<style scoped>
.content { padding: 16px 22px 28px; overflow-y: auto; }
.state-card { max-width: 920px; display: grid; gap: 18px; padding: 22px; border: 1px solid var(--line); border-radius: 12px; background: var(--panel); }
.state-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; }
.eyebrow { color: var(--accent); font-size: 10px; font-weight: 700; letter-spacing: .1em; }
h2 { margin: 5px 0 7px; font-size: 18px; }
.state-head p { margin: 0; color: var(--muted); font-size: 13px; }
.scope-grid { display: grid; grid-template-columns: minmax(0, 1.3fr) minmax(240px, .8fr); border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
.scope-grid section { padding: 18px; }
.scope-grid section + section { border-left: 1px solid var(--line); }
h3 { margin: 0 0 12px; font-size: 13px; }
ul { margin: 0; padding-left: 18px; color: #526078; font-size: 12px; line-height: 2; }
dl { margin: 0; }
dl div { display: flex; justify-content: space-between; gap: 16px; padding: 9px 0; border-bottom: 1px solid #edf0f6; font-size: 12px; }
dt { color: var(--muted); }
dd { margin: 0; font-weight: 600; }
@media (max-width: 720px) {
  .content { padding-inline: 14px; }
  .state-head { display: grid; }
  .scope-grid { grid-template-columns: 1fr; }
  .scope-grid section + section { border-left: 0; border-top: 1px solid var(--line); }
}
</style>
