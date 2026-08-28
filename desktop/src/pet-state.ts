export const DESKTOP_CONFIG_KEY = "ariaDesktopClientConfig";
export const PET_VISIBLE_KEY = "ariaDesktopPetVisible";
export const PET_CLICK_THROUGH_KEY = "ariaDesktopPetClickThrough";
export const PET_POSITION_KEY = "ariaDesktopPetPosition";

export interface PetPosition {
  x: number;
  y: number;
}

export function petStageUrl(rawConfig: string | null): string | null {
  if (!rawConfig) return null;
  try {
    const config = JSON.parse(rawConfig) as { hub_url?: unknown };
    if (typeof config.hub_url !== "string") return null;
    const hub = new URL(config.hub_url);
    if (hub.protocol !== "http:" && hub.protocol !== "https:") return null;
    hub.pathname = `${hub.pathname.replace(/\/$/, "")}/desktop/pet/`;
    hub.search = "";
    hub.hash = "";
    return hub.href;
  } catch {
    return null;
  }
}

export function parsePetPosition(raw: string | null): PetPosition | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Partial<PetPosition>;
    if (!Number.isFinite(value.x) || !Number.isFinite(value.y)) return null;
    return { x: Math.round(value.x!), y: Math.round(value.y!) };
  } catch {
    return null;
  }
}
