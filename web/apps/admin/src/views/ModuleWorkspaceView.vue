<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import ActivityView from "./ActivityView.vue";
import AppearanceView from "./AppearanceView.vue";
import AvatarsView from "./AvatarsView.vue";
import ConflictsView from "./ConflictsView.vue";
import DevicesView from "./DevicesView.vue";
import DiagnosticsView from "./DiagnosticsView.vue";
import HealthView from "./HealthView.vue";
import HomeAssistantDevicesView from "./HomeAssistantDevicesView.vue";
import JobsView from "./JobsView.vue";
import LiveLogsView from "./LiveLogsView.vue";
import MemoryView from "./MemoryView.vue";
import ModelsView from "./ModelsView.vue";
import LogsView from "./LogsView.vue";
import OverviewView from "./OverviewView.vue";
import PairingView from "./PairingView.vue";
import PersonasView from "./PersonasView.vue";
import PlaceholderView from "./PlaceholderView.vue";
import PrivacyView from "./PrivacyView.vue";
import ProactiveChannelsView from "./ProactiveChannelsView.vue";
import QualityView from "./QualityView.vue";
import ScreenAwarenessView from "./ScreenAwarenessView.vue";
import SystemView from "./SystemView.vue";
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
  if (props.module === "models") return ModelsView;
  if (props.module === "persona") return PersonasView;
  if (props.module === "memory" && activeTab.value === "library") return MemoryView;
  if (props.module === "memory" && activeTab.value === "quality") return QualityView;
  if (props.module === "memory" && activeTab.value === "conflicts") return ConflictsView;
  if (props.module === "memory" && activeTab.value === "timeline") return TimelineView;
  if (props.module === "memory" && activeTab.value === "deletion") return MemoryView;
  if (props.module === "devices" && ["registry", "commands"].includes(activeTab.value)) return DevicesView;
  if (props.module === "devices" && activeTab.value === "pairing") return PairingView;
  if (props.module === "devices" && activeTab.value === "channels") return ProactiveChannelsView;
  if (props.module === "devices" && activeTab.value === "home_assistant") return HomeAssistantDevicesView;
  if (props.module === "devices" && activeTab.value === "diagnostics") return DiagnosticsView;
  if (props.module === "logs" && activeTab.value === "live") return LiveLogsView;
  if (props.module === "logs") return LogsView;
  if (props.module === "privacy") return PrivacyView;
  if (props.module === "system") return SystemView;
  if (props.module === "jobs") return JobsView;
  if (props.module === "screen_awareness") return ScreenAwarenessView;
  if (props.module === "avatars") return AvatarsView;
  if (props.module === "appearance") return AppearanceView;
  return PlaceholderView;
});
</script>

<template>
  <KeepAlive>
    <component :is="currentView" :module="module" :mode="activeTab" @status="(text: string, error?: boolean) => emit('status', text, error)" />
  </KeepAlive>
</template>
