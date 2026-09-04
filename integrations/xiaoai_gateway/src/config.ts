import { readFile } from "node:fs/promises";

export interface GatewayConfig {
  enabled: boolean;
  xiaomi_user_id?: string;
  xiaomi_password?: string;
  xiaomi_pass_token?: string;
  speaker_name: string;
  ha_device_id?: string;
  model?: string;
  gateway_token: string;
  trigger_prefix: string;
  tts_siid?: number;
  tts_aiid?: number;
}

export async function loadGatewayConfig(): Promise<GatewayConfig> {
  const path = process.env.XIAOAI_CONFIG_PATH || "/data/config.json";
  for (;;) {
    try {
      const raw = JSON.parse(await readFile(path, "utf8")) as Partial<GatewayConfig>;
      if (!raw.enabled) throw new Error("XiaoAI is disabled in Hub admin");
      for (const key of ["speaker_name", "gateway_token"] as const) {
        if (typeof raw[key] !== "string" || !raw[key]?.trim()) {
          throw new Error(`Missing XiaoAI config field: ${key}`);
        }
      }
      const hasPassToken = typeof raw.xiaomi_pass_token === "string" && !!raw.xiaomi_pass_token.trim();
      const hasPasswordLogin = typeof raw.xiaomi_user_id === "string" && !!raw.xiaomi_user_id.trim()
        && typeof raw.xiaomi_password === "string" && !!raw.xiaomi_password.trim();
      if (!hasPassToken && !hasPasswordLogin) {
        throw new Error("Missing XiaoAI passToken or account/password");
      }
      return {
        enabled: true,
        xiaomi_user_id: raw.xiaomi_user_id,
        xiaomi_password: raw.xiaomi_password,
        xiaomi_pass_token: raw.xiaomi_pass_token,
        speaker_name: raw.speaker_name!,
        ha_device_id: raw.ha_device_id,
        model: raw.model,
        gateway_token: raw.gateway_token!,
        trigger_prefix: raw.trigger_prefix?.trim() || "请阿莉娅",
        tts_siid: raw.tts_siid,
        tts_aiid: raw.tts_aiid,
      };
    } catch (error) {
      process.stderr.write(`Waiting for Hub XiaoAI config: ${String(error)}\n`);
      await new Promise((resolve) => setTimeout(resolve, 5_000));
    }
  }
}
