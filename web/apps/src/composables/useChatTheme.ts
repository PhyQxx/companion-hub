import { ref, type Ref } from "vue";
import {
  ChatApi,
  THEME_CHANNEL_NAME,
  THEME_OPTIONS,
  broadcastThemePreference,
  readThemePreference,
  readThemeSchedule,
  saveThemePreference,
  type ThemePreference,
  type ThemeSchedule,
} from "@aria/shared";

/** 界面主题：本机持久化、跨标签页同步（BroadcastChannel）与服务端回写。 */
export function useChatTheme(options: {
  api: ChatApi;
  token: Ref<string>;
  setStatus: (text: string, error?: boolean) => void;
}) {
  const themePreference = ref<ThemePreference>(readThemePreference());
  let themeChannel: BroadcastChannel | null = null;

  async function onThemeChanged() {
    // 定时切换时沿用当前存储的时段边界（时段编辑入口在管理后台主题中心）
    const schedule = themePreference.value === "scheduled" ? readThemeSchedule() : undefined;
    saveThemePreference(themePreference.value, schedule);
    broadcastThemePreference(themePreference.value, schedule);
    if (!options.token.value) return;
    try {
      await options.api.updateThemePreference(options.token.value, themePreference.value, schedule);
      options.setStatus("主题已同步");
    } catch (error) {
      options.setStatus(error instanceof Error ? `${error.message}；已保存在本机` : "主题同步失败；已保存在本机", true);
    }
  }

  async function syncThemePreference() {
    if (!options.token.value) return;
    try {
      const preference = await options.api.themePreference(options.token.value);
      if (preference.selection !== themePreference.value) {
        themePreference.value = preference.selection;
        saveThemePreference(preference.selection, preference.schedule ?? undefined);
      }
    } catch {
      /* 服务不可用时保留本机最后可用主题 */
    }
  }

  function attachThemeChannel() {
    if (typeof BroadcastChannel === "undefined") return;
    themeChannel = new BroadcastChannel(THEME_CHANNEL_NAME);
    themeChannel.onmessage = (event: MessageEvent<{ selection?: ThemePreference; schedule?: ThemeSchedule }>) => {
      const selection = event.data?.selection;
      if (!selection || !THEME_OPTIONS.some((option) => option.value === selection)) return;
      themePreference.value = selection;
      saveThemePreference(selection, event.data?.schedule);
    };
  }

  function detachThemeChannel() {
    themeChannel?.close();
    themeChannel = null;
  }

  return { themePreference, onThemeChanged, syncThemePreference, attachThemeChannel, detachThemeChannel };
}
