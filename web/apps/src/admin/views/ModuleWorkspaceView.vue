<script setup lang="ts">
import { computed, defineAsyncComponent } from "vue";
import { useRoute } from "vue-router";
import PlaceholderView from "./PlaceholderView.vue";

// 各视图按需异步加载：聊天入口不打包任何 admin 视图，进入对应 Tab 时才拉取。
const ActivityView = defineAsyncComponent(() => import("./ActivityView.vue"));
const AppearanceView = defineAsyncComponent(() => import("./AppearanceView.vue"));
const AvatarsView = defineAsyncComponent(() => import("./AvatarsView.vue"));
const BrowserAwarenessView = defineAsyncComponent(() => import("./BrowserAwarenessView.vue"));
const ButlerDigestsView = defineAsyncComponent(() => import("./ButlerDigestsView.vue"));
const ButlerMeetingsView = defineAsyncComponent(() => import("./ButlerMeetingsView.vue"));
const ButlerScenesView = defineAsyncComponent(() => import("./ButlerScenesView.vue"));
const ButlerWorkflowsView = defineAsyncComponent(() => import("./ButlerWorkflowsView.vue"));
const DevicesView = defineAsyncComponent(() => import("./DevicesView.vue"));
const DiagnosticsView = defineAsyncComponent(() => import("./DiagnosticsView.vue"));
const HealthView = defineAsyncComponent(() => import("./HealthView.vue"));
const HomeAssistantDevicesView = defineAsyncComponent(() => import("./HomeAssistantDevicesView.vue"));
const IntegrationsCalendarView = defineAsyncComponent(() => import("./IntegrationsCalendarView.vue"));
const IntegrationsMailView = defineAsyncComponent(() => import("./IntegrationsMailView.vue"));
const IntegrationsXiaoaiView = defineAsyncComponent(() => import("./IntegrationsXiaoaiView.vue"));
const IntegrationsHermesView = defineAsyncComponent(() => import("./IntegrationsHermesView.vue"));
const JobsView = defineAsyncComponent(() => import("./JobsView.vue"));
const LiveLogsView = defineAsyncComponent(() => import("./LiveLogsView.vue"));
const MemoryQualityView = defineAsyncComponent(() => import("./MemoryQualityView.vue"));
const MemoryView = defineAsyncComponent(() => import("./MemoryView.vue"));
const McpIntegrationView = defineAsyncComponent(() => import("./McpIntegrationView.vue"));
const ModelsView = defineAsyncComponent(() => import("./ModelsView.vue"));
const LogsView = defineAsyncComponent(() => import("./LogsView.vue"));
const OverviewView = defineAsyncComponent(() => import("./OverviewView.vue"));
const PersonasView = defineAsyncComponent(() => import("./PersonasView.vue"));
const PrivacyView = defineAsyncComponent(() => import("./PrivacyView.vue"));
const ProactiveChannelsView = defineAsyncComponent(() => import("./ProactiveChannelsView.vue"));
const SafetyAdminView = defineAsyncComponent(() => import("./SafetyAdminView.vue"));
const ScreenAwarenessView = defineAsyncComponent(() => import("./ScreenAwarenessView.vue"));
const SenseAudioView = defineAsyncComponent(() => import("./SenseAudioView.vue"));
const SkillsView = defineAsyncComponent(() => import("./SkillsView.vue"));
const SystemView = defineAsyncComponent(() => import("./SystemView.vue"));
const TasksView = defineAsyncComponent(() => import("./TasksView.vue"));
const TimelineView = defineAsyncComponent(() => import("./TimelineView.vue"));
const UsageView = defineAsyncComponent(() => import("./UsageView.vue"));
const UsersView = defineAsyncComponent(() => import("./UsersView.vue"));

const props = defineProps<{ module: string }>();
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();
const route = useRoute();
const activeTab = computed(() => String(route.query.tab ?? ""));

const currentView = computed(() => {
  if (props.module === "overview" && activeTab.value === "runtime") return OverviewView;
  if (props.module === "overview" && activeTab.value === "health") return HealthView;
  if (props.module === "overview" && activeTab.value === "usage") return UsageView;
  if (props.module === "overview" && activeTab.value === "activity") return ActivityView;
  if (props.module === "models" && activeTab.value === "mcp") return McpIntegrationView;
  if (props.module === "skills") return SkillsView;
  if (props.module === "models" && activeTab.value === "sound") return SenseAudioView;
  if (props.module === "models") return ModelsView;
  if (props.module === "integrations" && activeTab.value === "mail") return IntegrationsMailView;
  if (props.module === "integrations" && activeTab.value === "calendar") return IntegrationsCalendarView;
  if (props.module === "integrations" && activeTab.value === "hermes") return IntegrationsHermesView;
  if (props.module === "integrations") return IntegrationsXiaoaiView;
  if (props.module === "persona" && activeTab.value === "gallery") return AvatarsView;
  if (props.module === "persona" && activeTab.value === "themes") return AppearanceView;
  if (props.module === "persona") return PersonasView;
  if (props.module === "memory" && activeTab.value === "library") return MemoryView;
  if (props.module === "memory" && activeTab.value === "quality") return MemoryQualityView;
  if (props.module === "memory" && activeTab.value === "timeline") return TimelineView;
  if (props.module === "memory" && activeTab.value === "deletion") return MemoryView;
  if (props.module === "tasks") {
    if (activeTab.value === "workflows") return ButlerWorkflowsView;
    if (activeTab.value === "scenes") return ButlerScenesView;
    if (activeTab.value === "meetings") return ButlerMeetingsView;
    if (activeTab.value === "digests") return ButlerDigestsView;
    return TasksView;
  }
  if (props.module === "devices" && ["registry", "commands"].includes(activeTab.value)) return DevicesView;
  if (props.module === "devices" && activeTab.value === "diagnostics") return DiagnosticsView;
  if (props.module === "perception" && activeTab.value === "home_assistant") return HomeAssistantDevicesView;
  if (props.module === "perception" && activeTab.value === "screen") return ScreenAwarenessView;
  if (props.module === "perception" && activeTab.value === "browser") return BrowserAwarenessView;
  if (props.module === "perception" && activeTab.value === "safety") return SafetyAdminView;
  if (props.module === "perception" && activeTab.value === "channels") return ProactiveChannelsView;
  if (props.module === "logs" && activeTab.value === "live") return LiveLogsView;
  // 观测与审计：日志域（LogsView）与隐私审计域（PrivacyView）共用一个模块
  if (props.module === "logs" && ["summary", "egress", "policies"].includes(activeTab.value)) {
    return PrivacyView;
  }
  if (props.module === "logs") return LogsView;
  if (props.module === "system" && activeTab.value === "jobs") return JobsView;
  if (props.module === "system") return SystemView;
  if (props.module === "users") return UsersView;
  return PlaceholderView;
});
</script>

<template>
  <KeepAlive>
    <component :is="currentView" :module="module" :mode="activeTab" @status="(text: string, error?: boolean) => emit('status', text, error)" />
  </KeepAlive>
</template>
