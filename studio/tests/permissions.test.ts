import { describe, expect, it } from "vitest";
import { allowsMicrophoneCheck, allowsMicrophoneRequest } from "../src/main/permissions.js";

describe("Electron media permission policy", () => {
  it("allows only an audio-only request from Studio's main frame", () => {
    expect(allowsMicrophoneRequest({
      permission: "media",
      attachedToStudioWindow: true,
      isMainFrame: true,
      mediaTypes: ["audio"],
    })).toBe(true);

    for (const mediaTypes of [["video"], ["audio", "video"], undefined] as const) {
      expect(allowsMicrophoneRequest({
        permission: "media",
        attachedToStudioWindow: true,
        isMainFrame: true,
        ...(mediaTypes ? { mediaTypes } : {}),
      })).toBe(false);
    }
  });

  it("denies detached, subframe, non-media, camera, and unknown checks", () => {
    const allowed = {
      permission: "media",
      attachedToStudioWindow: true,
      isMainFrame: true,
      mediaType: "audio",
    } as const;
    expect(allowsMicrophoneCheck(allowed)).toBe(true);
    expect(allowsMicrophoneCheck({ ...allowed, attachedToStudioWindow: false })).toBe(false);
    expect(allowsMicrophoneCheck({ ...allowed, isMainFrame: false })).toBe(false);
    expect(allowsMicrophoneCheck({ ...allowed, permission: "geolocation" })).toBe(false);
    expect(allowsMicrophoneCheck({ ...allowed, mediaType: "video" })).toBe(false);
    expect(allowsMicrophoneCheck({ ...allowed, mediaType: "unknown" })).toBe(false);
  });
});
