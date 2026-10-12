import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { removeLegacySecrets } from "../src/main/legacy-cleanup.js";

const dirs: string[] = [];
const temp = (): string => { const dir = mkdtempSync(join(tmpdir(), "studio-legacy-")); dirs.push(dir); return dir; };
afterEach(() => { for (const dir of dirs.splice(0)) rmSync(dir, { recursive: true, force: true }); });

describe("removeLegacySecrets", () => {
  it("removes the 0.4 encrypted secrets.json and leaves other user data alone", async () => {
    const dir = temp();
    writeFileSync(join(dir, "secrets.json"), "{\"grok\":\"encrypted\"}");
    writeFileSync(join(dir, "settings.json"), "{}");
    await removeLegacySecrets(dir);
    expect(existsSync(join(dir, "secrets.json"))).toBe(false);
    expect(existsSync(join(dir, "settings.json"))).toBe(true);
  });

  it("does not throw when there is nothing to remove or the directory is missing", async () => {
    await expect(removeLegacySecrets(temp())).resolves.toBeUndefined();
    await expect(removeLegacySecrets(join(temp(), "does", "not", "exist"))).resolves.toBeUndefined();
  });
});
