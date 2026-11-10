import { isMissionWaiting } from "../../core/attention.js";
import type { MissionSnapshot } from "../../core/mission.js";
import type { TimelineItem } from "./timeline.js";

/** A Decision Board Studio has loaded and the human has not submitted yet. */
export interface PendingBoard {
  readonly url: string;
  readonly questions: number;
  readonly context?: string;
}

export type InboxItem =
  | { readonly id: string; readonly kind: "approval"; readonly title: string; readonly detail: string; readonly approvalId: string }
  | { readonly id: string; readonly kind: "decision"; readonly title: string; readonly detail: string; readonly url: string }
  | { readonly id: string; readonly kind: "mission"; readonly title: string; readonly detail: string }
  /** Something waits in a session that is not on screen; opening it switches to that session. */
  | { readonly id: string; readonly kind: "session"; readonly title: string; readonly detail: string; readonly sessionId: string };

/** What the inbox needs to know about the sessions that are not in the foreground. */
export interface OtherSession {
  readonly sessionId: string;
  readonly name: string;
  readonly approvals: readonly { readonly approvalId: string; readonly title: string }[];
  readonly missionPhase: MissionSnapshot["phase"] | null;
  readonly missionGoal: string | null;
}

/**
 * Everything in the open session that is stopped until the human acts, most urgent first:
 * approvals block a live run, boards block an agent's next step, a mission waits for sign-off.
 */
export function buildInbox(timeline: readonly TimelineItem[], activeRunId: string | null, mission: MissionSnapshot | null, boards: readonly PendingBoard[], others: readonly OtherSession[] = []): InboxItem[] {
  const items: InboxItem[] = [];
  for (const item of timeline) {
    // An unanswered approval from a run that already ended can no longer be answered.
    if (item.kind !== "approval" || item.decision || item.runId !== activeRunId) continue;
    items.push({ id: `approval:${item.approvalId}`, kind: "approval", title: item.title || "Permission requested", detail: firstLine(item.detail) || "An agent is waiting for permission.", approvalId: item.approvalId });
  }
  for (const board of boards) {
    const count = `${board.questions} question${board.questions === 1 ? "" : "s"}`;
    items.push({ id: `decision:${board.url}`, kind: "decision", title: "Decision Board", detail: board.context ? `${count} · ${firstLine(board.context)}` : count, url: board.url });
  }
  if (mission && isMissionWaiting(mission.phase)) {
    items.push({
      id: `mission:${mission.id}:${mission.phase}`,
      kind: "mission",
      title: mission.phase === "paused" ? "Mission paused" : "Mission waiting for sign-off",
      detail: mission.phase === "paused" && mission.blockReason ? firstLine(mission.blockReason) : firstLine(mission.goal),
    });
  }
  for (const other of others) {
    for (const approval of other.approvals) {
      items.push({ id: `session:${other.sessionId}:approval:${approval.approvalId}`, kind: "session", title: `Approval needed in ${other.name}`, detail: firstLine(approval.title) || "An agent is waiting for permission.", sessionId: other.sessionId });
    }
    if (isMissionWaiting(other.missionPhase)) {
      items.push({ id: `session:${other.sessionId}:mission:${other.missionPhase}`, kind: "session", title: other.missionPhase === "paused" ? `Mission paused in ${other.name}` : `Mission waiting for sign-off in ${other.name}`, detail: firstLine(other.missionGoal ?? ""), sessionId: other.sessionId });
    }
  }
  return items;
}

function firstLine(text: string): string {
  const line = text.split(/\r?\n/).find((entry) => entry.trim().length > 0)?.trim() ?? "";
  return line.length > 140 ? `${line.slice(0, 139).trimEnd()}…` : line;
}
