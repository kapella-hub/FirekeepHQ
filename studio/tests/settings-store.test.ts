import { mkdtemp, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { JsonSettingsStore } from "../src/core/settings-store.js";

async function tempPath(name: string): Promise<string> {
  const directory = await mkdtemp(join(tmpdir(), "firekeep-studio-"));
  return join(directory, name);
}

describe("JsonSettingsStore", () => {
  it("round-trips settings with atomic file replacement", async () => {
    const path = await tempPath("settings.json");
    const store = new JsonSettingsStore<{ version: 1; primary: string }>(path);

    await store.save({ version: 1, primary: "codex" });
    await store.save({ version: 1, primary: "kiro" });

    expect(await store.load()).toEqual({ version: 1, primary: "kiro" });
    expect(JSON.parse(await readFile(path, "utf8"))).toEqual({ version: 1, primary: "kiro" });
  });

  it("recovers from corrupt state without overwriting it", async () => {
    const path = await tempPath("settings.json");
    const warning = vi.fn();
    const raw = "{this is not json";
    await new JsonSettingsStore<string>(path).save(raw);
    await import("node:fs/promises").then(({ writeFile }) => writeFile(path, raw));

    const store = new JsonSettingsStore(path, { onWarning: warning });

    expect(await store.load()).toBeNull();
    expect(warning).toHaveBeenCalledOnce();
    expect(await readFile(path, "utf8")).toBe(raw);
  });
});
