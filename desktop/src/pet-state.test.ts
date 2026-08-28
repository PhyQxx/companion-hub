import { describe, expect, it } from "vitest";
import {
  parsePetPosition,
  persistedPetPosition,
  petStageUrl,
  resolvePetPosition,
} from "./pet-state";

describe("petStageUrl", () => {
  it("builds a Hub-hosted pet stage URL", () => {
    expect(petStageUrl('{"hub_url":"http://127.0.0.1:8000/"}')).toBe(
      "http://127.0.0.1:8000/desktop/pet/",
    );
  });

  it("rejects missing and unsafe Hub protocols", () => {
    expect(petStageUrl(null)).toBeNull();
    expect(petStageUrl('{"hub_url":"javascript:alert(1)"}')).toBeNull();
  });
});

describe("parsePetPosition", () => {
  it("rounds a valid persisted physical position", () => {
    expect(parsePetPosition('{"x":12.4,"y":-4.6}')).toEqual({ x: 12, y: -5 });
  });

  it("rejects malformed positions", () => {
    expect(parsePetPosition('{"x":12}')).toBeNull();
    expect(parsePetPosition("nope")).toBeNull();
  });

  it("restores the same relative anchor when monitor scale or size changes", () => {
    expect(resolvePetPosition(
      { x: 100, y: 100, monitorName: "Studio", relativeX: 1, relativeY: 0.5 },
      [{ name: "Studio", x: -2560, y: 0, width: 2560, height: 1440 }],
      { width: 420, height: 560 },
    )).toEqual({
      x: -420,
      y: 440,
      monitorName: "Studio",
      relativeX: 1,
      relativeY: 0.5,
    });
  });

  it("clamps an off-screen position to the nearest remaining monitor", () => {
    expect(resolvePetPosition(
      { x: -3000, y: 200, monitorName: "Removed" },
      [{ name: "Built-in", x: 0, y: 25, width: 1728, height: 1080 }],
      { width: 420, height: 560 },
    )).toMatchObject({ x: 0, y: 200, monitorName: "Built-in" });
  });

  it("persists a monitor-relative position", () => {
    expect(persistedPetPosition(
      { x: 654, y: 285 },
      { name: "Built-in", x: 0, y: 25, width: 1728, height: 1080 },
      { width: 420, height: 560 },
    )).toEqual({ x: 654, y: 285, monitorName: "Built-in", relativeX: 0.5, relativeY: 0.5 });
  });
});
