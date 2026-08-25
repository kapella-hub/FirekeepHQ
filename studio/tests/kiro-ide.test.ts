import { describe, expect, it, vi } from "vitest";
import { KiroIdeLauncher } from "../src/main/kiro-ide.js";

describe("KiroIdeLauncher", () => {
  it("prefers an explicit executable and opens only an existing workspace without a shell", async () => {
    const launch = vi.fn();
    const launcher = new KiroIdeLauncher({
      platform: "win32",
      env: { KIRO_IDE_PATH: "C:\\Kiro\\Kiro.exe", PATH: "" },
      exists: (path) => path === "C:\\Kiro\\Kiro.exe" || path === "C:\\work",
      cwd: () => "C:\\work",
      launch,
    });

    await expect(launcher.probe()).resolves.toMatchObject({ available: true, executable: "C:\\Kiro\\Kiro.exe" });
    await expect(launcher.open()).resolves.toMatchObject({ message: expect.stringContaining("explicit external handoff") });
    expect(launch).toHaveBeenCalledWith("C:\\Kiro\\Kiro.exe", ["C:\\work"]);
    await expect(launcher.open("C:\\missing")).rejects.toThrow(/workspace does not exist/i);
  });

  it("keeps Kiro CLI usable when the optional IDE is absent", async () => {
    const launcher = new KiroIdeLauncher({ platform: "linux", env: { PATH: "" }, exists: () => false });
    await expect(launcher.probe()).resolves.toMatchObject({ available: false, detail: expect.stringContaining("Kiro CLI remains") });
    await expect(launcher.open()).rejects.toThrow(/not found/i);
  });
});
