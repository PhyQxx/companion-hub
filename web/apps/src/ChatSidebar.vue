<script setup lang="ts">
import {
  PhArchive,
  PhArrowCounterClockwise,
  PhDownloadSimple,
  PhPlus,
  PhTrash,
} from "@phosphor-icons/vue";
import {
  THEME_OPTIONS,
  type Conversation,
  type PrivacyLevel,
  type ThemePreference,
} from "@aria/shared";
import GoogleCalendarConnection from "./GoogleCalendarConnection.vue";

defineProps<{
  mobileOpen: boolean;
  showArchived: boolean;
  visibleConversations: Conversation[];
  archivedCount: number;
  activeId: string | null;
  token: string;
  privacy: PrivacyLevel;
}>();
const themePreference = defineModel<ThemePreference>("themePreference", { required: true });
const emit = defineEmits<{
  selectGroup: [archived: boolean];
  createConversation: [];
  openConversation: [id: string];
  exportConversation: [id: string];
  restoreConversation: [id: string];
  archiveConversation: [id: string];
  removeConversation: [id: string];
  themeChanged: [];
}>();
</script>

<template>
  <aside class="conversation-sidebar" :class="{ 'mobile-open': mobileOpen }">
    <header class="conversation-heading">
      <strong>{{ showArchived ? '已归档' : '会话' }}</strong>
      <small>{{ visibleConversations.length }} 个</small>
      <a class="admin-entry" href="/admin/" target="_blank" rel="noopener" title="打开 Aria 管理后台">管理</a>
    </header>
    <div class="conversation-tabs">
      <button type="button" :class="{ active: !showArchived }" @click="emit('selectGroup', false)">会话</button>
      <button type="button" :class="{ active: showArchived }" @click="emit('selectGroup', true)">归档 {{ archivedCount }}</button>
    </div>
    <button v-if="!showArchived" class="primary new-chat" type="button" @click="emit('createConversation')">
      <PhPlus :size="17" weight="bold" aria-hidden="true" />
      <span>新会话</span>
    </button>
    <div class="conversation-list">
      <p v-if="!visibleConversations.length" class="conversation-empty">{{ showArchived ? '还没有归档会话' : '还没有会话' }}</p>
      <div
        v-for="conversation in visibleConversations"
        :key="conversation.id"
        class="conversation-item"
        :class="{ active: conversation.id === activeId }"
      >
        <button class="conversation" type="button" @click="emit('openConversation', conversation.id)">
          {{ conversation.title || "新会话" }}
          <small>{{ conversation.last_seq }} 条消息</small>
        </button>
        <div class="conversation-actions">
          <button class="conversation-action export-action" type="button" title="导出会话为 Markdown" aria-label="导出会话为 Markdown" @click="emit('exportConversation', conversation.id)">
            <PhDownloadSimple :size="16" weight="bold" aria-hidden="true" />
          </button>
          <button v-if="showArchived" class="conversation-action" type="button" title="恢复会话" aria-label="恢复会话" @click="emit('restoreConversation', conversation.id)">
            <PhArrowCounterClockwise :size="16" weight="bold" aria-hidden="true" />
          </button>
          <button v-else class="conversation-action" type="button" title="归档会话" aria-label="归档会话" @click="emit('archiveConversation', conversation.id)">
            <PhArchive :size="16" weight="bold" aria-hidden="true" />
          </button>
          <button class="conversation-action danger-action" type="button" title="永久删除会话" aria-label="永久删除会话" @click="emit('removeConversation', conversation.id)">
            <PhTrash :size="16" weight="bold" aria-hidden="true" />
          </button>
        </div>
      </div>
    </div>
    <div class="aside-footer">
      <label class="theme-field">
        <span>界面主题</span>
        <select v-model="themePreference" aria-label="界面主题" @change="emit('themeChanged')">
          <option v-for="option in THEME_OPTIONS" :key="option.value" :value="option.value">{{ option.label }}</option>
        </select>
      </label>
      <GoogleCalendarConnection v-if="privacy === 'L1'" :key="token" :token="token" />
      <a class="debug-link" href="/chat/debug">调试台</a>
    </div>
  </aside>
</template>

<style scoped>
.admin-entry { font-size:11px; color:#9cbdad; border:1px solid #2b5142; border-radius:6px; padding:2px 8px; text-decoration:none; }
.admin-entry:hover { color:#fff; background:#2b5142; }
.conversation-heading { display:flex; align-items:center; justify-content:space-between; min-height:34px; }
.conversation-heading strong { font-size:16px; }
.conversation-heading small { color:var(--muted); font-size:11px; }
.new-chat { display:flex; align-items:center; justify-content:center; gap:7px; width:100%; box-shadow:0 5px 14px color-mix(in srgb,var(--accent) 20%,transparent); }
.conversation-tabs { display:grid; grid-template-columns:1fr 1fr; gap:6px; }
.conversation-tabs button { padding:7px 8px; color:var(--muted); background:var(--panel2); }
.conversation-tabs button.active { border-color:var(--accent); color:var(--accent); background:var(--accent-soft); }
.conversation-list { flex: 1; min-width:0; min-height: 0; overflow-x:hidden; overflow-y: auto; display: grid; align-content: start; gap: 6px; }
.conversation-empty { margin:10px 4px; color:var(--muted); font-size:12px; text-align:center; }
.conversation-item { position:relative; display:flex; align-items:center; min-width:0; max-width:100%; border-radius:10px; }
.conversation { flex:1 1 auto; min-width:0; max-width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; text-align:left; border:1px solid var(--line); background:var(--panel); padding:9px 12px; border-radius:10px; transition:padding-right 160ms ease, background-color 160ms ease, border-color 160ms ease, box-shadow 160ms ease; }
.conversation-item:hover .conversation,.conversation-item:focus-within .conversation { padding-right:98px; border-color:color-mix(in srgb,var(--accent) 28%,var(--line)); box-shadow:0 5px 16px color-mix(in srgb,var(--text) 6%,transparent); }
.conversation-item.active .conversation { border-color: var(--accent); background: var(--accent-soft); color:var(--accent); font-weight:600; }
.conversation small { display: block; overflow:hidden; text-overflow:ellipsis; color: var(--muted); margin-top: 3px; font-size: 11px; font-weight:400; }
.conversation-actions { position:absolute; right:6px; display:flex; align-items:center; gap:2px; opacity:0; transform:translateX(4px); pointer-events:none; transition:opacity 150ms ease,transform 150ms ease; }
.conversation-item:hover .conversation-actions,.conversation-item:focus-within .conversation-actions,.conversation-item.active .conversation-actions { opacity:1; transform:translateX(0); pointer-events:auto; }
.conversation-action { display:grid; place-items:center; width:27px; height:27px; padding:0; border:0; border-radius:7px; background:color-mix(in srgb,var(--panel) 86%,transparent); color:var(--muted); }
.conversation-action:hover:not(:disabled),.conversation-action:focus-visible { border-color:transparent; background:var(--accent-soft); color:var(--accent); outline:none; }
.conversation-action.danger-action:hover:not(:disabled),.conversation-action.danger-action:focus-visible { background:color-mix(in srgb,var(--danger) 10%,var(--panel)); color:var(--danger); }
.debug-link { color: var(--muted); font-size: 12px; text-decoration: none; }
.aside-footer { display:flex; align-items:end; gap:8px; min-width:0; padding-top:10px; border-top:1px solid var(--line); }
.theme-field { display:grid; flex:1; min-width:0; gap:5px; color:var(--muted); font-size:10px; }
.theme-field select { min-width:0; width:100%; padding:7px 9px; font-size:12px; }
.aside-footer .debug-link { flex:none; padding:8px 2px; }

@media (max-width: 760px) {
  .conversation-heading { display:flex; }
  .conversation-list { display:grid; overflow-x:hidden; overflow-y:auto; gap:6px; }
  .conversation-item { flex:initial; }
  .aside-footer { display:flex; }
}
</style>
