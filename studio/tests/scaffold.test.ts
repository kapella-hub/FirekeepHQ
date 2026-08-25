import { describe, expect, it } from "vitest";
import { STUDIO_EVENT_CHANNEL, STUDIO_INVOKE_CHANNEL } from "../src/shared/ipc.js";

describe("Studio scaffold", () => {
  it("uses distinct allowlisted IPC channels", () => {
    expect(STUDIO_INVOKE_CHANNEL).toBe("studio:invoke");
    expect(STUDIO_EVENT_CHANNEL).toBe("studio:event");
    expect(STUDIO_EVENT_CHANNEL).not.toBe(STUDIO_INVOKE_CHANNEL);
  });
});
