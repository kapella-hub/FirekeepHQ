import type { CSSProperties } from "react";
import { DEFAULT_SESSION_COLOR, type SessionColor } from "../../../core/session-store";

export const SESSION_COLOR_OPTIONS: readonly { readonly id: SessionColor; readonly label: string; readonly value: string }[] = [
  { id: "ember", label: "Ember", value: "#ff7a2f" },
  { id: "gold", label: "Gold", value: "#dca63a" },
  { id: "moss", label: "Moss", value: "#65a86f" },
  { id: "teal", label: "Teal", value: "#3da5a0" },
  { id: "ocean", label: "Ocean", value: "#4b8fdf" },
  { id: "violet", label: "Violet", value: "#8f83ff" },
  { id: "rose", label: "Rose", value: "#d66d91" },
  { id: "slate", label: "Slate", value: "#7f8997" },
];

export function relativeTime(value: string): string {
  const delta = Date.now() - new Date(value).getTime();
  if (!Number.isFinite(delta) || delta < 60_000) return "now";
  if (delta < 3_600_000) return `${Math.floor(delta / 60_000)}m`;
  if (delta < 86_400_000) return `${Math.floor(delta / 3_600_000)}h`;
  return `${Math.floor(delta / 86_400_000)}d`;
}

export function sessionAccentStyle(color: SessionColor = DEFAULT_SESSION_COLOR): CSSProperties {
  return { "--session-accent": SESSION_COLOR_OPTIONS.find((option) => option.id === color)?.value ?? SESSION_COLOR_OPTIONS[0]!.value } as CSSProperties;
}
