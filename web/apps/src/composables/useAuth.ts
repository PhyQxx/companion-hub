import { ref, type Ref } from "vue";
import { ChatApi, safeWebStorage, type AuthSession } from "@aria/shared";
import { disablePushNotifications } from "../push";

/**
 * 登录态：密码/SSO 登录、会话恢复、密码面板、登出与会话过期统一处理。
 * token 等消息域状态由调用方持有并以参数注入；401 订阅返回 dispose 解绑。
 */
export function useAuth(options: {
  api: ChatApi;
  token: Ref<string>;
  displayName: Ref<string>;
  accountName: Ref<string>;
  notificationsEnabled: Ref<boolean>;
  isStandalone: boolean;
  pushSupported: boolean;
  setStatus: (text: string, error?: boolean) => void;
  closeSocket: () => void;
  closeVoice: () => Promise<void>;
  enterChat: () => Promise<void>;
  refreshPushToggle: () => Promise<void>;
  clearConversationState: () => void;
}) {
  const TOKEN_KEY = "ariaChatToken";
  const authStorage = safeWebStorage(options.isStandalone ? "session" : "local");
  const ssoEnabled = ref(false);
  const authBusy = ref(false);
  const loginUsername = ref("");
  const loginPassword = ref("");
  const loginBusy = ref(false);
  const passwordPanelOpen = ref(false);
  const passwordBusy = ref(false);
  const passwordCurrent = ref("");
  const passwordNew = ref("");
  const passwordConfirm = ref("");

  function rememberSession(session: AuthSession) {
    options.token.value = session.access_token;
    options.displayName.value = session.user.display_name;
    options.accountName.value = session.user.username ?? "";
    authStorage.setItem(TOKEN_KEY, session.access_token);
  }

  function loginWithPnkx() {
    window.location.href = "/api/v1/auth/sso/login";
  }

  async function loginWithPassword() {
    if (!loginPassword.value || loginBusy.value) return;
    loginBusy.value = true;
    try {
      const session = await options.api.login(
        loginPassword.value,
        loginUsername.value.trim() || null,
      );
      rememberSession(session);
      loginPassword.value = "";
      options.setStatus("");
      await options.enterChat();
      void options.refreshPushToggle();
    } catch (error) {
      options.setStatus(error instanceof Error ? error.message : "登录失败", true);
    } finally {
      loginBusy.value = false;
    }
  }

  function togglePasswordPanel() {
    passwordPanelOpen.value = !passwordPanelOpen.value;
    passwordCurrent.value = "";
    passwordNew.value = "";
    passwordConfirm.value = "";
  }

  async function submitPasswordChange() {
    if (passwordBusy.value) return;
    if (passwordNew.value.length < 8) {
      options.setStatus("新密码至少 8 位", true);
      return;
    }
    if (passwordNew.value !== passwordConfirm.value) {
      options.setStatus("两次输入的新密码不一致", true);
      return;
    }
    passwordBusy.value = true;
    try {
      await options.api.setPassword(
        options.token.value,
        passwordNew.value,
        passwordCurrent.value || null,
      );
      togglePasswordPanel();
      options.setStatus("密码已更新");
    } catch (error) {
      options.setStatus(error instanceof Error ? error.message : "密码更新失败", true);
    } finally {
      passwordBusy.value = false;
    }
  }

  async function logout() {
    const current = options.token.value;
    options.closeSocket();
    await options.closeVoice();
    if (current && options.pushSupported) {
      await disablePushNotifications(options.api, current);
      options.notificationsEnabled.value = false;
    }
    options.token.value = "";
    options.displayName.value = "";
    options.accountName.value = "";
    authStorage.removeItem(TOKEN_KEY);
    options.clearConversationState();
    if (current) {
      try {
        await options.api.logout(current);
      } catch {
        /* session already gone server-side */
      }
    }
  }

  /**
   * 会话过期（登录态 8 小时 TTL）：轮询面板收到 401 时触发。WebSocket 是
   * 长连接、只在建连时鉴权，所以聊天看似正常而 REST 轮询持续 401——这里
   * 统一登出并明确提示，停止无效轮询刷屏。
   */
  let sessionExpiring = false;
  const disposeUnauthorized = ChatApi.subscribeUnauthorized(() => {
    if (!options.token.value || sessionExpiring) return;
    sessionExpiring = true;
    void (async () => {
      options.closeSocket();
      await options.closeVoice();
      options.token.value = "";
      options.displayName.value = "";
      authStorage.removeItem(TOKEN_KEY);
      options.clearConversationState();
      options.setStatus("登录已过期，请重新登录", true);
      sessionExpiring = false;
    })();
  });

  /** 挂载时恢复登录态；未登录或恢复失败返回 false（同时完成 SSO 状态探测）。 */
  async function restoreSession(): Promise<boolean> {
    const saved = authStorage.getItem(TOKEN_KEY);
    if (!saved) {
      // 未登录不预载人格信息：伴侣名字等是登录后每个用户自己的视图
      if (location.hash.startsWith("#sso-error=")) {
        options.setStatus(decodeURIComponent(location.hash.slice("#sso-error=".length)), true);
      }
      try {
        const data = await options.api.ssoStatus();
        ssoEnabled.value = data.enabled === true;
      } catch {
        options.setStatus("无法连接 Aria 服务", true);
      }
      return false;
    }
    options.token.value = saved;
    try {
      const session = await options.api.me(options.token.value);
      options.displayName.value = session.user.display_name;
      options.accountName.value = session.user.username ?? "";
    } catch {
      authStorage.removeItem(TOKEN_KEY);
      options.token.value = "";
      return false;
    }
    return true;
  }

  function disposeAuth() {
    disposeUnauthorized();
  }

  return {
    ssoEnabled,
    authBusy,
    loginUsername,
    loginPassword,
    loginBusy,
    loginWithPnkx,
    loginWithPassword,
    passwordPanelOpen,
    passwordBusy,
    passwordCurrent,
    passwordNew,
    passwordConfirm,
    togglePasswordPanel,
    submitPasswordChange,
    logout,
    restoreSession,
    disposeAuth,
  };
}
