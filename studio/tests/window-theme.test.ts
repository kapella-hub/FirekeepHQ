import { describe, expect, it } from "vitest";
import { windowThemeColors } from "../src/main/window-theme.js";

describe("Studio window theme", () => {
  it("keeps the native title bar in step with explicit and system appearance", () => {
    expect(windowThemeColors("dark", false)).toEqual({ background: "#0b0f0c", overlay: "#111612", symbols: "#dce6de" });
    expect(windowThemeColors("light", true)).toEqual({ background: "#f1f3ef", overlay: "#fafbf8", symbols: "#243027" });
    expect(windowThemeColors("system", false)).toEqual({ background: "#f1f3ef", overlay: "#fafbf8", symbols: "#243027" });
  });
});
