import type { RuntimeEventPayload } from "../runtime.js";

/** Agent label of Studio's own Keep-panel gateway (spec §7.2). */
export const PANEL_AGENT_SUFFIX = "-studio-panel";

export interface ReplayEvent {
  readonly id: string;
  readonly agent_id: string;
  readonly event_type: string;
  readonly timestamp: string;
  readonly payload: Readonly<Record<string, unknown>>;
}

export type TimelinePage =
  | { readonly kind: "page"; readonly events: readonly ReplayEvent[]; readonly total: number; readonly hasMore: boolean }
  | { readonly kind: "unsupported" }
  | { readonly kind: "error"; readonly message: string };

export interface ReceiptRecall {
  readonly query: string;
  readonly trigger: "prompt-hook" | "agent";
  readonly resultCount: number;
  readonly memoryIds: readonly string[];
}

export interface ReceiptWrite {
  readonly summary: string;
  readonly memoryId: string;
}

export interface KeepReceipt {
  readonly status: "measured" | "partial" | "unavailable";
  readonly final: boolean;
  readonly recalls: readonly ReceiptRecall[];
  readonly writes: readonly ReceiptWrite[];
  readonly recalledCount: number;
  readonly learnedCount: number;
  readonly keepSessionIds: readonly string[];
  readonly cursorsAfter: Readonly<Record<string, number>>;
  readonly detail?: string;
}

const PARTIAL_NOTE = "partial — hook-injected recall not shown";

/**
 * replay_timeline(format="json") returns the REST TimelineResponse; an older
 * server returns Markdown (or rejects the unknown argument). Anything that is
 * not JSON with an `events` array or an `error` string is an old server.
 */
export function parseTimelineReply(text: string): TimelinePage {
  let value: unknown;
  try { value = JSON.parse(text); } catch { return { kind: "unsupported" }; }
  if (!value || typeof value !== "object") return { kind: "unsupported" };
  const record = value as Record<string, unknown>;
  if (typeof record.error === "string") return { kind: "error", message: record.error };
  if (!Array.isArray(record.events)) return { kind: "unsupported" };
  const events = record.events.filter(isReplayEvent);
  return { kind: "page", events, total: typeof record.total === "number" ? record.total : events.length, hasMore: record.has_more === true };
}

export function measuredReceipt(events: readonly ReplayEvent[], keepSessionIds: readonly string[], cursorsAfter: Readonly<Record<string, number>>, final: boolean): KeepReceipt {
  const own = events.filter((event) => !event.agent_id.endsWith(PANEL_AGENT_SUFFIX));
  const recalls: ReceiptRecall[] = own.filter((event) => event.event_type === "memory_read").map((event) => ({
    query: stringField(event.payload.query),
    trigger: event.payload.trigger === "prompt-hook" ? "prompt-hook" : "agent",
    resultCount: typeof event.payload.result_count === "number" ? event.payload.result_count : 0,
    memoryIds: Array.isArray(event.payload.memory_ids) ? event.payload.memory_ids.filter((id): id is string => typeof id === "string") : [],
  }));
  const writes: ReceiptWrite[] = own.filter((event) => event.event_type === "memory_write").map((event) => ({
    summary: stringField(event.payload.action_summary),
    memoryId: stringField(event.payload.memory_id),
  }));
  const distinct = new Set(recalls.flatMap((recall) => recall.memoryIds));
  const countFromIdless = recalls.filter((recall) => recall.memoryIds.length === 0).reduce((sum, recall) => sum + recall.resultCount, 0);
  return { status: "measured", final, recalls, writes, recalledCount: distinct.size + countFromIdless, learnedCount: writes.length, keepSessionIds: [...keepSessionIds], cursorsAfter: { ...cursorsAfter } };
}

/** Fallback: Firekeep tool calls visible in the runtime stream (spec §7.3). */
export function partialReceipt(runEvents: readonly RuntimeEventPayload[], final: boolean): KeepReceipt {
  const calls = runEvents.filter((event): event is Extract<RuntimeEventPayload, { kind: "tool.started" }> => event.kind === "tool.started");
  const recalls: ReceiptRecall[] = calls.filter((call) => toolBase(call.name) === "memory_recall").map((call) => ({
    query: stringField((call.input as Record<string, unknown> | undefined)?.task), trigger: "agent", resultCount: 0, memoryIds: [],
  }));
  const writes: ReceiptWrite[] = calls.filter((call) => toolBase(call.name) === "memory_learn").map((call) => ({
    summary: stringField((call.input as Record<string, unknown> | undefined)?.action), memoryId: "",
  }));
  return { status: "partial", final, recalls, writes, recalledCount: recalls.length, learnedCount: writes.length, keepSessionIds: [], cursorsAfter: {}, detail: PARTIAL_NOTE };
}

export function unavailableReceipt(message: string, final: boolean, cursors: Readonly<Record<string, number>>): KeepReceipt {
  return { status: "unavailable", final, recalls: [], writes: [], recalledCount: 0, learnedCount: 0, keepSessionIds: [], cursorsAfter: { ...cursors }, detail: message };
}

export function receiptLabel(receipt: KeepReceipt): string {
  if (receipt.status === "unavailable") return "Keep receipt unavailable · retry";
  const counts = receipt.recalledCount || receipt.learnedCount
    ? `recalled ${receipt.recalledCount} · learned ${receipt.learnedCount}`
    : "";
  if (receipt.status === "partial") return counts ? `Keep · ${counts} · ${PARTIAL_NOTE}` : `Keep · ${PARTIAL_NOTE}`;
  return counts ? `Keep · ${counts}` : "Keep · nothing recalled";
}

function toolBase(name: string): string {
  // Claude: mcp__firekeep__memory_recall; Codex/Kiro may report the bare name or server.tool.
  return name.split(/__|\./).pop() ?? name;
}

function stringField(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function isReplayEvent(value: unknown): value is ReplayEvent {
  if (!value || typeof value !== "object") return false;
  const event = value as Record<string, unknown>;
  return typeof event.id === "string" && typeof event.agent_id === "string" && typeof event.event_type === "string"
    && typeof event.payload === "object" && event.payload !== null;
}
