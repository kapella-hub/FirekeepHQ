import { describe, expect, it } from "vitest";
import { RuntimeRegistry } from "../src/core/runtime-registry.js";
import { MemorySettingsStore } from "../src/core/settings-store.js";
import { StudioService, type StudioPersistedState } from "../src/core/studio-service.js";
import { FakeRuntime } from "./helpers/fake-runtime.js";

function legacyState(): Record<string, unknown> {
  return {
    version: 1,
    activeSessionId: "session-legacy",
    workspacePath: "C:\work",
    primaryRuntimeId: "grok",
    reviewerRuntimeIds: ["grok", "claude"],
    reviewerMode: "after-turn",
    selectedModels: { grok: "grok-4" },
    selectedEfforts: {},
    permissionModes: {},
    nativeSessionIds: {},
    tokenBudget: null,
    voiceEnabled: true,
    theme: "dark",
  };
}

describe("0.5 settings migration", () => {
  it("drops grok and voiceEnabled without crashing", async () => {
    const settings = new MemorySettingsStore<StudioPersistedState>();
    await settings.save(legacyState() as unknown as StudioPersistedState);
    const claude = new FakeRuntime({ id: "claude", displayName: "Claude", description: "", transport: "test", capabilities: ["chat", "review"] });
    const service = new StudioService({ runtimes: new RuntimeRegistry([claude]), settings });
    await service.initialize();
    const snapshot = service.snapshot();
    expect(snapshot.primaryRuntimeId).toBeNull();
    expect(snapshot.reviewerRuntimeIds).toEqual(["claude"]);
    expect(snapshot.theme).toBe("dark");
    expect("voiceEnabled" in snapshot).toBe(false);
  });

  it("registers exactly codex, claude and kiro", async () => {
    const { createRuntimeRegistry } = await import("../src/main/runtime/index.js");
    const ids = createRuntimeRegistry("0.5.0").list().map((runtime) => runtime.descriptor.id).sort();
    expect(ids).toEqual(["claude", "codex", "kiro"]);
  });
});
