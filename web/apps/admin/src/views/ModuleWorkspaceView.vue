<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import DevicesView from "./DevicesView.vue";
import HomeAssistantDevicesView from "./HomeAssistantDevicesView.vue";
import MemoryView from "./MemoryView.vue";
import ModelsView from "./ModelsView.vue";
import OverviewView from "./OverviewView.vue";
import PersonasView from "./PersonasView.vue";
import PlaceholderView from "./PlaceholderView.vue";
import TimelineView from "./TimelineView.vue";

const props = defineProps<{ module: string }>();
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();
const route = useRoute();
const activeTab = computed(() => String(route.query.tab ?? ""));

const currentView = computed(() => {
  if (props.module === "overview" && activeTab.value === "runtime") return OverviewView;
  if (props.module === "models") return ModelsView;
  if (props.module === "persona") return PersonasView;
  if (props.module === "memory" && activeTab.value === "library") return MemoryView;
  if (props.module === "memory" && activeTab.value === "timeline") return TimelineView;
  if (props.module === "memory" && activeTab.value === "deletion") return MemoryView;
  if (props.module === "devices" && ["registry", "commands"].includes(activeTab.value)) return DevicesView;
  if (props.module === "devices" && activeTab.value === "home_assistant") return HomeAssistantDevicesView;
  return PlaceholderView;
});
</script>

<template>
  <component :is="currentView" :module="module" :mode="activeTab" @status="(text: string, error?: boolean) => emit('status', text, error)" />
</template>
