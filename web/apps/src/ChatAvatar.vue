<script setup lang="ts">
import { type AvatarChoice } from "@aria/shared";
import Live2DStage from "./Live2DStage.vue";
import { useMessagePresentation } from "./composables/useMessagePresentation";
import type { AvatarMeta } from "./composables/useAvatar";

defineProps<{
  mobileOpen: boolean;
  personaMeta: { version: number; name: string } | null;
  avatarMeta: AvatarMeta | null;
  avatars: AvatarChoice[];
  avatarSwitchBusy: boolean;
  avatarMotion: { value: string; sequence: number } | null;
  avatarEmotion: string;
  avatarExpression: string | null;
  avatarSpeaking: boolean;
  avatarImageUrl: string | null;
  voiceViseme: number;
  displayName: string;
  accountName: string;
  passwordPanelOpen: boolean;
  passwordBusy: boolean;
  isStandalone: boolean;
  canInstall: boolean;
}>();
const passwordCurrent = defineModel<string>("passwordCurrent", { default: "" });
const passwordNew = defineModel<string>("passwordNew", { default: "" });
const passwordConfirm = defineModel<string>("passwordConfirm", { default: "" });
const emit = defineEmits<{
  togglePasswordPanel: [];
  submitPasswordChange: [];
  install: [];
  logout: [];
  switchAvatar: [event: Event];
}>();
const { emotionLabels } = useMessagePresentation();
</script>

<template>
  <aside class="avatar-sidebar" :class="{ 'mobile-open': mobileOpen }">
    <header class="companion-heading">
      <div>
        <strong>{{ personaMeta?.name ?? '助手' }}</strong>
        <small v-if="personaMeta" class="persona-version">Persona v{{ personaMeta.version }}</small>
      </div>
      <div class="aside-meta">
        <span :title="accountName || undefined">{{ displayName }}</span>
        <button class="ghost" type="button" @click="emit('togglePasswordPanel')">密码</button>
        <button v-if="canInstall && !isStandalone" class="ghost" type="button" @click="emit('install')">安装</button>
        <button class="ghost" type="button" @click="emit('logout')">退出</button>
      </div>
      <form v-if="passwordPanelOpen" class="password-panel" @submit.prevent="emit('submitPasswordChange')">
        <p class="password-hint">设置后可用「{{ accountName || '用户名' }} + 密码」直接登录{{ accountName ? '' : '（用户名由管理员在用户管理中设置）' }}</p>
        <input v-model="passwordCurrent" type="password" autocomplete="current-password" placeholder="当前密码（首次设置留空）">
        <input v-model="passwordNew" type="password" autocomplete="new-password" placeholder="新密码（至少 8 位）">
        <input v-model="passwordConfirm" type="password" autocomplete="new-password" placeholder="再次输入新密码">
        <div class="password-actions">
          <button class="ghost" type="button" @click="emit('togglePasswordPanel')">取消</button>
          <button class="primary" type="submit" :disabled="passwordBusy">保存</button>
        </div>
      </form>
    </header>
    <section v-if="avatarMeta" class="avatar-stage" :class="`avatar-${avatarMeta.engine}`">
      <Transition name="avatar-expression" mode="out-in">
        <Live2DStage
          v-if="avatarMeta.engine === 'live2d' && avatarMeta.assets.model"
          :key="avatarMeta.assets.model"
          :model-url="avatarMeta.assets.model"
          :emotion="avatarEmotion"
          :expression="avatarExpression"
          :lip-sync="voiceViseme"
          :speaking="avatarSpeaking"
          :motion="avatarMotion?.value"
          :motion-sequence="avatarMotion?.sequence"
        />
        <img v-else-if="avatarImageUrl" :key="avatarImageUrl" class="avatar-image" :src="avatarImageUrl" :alt="`${avatarMeta.name} · ${emotionLabels[avatarEmotion] ?? avatarEmotion}`" />
        <div v-else :key="avatarMeta.instanceId" class="avatar-orb"><span>{{ avatarMeta.name[0] }}</span></div>
      </Transition>
      <div class="avatar-caption"><strong>{{ avatarMeta.name }}</strong><small>{{ emotionLabels[avatarEmotion] ?? avatarEmotion }}</small></div>
    </section>
    <label v-if="avatars.length" class="avatar-switcher">
      <span>当前形象</span>
      <select :value="avatarMeta?.instanceId ?? ''" :disabled="avatarSwitchBusy" @change="emit('switchAvatar', $event)">
        <option v-for="avatar in avatars" :key="avatar.instance_id" :value="avatar.instance_id">{{ avatar.name }}</option>
      </select>
    </label>
  </aside>
</template>

<style scoped>
.companion-heading { display:grid; gap:12px; }
.companion-heading strong { font-size:16px; }
.persona-version { display:block; margin-top:3px; color:var(--muted); font-size:10px; }
.aside-meta { display:flex; justify-content:space-between; align-items:center; color:var(--muted); font-size:12px; }
.avatar-stage { position:relative; display:grid; flex:1 1 auto; place-items:center; min-height:300px; overflow:hidden; border:1px solid var(--line); border-radius:16px; background:linear-gradient(160deg,var(--stage-from),var(--stage-to)); }
.avatar-image { width:100%; height:100%; min-height:300px; object-fit:contain; object-position:center; transition:opacity 180ms ease,transform 220ms ease; }
.avatar-expression-enter-from,.avatar-expression-leave-to { opacity:0; transform:scale(1.015); }
.avatar-expression-leave-active { position:absolute; }
.avatar-orb { display:grid; place-items:center; width:68px; height:68px; border-radius:50%; background:linear-gradient(135deg,#f2b6aa,#b97679); box-shadow:0 12px 34px #b9767955; color:#fff; font-size:24px; font-weight:700; }
.avatar-abstract .avatar-orb { background:radial-gradient(circle at 35% 30%,#fff,#75d8d7 24%,#9a86d2 62%,#28244c); box-shadow:0 0 38px #75d8d777; animation:avatar-pulse 3s ease-in-out infinite; }
.avatar-caption { position:absolute; inset:auto 12px 10px; display:flex; justify-content:space-between; align-items:end; gap:8px; }.avatar-caption strong { max-width:78%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:11px; }.avatar-caption small { color:var(--muted); font-size:9px; }
.avatar-switcher{display:grid;flex:none;gap:6px;color:var(--muted);font-size:10px}.avatar-switcher select{min-width:0;width:100%;padding:9px 10px;font-size:12px}
@keyframes avatar-pulse { 50% { transform:scale(1.06); filter:brightness(1.12); } }
.password-panel { display: grid; gap: 8px; margin: 0 0 10px; padding: 12px; border: 1px solid var(--line); border-radius: 12px; background: var(--panel2); }
.password-panel input { min-height: 38px; border: 1px solid var(--line); border-radius: 10px; padding: 0 12px; background: var(--panel2); color: var(--text); font: inherit; }
.password-hint { margin: 0; font-size: 12px; color: var(--muted); line-height: 1.5; }
.password-actions { display: flex; justify-content: flex-end; gap: 8px; }

@media (max-width:1100px) {
  .avatar-stage { min-height:260px; }
}

@media (max-width: 760px) {
  .avatar-stage { flex:1 1 auto; height:auto; min-height:260px; }
  .avatar-image { min-height:260px; }
}
</style>
