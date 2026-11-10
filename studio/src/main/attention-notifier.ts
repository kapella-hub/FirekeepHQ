import { attentionForDecision, attentionForEvent, attentionForMission, type AttentionNotice } from "../core/attention.js";
import type { MissionPhase } from "../core/mission.js";
import type { RuntimeEvent } from "../core/runtime.js";
import type { StudioSnapshot } from "../core/studio-service.js";
import type { DecisionBoardDocument } from "../shared/decision-board.js";

export interface AttentionNotifierHost {
  /** The persisted "Notify me" setting. */
  readonly enabled: () => boolean;
  /** True while a Studio window has focus: the human is already looking. */
  readonly focused: () => boolean;
  /** Show one OS notification. */
  readonly show: (notice: AttentionNotice) => void;
  /** Number of notices raised since Studio last had focus (dock badge, taskbar flash). */
  readonly setPending: (count: number) => void;
}

const REMEMBERED_KEYS = 500;

/**
 * Raises an OS notification when an agent stops to wait on the human, or finishes, while
 * Studio is not the focused window. Every decision about WHAT is worth a notice lives in
 * `core/attention.ts`; this class only tracks what was already said and whether anyone is looking.
 */
export class AttentionNotifier {
  readonly #host: AttentionNotifierHost;
  readonly #seen = new Set<string>();
  #mission: { readonly id: string; readonly phase: MissionPhase } | null = null;
  #pending = 0;

  constructor(host: AttentionNotifierHost) {
    this.#host = host;
  }

  event(event: RuntimeEvent): void {
    this.#raise(attentionForEvent(event, this.#mission?.phase ?? null));
  }

  snapshot(snapshot: StudioSnapshot): void {
    const mission = snapshot.mission;
    const previous = mission && this.#mission?.id === mission.id ? this.#mission.phase : null;
    this.#mission = mission ? { id: mission.id, phase: mission.phase } : null;
    this.#raise(attentionForMission(previous, mission));
  }

  decision(board: DecisionBoardDocument): void {
    this.#raise(attentionForDecision(board));
  }

  /** Studio gained focus: whatever was pending has been seen. */
  focus(): void {
    if (this.#pending === 0) return;
    this.#pending = 0;
    this.#host.setPending(0);
  }

  #raise(notice: AttentionNotice | null): void {
    if (!notice || this.#seen.has(notice.key)) return;
    this.#seen.add(notice.key);
    if (this.#seen.size > REMEMBERED_KEYS) this.#seen.delete(this.#seen.values().next().value as string);
    if (!this.#host.enabled() || this.#host.focused()) return;
    this.#pending += 1;
    this.#host.setPending(this.#pending);
    this.#host.show(notice);
  }
}
