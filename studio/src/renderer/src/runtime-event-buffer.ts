import type { RuntimeEvent } from "../../core/runtime.js";

/** Compact display-only event history so token streams do not grow one React row per delta. */
export function coalesceRuntimeEvents(current: readonly RuntimeEvent[], incoming: readonly RuntimeEvent[]): RuntimeEvent[] {
  const next = [...current];
  const seen = new Set(current.map((event) => event.id));
  const positions = new Map<string, number>();
  next.forEach((event, index) => {
    const key = coalesceKey(event);
    if (key) positions.set(key, index);
  });

  for (const event of incoming) {
    if (seen.has(event.id)) continue;
    seen.add(event.id);
    const key = coalesceKey(event);
    const position = key ? positions.get(key) : undefined;
    const existing = position === undefined ? undefined : next[position];
    const merged = existing ? merge(existing, event) : null;
    if (position !== undefined && merged) next[position] = merged;
    else {
      if (position !== undefined && isLateMessageDelta(existing, event)) continue;
      if (key) positions.set(key, next.length);
      next.push(event);
    }
  }
  return next;
}

function coalesceKey(event: RuntimeEvent): string | null {
  const payload = event.payload;
  if (payload.kind === "message.delta" || payload.kind === "message.completed") return `message:${event.runId}:${payload.messageId}:${payload.role}`;
  if (payload.kind === "reasoning.delta") return `reasoning:${event.runId}:${payload.itemId}`;
  if (payload.kind === "tool.updated") return `tool-update:${event.runId}:${payload.toolCallId}`;
  if (payload.kind === "diff.updated") return `diff:${event.runId}:${payload.itemId}`;
  if (payload.kind === "usage.updated") return `usage:${event.runId}`;
  return null;
}

function merge(existing: RuntimeEvent, incoming: RuntimeEvent): RuntimeEvent | null {
  const previous = existing.payload;
  const next = incoming.payload;
  if (next.kind === "message.completed" && (previous.kind === "message.delta" || previous.kind === "message.completed")) return { ...incoming, id: existing.id };
  if (next.kind === "message.delta" && previous.kind === "message.delta") {
    return { ...incoming, id: existing.id, payload: { ...next, text: previous.text + next.text } };
  }
  if (next.kind === "reasoning.delta" && previous.kind === "reasoning.delta") {
    return { ...incoming, id: existing.id, payload: { ...next, text: previous.text + next.text } };
  }
  if (next.kind === "tool.updated" && previous.kind === "tool.updated") return { ...incoming, id: existing.id };
  if (next.kind === "diff.updated" && previous.kind === "diff.updated") return { ...incoming, id: existing.id };
  if (next.kind === "usage.updated" && previous.kind === "usage.updated") return { ...incoming, id: existing.id };
  return null;
}

function isLateMessageDelta(existing: RuntimeEvent | undefined, incoming: RuntimeEvent): boolean {
  return existing?.payload.kind === "message.completed" && incoming.payload.kind === "message.delta";
}
