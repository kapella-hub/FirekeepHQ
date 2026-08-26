import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { afterEach, describe, expect, it } from "vitest";

const temporaryDirectories: string[] = [];

afterEach(async () => {
  await Promise.all(temporaryDirectories.splice(0).map((directory) => rm(directory, { recursive: true, force: true })));
});

async function releaseDirectory(): Promise<string> {
  const root = await mkdtemp(join(tmpdir(), "firekeep-studio-release-test-"));
  temporaryDirectories.push(root);
  await mkdir(root, { recursive: true });
  const files: Record<string, string> = {
    "Firekeep-Studio-0.4.1-Setup.exe": "windows installer",
    "Firekeep-Studio-0.4.1-Setup.exe.blockmap": "windows blockmap",
    "Firekeep-Studio-0.4.1-universal.dmg": "mac installer",
    "Firekeep-Studio-0.4.1-universal.zip": "mac updater",
    "Firekeep-Studio-0.4.1-universal.zip.blockmap": "mac blockmap",
    "latest.yml": "version: 0.4.1\nfiles:\n  - url: Firekeep-Studio-0.4.1-Setup.exe\n    sha512: test\npath: Firekeep-Studio-0.4.1-Setup.exe\n",
    "latest-mac.yml": "version: 0.4.1\nfiles:\n  - url: Firekeep-Studio-0.4.1-universal.zip\n    sha512: test\n  - url: Firekeep-Studio-0.4.1-universal.dmg\n    sha512: test\npath: Firekeep-Studio-0.4.1-universal.zip\n",
  };
  await Promise.all(Object.entries(files).map(([name, contents]) => writeFile(join(root, name), contents)));
  return root;
}

describe("Studio release manifest builder", () => {
  it("creates a deterministic platform manifest and isolates metadata on the immutable release", async () => {
    const directory = await releaseDirectory();
    const baseUrl = "https://github.com/kapella-hub/firekeep-dist/releases/download/studio-v0.4.1";
    const result = spawnSync(process.execPath, [
      "scripts/make-update-manifest.mjs",
      "--version", "0.4.1",
      "--directory", directory,
      "--base-url", baseUrl,
      "--published-at", "2026-08-26T18:00:00Z",
      "--mac-automatic", "false",
    ], { cwd: process.cwd(), encoding: "utf8" });

    expect(result.status, result.stderr).toBe(0);
    const manifest = JSON.parse(await readFile(join(directory, "studio-update.json"), "utf8"));
    expect(manifest).toMatchObject({
      schema: 1,
      channel: "stable",
      version: "0.4.1",
      publishedAt: "2026-08-26T18:00:00Z",
      platforms: {
        win32: { automatic: true, installer: { fileName: "Firekeep-Studio-0.4.1-Setup.exe", url: `${baseUrl}/Firekeep-Studio-0.4.1-Setup.exe` } },
        darwin: { automatic: false, installer: { fileName: "Firekeep-Studio-0.4.1-universal.dmg" }, updater: { fileName: "Firekeep-Studio-0.4.1-universal.zip" } },
      },
    });
    expect(manifest.platforms.win32.installer.sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(await readFile(join(directory, "latest.yml"), "utf8")).toContain(`url: ${baseUrl}/Firekeep-Studio-0.4.1-Setup.exe`);
    expect(await readFile(join(directory, "latest-mac.yml"), "utf8")).toContain(`path: ${baseUrl}/Firekeep-Studio-0.4.1-universal.zip`);
  });

  it("fails before writing a manifest when a required installer is missing", async () => {
    const directory = await releaseDirectory();
    await rm(join(directory, "Firekeep-Studio-0.4.1-universal.dmg"));
    const result = spawnSync(process.execPath, [
      "scripts/make-update-manifest.mjs",
      "--version", "0.4.1",
      "--directory", directory,
      "--base-url", "https://github.com/kapella-hub/firekeep-dist/releases/download/studio-v0.4.1",
      "--published-at", "2026-08-26T18:00:00Z",
      "--mac-automatic", "false",
    ], { cwd: process.cwd(), encoding: "utf8" });

    expect(result.status).not.toBe(0);
    expect(result.stderr).toMatch(/required release artifact is missing/i);
  });
});
