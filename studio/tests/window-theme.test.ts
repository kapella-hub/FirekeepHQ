import { describe, expect, it } from "vitest";
import { windowThemeColors } from "../src/main/window-theme.js";

describe("Studio window theme", () => {
  it("keeps the native title bar in step with explicit and system appearance", () => {
    expect(windowThemeColors("dark", false)).toEqual({ background: "#0b0a10", overlay: "#110f17", symbols: "#e6dfd3" });
    expect(windowThemeColors("light", true)).toEqual({ background: "#f9f5ed", overlay: "#fdfbf6", symbols: "#2a2c3a" });
    expect(windowThemeColors("system", false)).toEqual({ background: "#f9f5ed", overlay: "#fdfbf6", symbols: "#2a2c3a" });
  });
});
