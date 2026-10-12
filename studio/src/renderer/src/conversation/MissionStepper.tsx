import type { MissionSnapshot } from "../../../core/mission.js";
import type { StudioAction } from "../../../shared/ipc.js";

export interface MissionStepperProps {
  readonly mission: MissionSnapshot;
  readonly workspacePath: string | null;
  readonly primaryRuntimeId: string | null;
  readonly busy: boolean;
  readonly onAction: (action: StudioAction) => void;
  readonly onCommand: (value: string) => void;
}

type StepState = "done" | "current" | "todo";

/** Write -> Check -> Repair n/N -> Review -> outcome, derived from the mission phase. */
function stepIndex(phase: MissionSnapshot["phase"]): number {
  switch (phase) {
    case "draft": return -1;
    case "running": return 0;
    case "verifying": return 1;
    case "repairing": return 2;
    case "reviewing": return 3;
    case "paused": return 0;
    default: return 4; // awaiting-approval and terminal phases
  }
}

export function MissionStepper({ mission, workspacePath, primaryRuntimeId, busy, onAction, onCommand }: MissionStepperProps): React.JSX.Element {
  const current = stepIndex(mission.phase);
  const terminal = ["succeeded", "partial", "failed", "cancelled"].includes(mission.phase);
  const repairsUsed = Math.min(Math.max(mission.attempt - 1, 0), mission.maxRepairAttempts);
  const outcomeLabel = terminal ? mission.phase : mission.phase === "awaiting-approval" ? "Awaiting you" : "Outcome";
  const steps = ["Write", "Check", `Repair ${repairsUsed}/${mission.maxRepairAttempts}`, "Review", outcomeLabel];
  const stateOf = (index: number): StepState => terminal && index === 4 ? "done" : index < current ? "done" : index === current ? "current" : "todo";

  const effectivePrimary = mission.primaryRuntimeId ?? primaryRuntimeId;
  const runBlocker = mission.phase !== "draft"
    ? null
    : !workspacePath
      ? "Choose an explicit workspace before running"
      : !effectivePrimary
        ? "Choose a mission primary before running"
        : mission.checks.length === 0
          ? "Add at least one deterministic check before running"
          : null;
  const awaiting = mission.phase === "awaiting-approval";

  return <section className="mission-stepper" aria-label="Mission">
    <header><strong>{mission.goal}</strong><span className={`status-pill mission-${mission.phase}`}>{mission.phase}</span></header>
    <ol className="mission-steps" aria-label="Mission progress">
      {steps.map((label, index) => <li key={index} className={`mission-step ${stateOf(index)}`} aria-current={stateOf(index) === "current" ? "step" : undefined}>{label}</li>)}
    </ol>
    {mission.blockReason ? <small className="mission-block">{mission.blockReason}</small> : null}
    {runBlocker ? <small className="mission-block">{runBlocker === "Add at least one deterministic check before running" ? "Add a deterministic check before running." : `${runBlocker}.`}</small> : null}
    {mission.outcome ? <small className="mission-outcome">{mission.outcome.taskResult} · {mission.outcome.taskResultSource}</small> : null}
    <div className="mission-actions">
      {mission.phase === "draft" ? <button type="button" disabled={busy || Boolean(runBlocker)} title={runBlocker ?? "Run mission"} className="primary-action" onClick={() => onAction({ type: "mission.run" })}>Run</button> : null}
      {mission.phase === "draft" && mission.checks.length === 0 ? <button type="button" disabled={busy} onClick={() => onCommand("/mission check add -- ")}>Add check</button> : null}
      {mission.phase === "paused" ? <button type="button" disabled={busy} className="primary-action" onClick={() => onAction({ type: "mission.continue" })}>Continue</button> : null}
      {awaiting ? <button type="button" disabled={busy} className="primary-action" onClick={() => onAction({ type: "mission.complete", taskResult: "success" })}>Accept</button> : null}
      {awaiting ? <button type="button" disabled={busy} onClick={() => onAction({ type: "mission.complete", taskResult: "partial" })}>Partial</button> : null}
      {awaiting ? <button type="button" disabled={busy} onClick={() => onAction({ type: "mission.complete", taskResult: "failure" })}>Failure</button> : null}
      {awaiting && mission.attempt - 1 < mission.maxRepairAttempts ? <button type="button" disabled={busy} onClick={() => onCommand('/mission repair --note "')}>Repair</button> : null}
      {!terminal ? <button type="button" className="danger-action" onClick={() => onAction({ type: "mission.cancel" })}>Cancel</button> : null}
      <button type="button" onClick={() => onCommand(mission.phase === "draft" ? "/mission status" : "/mission report")}>{mission.phase === "draft" ? "Inspect" : "Report"}</button>
    </div>
  </section>;
}
