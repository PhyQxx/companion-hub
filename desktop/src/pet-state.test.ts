import { describe, expect, it } from "vitest";
import { parsePetPosition, petStageUrl } from "./pet-state";

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
});
