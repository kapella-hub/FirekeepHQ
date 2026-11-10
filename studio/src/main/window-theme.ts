import type { ThemeMode } from "../core/studio-service.js";

export interface WindowThemeColors {
  readonly background: string;
  readonly overlay: string;
  readonly symbols: string;
}

export function windowThemeColors(theme: ThemeMode, systemDark: boolean): WindowThemeColors {
  const dark = theme === "dark" || (theme === "system" && systemDark);
  return dark
    ? { background: "#0b0a10", overlay: "#110f17", symbols: "#e6dfd3" }
    : { background: "#f9f5ed", overlay: "#fdfbf6", symbols: "#2a2c3a" };
}
