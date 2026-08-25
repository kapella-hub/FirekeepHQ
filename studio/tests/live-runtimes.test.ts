import { describe, expect, it } from "vitest";
import { ClaudeRuntime } from "../src/main/runtime/claude-runtime.js";
import { CodexRuntime } from "../src/main/runtime/codex-runtime.js";
import { KiroRuntime } from "../src/main/runtime/kiro-runtime.js";
import { FirekeepClient } from "../src/main/firekeep-client.js";

const live = process.env.STUDIO_LIVE_RUNTIMES === "1";

describe.skipIf(!live)("installed runtime smoke", () => {
  it.each([
    ["Codex", () => new CodexRuntime()],
    ["Claude", () => new ClaudeRuntime()],
    ["Kiro", () => new KiroRuntime()],
  ] as const)("discovers %s auth, models, and reasoning controls without an inference turn", async (_name, create) => {
    const runtime = create();
    const connection = await runtime.probe();
    expect(connection, connection.detail).toMatchObject({ state: "ready" });
    const auth = await runtime.authStatus();
    expect(auth, auth.detail).toMatchObject({ state: "connected" });
    const models = await runtime.listModels();
    expect(models.length, `${_name} returned no live models`).toBeGreaterThan(0);
    expect(models.some((model) => model.efforts?.length), `${_name} returned no live reasoning controls`).toBe(true);
  }, 45_000);

  it("executes the installed Firekeep Client Kit status command", async () => {
    const result = await new FirekeepClient().execute("status", []);
    expect(result, result.output).toMatchObject({ ok: true, exitCode: 0 });
  }, 30_000);
});
