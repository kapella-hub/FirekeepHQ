import { readFile } from "node:fs/promises";
import { describe, expect, it } from "vitest";
import { PINNED_FIREKEEP_RELEASE_KEY } from "../src/main/release-signing.js";
import { STUDIO_UPDATE_CHANNEL_URL } from "../src/main/studio-updater.js";

describe("Studio release workflow", () => {
  it("builds Windows and universal macOS update targets on an isolated channel", async () => {
    const packageJson = JSON.parse(await readFile("package.json", "utf8"));
    const workflow = await readFile("../.github/workflows/studio-release.yml", "utf8");

    expect(packageJson.version).toBe("0.4.0");
    expect(packageJson.build.mac.target).toEqual(["dmg", "zip"]);
    expect(packageJson.build.publish).toEqual([{ provider: "generic", url: "https://github.com/kapella-hub/firekeep-dist/releases/download/studio-latest", channel: "latest" }]);
    expect(STUDIO_UPDATE_CHANNEL_URL).toBe(packageJson.build.publish[0].url);
    expect(workflow).toContain('tags:\n      - "studio-v*"');
    expect(workflow).toContain("npm run dist -- --win --x64 --publish never");
    expect(workflow).toContain("npm run dist -- --mac --universal --publish never");
    expect(workflow.match(/npm run smoke:package/g)).toHaveLength(2);
    expect(workflow.match(/timeout-minutes: 2/g)).toHaveLength(2);
    expect(workflow).toContain("studio/scripts/sign_update_manifest.py");
    expect(workflow).toContain("FIREKEEP_SIGNING_KEY");
    expect(workflow).toContain("studio-latest");
    expect(workflow).toContain("--clobber");
    expect(workflow).toContain("immutable Studio release asset differs");
    expect(workflow).toContain("public Studio channel does not serve");
  });

  it("pins the same release verification key as the Python Client Kit", async () => {
    const signing = await readFile("../client/firekeep_client/signing.py", "utf8");
    const match = /^PINNED_PUBLIC_KEY\s*=\s*"([^"]+)"/m.exec(signing);

    expect(match?.[1]).toBe(PINNED_FIREKEEP_RELEASE_KEY);
  });

  it("loads electron-updater only after Electron is ready", async () => {
    const client = await readFile("src/main/electron-update-client.ts", "utf8");

    expect(client).toContain('import type { AppUpdater, ProgressInfo, UpdateDownloadedEvent } from "electron-updater"');
    expect(client).toContain('createRequire(import.meta.url)');
    expect(client).toContain('require("electron-updater")');
    expect(client).not.toMatch(/^import\s*\{[^}]*autoUpdater[^}]*\}\s*from\s*["']electron-updater["']/m);
  });

  it("keeps every packaged-debug poll inside the smoke deadline", async () => {
    const smoke = await readFile("scripts/package-smoke.mjs", "utf8");

    expect(smoke).toContain("deadline - Date.now()");
    expect(smoke).toContain("AbortSignal.timeout(attemptTimeout)");
    expect(smoke).toContain("DevTools command timed out after");
    expect(smoke).toContain('socket.addEventListener("close", () => rejectPending');
    expect(smoke).toContain('process.kill(-pid, "SIGKILL")');
    expect(smoke).toContain("childProcess.stdout?.destroy()");
    expect(smoke).toContain("childProcess.unref()");
  });
});
