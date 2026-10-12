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
    this.#cursors.set(studioSessionId, { ...cursors });
  }

  commit(studioSessionId: string, cursors: Readonly<Record<string, number>>): void {
    this.#cursors.set(studioSessionId, { ...cursors });
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
    const source = this.#deps.source();
    if (ids.length === 0 || !source) return partialReceipt(runEvents, final);

    const events: ReplayEvent[] = [];
    const after: Record<string, number> = { ...committed };
    for (const id of ids) {
      let offset = committed[id] ?? 0;
      for (let page = 0; page < MAX_PAGES; page += 1) {
        let reply: TimelinePage;
        try { reply = await source.timeline(id, offset, PAGE_LIMIT); }
        catch (error) { return unavailableReceipt(message(error), final, committed); }
        if (reply.kind === "unsupported") return partialReceipt(runEvents, final);
        if (reply.kind === "error") return unavailableReceipt(reply.message, final, committed);
        events.push(...reply.events);
        offset += reply.events.length;
        if (!reply.hasMore || reply.events.length === 0) break;
      }
      after[id] = offset;
    }
    if (final) this.#cursors.set(studioSessionId, after);
    return measuredReceipt(events, ids, after, final);
  }
}

function unique(values: readonly string[]): string[] {
  return [...new Set(values)];
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
