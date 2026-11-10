import type { MissionPhase, MissionSnapshot } from "./mission.js";
import type { RuntimeEvent } from "./runtime.js";

/**
 * What deserves the human's attention while Studio is in the background.
 *
 * Pure decisions only: the main process turns a notice into an OS notification, the
 * renderer lists the same moments in the top-bar inbox. Nothing here reads a clock,
 * a window or a setting.
 */
export type AttentionKind = "approval" | "decision" | "mission" | "run";

export interface AttentionNotice {
  /** Stable per moment, so the same approval or phase never notifies twice. */
  readonly key: string;
  readonly kind: AttentionKind;
  readonly title: string;
  readonly body: string;
}

const BODY_LIMIT = 180;

function clip(text: string, limit = BODY_LIMIT): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > limit ? `${flat.slice(0, limit - 1).trimEnd()}…` : flat;
}

/** Mission phases in which Studio is stopped until the human acts. */
export function isMissionWaiting(phase: MissionPhase | null | undefined): boolean {
  return phase === "awaiting-approval" || phase === "paused";
}

function isMissionWorking(phase: MissionPhase | null | undefined): boolean {
  return phase === "running" || phase === "verifying" || phase === "repairing" || phase === "reviewing";
}

/**
 * A notice for one runtime event, or null. `missionPhase` is the phase of the session's
 * mission when the event arrived: a mission is many runs, so the end of one of them is
 * not news; the mission's own phase change is (see `attentionForMission`).
 */
export function attentionForEvent(event: RuntimeEvent, missionPhase: MissionPhase | null = null): AttentionNotice | null {
  const payload = event.payload;
  if (payload.kind === "approval.requested") {
    return {
      key: `approval:${payload.approvalId}`,
      kind: "approval",
      title: "An agent needs your approval",
      body: clip(payload.title || payload.detail || "Open Studio to answer."),
    };
  }
  if (isMissionWorking(missionPhase) || isMissionWaiting(missionPhase)) return null;
  if (payload.kind === "run.completed") {
    return { key: `run:${event.runId}`, kind: "run", title: "The agent finished", body: "The turn is complete and ready to read." };
  }
  if (payload.kind === "run.failed" && !payload.cancelled) {
    return { key: `run:${event.runId}`, kind: "run", title: "The run failed", body: clip(payload.error || "Open Studio to see what happened.") };
  }
  return null;
}

/** A notice when a mission moves into a phase that waits on the human, or ends. */
export function attentionForMission(previous: MissionPhase | null, mission: MissionSnapshot | null): AttentionNotice | null {
  if (!mission || mission.phase === previous) return null;
  // A mission first seen (restored from disk, or a session switch) is already where it was;
  // only a transition observed live is news.
  if (previous === null) return null;
  const goal = clip(mission.goal, 120);
  const key = `mission:${mission.id}:${mission.phase}:${mission.attempt}`;
  if (mission.phase === "awaiting-approval") return { key, kind: "mission", title: "Mission is waiting for your sign-off", body: goal };
  if (mission.phase === "paused") return { key, kind: "mission", title: "Mission paused", body: clip(mission.blockReason ? `${mission.blockReason} · ${mission.goal}` : mission.goal) };
  if (mission.phase === "succeeded") return { key, kind: "mission", title: "Mission succeeded", body: goal };
  if (mission.phase === "partial") return { key, kind: "mission", title: "Mission finished with partial results", body: goal };
  if (mission.phase === "failed") return { key, kind: "mission", title: "Mission failed", body: goal };
  return null;
}

/** A notice for a Decision Board an agent just opened. */
export function attentionForDecision(board: { readonly url: string; readonly spec: { readonly context?: string; readonly questions: readonly unknown[] } }): AttentionNotice {
  const count = board.spec.questions.length;
  const questions = `${count} question${count === 1 ? "" : "s"}`;
  return {
    key: `decision:${board.url}`,
    kind: "decision",
    title: "An agent has questions for you",
    body: clip(board.spec.context ? `${questions} · ${board.spec.context}` : `${questions} on a Decision Board.`),
  };
}
