import { useEffect, useRef, useState } from "react";
import { AlertTriangle, CalendarClock, Trash2, X } from "lucide-react";
import { MAX_SCHEDULES, describeRepeat, type MissionSchedule, type ScheduleRepeat } from "../../../core/schedule.js";
import type { StudioAction, StudioActionResult } from "../../../shared/ipc";

/** `YYYY-MM-DDTHH:mm` in local time, the value a datetime-local input takes. */
export function localInputValue(date: Date): string {
  const pad = (value: number): string => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function when(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

/**
 * Missions approved in advance. Scheduling one opens the same native confirmation a mission
 * launch does, once; after that it starts on time without asking, while Studio is open.
 */
export function ScheduleDialog({ schedules, workspacePath, agentName, canWorktree, invoke, onOpenSession, onClose }: {
  readonly schedules: readonly MissionSchedule[];
  readonly workspacePath: string | null;
  /** The agent a new schedule would be bound to, or null when none is chosen. */
  readonly agentName: string | null;
  readonly canWorktree: boolean;
  /** Rejects with the reason on failure; shown inside the dialog. */
  readonly invoke: (action: StudioAction) => Promise<StudioActionResult>;
  readonly onOpenSession: (sessionId: string) => void;
  readonly onClose: () => void;
}): React.JSX.Element {
  const [goal, setGoal] = useState("");
  const [checks, setChecks] = useState("");
  const [first, setFirst] = useState(() => { const next = new Date(Date.now() + 3_600_000); next.setMinutes(0, 0, 0); return localInputValue(next); });
  const [repeat, setRepeat] = useState<ScheduleRepeat>("once");
  const [worktree, setWorktree] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const goalRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => { goalRef.current?.focus(); }, []);
  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      if (event.key !== "Escape") return;
      // Capture phase: closing the dialog must not also reach App's Escape (run.cancel).
      event.stopImmediatePropagation();
      onClose();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  const checkLines = checks.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const blocked = !workspacePath ? "Choose a workspace folder first." : !agentName ? "Choose an agent first." : schedules.length >= MAX_SCHEDULES ? `Studio keeps at most ${MAX_SCHEDULES} scheduled missions.` : null;
  const ready = !blocked && goal.trim().length > 0 && checkLines.length > 0 && first.length > 0;

  const run = async (action: StudioAction): Promise<boolean> => {
    setBusy(true);
    setFailure(null);
    try { await invoke(action); return true; }
    catch (caught) { setFailure(caught instanceof Error ? caught.message : String(caught)); return false; }
    finally { setBusy(false); }
  };
  const create = async (): Promise<void> => {
    const date = new Date(first);
    if (Number.isNaN(date.getTime())) { setFailure("Choose a date and time."); return; }
    // Declining the native confirmation is not an error: the call succeeds and nothing is added.
    if (await run({ type: "schedule.create", goal: goal.trim(), checks: checkLines, firstRunAt: date.toISOString(), repeat, ...(worktree ? { worktree: true } : {}) })) {
      setGoal("");
      setChecks("");
    }
  };

  return (
    <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div className="modal schedule-dialog" role="dialog" aria-modal="true" aria-labelledby="schedule-title">
        <button type="button" className="icon-button keep-save-close" aria-label="Close" onClick={onClose}><X size={16} /></button>
        <h2 id="schedule-title">Scheduled missions</h2>
        <p className="keep-save-hint">Approve a mission now and it starts at the set time without asking again, in a session of its own. Scheduled missions run only while Studio is open; a time that passes while it is closed is skipped, not replayed.</p>

        {schedules.length > 0 ? (
          <ul className="schedule-list" aria-label="Scheduled missions">
            {schedules.map((schedule) => (
              <li key={schedule.id}>
                <CalendarClock size={15} aria-hidden="true" />
                <div>
                  <strong>{schedule.goal}</strong>
                  <small>{schedule.nextRunAt ? `Next: ${when(schedule.nextRunAt)} · ${describeRepeat(schedule.repeat)}` : "Finished: this one-time schedule has had its turn"} · {schedule.primaryRuntimeId}{schedule.worktree ? " · own worktree" : ""}</small>
                  <small className="schedule-path">{schedule.workspacePath}</small>
                  {schedule.lastRun ? (
                    schedule.lastRun.error
                      ? <small className="schedule-error"><AlertTriangle size={11} aria-hidden="true" /> {when(schedule.lastRun.at)}: {schedule.lastRun.error}</small>
                      : <small>Last started {when(schedule.lastRun.at)}{schedule.lastRun.sessionId ? <> · <button type="button" className="schedule-link" onClick={() => onOpenSession(schedule.lastRun?.sessionId as string)}>Open that session</button></> : null}</small>
                  ) : null}
                </div>
                <button type="button" className="icon-button compact" aria-label={`Delete schedule: ${schedule.goal}`} title="Delete this schedule" disabled={busy} onClick={() => void run({ type: "schedule.delete", scheduleId: schedule.id })}><Trash2 size={14} /></button>
              </li>
            ))}
          </ul>
        ) : <p className="schedule-empty">Nothing is scheduled.</p>}

        <form className="schedule-form" aria-label="Schedule a mission" onSubmit={(event) => { event.preventDefault(); if (ready && !busy) void create(); }}>
          <h3>Schedule a mission</h3>
          <label className="keep-save-field">Goal<textarea ref={goalRef} rows={2} maxLength={4000} value={goal} onChange={(event) => setGoal(event.target.value)} placeholder="Update dependencies and keep the tests passing" /></label>
          <label className="keep-save-field">Done when these commands pass <small>one per line, run locally in the workspace</small><textarea rows={2} value={checks} onChange={(event) => setChecks(event.target.value)} placeholder="npm test" /></label>
          <div className="schedule-row">
            <label className="keep-save-field">First run<input type="datetime-local" value={first} onChange={(event) => setFirst(event.target.value)} /></label>
            <label className="keep-save-field">Repeat<select value={repeat} onChange={(event) => setRepeat(event.target.value as ScheduleRepeat)}>
              <option value="once">Once</option><option value="daily">Every day</option><option value="weekdays">Every weekday</option><option value="weekly">Every week</option>
            </select></label>
          </div>
          {canWorktree ? <label className="settings-check"><input type="checkbox" checked={worktree} onChange={(event) => setWorktree(event.target.checked)} /><span>Run in its own git worktree<small>Each run works on a new branch in a separate copy, leaving this folder untouched.</small></span></label> : null}
          <p className="schedule-bound">{blocked ?? `Runs in ${workspacePath} with ${agentName}, using the model and permission mode set right now.`}</p>
          {failure ? <p className="keep-save-failure" role="alert"><AlertTriangle size={14} /> {failure}</p> : null}
          <div className="keep-save-actions">
            <span>You confirm the details in the next step.</span>
            <button type="button" onClick={onClose}>Close</button>
            <button type="submit" className="primary-action" disabled={!ready || busy}>{busy ? "Working…" : "Review and schedule…"}</button>
          </div>
        </form>
      </div>
    </div>
  );
}
