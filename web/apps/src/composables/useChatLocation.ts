import { ref, type Ref } from "vue";
import {
  matchesLocationIntent,
  type ClientLocationPayload,
  type PrivacyLevel,
} from "@aria/shared";

const LOCATION_ENABLED_KEY = "ariaLocationEnabled";
// 与服务端 tools/location.py 的 LOCATION_TTL(15 分钟)保持一致。
const LOCATION_TTL_MS = 15 * 60 * 1000;

/**
 * 终端定位：命中位置类查询时才请求/附带(惰性授权)，坐标只随帧内存传输。
 */
export function useChatLocation(options: {
  privacy: Ref<PrivacyLevel>;
  setStatus: (text: string, error?: boolean) => void;
}) {
  const locationEnabled = ref(localStorage.getItem(LOCATION_ENABLED_KEY) !== "0");
  const locationSupported = typeof navigator !== "undefined" && "geolocation" in navigator;
  const locationPolicy = ref<"ask_each_time" | "allow_session">("ask_each_time");
  let cachedLocation: { payload: ClientLocationPayload; at: number } | null = null;

  function freshCachedLocation(): ClientLocationPayload | null {
    if (cachedLocation && Date.now() - cachedLocation.at < LOCATION_TTL_MS) {
      return cachedLocation.payload;
    }
    cachedLocation = null;
    return null;
  }

  function fetchBrowserPosition(): Promise<ClientLocationPayload | null> {
    return new Promise((resolve) => {
      if (!locationSupported) return resolve(null);
      if (locationPolicy.value === "allow_session") {
        const cached = freshCachedLocation();
        if (cached) return resolve(cached);
      }
      // ask_each_time: 每次都取新位置(maximumAge 0); 浏览器授权弹窗只在首次出现。
      navigator.geolocation.getCurrentPosition(
        (position) => {
          const payload: ClientLocationPayload = {
            latitude: position.coords.latitude,
            longitude: position.coords.longitude,
            accuracy_m: Math.round(position.coords.accuracy ?? 0),
          };
          cachedLocation = { payload, at: Date.now() };
          resolve(payload);
        },
        () => resolve(null),
        {
          timeout: 8000,
          maximumAge: locationPolicy.value === "allow_session" ? LOCATION_TTL_MS : 0,
          enableHighAccuracy: false,
        },
      );
    });
  }

  async function resolveLocationForText(text: string): Promise<ClientLocationPayload | null> {
    if (!locationEnabled.value || !locationSupported) return null;
    if (options.privacy.value === "L2" || !matchesLocationIntent(text)) return null;
    const payload = await fetchBrowserPosition();
    if (!payload) options.setStatus("未获取到本机定位，将使用默认城市", false);
    return payload;
  }

  function onLocationEnabledChanged() {
    localStorage.setItem(LOCATION_ENABLED_KEY, locationEnabled.value ? "1" : "0");
    if (!locationEnabled.value) cachedLocation = null;
  }

  return {
    locationEnabled,
    locationSupported,
    locationPolicy,
    freshCachedLocation,
    resolveLocationForText,
    onLocationEnabledChanged,
  };
}
