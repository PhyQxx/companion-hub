<script setup lang="ts">
defineProps<{
  ssoEnabled: boolean;
  authBusy: boolean;
  loginBusy: boolean;
  isStandalone: boolean;
  isIos: boolean;
  canInstall: boolean;
  statusText: string;
  statusError: boolean;
}>();
const loginUsername = defineModel<string>("loginUsername", { default: "" });
const loginPassword = defineModel<string>("loginPassword", { default: "" });
const emit = defineEmits<{ login: []; loginPnkx: []; install: [] }>();
</script>

<template>
  <div class="auth">
    <div class="card">
      <h1>Aria Hub</h1>
      <form class="login-form" @submit.prevent="emit('login')">
        <label>
          <span>账号</span>
          <input
            v-model="loginUsername"
            type="text"
            autocomplete="username"
            placeholder="用户名（未设置可留空）"
          >
        </label>
        <label>
          <span>密码</span>
          <input
            v-model="loginPassword"
            type="password"
            autocomplete="current-password"
            placeholder="密码"
          >
        </label>
        <button class="primary" type="submit" :disabled="loginBusy || !loginPassword">登录</button>
      </form>
      <div v-if="ssoEnabled" class="login-divider"><span>或</span></div>
      <button v-if="ssoEnabled" class="ghost" type="button" :disabled="authBusy" @click="emit('loginPnkx')">
        使用 pnkx 账号登录
      </button>
      <button v-if="canInstall && !isStandalone" class="ghost" type="button" @click="emit('install')">安装到手机</button>
      <p v-else-if="isIos && !isStandalone" class="install-hint">iPhone：点 Safari“分享”→“添加到主屏幕”</p>
      <p v-if="statusText" class="status" :class="{ error: statusError }">{{ statusText }}</p>
    </div>
  </div>
</template>

<style scoped>
.auth { display: grid; place-items: center; height: 100%; }
.card { display: grid; gap: 12px; width: min(360px, 90vw); background: var(--panel); border: 1px solid var(--line); border-radius: 16px; padding: 28px; box-shadow: var(--shadow); }
.login-form { display: grid; gap: 10px; }
.login-form label { display: grid; gap: 4px; font-size: 12px; color: var(--muted); }
.login-form input { min-height: 38px; border: 1px solid var(--line); border-radius: 10px; padding: 0 12px; background: var(--panel2); color: var(--text); font: inherit; }
.login-divider { display: flex; align-items: center; gap: 10px; color: var(--muted); font-size: 12px; }
.login-divider::before, .login-divider::after { content: ""; flex: 1; height: 1px; background: var(--line); }
.card h1 { margin: 0; font-size: 22px; }
.hint { color: var(--muted); margin: 0; font-size: 13px; }
.install-hint { margin:0; color:var(--muted); font-size:12px; line-height:1.5; text-align:center; }
.status { color: var(--muted); font-size: 12px; margin: 0; }
.status.error { color: var(--danger); }
</style>
