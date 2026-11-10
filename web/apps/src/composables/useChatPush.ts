import { ref, type Ref } from "vue";
import { ChatApi } from "@aria/shared";
import {
  disablePushNotifications,
  enablePushNotifications,
  pushSubscriptionState,
} from "../push";

/** 移动推送开关：订阅状态刷新与开启/关闭（PWA 关闭页面也能收到提醒）。 */
export function useChatPush(options: {
  api: ChatApi;
  token: Ref<string>;
  setStatus: (text: string, error?: boolean) => void;
}) {
  const pushSupported =
    typeof Notification !== "undefined" && "serviceWorker" in navigator && "PushManager" in window;
  const notificationsEnabled = ref(false);
  let pushToggling = false;

  async function refreshPushToggle() {
    if (!pushSupported || !options.token.value) {
      notificationsEnabled.value = false;
      return;
    }
    notificationsEnabled.value = await pushSubscriptionState(options.api, options.token.value);
  }

  async function onNotificationsChanged() {
    if (pushToggling) return;
    pushToggling = true;
    try {
      if (notificationsEnabled.value) {
        const result = await enablePushNotifications(options.api, options.token.value);
        if (result === "enabled") {
          options.setStatus("已开启移动通知");
        } else {
          notificationsEnabled.value = false;
          options.setStatus(
            result === "denied"
              ? "浏览器拒绝了通知权限，请在系统设置中允许"
              : result === "disabled"
                ? "服务端尚未启用移动推送（需配置 VAPID 密钥）"
                : result === "unsupported"
                  ? "当前浏览器不支持移动推送"
                  : "开启移动通知失败，请稍后再试",
            result === "failed" || result === "denied",
          );
        }
      } else {
        await disablePushNotifications(options.api, options.token.value);
        options.setStatus("已关闭移动通知");
      }
    } finally {
      pushToggling = false;
    }
  }

  return { pushSupported, notificationsEnabled, refreshPushToggle, onNotificationsChanged };
}
