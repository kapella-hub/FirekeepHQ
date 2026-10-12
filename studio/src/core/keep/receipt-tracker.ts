import type { RuntimeEventPayload } from "../runtime.js";
import { measuredReceipt, partialReceipt, unavailableReceipt, type KeepReceipt, type ReplayEvent, type TimelinePage } from "./receipt.js";

export interface KeepTimelineSource {
  timeline(keepSessionId: string, offset: number, limit: number): Promise<TimelinePage>;
}

export interface ReceiptTrackerDeps {
  /** The live gateway, or null while the Keep is unavailable. */
  readonly source: () => KeepTimelineSource | null;
  /** Keep session ids the agents reported for this Studio session (kit 1.8 report file). */
  readonly readReport: (studioSessionId: string) => Promise<readonly string[]>;
}

export const PAGE_LIMIT = 200;
export const MAX_PAGES = 10;
const SESSION_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

/**
 * Turns Keep replay timelines into per-turn receipts (spec §7.3). Cursors are
 * event counts per Keep session, i.e. server ordering — Studio never compares
 * its clock with the Keep's. A provisional collect (turn just ended) never
 * commits; the final collect (made just before the next prompt leaves)
 * commits, so late writes land on the turn that caused them.
 */
export class ReceiptTracker {
  readonly #deps: ReceiptTrackerDeps;
  readonly #cursors = new Map<string, Record<string, number>>();

  constructor(deps: ReceiptTrackerDeps) {
    this.#deps = deps;
  }

  restore(studioSessionId: string, cursors: Readonly<Record<string, number>>): void {
    this.#cursors.set(studioSessionId, { ...validated(cursors) });
  }

  commit(studioSessionId: string, cursors: Readonly<Record<string, number>>): void {
    const existing = this.#cursors.get(studioSessionId) ?? {};
    const incoming = validated(cursors);
    const merged = { ...existing };
    for (const [id, newOffset] of Object.entries(incoming)) {
      const existingOffset = existing[id] ?? 0;
      merged[id] = Math.max(existingOffset, newOffset);
    }
    this.#cursors.set(studioSessionId, merged);
  }

  cursors(studioSessionId: string): Readonly<Record<string, number>> {
    return { ...(this.#cursors.get(studioSessionId) ?? {}) };
  }

  forget(studioSessionId: string): void {
    this.#cursors.delete(studioSessionId);
  }

  async collect(studioSessionId: string, runEvents: readonly RuntimeEventPayload[], final: boolean): Promise<KeepReceipt> {
    const committed = this.#cursors.get(studioSessionId) ?? {};
    const ids = unique((await this.#deps.readReport(studioSessionId).catch(() => [])).filter((id) => SESSION_ID.test(id)));
    let source;
    try { source = this.#deps.source(); }
    catch { return partialReceipt(runEvents, final); }
    if (ids.length === 0 || !source) return partialReceipt(runEvents, final);

    // Every reported Keep session is read in parallel; each one pages sequentially from its own cursor.
    const results = await Promise.all(ids.map((id) => readSession(source, id, committed[id] ?? 0)));
    const events: ReplayEvent[] = [];
    const after: Record<string, number> = { ...committed };
    let hitMaxPages = false;
    // Results are walked in report order, so the first failing id decides the outcome exactly as a
    // sequential read would have, and the events keep report order.
    for (const [index, result] of results.entries()) {
      if (result.kind === "unsupported") return partialReceipt(runEvents, final);
      if (result.kind === "error") return unavailableReceipt(result.message, final, committed);
      events.push(...result.events);
      after[ids[index]!] = result.offset;
      hitMaxPages ||= result.hitMaxPages;
    }
    // Merge, never set: a final collect Studio stopped waiting for can resolve after a later turn committed further.
    if (final) this.commit(studioSessionId, after);
    const receipt = measuredReceipt(events, ids, after, final);
    if (hitMaxPages) return { ...receipt, detail: "more Keep activity than one receipt shows" };
    return receipt;
  }
}

type SessionRead =
  | { readonly kind: "page"; readonly events: readonly ReplayEvent[]; readonly offset: number; readonly hitMaxPages: boolean }
  | { readonly kind: "unsupported" }
  | { readonly kind: "error"; readonly message: string };

/** Pages one Keep session's timeline from `offset`, bounded by MAX_PAGES. Never rejects. */
async function readSession(source: KeepTimelineSource, id: string, start: number): Promise<SessionRead> {
  const events: ReplayEvent[] = [];
  let offset = start;
  let hitMaxPages = false;
  for (let page = 0; page < MAX_PAGES; page += 1) {
    let reply: TimelinePage;
    try { reply = await source.timeline(id, offset, PAGE_LIMIT); }
    catch (error) { return { kind: "error", message: message(error) }; }
    if (reply.kind === "unsupported") return { kind: "unsupported" };
    if (reply.kind === "error") return { kind: "error", message: reply.message };
    events.push(...reply.events);
    offset += reply.events.length;
    if (!reply.hasMore || reply.events.length === 0) break;
    if (page === MAX_PAGES - 1) hitMaxPages = true;
  }
  return { kind: "page", events, offset, hitMaxPages };
}

function unique(values: readonly string[]): string[] {
  return [...new Set(values)];
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function validated(cursors: Readonly<Record<string, number>>): Record<string, number> {
  const result: Record<string, number> = {};
  for (const [id, value] of Object.entries(cursors)) {
    if (typeof value === "number" && Number.isInteger(value) && value >= 0) {
      result[id] = value;
    }
  }
  return result;
}
