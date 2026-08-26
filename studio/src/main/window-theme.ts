import type { ThemeMode } from "../core/studio-service.js";

export interface WindowThemeColors {
  readonly background: string;
  readonly overlay: string;
  readonly symbols: string;
}

export function windowThemeColors(theme: ThemeMode, systemDark: boolean): WindowThemeColors {
  const dark = theme === "dark" || (theme === "system" && systemDark);
  return dark
    ? { background: "#0b0f0c", overlay: "#111612", symbols: "#dce6de" }
    : { background: "#f1f3ef", overlay: "#fafbf8", symbols: "#243027" };
}
