<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import ActivityView from "./ActivityView.vue";
import AppearanceView from "./AppearanceView.vue";
import AvatarsView from "./AvatarsView.vue";
import BrowserAwarenessView from "./BrowserAwarenessView.vue";
import ButlerDigestsView from "./ButlerDigestsView.vue";
import ButlerMeetingsView from "./ButlerMeetingsView.vue";
import ButlerScenesView from "./ButlerScenesView.vue";
import ButlerWorkflowsView from "./ButlerWorkflowsView.vue";
import DevicesView from "./DevicesView.vue";
import DiagnosticsView from "./DiagnosticsView.vue";
import HealthView from "./HealthView.vue";
import HomeAssistantDevicesView from "./HomeAssistantDevicesView.vue";
import IntegrationsCalendarView from "./IntegrationsCalendarView.vue";
import IntegrationsMailView from "./IntegrationsMailView.vue";
import IntegrationsXiaoaiView from "./IntegrationsXiaoaiView.vue";
import JobsView from "./JobsView.vue";
import LiveLogsView from "./LiveLogsView.vue";
import MemoryQualityView from "./MemoryQualityView.vue";
import MemoryView from "./MemoryView.vue";
import McpIntegrationView from "./McpIntegrationView.vue";
import ModelsView from "./ModelsView.vue";
import LogsView from "./LogsView.vue";
import OverviewView from "./OverviewView.vue";
import PersonasView from "./PersonasView.vue";
import PlaceholderView from "./PlaceholderView.vue";
import PrivacyView from "./PrivacyView.vue";
import ProactiveChannelsView from "./ProactiveChannelsView.vue";
import SafetyAdminView from "./SafetyAdminView.vue";
import ScreenAwarenessView from "./ScreenAwarenessView.vue";
import SenseAudioView from "./SenseAudioView.vue";
import SystemView from "./SystemView.vue";
import TasksView from "./TasksView.vue";
import TimelineView from "./TimelineView.vue";
import UsageView from "./UsageView.vue";

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
  if (props.module === "models" && activeTab.value === "sound") return SenseAudioView;
  if (props.module === "models") return ModelsView;
  if (props.module === "integrations" && activeTab.value === "mail") return IntegrationsMailView;
  if (props.module === "integrations" && activeTab.value === "calendar") return IntegrationsCalendarView;
  if (props.module === "integrations") return IntegrationsXiaoaiView;
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
  if (props.module === "output") return ProactiveChannelsView;
  if (props.module === "logs" && activeTab.value === "live") return LiveLogsView;
  if (props.module === "logs") return LogsView;
  if (props.module === "privacy") return PrivacyView;
  if (props.module === "system" && activeTab.value === "jobs") return JobsView;
  if (props.module === "system") return SystemView;
  if (props.module === "appearance" && activeTab.value === "gallery") return AvatarsView;
  if (props.module === "appearance") return AppearanceView;
  return PlaceholderView;
});
</script>

<template>
  <KeepAlive>
    <component :is="currentView" :module="module" :mode="activeTab" @status="(text: string, error?: boolean) => emit('status', text, error)" />
  </KeepAlive>
</template>
