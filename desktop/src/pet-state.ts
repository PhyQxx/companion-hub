export const DESKTOP_CONFIG_KEY = "ariaDesktopClientConfig";
export const PET_VISIBLE_KEY = "ariaDesktopPetVisible";
export const PET_CLICK_THROUGH_KEY = "ariaDesktopPetClickThrough";
export const PET_POSITION_KEY = "ariaDesktopPetPosition";

export interface PetPosition {
  x: number;
  y: number;
  monitorName?: string | null;
  relativeX?: number;
  relativeY?: number;
}

export interface PetMonitorArea {
  name: string | null;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface PetWindowSize {
  width: number;
  height: number;
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
    const position: PetPosition = { x: Math.round(value.x!), y: Math.round(value.y!) };
    if (typeof value.monitorName === "string" || value.monitorName === null) {
      position.monitorName = value.monitorName;
    }
    if (
      typeof value.relativeX === "number" && value.relativeX >= 0 && value.relativeX <= 1 &&
      typeof value.relativeY === "number" && value.relativeY >= 0 && value.relativeY <= 1
    ) {
      position.relativeX = value.relativeX;
      position.relativeY = value.relativeY;
    }
    return position;
  } catch {
    return null;
  }
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(Math.max(value, minimum), Math.max(minimum, maximum));
}

function distanceToArea(position: PetPosition, monitor: PetMonitorArea): number {
  const dx = Math.max(monitor.x - position.x, 0, position.x - (monitor.x + monitor.width));
  const dy = Math.max(monitor.y - position.y, 0, position.y - (monitor.y + monitor.height));
  return dx * dx + dy * dy;
}

export function resolvePetPosition(
  stored: PetPosition | null,
  monitors: PetMonitorArea[],
  windowSize: PetWindowSize,
): PetPosition | null {
  if (!stored || monitors.length === 0) return null;
  const named = stored.monitorName == null
    ? null
    : monitors.find((monitor) => monitor.name === stored.monitorName) ?? null;
  const monitor = named ?? [...monitors].sort(
    (left, right) => distanceToArea(stored, left) - distanceToArea(stored, right),
  )[0]!;
  const maxX = monitor.x + monitor.width - windowSize.width;
  const maxY = monitor.y + monitor.height - windowSize.height;
  const relativeReady = named && stored.relativeX != null && stored.relativeY != null;
  const desiredX = relativeReady
    ? monitor.x + stored.relativeX! * Math.max(0, monitor.width - windowSize.width)
    : stored.x;
  const desiredY = relativeReady
    ? monitor.y + stored.relativeY! * Math.max(0, monitor.height - windowSize.height)
    : stored.y;
  return {
    x: Math.round(clamp(desiredX, monitor.x, maxX)),
    y: Math.round(clamp(desiredY, monitor.y, maxY)),
    monitorName: monitor.name,
    relativeX: maxX > monitor.x ? clamp((desiredX - monitor.x) / (maxX - monitor.x), 0, 1) : 0,
    relativeY: maxY > monitor.y ? clamp((desiredY - monitor.y) / (maxY - monitor.y), 0, 1) : 0,
  };
}

export function persistedPetPosition(
  position: Pick<PetPosition, "x" | "y">,
  monitor: PetMonitorArea,
  windowSize: PetWindowSize,
): PetPosition {
  const availableWidth = Math.max(0, monitor.width - windowSize.width);
  const availableHeight = Math.max(0, monitor.height - windowSize.height);
  return {
    x: Math.round(position.x),
    y: Math.round(position.y),
    monitorName: monitor.name,
    relativeX: availableWidth > 0 ? clamp((position.x - monitor.x) / availableWidth, 0, 1) : 0,
    relativeY: availableHeight > 0 ? clamp((position.y - monitor.y) / availableHeight, 0, 1) : 0,
  };
}
