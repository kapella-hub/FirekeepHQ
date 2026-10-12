import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { detectWorkspaceTestCommand } from "../src/main/workspace-detect.js";

const roots: string[] = [];
afterEach(async () => { await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true }))); });

describe("detectWorkspaceTestCommand", () => {
  it("reads the workspace's top-level entries", async () => {
    const root = await mkdtemp(join(tmpdir(), "studio-detect-"));
    roots.push(root);
    await mkdir(join(root, "tests"));
    await expect(detectWorkspaceTestCommand(root)).resolves.toBe("pytest");
    await writeFile(join(root, "package.json"), "{}");
    await expect(detectWorkspaceTestCommand(root)).resolves.toBe("npm test");
  });

  it("returns null without a workspace or when it cannot be read", async () => {
    await expect(detectWorkspaceTestCommand(null)).resolves.toBeNull();
    await expect(detectWorkspaceTestCommand(join(tmpdir(), "studio-detect-does-not-exist-7f3a"))).resolves.toBeNull();
  });
});
